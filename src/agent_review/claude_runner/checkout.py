"""Checkout de solo lectura del servicio claude (025 R4).

Árbol de trabajo propio. No es el clon *bare* del `sandbox-runner`. La
credencial de clonado entra en el `git fetch` y sale al quitar el remoto:
el subproceso `claude` no la hereda, y sin remoto no hay push.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_review.adapters.tooling_source import safe_segment
from agent_review.errors import TransientError
from agent_review.sandbox_runner.read_workspace import BARE_CLONE_PREFIX


class CheckoutError(TransientError):
    """No se pudo preparar el árbol. El CLI no llega a arrancar."""


def checkout_directory(root: Path, *, repo: str, head_sha: str) -> Path:
    """Ruta del árbol. Rechaza un directorio que sea el clon *bare*."""
    relative = Path(_segment(repo), _segment(head_sha))
    resolved = (root / relative).resolve()
    if _is_bare(resolved) or _is_bare(root.resolve()):
        raise ValueError(f"el checkout de claude no puede vivir en el clon bare ({resolved})")
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"la ruta del checkout escapa de {root}") from exc
    return resolved


def prepare_checkout(
    directory: Path,
    *,
    repo: str,
    head_sha: str,
    git_token: str,
    git_bin: str = "git",
) -> None:
    """Deja `directory` en `head_sha` y sin remotos.

    El token solo viaja en el `remote add` de este proceso. El `finally` lo
    retira aunque el fetch falle, para que no quede en `.git/config`.
    """
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / ".git").exists():
        init = _git(directory, ["init", "-q"], git_bin)
        if init.returncode != 0:
            raise CheckoutError(_redact(init.stderr, git_token) or "git init fallo")
    drop_push_remotes(directory, git_bin=git_bin)
    added = _git(directory, ["remote", "add", "origin", _auth_url(repo, git_token)], git_bin)
    if added.returncode != 0:
        drop_push_remotes(directory, git_bin=git_bin)
        raise CheckoutError(_redact(added.stderr, git_token) or "no se pudo añadir el remoto")
    try:
        fetched = _git(directory, ["fetch", "-q", "--depth", "1", "origin", head_sha], git_bin)
        if fetched.returncode != 0:
            raise CheckoutError(_redact(fetched.stderr, git_token) or "git fetch fallo")
        checked = _git(directory, ["checkout", "-q", "FETCH_HEAD"], git_bin)
        if checked.returncode != 0:
            raise CheckoutError(_redact(checked.stderr, git_token) or "git checkout fallo")
    finally:
        drop_push_remotes(directory, git_bin=git_bin)


def drop_push_remotes(directory: Path, *, git_bin: str = "git") -> None:
    """Quita todos los remotos. Sin remoto no hay push ni URL con token."""
    listed = _git(directory, ["remote"], git_bin)
    for name in listed.stdout.split():
        _git(directory, ["remote", "remove", name], git_bin)


def _is_bare(path: Path) -> bool:
    return any(part.startswith(BARE_CLONE_PREFIX) for part in path.parts)


def _segment(value: str) -> str:
    cleaned = safe_segment(value, default="")
    if not cleaned:
        raise ValueError("el segmento del checkout no puede estar vacio")
    return cleaned


def _auth_url(repo: str, token: str) -> str:
    token = token.strip()
    if not token:
        return f"https://bitbucket.org/{repo}.git"
    return f"https://x-token-auth:{token}@bitbucket.org/{repo}.git"


def _redact(text: str, token: str) -> str:
    cleaned = (text or "").strip()
    if token.strip():
        cleaned = cleaned.replace(token.strip(), "***")
    return cleaned[-500:]


def _git(directory: Path, args: list[str], git_bin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [git_bin, "-C", str(directory), *args],
        capture_output=True,
        text=True,
        check=False,
    )
