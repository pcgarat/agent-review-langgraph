"""Checkout de solo lectura del servicio claude (025 R4).

Árbol de trabajo propio. No es el clon *bare* del `sandbox-runner`. La
credencial de clonado entra en el `git fetch` y sale al quitar el remoto:
el subproceso `claude` no la hereda, y sin remoto no hay push.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from urllib.parse import quote

from agent_review.adapters.tooling_source import safe_segment
from agent_review.errors import TransientError
from agent_review.git_merge_base import DEEPEN_STEPS, DeepenFailed, deepen_to_merge_base
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
    base_sha: str,
    git_token: str,
    git_bin: str = "git",
    timeout_s: float = 120,
    remote_url: str | None = None,
    provider: str = "twg",
) -> str:
    """Deja `review` en `head_sha`, `base` en `base_sha` y quita el remoto.

    Devuelve el merge-base de `review` y `base`: la base del diff del PR.
    `base_sha` es la punta viva de la rama destino; diffear contra ella mete
    en la review lo que entró en destino después de la bifurcación. Si el
    ancestro común no aparece al profundizar, el checkout falla cerrado.

    Sin `base_sha` no se llega a `git`: una review del árbol entero no es
    una review del PR. El token solo viaja en el `remote add` de este
    proceso. Si no se puede quitar, el checkout falla y el CLI no arranca.
    `remote_url` sustituye la URL del hosting (tests locales). `provider`
    elige GitHub o Bitbucket cuando la URL no viene dada.
    """
    if not base_sha.strip():
        raise CheckoutError("sin base_sha el CLI no arranca")
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / ".git").exists():
        init = _git(directory, ["init", "-q"], git_bin, timeout_s=timeout_s)
        if init.returncode != 0:
            raise CheckoutError(_redact(init.stderr, git_token) or "git init fallo")
    _require_no_remote(directory, git_bin=git_bin, timeout_s=timeout_s)
    origin = remote_url if remote_url is not None else _auth_url(repo, git_token, provider=provider)
    added = _git(
        directory,
        ["remote", "add", "origin", origin],
        git_bin,
        timeout_s=timeout_s,
    )
    if added.returncode != 0:
        _require_no_remote(directory, git_bin=git_bin, timeout_s=timeout_s)
        raise CheckoutError(_redact(added.stderr, git_token) or "no se pudo añadir el remoto")
    fetch_error: CheckoutError | None = None
    merge_base = ""
    try:
        _git(
            directory,
            ["config", "core.symlinks", "false"],
            git_bin,
            timeout_s=timeout_s,
        )
        # El reintento reutiliza este árbol. Git no deja hacer fetch sobre la
        # rama que sigue checked out (`review` tras el intento anterior).
        _detach_head(directory, git_bin=git_bin, timeout_s=timeout_s, git_token=git_token)
        _fetch_ref(
            directory,
            sha=head_sha,
            ref="refs/heads/review",
            git_bin=git_bin,
            timeout_s=timeout_s,
            git_token=git_token,
            what="el head",
        )
        _fetch_ref(
            directory,
            sha=base_sha,
            ref="refs/heads/base",
            git_bin=git_bin,
            timeout_s=timeout_s,
            git_token=git_token,
            what="la base",
        )
        merge_base = _resolve_merge_base(
            directory,
            head_sha=head_sha,
            base_sha=base_sha,
            git_bin=git_bin,
            timeout_s=timeout_s,
            git_token=git_token,
        )
        checked = _git(
            directory,
            ["-c", "core.symlinks=false", "checkout", "-q", "review"],
            git_bin,
            timeout_s=timeout_s,
        )
        if checked.returncode != 0:
            raise CheckoutError(_redact(checked.stderr, git_token) or "git checkout fallo")
    except CheckoutError as exc:
        fetch_error = exc
    finally:
        removed = drop_push_remotes(directory, git_bin=git_bin, timeout_s=timeout_s)
    if not removed:
        raise CheckoutError("no se pudo quitar el remoto; el CLI no arranca")
    if fetch_error is not None:
        raise fetch_error
    return merge_base


def _resolve_merge_base(
    directory: Path,
    *,
    head_sha: str,
    base_sha: str,
    git_bin: str,
    timeout_s: float,
    git_token: str,
) -> str:
    """Merge-base de `head_sha` y `base_sha`. Sin él, el CLI no arranca."""

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return _git(directory, args, git_bin, timeout_s=timeout_s)

    try:
        merge_base = deepen_to_merge_base(run, head_sha=head_sha, base_sha=base_sha)
    except DeepenFailed as exc:
        detail = _redact(str(exc), git_token)
        raise CheckoutError(f"git fetch --deepen fallo: {detail}".rstrip(": ").rstrip()) from None
    if merge_base is None:
        raise CheckoutError(
            f"sin ancestro común entre {head_sha[:12]} y {base_sha[:12]} "
            f"tras {sum(DEEPEN_STEPS)} commits; el CLI no arranca"
        )
    return merge_base


def _detach_head(
    directory: Path,
    *,
    git_bin: str,
    timeout_s: float,
    git_token: str,
) -> None:
    """Pasa a detached HEAD si ya hay un commit. Un `git init` vacío no."""
    head = _git(
        directory,
        ["rev-parse", "--verify", "-q", "HEAD"],
        git_bin,
        timeout_s=timeout_s,
    )
    if head.returncode != 0:
        return
    detached = _git(
        directory,
        ["-c", "core.symlinks=false", "checkout", "--detach", "-q"],
        git_bin,
        timeout_s=timeout_s,
    )
    if detached.returncode != 0:
        detail = _redact(detached.stderr, git_token)
        raise CheckoutError(f"git checkout --detach fallo: {detail}".rstrip(": ").rstrip())


def _fetch_ref(
    directory: Path,
    *,
    sha: str,
    ref: str,
    git_bin: str,
    timeout_s: float,
    git_token: str,
    what: str,
) -> None:
    fetched = _git(
        directory,
        ["fetch", "-q", "--depth", "1", "origin", f"{sha}:{ref}"],
        git_bin,
        timeout_s=timeout_s,
    )
    if fetched.returncode != 0:
        detail = _redact(fetched.stderr, git_token)
        raise CheckoutError(f"git fetch de {what} fallo: {detail}".rstrip(": ").rstrip())


def drop_push_remotes(directory: Path, *, git_bin: str = "git", timeout_s: float = 60) -> bool:
    """Quita todos los remotos. `False` si alguno sigue, con su URL."""
    listed = _git(directory, ["remote"], git_bin, timeout_s=timeout_s)
    if listed.returncode != 0:
        return False
    ok = True
    for name in listed.stdout.split():
        removed = _git(directory, ["remote", "remove", name], git_bin, timeout_s=timeout_s)
        if removed.returncode != 0:
            ok = False
    remaining = _git(directory, ["remote"], git_bin, timeout_s=timeout_s)
    if remaining.returncode != 0 or remaining.stdout.strip():
        return False
    return ok


def _require_no_remote(directory: Path, *, git_bin: str, timeout_s: float) -> None:
    if not drop_push_remotes(directory, git_bin=git_bin, timeout_s=timeout_s):
        raise CheckoutError("no se pudo quitar el remoto; el CLI no arranca")


def _is_bare(path: Path) -> bool:
    return any(part.startswith(BARE_CLONE_PREFIX) for part in path.parts)


def _segment(value: str) -> str:
    cleaned = safe_segment(value, default="")
    if not cleaned:
        raise ValueError("el segmento del checkout no puede estar vacio")
    return cleaned


def _auth_url(repo: str, token: str, *, provider: str = "twg") -> str:
    token = quote(token.strip(), safe="")
    if provider == "github":
        if not token:
            return f"https://github.com/{repo}.git"
        return f"https://x-access-token:{token}@github.com/{repo}.git"
    if not token:
        return f"https://bitbucket.org/{repo}.git"
    return f"https://x-token-auth:{token}@bitbucket.org/{repo}.git"


_GIT_ENV_ALLOW = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TZ",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "GIT_EXEC_PATH",
)


def _redact(text: str, token: str) -> str:
    from agent_review.adapters.claude_cli import redact_text

    return redact_text((text or "").strip(), [token])[-500:]


def _git_env() -> dict[str, str]:
    """Entorno de git sin los tokens del proceso. Tampoco lee la config global."""
    env = {key: os.environ[key] for key in _GIT_ENV_ALLOW if key in os.environ}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(
    directory: Path,
    args: list[str],
    git_bin: str,
    *,
    timeout_s: float,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [git_bin, "-C", str(directory), *args],
            capture_output=True,
            text=True,
            check=False,
            env=_git_env(),
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise CheckoutError(f"git excedio {timeout_s}s") from exc
