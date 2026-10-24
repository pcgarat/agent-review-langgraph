from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import IO, Protocol, runtime_checkable

from pydantic import BaseModel

from agent_review.errors import PermanentError, TransientError

logger = logging.getLogger(__name__)

# Callback de linea: `(stream, line)` con `stream` en `{"stdout", "stderr"}`. Se usa
# para la consola en vivo (019 W5); nunca para el `VerifyReport`, que sigue siendo
# el canal fiable.
LineSink = Callable[[str, str], None]


class ContainerError(PermanentError):
    """Fallo no recuperable del runtime de contenedores (imagen inexistente, CLI ausente)."""


class ContainerUnavailable(TransientError):
    """`docker pull` o `login` fallaron: el registro puede responder en el reintento."""


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

    def start(
        self, container_id: str, *, timeout_s: float, on_line: LineSink | None = None
    ) -> ContainerOutcome: ...

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
            raise ContainerUnavailable(f"docker pull {reference} fallo: {completed.stderr.strip()}")

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
            raise ContainerUnavailable(f"docker login fallo: {exc}") from exc
        if completed.returncode != 0:
            raise ContainerUnavailable(
                f"docker login fallo en {registry}: {completed.stderr.strip()}"
            )

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

    def start(
        self, container_id: str, *, timeout_s: float, on_line: LineSink | None = None
    ) -> ContainerOutcome:
        """`docker start -a` leyendo `stdout`/`stderr` linea a linea.

        Dos hilos lectores (uno por flujo) evitan el bloqueo clasico de un *pipe*
        lleno (R5) y permiten emitir cada linea a `on_line` mientras corre, sin
        renunciar a acumular la cola completa para el `VerifyReport`. No se usa
        `stderr=STDOUT`: el informe mantiene ambos flujos separados.
        """
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                [self._docker, "start", "-a", container_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise ContainerError(f"No se encontro el binario {self._docker!r}") from exc

        # `list` y no `str`: los hilos anaden con `append` (atomico) y el hilo
        # principal solo une al terminar, asi que no hace falta lock adicional.
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        readers = [
            threading.Thread(
                target=self._pump,
                args=("stdout", process.stdout, stdout_lines, on_line),
                name=f"docker-stdout-{container_id}",
                daemon=True,
            ),
            threading.Thread(
                target=self._pump,
                args=("stderr", process.stderr, stderr_lines, on_line),
                name=f"docker-stderr-{container_id}",
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        try:
            returncode = process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            # El contenedor puede seguir vivo tras el timeout: que el `finally` del
            # runner lo mate y lo elimine. Se relanza para no entregar un informe.
            self._kill(process)
            raise ContainerTimeout(f"docker start -a {container_id} excedio {timeout_s}s") from None
        finally:
            # Los hilos cierran solos al ver EOF (el hijo termino o se mato); se les
            # da tiempo acotado antes de devolver el control.
            for reader in readers:
                reader.join(timeout=5.0)
        return ContainerOutcome(
            exit_code=returncode,
            duration_s=time.monotonic() - started,
            stdout="".join(stdout_lines),
            stderr="".join(stderr_lines),
        )

    def _pump(
        self,
        stream: str,
        pipe: IO[str] | None,
        sink: list[str],
        on_line: LineSink | None,
    ) -> None:
        """Lee un flujo hasta EOF, guardando cada linea y emitiendola si hay callback.

        `on_line` se aisla en un `try`: la consola es *best-effort* y una excepcion
        del suscriptor no puede tumbar una verificacion que ya corre (W8).
        """
        if pipe is None:
            return
        try:
            for line in pipe:
                sink.append(line)
                if on_line is not None:
                    try:
                        on_line(stream, line.rstrip("\n"))
                    except Exception:  # noqa: BLE001 - la consola nunca rompe el informe
                        logger.warning("la consola %s fallo al emitir una linea", stream)
        finally:
            pipe.close()

    def _kill(self, process: subprocess.Popen[str]) -> None:
        process.kill()
        try:
            process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            logger.error("el proceso docker no murio tras el kill")

    def remove(self, container_id: str) -> None:
        if not container_id:
            return
        # `-f` por si sigue corriendo tras un timeout; `-v` limpia volumenes anonimos.
        self._run(["rm", "-f", "-v", container_id], timeout_s=60.0)
