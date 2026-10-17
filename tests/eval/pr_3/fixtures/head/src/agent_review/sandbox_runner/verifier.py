from __future__ import annotations

import logging
import threading
from typing import Any, Protocol, runtime_checkable

from agent_review.adapters.diff_parser import parse_unified_diff
from agent_review.domain.models import CommandResult
from agent_review.errors import PermanentError
from agent_review.ports.sandbox import VerifyReport, VerifyRequest
from agent_review.profile import resolve_ci_image
from agent_review.sandbox_runner.recipes import (
    RecipeError,
    declared_checks,
    discover_targets,
    expand_check,
    expand_setup,
    validate_targets,
)
from agent_review.sandbox_runner.runtime import (
    ContainerRuntime,
    ContainerSpec,
    ContainerTimeout,
)
from agent_review.sandbox_runner.workspace import WorkspacePort

logger = logging.getLogger(__name__)

# Limite del log que viaja de vuelta al worker: suficiente para diagnosticar el
# fallo, nunca el volcado completo (que puede llevar rutas o salida del PR).
_TAIL_CHARS = 4000

_SETUP_NETWORK = "setup_network"
_SETUP_OFFLINE = "setup"


@runtime_checkable
class ProfileSource(Protocol):
    def load(self) -> dict[str, Any]: ...


class FixedProfileSource:
    """Perfil fijo: en el servicio se carga del fichero; en tests, en memoria."""

    def __init__(self, profile: dict[str, Any]) -> None:
        self._profile = profile

    def load(self) -> dict[str, Any]:
        return self._profile


class RunnerError(PermanentError):
    """Peticion de verificacion que el runner no puede atender (perfil, imagen, targets)."""


