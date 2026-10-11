from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from agent_review.errors import PermanentError, TransientError


class ContainerError(PermanentError):
    """Fallo no recuperable del runtime de contenedores (imagen inexistente, CLI ausente)."""


class ContainerTimeout(TransientError):
    """Un comando excedio su timeout: el contenedor se mata y se destruye igual."""


class ContainerOutcome(BaseModel):
    exit_code: int
    duration_s: float
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class ContainerSpec:
    """Como se lanza un comando del PR dentro del contenedor.

    Todo lo sensible esta aqui y no lo decide el codigo del PR: imagen pinada por
    digest, red acotada, sin capacidades, usuario no root y limites de CPU/memoria.
    """

    image: str  # `repo@sha256:...`, nunca un tag mutable
    argv: list[str]
    workspace: str
    env: dict[str, str] = field(default_factory=dict)
    network: str = "none"
    user: str = "1000:1000"
    memory: str = "4g"
    cpus: str = "2"


@runtime_checkable
class ContainerRuntime(Protocol):
    """Ciclo de vida explicito del contenedor.

    `create` + `start` + `remove` (y no un `docker run --rm` opaco) para que el
    runner pueda destruir SIEMPRE en un `finally` y un fake pueda contar
    creaciones y destrucciones por igual (§9.5).
    """

    def pull(self, reference: str, *, registry_user: str, registry_token: str) -> None: ...

    def create(self, spec: ContainerSpec) -> str: ...

    def start(self, container_id: str, *, timeout_s: float) -> ContainerOutcome: ...

    def remove(self, container_id: str) -> None: ...


class DockerCliRuntime:
    """Runtime real: invoca el CLI de `docker` (rootless del host del runner).

    No monta `docker.sock` en ningun contenedor del PR: el unico que habla con el
    demonio es este proceso, que corre en el host del runner.
    """

    def __init__(self, *, docker_bin: str = "docker") -> None:
        self._docker = docker_bin

    def _run(self, args: list[str], *, timeout_s: float) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self._docker, *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise ContainerTimeout(f"docker {' '.join(args)} excedio {timeout_s}s") from exc
        except FileNotFoundError as exc:
            raise ContainerError(f"No se encontro el binario {self._docker!r}") from exc

    def pull(self, reference: str, *, registry_user: str, registry_token: str) -> None:
        if registry_user:
            self._login(reference, registry_user, registry_token)
        completed = self._run(["pull", reference], timeout_s=600.0)
        if completed.returncode != 0:
            raise ContainerError(f"docker pull {reference} fallo: {completed.stderr.strip()}")

    def _login(self, reference: str, user: str, token: str) -> None:
        """`docker login` sin exponer el token en argv ni en los logs.

        Se pasa por stdin (`--password-stdin`): un token en la linea de comandos
        queda visible en el `ps` del host para cualquier usuario.
        """
        registry = reference.split("@")[0].split("/")[0]
        try:
            completed = subprocess.run(
                [self._docker, "login", registry, "--username", user, "--password-stdin"],
                input=token,
                capture_output=True,
                text=True,
                check=False,
                timeout=60.0,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            raise ContainerError(f"docker login fallo: {exc}") from exc
        if completed.returncode != 0:
            raise ContainerError(f"docker login fallo en {registry}: {completed.stderr.strip()}")

    def create(self, spec: ContainerSpec) -> str:
        argv = [
            "create",
            "--rm",
            "--network",
            spec.network,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            spec.user,
            "--memory",
            spec.memory,
            "--cpus",
            spec.cpus,
            "-v",
            f"{spec.workspace}:/workspace:rw",
            "-w",
            "/workspace",
        ]
        for key, value in spec.env.items():
            argv += ["-e", f"{key}={value}"]
        argv += [spec.image, *spec.argv]
        completed = self._run(argv, timeout_s=60.0)
        if completed.returncode != 0:
            raise ContainerError(f"docker create fallo: {completed.stderr.strip()}")
        return completed.stdout.strip()

    def start(self, container_id: str, *, timeout_s: float) -> ContainerOutcome:
        started = time.monotonic()
        try:
            completed = self._run(["start", "-a", container_id], timeout_s=timeout_s)
        except ContainerTimeout:
            # El contenedor puede seguir vivo tras el timeout: que el `finally` del
            # runner lo mate y lo elimine. Se relanza para no entregar un informe.
            raise
        return ContainerOutcome(
            exit_code=completed.returncode,
            duration_s=time.monotonic() - started,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def remove(self, container_id: str) -> None:
        if not container_id:
            return
        # `-f` por si sigue corriendo tras un timeout; `-v` limpia volumenes anonimos.
        self._run(["rm", "-f", "-v", container_id], timeout_s=60.0)
