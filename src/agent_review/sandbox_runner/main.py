from __future__ import annotations

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from agent_review.ports.context import Ref
from agent_review.ports.sandbox import VerifyReport, VerifyRequest
from agent_review.profile import context_denylist, load_profile
from agent_review.sandbox_runner.read_workspace import (
    PathDenied,
    ReadWorkspaceError,
    ReadWorkspaceManager,
    ReadWorkspaceUnavailable,
)
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


# ---------------------------------------------------------------------------
# Workspace de lectura (iniciativa 016 / ADR-0018): clon *bare* que sirve fichero,
# `grep` y arbol a un lector de contexto, sin ejecutar codigo del PR.
# ---------------------------------------------------------------------------


class OpenWorkspaceRequest(BaseModel):
    repo: str
    head_sha: str
    base_sha: str = ""


class WorkspaceResponse(BaseModel):
    workspace_id: str
    expires_at: float


class GrepRequest(BaseModel):
    pattern: str
    glob: str | None = None
    max_matches: int | None = Field(default=None, ge=1)


class GrepResponse(BaseModel):
    matches: list[str] = Field(default_factory=list)


class TreeResponse(BaseModel):
    entries: list[str] = Field(default_factory=list)


class FileResponse(BaseModel):
    path: str
    ref: Ref
    content: str


_workspaces: ReadWorkspaceManager | None = None


def build_workspaces(settings: Settings) -> ReadWorkspaceManager:
    """Gestor unico del proceso (memoria del runner; TTL y tope con reaper perezoso)."""
    global _workspaces
    if _workspaces is None:
        profile = _load_runner_profile(settings)
        _workspaces = ReadWorkspaceManager(
            git_bin="git",
            git_token=settings.sandbox_git_token,
            ttl_s=settings.sandbox_workspace_ttl_s,
            max_workspaces=settings.sandbox_max_workspaces,
            denylist=context_denylist(profile),
            timeout_s=settings.twg_timeout_s,
        )
    return _workspaces


def _load_runner_profile(settings: Settings) -> dict[str, Any]:
    """Perfil del runner; un perfil ilegible degrada al default de denylist."""
    try:
        return load_profile(settings)
    except Exception:  # noqa: BLE001 - el perfil nunca tumba el arranque (B4)
        return {}


@app.post("/workspaces")
def open_workspace(
    request: OpenWorkspaceRequest,
    authorization: str | None = Header(default=None),
) -> WorkspaceResponse:
    """Abre (o reutiliza) un workspace de lectura. Idempotente por `(repo, head_sha)`."""
    settings = get_settings()
    _verify_token(settings, authorization)
    try:
        info = build_workspaces(settings).open(
            repo=request.repo, head_sha=request.head_sha, base_sha=request.base_sha
        )
    except ReadWorkspaceUnavailable as exc:
        # 503: fallo recuperable de clonado; el adaptador reabre o degrada (W8).
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except ReadWorkspaceError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return WorkspaceResponse(workspace_id=info.workspace_id, expires_at=info.expires_at)


@app.get("/workspaces/{workspace_id}/file")
def read_workspace_file(
    workspace_id: str,
    path: str,
    ref: Ref = "head",
    start: int | None = None,
    end: int | None = None,
    authorization: str | None = Header(default=None),
) -> FileResponse:
    """`git show <sha>:<path>`. `404` si no existe, `403` si la ruta esta vetada."""
    settings = get_settings()
    _verify_token(settings, authorization)
    workspace = _get_workspace(settings, workspace_id)
    try:
        content = workspace.show_file(path, ref=ref, start=start, end=end)
    except PathDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    if content is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no existe {path} en {ref}")
    return FileResponse(path=path, ref=ref, content=content)


@app.post("/workspaces/{workspace_id}/grep")
def grep_workspace(
    workspace_id: str,
    request: GrepRequest,
    authorization: str | None = Header(default=None),
) -> GrepResponse:
    """`git grep -F` literal sobre `head_sha`, con glob opcional y tope de coincidencias."""
    settings = get_settings()
    _verify_token(settings, authorization)
    workspace = _get_workspace(settings, workspace_id)
    matches = workspace.grep(request.pattern, glob=request.glob)
    if request.max_matches is not None:
        matches = matches[: request.max_matches]
    return GrepResponse(matches=matches)


@app.get("/workspaces/{workspace_id}/tree")
def tree_workspace(
    workspace_id: str,
    path: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> TreeResponse:
    """`git ls-tree` del directorio a `head_sha`."""
    settings = get_settings()
    _verify_token(settings, authorization)
    workspace = _get_workspace(settings, workspace_id)
    return TreeResponse(entries=workspace.tree(path))


@app.delete("/workspaces/{workspace_id}")
def delete_workspace(
    workspace_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, bool]:
    """Borra el clon; el TTL lo borra igualmente."""
    settings = get_settings()
    _verify_token(settings, authorization)
    return {"deleted": build_workspaces(settings).delete(workspace_id)}


def _get_workspace(settings: Settings, workspace_id: str) -> Any:
    try:
        return build_workspaces(settings).get(workspace_id)
    except ReadWorkspaceError as exc:
        # `410` y no `404`: el `404` es de un fichero que no existe en el commit. Si
        # compartieran codigo, el lector gastaria su unica reapertura en una ruta
        # inventada por el modelo y no degradaria al VCS ante una perdida real (W8).
        raise HTTPException(status.HTTP_410_GONE, str(exc)) from exc


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
