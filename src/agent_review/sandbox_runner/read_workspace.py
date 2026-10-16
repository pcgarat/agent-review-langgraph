from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass

from agent_review.errors import PermanentError, TransientError
from agent_review.ports.context import Ref, is_denied

logger = logging.getLogger(__name__)

# Limite de log que viaja al worker: suficiente para diagnosticar, nunca el volcado.
_TAIL_CHARS = 4000
# Coincidencias por defecto de un `grep`: acota la salida y el coste (016 R4).
_DEFAULT_MAX_MATCHES = 200
_DEFAULT_MAX_FILE_BYTES = 200_000


class ReadWorkspaceError(PermanentError):
    """Peticion de lectura que no se puede atender (ruta vetada, workspace ausente)."""


class PathDenied(ReadWorkspaceError):
    """La ruta esta vetada por el `context_denylist` del perfil (016 W7 -> 403)."""


class ReadWorkspaceUnavailable(TransientError):
    """El clon no se pudo preparar por un fallo recuperable (red, remoto caido)."""


@dataclass(frozen=True)
class WorkspaceInfo:
    workspace_id: str
    expires_at: float


class ReadWorkspace:
    """Clon *bare* de `repo@head_sha` leido con `git show`/`git grep`/`git ls-tree`.

    Sin arbol de trabajo ni `checkout`: no hay *hooks* ni filtros y ningun *symlink*
    puede apuntar fuera. El clon vive fuera de lo que se monta en los contenedores
    de `/verify` (ADR-0018).
    """

    def __init__(
        self,
        directory: str,
        *,
        repo: str,
        head_sha: str,
        base_sha: str = "",
        git_bin: str = "git",
        denylist: tuple[str, ...] = (),
        timeout_s: float = 60.0,
        max_matches: int = _DEFAULT_MAX_MATCHES,
        max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        self.directory = directory
        self.repo = repo
        self.head_sha = head_sha
        self.base_sha = base_sha
        self._git = git_bin
        self._denylist = denylist
        self._timeout = timeout_s
        self._max_matches = max_matches
        self._max_bytes = max_file_bytes

    def show_file(
        self,
        path: str,
        *,
        ref: Ref = "head",
        start: int | None = None,
        end: int | None = None,
    ) -> str | None:
        """`git show <sha>:<path>`; `None` si la ruta no existe en ese commit."""
        _reject_denied(path, self._denylist)
        sha = self._sha(ref)
        if sha is None:
            return None
        result = self._run(["show", f"{sha}:{path}"])
        if result is None or result.returncode != 0:
            return None
        return _slice(result.stdout[:_MAX_READ_BYTES], start, end)

    def grep(self, pattern: str, *, glob: str | None = None) -> list[str]:
        """`git grep -F` literal sobre `head_sha`; vetadas fuera y tope de coincidencias."""
        if not pattern:
            return []
        args = ["grep", "-n", "-I", "-F", "--no-color", "-e", pattern, self.head_sha]
        if glob:
            args += ["--", glob]
        result = self._run(args)
        if result is None or result.returncode not in (0, 1):
            # 1 = sin coincidencias; cualquier otro codigo es un fallo del grep.
            return []
        prefix = f"{self.head_sha}:"
        matches: list[str] = []
        for line in result.stdout.splitlines():
            # `git grep <sha>` prefija cada linea con `<sha>:` y no deja espacio tras
            # la linea (`path:line:texto`). Se normaliza a `path:line: texto` para que
            # la cita sea la misma en los tres adaptadores (contrato del puerto).
            normalized = line[len(prefix) :] if line.startswith(prefix) else line
            path, _, rest = normalized.partition(":")
            line_number, _, text = rest.partition(":")
            if is_denied(path, self._denylist):
                continue
            matches.append(f"{path}:{line_number}: {text.strip()}")
            if len(matches) >= self._max_matches:
                break
        return matches

    def tree(self, path: str = "") -> list[str]:
        """`git ls-tree` del directorio. Nombres directos, ordenados."""
        sha = self.head_sha
        target = f"{sha}:{path}" if path else sha
        result = self._run(["ls-tree", "--name-only", target])
        if result is None or result.returncode != 0:
            return []
        return sorted(result.stdout.splitlines())

    def cleanup(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)

    def _sha(self, ref: Ref) -> str | None:
        if ref == "base":
            return self.base_sha or None
        return self.head_sha

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                [self._git, *args],
                cwd=self.directory,
                capture_output=True,
                text=True,
                check=False,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning("workspace de lectura: git %s excedio el timeout", args[0])
            return None
        except FileNotFoundError:
            logger.error("workspace de lectura: no se encontro el binario %r", self._git)
            return None


# Tope duro del `show` antes del recorte por rango: un fichero enorme no se lee entero.
_MAX_READ_BYTES = 500_000


class ReadWorkspaceManager:
    """Abre y sirve workspaces de lectura, idempotente por `(repo, head_sha)` (016 W6).

    El estado vive en la memoria del runner: un TTL (`ttl_s`) y un tope de abiertos
    (`max_workspaces`) con *reaper* perezoso. Una pausa HITL de dias no deja clones
    vivos, y dos consumidores no coordinan quien lo abre: el mismo `(repo, head_sha)`
    devuelve el mismo workspace.
    """

    def __init__(
        self,
        *,
        git_bin: str = "git",
        git_token: str = "",
        ttl_s: float = 1800.0,
        max_workspaces: int = 4,
        denylist: tuple[str, ...] = (),
        timeout_s: float = 60.0,
        url_template: str = "https://x-token-auth:{token}@bitbucket.org/{repo}.git",
    ) -> None:
        self._git = git_bin
        self._token = git_token.strip()
        self._ttl = ttl_s
        self._max = max(1, max_workspaces)
        self._denylist = denylist
        self._timeout = timeout_s
        self._url_template = url_template
        self._open: dict[str, tuple[ReadWorkspace, float]] = {}
        self._by_key: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()
        self._clock = time.monotonic

    def open(self, *, repo: str, head_sha: str, base_sha: str = "") -> WorkspaceInfo:
        with self._lock:
            self._reap_locked()
            key = (repo, head_sha)
            existing = self._by_key.get(key)
            if existing is not None:
                workspace, _ = self._open[existing]
                expires = self._clock() + self._ttl
                self._open[existing] = (workspace, expires)
                return WorkspaceInfo(workspace_id=existing, expires_at=expires)
            self._evict_locked()
            workspace = self._clone(repo=repo, head_sha=head_sha, base_sha=base_sha)
            workspace_id = uuid.uuid4().hex[:12]
            expires = self._clock() + self._ttl
            self._open[workspace_id] = (workspace, expires)
            self._by_key[key] = workspace_id
            logger.info("workspace de lectura %s abierto para %s@%s", workspace_id, repo, head_sha)
            return WorkspaceInfo(workspace_id=workspace_id, expires_at=expires)

    def get(self, workspace_id: str) -> ReadWorkspace:
        with self._lock:
            self._reap_locked()
            entry = self._open.get(workspace_id)
            if entry is None:
                raise ReadWorkspaceError(f"el workspace {workspace_id} no existe o caduco")
            return entry[0]

    def delete(self, workspace_id: str) -> bool:
        with self._lock:
            return self._drop_locked(workspace_id)

    def _drop_locked(self, workspace_id: str) -> bool:
        """Borra un workspace dando por hecho que el lock ya se tiene.

        `_evict_locked` y `_reap_locked` corren con el lock tomado: llamar a
        `delete()` desde ahi volveria a pedirlo y bloquearia el gestor (el lock no
        es reentrante).
        """
        entry = self._open.pop(workspace_id, None)
        if entry is None:
            return False
        for key, value in list(self._by_key.items()):
            if value == workspace_id:
                del self._by_key[key]
        entry[0].cleanup()
        logger.info("workspace de lectura %s borrado", workspace_id)
        return True

    def close(self) -> None:
        with self._lock:
            for workspace, _ in self._open.values():
                workspace.cleanup()
            self._open.clear()
            self._by_key.clear()

    def _clone(self, *, repo: str, head_sha: str, base_sha: str) -> ReadWorkspace:
        directory = tempfile.mkdtemp(prefix="read-ws-")
        url = self._url_template.format(token=self._token, repo=repo)
        self._init_bare(directory, url)
        self._fetch(directory, head_sha, required=True)
        if base_sha:
            # El `base_sha` puede no estar en el remoto (squash, force-push): su
            # ausencia no invalida el workspace, solo deja `ref="base"` sin servir.
            self._fetch(directory, base_sha, required=False)
        return ReadWorkspace(
            directory,
            repo=repo,
            head_sha=head_sha,
            base_sha=base_sha,
            git_bin=self._git,
            denylist=self._denylist,
            timeout_s=self._timeout,
        )

    def _init_bare(self, directory: str, url: str) -> None:
        init = self._run(directory, ["init", "-q", "--bare"])
        if init is None or init.returncode != 0:
            shutil.rmtree(directory, ignore_errors=True)
            raise ReadWorkspaceUnavailable(f"git init fallo: {_tail(init)}")
        remote = self._run(directory, ["remote", "add", "origin", url])
        if remote is None or remote.returncode != 0:
            shutil.rmtree(directory, ignore_errors=True)
            raise ReadWorkspaceUnavailable(f"git remote fallo: {_tail(remote)}")

    def _fetch(self, directory: str, sha: str, *, required: bool) -> None:
        result = self._run(directory, ["fetch", "-q", "--depth", "1", "origin", sha])
        if result is None or result.returncode != 0:
            if required:
                shutil.rmtree(directory, ignore_errors=True)
                raise ReadWorkspaceUnavailable(f"git fetch de {sha} fallo: {_tail(result)}")
            logger.info(
                "workspace de lectura: base_sha %s no disponible; ref=base no se sirve", sha
            )

    def _run(self, cwd: str, args: list[str]) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                [self._git, *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired:
            return None
        except FileNotFoundError:
            logger.error("workspace de lectura: no se encontro el binario %r", self._git)
            return None

    def _reap_locked(self) -> None:
        now = self._clock()
        expired = [wid for wid, (_, expires) in self._open.items() if expires <= now]
        for workspace_id in expired:
            self._drop_locked(workspace_id)
            logger.info("workspace de lectura %s caducado y borrado", workspace_id)

    def _evict_locked(self) -> None:
        while len(self._open) >= self._max:
            oldest = min(self._open.items(), key=lambda item: item[1][1])
            self._drop_locked(oldest[0])


def _reject_denied(path: str, denylist: tuple[str, ...]) -> None:
    if is_denied(path, denylist):
        raise PathDenied(f"la ruta {path} esta vetada para lectura")


def _slice(text: str, start: int | None, end: int | None) -> str:
    if start is None and end is None:
        return text
    lines = text.splitlines()
    first = max(0, (start or 1) - 1)
    last = end if end is not None else len(lines)
    return "\n".join(lines[first:last])


def _tail(result: subprocess.CompletedProcess[str] | None) -> str:
    if result is None:
        return "git no respondio (timeout o binario ausente)"
    return (result.stderr or result.stdout).strip()[-_TAIL_CHARS:] or "sin detalle"
