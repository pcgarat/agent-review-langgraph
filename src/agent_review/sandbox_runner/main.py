from __future__ import annotations

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, Header, HTTPException, status

from agent_review.ports.sandbox import VerifyReport, VerifyRequest
from agent_review.sandbox_runner.runtime import ContainerError, DockerCliRuntime
from agent_review.sandbox_runner.verifier import RunnerError, Verifier
from agent_review.sandbox_runner.workspace import GitWorkspace, WorkspaceError
from agent_review.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_INSECURE_TOKENS = {"change-me", "changeme"}


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    get_settings()
    yield


app = FastAPI(title="Sandbox Runner", version="0.1.0", lifespan=lifespan)


def _verify_token(settings: Settings, authorization: str | None) -> None:
    """Bearer compartido worker<->runner. Relajado con el valor de ejemplo en dev."""
    token = settings.sandbox_runner_token.strip()
    if not token or token in _INSECURE_TOKENS:
        return
    expected = f"Bearer {token}"
    if not authorization or not hmac.compare_digest(expected, authorization):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token del runner invalido")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/verify")
def verify(
    request: VerifyRequest,
    authorization: str | None = Header(default=None),
) -> VerifyReport:
    """Verifica un parche de forma bloqueante e idempotente por `run_key`.

    El ciclo de vida del workspace vive entero aqui: el worker solo envia el
    parche y los nombres de recipe. Ningun contenedor del PR monta `docker.sock`.
    """
    settings = get_settings()
    _verify_token(settings, authorization)
    try:
        return build_verifier(settings).verify(request)
    except RunnerError as exc:
        # 4xx: la peticion no es atendible (perfil sin digest, targets invalidos,
        # recipe desconocida). El worker la marca `NOT_VERIFIABLE` con el detalle.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except WorkspaceError as exc:
        # El parche no aplica o el repo no se pudo preparar: no es un fallo del
        # runner sino de esta peticion concreta. `WorkspaceUnavailable` es
        # transitorio y no entra aqui (sube para que el worker reintente).
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ContainerError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


class _FileProfileSource:
    """Carga el perfil del fichero en cada peticion (un perfil editado se ve sin reiniciar)."""

    def __init__(self, path: str) -> None:
        self._path = path

    def load(self) -> dict[str, object]:
        raw = Path(self._path).read_text(encoding="utf-8")
        data = yaml.safe_load(raw) or {}
        return data if isinstance(data, dict) else {}


_verifier: Verifier | None = None


def build_verifier(settings: Settings) -> Verifier:
    """Verificador unico del proceso (concurrencia 1 en v1)."""
    global _verifier
    if _verifier is None:
        _verifier = Verifier(
            profile_source=_FileProfileSource(settings.repo_profile_config),
            workspace=GitWorkspace(git_token=settings.sandbox_git_token),
            runtime=DockerCliRuntime(docker_bin=settings.sandbox_docker_bin),
            registry_user=settings.sandbox_registry_user,
            registry_token=settings.sandbox_registry_token,
        )
    return _verifier


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8099)


if __name__ == "__main__":
    main()