class Verifier:
    """Ejecuta la verificacion de un parche en el sandbox (§5.3 del spec).

    Ciclo: resolver imagen -> clonar -> aplicar parche -> `setup` (con red los que
    la necesiten) -> checks (sin red) -> destruir **siempre** en un `finally`. La
    idempotencia por `run_key` hace que una reentrega tras una caida no lance una
    segunda ejecucion (R8).
    """

    def __init__(
        self,
        *,
        profile_source: ProfileSource,
        workspace: WorkspacePort,
        runtime: ContainerRuntime,
        registry_user: str = "",
        registry_token: str = "",
        image_already_pulled: bool = False,
    ) -> None:
        self._profile_source = profile_source
        self._workspace = workspace
        self._runtime = runtime
        self._registry_user = registry_user
        self._registry_token = registry_token
        self._image_already_pulled = image_already_pulled
        self._completed: dict[str, VerifyReport] = {}
        self._running: set[str] = set()
        self._lock = threading.Lock()

    def verify(self, request: VerifyRequest) -> VerifyReport:
        with self._lock:
            cached = self._completed.get(request.run_key)
            if cached is not None:
                logger.info(
                    "run_key %s ya verificado; se devuelve el informe previo", request.run_key
                )
                return cached
            if request.run_key in self._running:
                # Concurrencia 1 en v1: una reentrega se rechaza en vez de lanzar un
                # segundo contenedor sobre el mismo parche.
                raise RunnerError(f"la verificacion {request.run_key} ya esta en curso")
            self._running.add(request.run_key)
        try:
            report = self._verify(request)
        finally:
            with self._lock:
                self._running.discard(request.run_key)
        with self._lock:
            self._completed[request.run_key] = report
        return report

    def _verify(self, request: VerifyRequest) -> VerifyReport:
        profile = self._profile_source.load()
        image = resolve_ci_image(profile)
        if image is None:
            raise RunnerError(
                "el perfil no declara un `ci_image_digest` `sha256:...`; no se verifica"
            )
        targets = validate_targets(request.targets)
        checks = declared_checks(profile)
        env = _env(profile)
        timeout_s = _timeout(profile)
        recipes = self._plan(request.recipes, checks)

        workspace = self._workspace.prepare(
            repo=request.repo, head_sha=request.head_sha, patch=request.patch
        )
        if not targets:
            # Sin targets explicitos, los tests dirigidos salen del fichero que toca
            # el parche; se buscan en el workspace clonado, que es quien los conoce.
            targets = discover_targets(workspace, list(parse_unified_diff(request.patch)))
        results: list[CommandResult] = []
        try:
            if not self._image_already_pulled:
                self._runtime.pull(
                    image, registry_user=self._registry_user, registry_token=self._registry_token
                )
            results = self._run_setup(profile, workspace, image, env, timeout_s)
            if all(result.ok for result in results):
                results.extend(
                    self._run_checks(recipes, workspace, image, checks, targets, env, timeout_s)
                )
        finally:
            # Pase lo que pase (exito, fallo o timeout): el workspace se borra. Los
            # contenedores se destruyen en `_run_command` con su propio `finally`.
            self._workspace.cleanup(workspace)
        return VerifyReport(
            run_key=request.run_key,
            image_digest=image.split("@", 1)[1],
            results=results,
            passed=bool(results) and all(result.ok for result in results),
        )

    def _plan(self, requested: list[str], checks: dict[str, Any]) -> list[str]:
        if requested:
            return list(requested)
        verify = self._profile_source.load().get("verify")
        if isinstance(verify, list):
            return [str(name) for name in verify]
        raise RunnerError("no hay recipes que ejecutar (ni `recipes` ni `verify` en el perfil)")

    def _run_setup(
        self,
        profile: dict[str, Any],
        workspace: str,
        image: str,
        env: dict[str, str],
        timeout_s: float,
    ) -> list[CommandResult]:
        """`setup_network` con red, luego el resto de `setup` sin red.

        `composer install --no-scripts` con red no ejecuta codigo del PR; el resto
        del setup y los checks van con `--network none` (R5).
        """
        results: list[CommandResult] = []
        with_network = profile.get(_SETUP_NETWORK) or []
        if isinstance(with_network, list):
            for argv in expand_setup([str(cmd) for cmd in with_network]):
                result = self._run_command(
                    "setup_network", argv, workspace, image, env, timeout_s, network="bridge"
                )
                results.append(result)
                if not result.ok:
                    return results
        offline = profile.get(_SETUP_OFFLINE) or []
        if isinstance(offline, list):
            for argv in expand_setup([str(cmd) for cmd in offline]):
                result = self._run_command(
                    "setup", argv, workspace, image, env, timeout_s, network="none"
                )
                results.append(result)
                if not result.ok:
                    return results
        return results

    def _run_checks(
        self,
        recipes: list[str],
        workspace: str,
        image: str,
        checks: dict[str, Any],
        targets: list[str],
        env: dict[str, str],
        timeout_s: float,
    ) -> list[CommandResult]:
        results: list[CommandResult] = []
        for name in recipes:
            argv = expand_check(name, checks, targets)
            if argv is None:
                logger.info("recipe %s sin targets; se salta", name)
                continue
            result = self._run_command(name, argv, workspace, image, env, timeout_s, network="none")
            results.append(result)
            if not result.ok:
                # Para al primer fallo: los checks siguientes serian ruido caro.
                break
        return results

    def _run_command(
        self,
        recipe: str,
        argv: list[str],
        workspace: str,
        image: str,
        env: dict[str, str],
        timeout_s: float,
        *,
        network: str,
    ) -> CommandResult:
        spec = ContainerSpec(image=image, argv=argv, workspace=workspace, env=env, network=network)
        container_id = self._runtime.create(spec)
        try:
            outcome = self._runtime.start(container_id, timeout_s=timeout_s)
        except ContainerTimeout as exc:
            return CommandResult(
                recipe=recipe,
                argv=argv,
                exit_code=124,
                duration_s=timeout_s,
                stderr_tail=str(exc)[:_TAIL_CHARS],
            )
        finally:
            # El contenedor se destruye en cualquier salida, tambien tras un timeout.
            self._runtime.remove(container_id)
        return CommandResult(
            recipe=recipe,
            argv=argv,
            exit_code=outcome.exit_code,
            duration_s=outcome.duration_s,
            stdout_tail=outcome.stdout[-_TAIL_CHARS:],
            stderr_tail=outcome.stderr[-_TAIL_CHARS:],
        )


def _env(profile: dict[str, Any]) -> dict[str, str]:
    env = profile.get("env")
    if not isinstance(env, dict):
        return {}
    return {str(key): str(value) for key, value in env.items()}


def _timeout(profile: dict[str, Any]) -> float:
    sandbox = profile.get("sandbox")
    if isinstance(sandbox, dict):
        declared = sandbox.get("timeout_s")
        if isinstance(declared, (int, float)) and declared > 0:
            return float(declared)
    return 1200.0


__all__ = [
    "FixedProfileSource",
    "ProfileSource",
    "RecipeError",
    "RunnerError",
    "Verifier",
]
