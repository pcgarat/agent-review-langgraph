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
from agent_review.globs import path_matches
from agent_review.outline import outline_text
from agent_review.ports.context import (
    GrepMode,
    ReadMode,
    ReadResult,
    Ref,
    ToolError,
    ToolResult,
    is_denied,
    portable_pattern,
    slice_lines,
    truncate_bytes,
)

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
        mode: ReadMode = "full",
    ) -> ReadResult | None:
        """`git show <sha>:<path>`; `None` si la ruta no existe en ese commit.

        El rango se aplica **antes** del recorte: pedir `start=900` de un fichero
        grande no puede devolver vacio porque el tope se gasto en las primeras
        lineas. `total_bytes` es el tamaño del blob completo, ya leido por `git`.
        """
        _reject_denied(path, self._denylist)
        sha = self._sha(ref)
        if sha is None:
            return None
        result = self._run(["show", f"{sha}:{path}"])
        if result is None or result.returncode != 0:
            return None
        text = result.stdout
        total_bytes = len(text.encode("utf-8"))
        if mode == "outline":
            index = outline_text(path, text)
            if index is None:
                raise ToolError(f"no se puede indexar {path}; pide `start`/`end` tras un `grep`")
            return ReadResult(content=index, total_bytes=total_bytes)
        if mode != "full":
            raise ToolError(f"mode={mode!r} no existe; usa `full` u `outline`")
        content, served_range = slice_lines(text, start, end)
        content, truncated = truncate_bytes(content, self._max_bytes)
        return ReadResult(
            content=content,
            total_bytes=total_bytes,
            truncated=truncated,
            served_range=served_range,
        )

    def grep(
        self,
        pattern: str,
        *,
        glob: str | None = None,
        path: str | None = None,
        ref: Ref = "head",
        mode: GrepMode = "literal",
        files_only: bool = False,
    ) -> ToolResult:
        """`git grep -F`/`-E` sobre el lado pedido; vetadas fuera y tope.

        En `regex` el patron pasa por el dialecto portable (ADR-0027) **antes** de
        llegar a `git grep -E`: la cadena canonica es la misma que compilan el
        fixture y el VCS. Un patron invalido o no portable es `ToolError`, nunca
        «sin coincidencias» (el codigo 2 de `git grep` tampoco se traga).
        """
        if not pattern:
            return ToolResult()
        effective = portable_pattern(pattern) if mode == "regex" else pattern
        sha = self._sha(ref)
        if sha is None:
            raise ValueError(f"la ref {ref!r} no esta disponible en este workspace")
        flag = "-E" if mode == "regex" else "-F"
        args = ["grep", "-n", "-I", flag, "--no-color", "-e", effective, sha]
        result = self._run(args)
        if result is None:
            raise ToolError(
                "el grep excedio el tiempo del workspace; acota con `path`/`glob` "
                "o usa `files_only`"
            )
        if result.returncode not in (0, 1):
            # 1 es «sin coincidencias»; cualquier otro codigo es un fallo real y
            # decirlo es mejor que devolver una lista vacia que parece un resultado.
            detail = (result.stderr or "").strip()[:300] or f"codigo {result.returncode}"
            raise ToolError(f"git grep no pudo buscar: {detail}")
        prefix = f"{sha}:"
        path_prefix = _normalize_dir(path)
        matches: list[str] = []
        all_files: set[str] = set()
        listed: set[str] = set()
        total = 0
        for line in result.stdout.splitlines():
            # `git grep <sha>` prefija cada linea con `<sha>:` y no deja espacio tras
            # la linea (`path:line:texto`). Se normaliza a `path:line: texto` para que
            # la cita sea la misma en los tres adaptadores (contrato del puerto).
            normalized = line[len(prefix) :] if line.startswith(prefix) else line
            file_path, _, rest = normalized.partition(":")
            if (
                path_prefix
                and file_path != path_prefix
                and not file_path.startswith(f"{path_prefix}/")
            ):
                continue
            # `glob` se mide contra la ruta relativa a `path` y lo aplica este lado:
            # el de `git` (pathspec) no es el mismo glob (`*` cruza directorios).
            if glob and not path_matches(_under(file_path, path_prefix), glob):
                continue
            line_number, _, text = rest.partition(":")
            if is_denied(file_path, self._denylist):
                continue
            if files_only:
                all_files.add(file_path)
                if file_path in listed:
                    continue
                listed.add(file_path)
                if len(matches) < self._max_matches:
                    matches.append(file_path)
                continue
            total += 1
            if len(matches) < self._max_matches:
                matches.append(f"{file_path}:{line_number}: {text.strip()}")
        if files_only:
            total = len(all_files)
        return ToolResult(
            items=tuple(matches),
            total=total,
            truncated=total > len(matches),
        )

    def tree(
        self,
        path: str = "",
        *,
        glob: str | None = None,
        depth: int = 1,
    ) -> ToolResult:
        """`git ls-tree` del directorio, con profundidad y glob acotados."""
        if depth < 1:
            raise ToolError(f"`depth` debe ser >= 1 (recibido {depth})")
        sha = self.head_sha
        target = f"{sha}:{path}" if path else sha
        # Con `depth > 1`, `-r -t` incluye los subarboles: el listado es el mismo
        # arbol logico que sirven el fixture y el VCS, no solo las hojas.
        args = ["ls-tree", "-r", "-t", target] if depth > 1 else ["ls-tree", target]
        result = self._run(args)
        if result is None:
            raise ToolError(
                "git ls-tree excedio el tiempo; "
                f"acota con `depth` o lista el padre de {path or '.'}"
            )
        if result.returncode != 0:
            # `ls-tree` falla igual para un fichero y para una ruta ausente; el
            # tipo lo dice. Vacio e inexistente no son lo mismo (L1).
            if path and self._is_file(target):
                raise ToolError(f"`{path}` es un fichero, no un directorio; usa `read_file`")
            raise ToolError(f"no se pudo listar `{path or '.'}`: {_tail(result)}")
        prefix = _normalize_dir(path)
        names: list[str] = []
        for line in result.stdout.splitlines():
            entry = _ls_tree_entry(line)
            if not entry:
                continue
            # `ls-tree <sha>:<dir>` devuelve rutas relativas a `<dir>`; sobre la
            # raiz (`<sha>`) las devuelve completas.
            under = bool(prefix) and entry.startswith(f"{prefix}/")
            relative = entry[len(prefix) + 1 :] if under else entry
            if len(relative.split("/")) > depth:
                continue
            full = entry if under else (f"{prefix}/{relative}" if prefix else relative)
            if is_denied(full, self._denylist):
                continue
            if glob and not path_matches(relative, glob):
                continue
            names.append(relative)
        items = tuple(sorted(names))
        return ToolResult(items=items, total=len(items))

    def _is_file(self, target: str) -> bool:
        probe = self._run(["cat-file", "-t", target])
        return probe is not None and probe.returncode == 0 and probe.stdout.strip() == "blob"

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
        self._cloning: dict[tuple[str, str], threading.Event] = {}
        self._lock = threading.Lock()
        self._clock = time.monotonic

    def open(self, *, repo: str, head_sha: str, base_sha: str = "") -> WorkspaceInfo:
        """Devuelve el workspace de `(repo, head_sha)`, clonandolo si no existe.

        El clon (hasta tres procesos `git` con `timeout_s` cada uno) corre **fuera**
        del lock: con el lock tomado, cada `get()` de las demas reviews esperaria a
        un clon ajeno y agotaria su timeout de cliente. Una marca por clave
        (`_cloning`) mantiene la idempotencia: una segunda apertura del mismo par
        espera al clon en curso en vez de lanzar otro.
        """
        key = (repo, head_sha)
        while True:
            with self._lock:
                self._reap_locked()
                existing = self._by_key.get(key)
                if existing is not None:
                    workspace, _ = self._open[existing]
                    expires = self._clock() + self._ttl
                    self._open[existing] = (workspace, expires)
                    return WorkspaceInfo(workspace_id=existing, expires_at=expires)
                in_progress = self._cloning.get(key)
                if in_progress is None:
                    done = threading.Event()
                    self._cloning[key] = done
                    break
            # Otro hilo clona este par: al terminar, la vuelta lo encuentra abierto,
            # o, si su clon fallo, este hilo lo intenta.
            in_progress.wait()

        try:
            workspace = self._clone(repo=repo, head_sha=head_sha, base_sha=base_sha)
        except BaseException:
            with self._lock:
                self._cloning.pop(key, None)
            done.set()
            raise
        with self._lock:
            self._cloning.pop(key, None)
            self._evict_locked()
            workspace_id = uuid.uuid4().hex[:12]
            expires = self._clock() + self._ttl
            self._open[workspace_id] = (workspace, expires)
            self._by_key[key] = workspace_id
        done.set()
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


def _normalize_dir(path: str | None) -> str:
    normalized = (path or "").strip()
    while normalized.startswith(("./", "/")):
        normalized = normalized[2:] if normalized.startswith("./") else normalized[1:]
    return normalized.rstrip("/")


def _under(relative: str, prefix: str) -> str:
    """Ruta `relative` vista desde `prefix`; el glob se mide ahi (ADR-0027)."""
    return relative[len(prefix) + 1 :] if prefix else relative


def _ls_tree_entry(line: str) -> str:
    """Ruta de una linea `<modo> <tipo> <sha>\\t<ruta>` de `git ls-tree`."""
    _, _, entry = line.partition("\t")
    return entry


def _tail(result: subprocess.CompletedProcess[str] | None) -> str:
    if result is None:
        return "git no respondio (timeout o binario ausente)"
    return (result.stderr or result.stdout).strip()[-_TAIL_CHARS:] or "sin detalle"
