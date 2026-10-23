"""Subproceso `claude` (025 R2).

El ejecutable es inyectable. El argv lleva el tope de turnos y solo
herramientas de lectura. El entorno del hijo es una lista cerrada: filtrar
por denegación deja pasar el secreto que nadie recordó nombrar.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from agent_review.ports.claude import (
    ClaudeProcessCrashed,
    ClaudeQuotaExceeded,
    ClaudeTimeout,
)

# Lo que el hijo puede heredar. Un token de VCS o de Teams no está aquí.
CHILD_ENV_ALLOW = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TZ",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)

# Herramientas de lectura. Las de escritura no se nombran: nombrarlas en un
# `--disallowedTools` las metería igual en el argv.
_READ_TOOLS = "Read,Grep,Glob"

_PROMPT = (
    "Revisa el árbol de trabajo. Responde solo un array JSON de hallazgos "
    "del esquema de review, sin markdown. Si no hay hallazgos, responde []."
)

_QUOTA_MARKERS = (
    "quota",
    "rate limit",
    "rate_limit",
    "usage limit",
    "credit balance",
    "insufficient credit",
)


def child_env(parent: Mapping[str, str]) -> dict[str, str]:
    """Entorno del subproceso: solo claves de la lista cerrada."""
    return {key: value for key, value in parent.items() if key in CHILD_ENV_ALLOW}


def claude_argv(prefix: Sequence[str], *, max_turns: int) -> list[str]:
    """Argv del CLI. `prefix` es el ejecutable inyectable, con sus argumentos."""
    if max_turns < 1:
        raise ValueError("el tope de turnos tiene que ser al menos 1")
    return [
        *prefix,
        "-p",
        "--max-turns",
        str(max_turns),
        "--allowedTools",
        _READ_TOOLS,
        _PROMPT,
    ]


class SubprocessClaudeRunner:
    """Lanza el ejecutable y devuelve stdout. No valida el esquema."""

    def __init__(
        self,
        *,
        argv_prefix: Sequence[str],
        max_turns: int,
        timeout_s: float,
        environ: Mapping[str, str] | None = None,
        extra_env: Mapping[str, str] | None = None,
        cwd: str | Path | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("el tiempo de pared tiene que ser positivo")
        self._argv = claude_argv(argv_prefix, max_turns=max_turns)
        self._timeout = timeout_s
        # `extra_env` se añade después del filtro: es una inyección deliberada
        # (el token de sesión), no una herencia del proceso padre.
        self._environ = child_env(os.environ if environ is None else environ)
        if extra_env:
            self._environ.update(extra_env)
        self._cwd = None if cwd is None else str(cwd)

    def run(self, *, repo: str, pr_id: int, head_sha: str) -> str:
        del repo, pr_id, head_sha
        try:
            completed = subprocess.run(
                self._argv,
                cwd=self._cwd,
                env=self._environ,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ClaudeTimeout(f"claude excedio {self._timeout}s") from exc
        except FileNotFoundError as exc:
            raise ClaudeProcessCrashed("no se encontro el ejecutable de claude") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            if _is_quota(detail):
                raise ClaudeQuotaExceeded("cupo de claude agotado")
            raise ClaudeProcessCrashed(detail[-500:] or f"claude salio con {completed.returncode}")
        return completed.stdout


def _is_quota(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _QUOTA_MARKERS)
