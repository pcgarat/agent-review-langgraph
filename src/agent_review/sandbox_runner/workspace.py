from __future__ import annotations

import subprocess
import tempfile
from typing import Protocol, runtime_checkable

from agent_review.errors import PermanentError, TransientError


class WorkspaceError(PermanentError):
    """No se pudo preparar el workspace (clon o parche invalido)."""


class WorkspaceUnavailable(TransientError):
    """Fallo recuperable al clonar (red, remoto caido)."""


@runtime_checkable
class WorkspacePort(Protocol):
    """Prepara un workspace efimero con el parche aplicado.

    Aisla el `git` del runner para poder testear la orquestacion sin clonar de
    verdad: un fake devuelve un directorio temporal.
    """

    def prepare(self, *, repo: str, head_sha: str, patch: str) -> str: ...

    def cleanup(self, workspace: str) -> None: ...


class GitWorkspace:
    """Clona `repo@head_sha` y aplica el parche con `git apply`.

    El clon usa un token de Bitbucket de **solo lectura** (`SANDBOX_GIT_TOKEN`),
    distinto del de `twg` del worker; sin el, un PR malicioso no podria pedir un
    push. Se clona por `https` con el token en el remoto efimero, nunca en argv
    persistente ni en el `.git/config` del workspace (se usa el entorno).
    """

    def __init__(
        self, *, git_bin: str = "git", git_token: str = "", timeout_s: float = 600.0
    ) -> None:
        self._git = git_bin
        self._token = git_token.strip()
        self._timeout = timeout_s

    def _auth_url(self, repo: str) -> str:
        if not self._token:
            return f"https://bitbucket.org/{repo}.git"
        # `x-token-auth` es el usuario que Bitbucket acepta para un access token.
        return f"https://x-token-auth:{self._token}@bitbucket.org/{repo}.git"

    def _git_run(
        self, cwd: str, args: list[str], *, timeout_s: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self._git, *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_s or self._timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorkspaceUnavailable(f"git {' '.join(args)} excedio el timeout") from exc
        except FileNotFoundError as exc:
            raise WorkspaceError(f"No se encontro el binario {self._git!r}") from exc

    def prepare(self, *, repo: str, head_sha: str, patch: str) -> str:
        workspace = tempfile.mkdtemp(prefix="sandbox-")
        init = self._git_run(workspace, ["init", "-q"])
        if init.returncode != 0:
            raise WorkspaceError(f"git init fallo: {init.stderr.strip()}")
        self._git_run(workspace, ["remote", "add", "origin", self._auth_url(repo)])
        fetch = self._git_run(workspace, ["fetch", "-q", "--depth", "1", "origin", head_sha])
        if fetch.returncode != 0:
            raise WorkspaceUnavailable(f"git fetch fallo: {fetch.stderr.strip()}")
        checkout = self._git_run(workspace, ["checkout", "-q", "FETCH_HEAD"])
        if checkout.returncode != 0:
            raise WorkspaceError(f"git checkout fallo: {checkout.stderr.strip()}")
        self._apply(workspace, patch)
        return workspace

    def _apply(self, workspace: str, patch: str) -> None:
        """`--check` primero: un parche que no aplica se detecta sin tocar el arbol."""
        patch_file = f"{workspace}/.reviews-fix.patch"
        with open(patch_file, "w", encoding="utf-8") as handle:
            handle.write(patch)
        check = self._git_run(workspace, ["apply", "--check", patch_file])
        if check.returncode != 0:
            raise WorkspaceError(f"el parche no aplica: {check.stderr.strip()}")
        applied = self._git_run(workspace, ["apply", patch_file])
        if applied.returncode != 0:
            raise WorkspaceError(f"git apply fallo: {applied.stderr.strip()}")

    def cleanup(self, workspace: str) -> None:
        import shutil

        shutil.rmtree(workspace, ignore_errors=True)
