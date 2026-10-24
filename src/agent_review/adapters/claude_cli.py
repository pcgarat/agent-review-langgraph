"""Subproceso `claude` (025 R2).

El ejecutable es inyectable. El argv lleva el tope de turnos y solo
herramientas de lectura. El entorno del hijo es una lista cerrada: filtrar
por denegación deja pasar el secreto que nadie recordó nombrar.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from agent_review.ports.claude import (
    ClaudeCeiling,
    ClaudeProcessCrashed,
    ClaudeQuotaExceeded,
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

# Bash entra en las permitidas: en dontAsk, denegarlo entero quita también
# el git de lectura. La red y la escritura las cierra el sandbox, no esta lista.
_READ_TOOLS = "Read,Grep,Glob,Bash"
_DENIED_TOOLS = "Edit,Write,NotebookEdit,WebFetch,WebSearch"

_QUOTA_MARKERS = (
    "quota",
    "rate limit",
    "rate_limit",
    "usage limit",
    "credit balance",
    "insufficient credit",
)


def redact_text(text: str, secrets: Sequence[str]) -> str:
    """Sustituye secretos en texto que puede salir del proceso o del checkout."""
    cleaned = text
    for secret in secrets:
        token = secret.strip()
        if len(token) < 8:
            continue
        cleaned = cleaned.replace(token, "***")
    return cleaned


def child_env(parent: Mapping[str, str]) -> dict[str, str]:
    """Entorno del subproceso: solo claves de la lista cerrada."""
    return {key: value for key, value in parent.items() if key in CHILD_ENV_ALLOW}


def review_prompt(base_sha: str) -> str:
    """La skill revisa el PR. Un peer externo no entra en esta pasada."""
    sha = base_sha.strip()
    if not sha:
        raise ValueError("sin base_sha el CLI no arranca")
    return (
        f"/ce-code-review mode:agent base:{sha} "
        "No uses un peer externo ni envíes el diff fuera de esta máquina."
    )


def claude_argv(prefix: Sequence[str], *, max_turns: int, base_sha: str) -> list[str]:
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
        "--disallowedTools",
        _DENIED_TOOLS,
        "--permission-mode",
        "dontAsk",
        "--setting-sources",
        "user",
        "--output-format",
        "stream-json",
        "--verbose",
        review_prompt(base_sha),
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
        redact: Sequence[str] = (),
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("el tiempo de pared tiene que ser positivo")
        self._prefix = tuple(argv_prefix)
        self._max_turns = max_turns
        self._timeout = timeout_s
        # `extra_env` se añade después del filtro: es una inyección deliberada
        # (el token de sesión), no una herencia del proceso padre.
        self._environ = child_env(os.environ if environ is None else environ)
        if extra_env:
            self._environ.update(extra_env)
        self._cwd = None if cwd is None else str(cwd)
        self._redact = tuple(redact)

    def run(
        self,
        *,
        repo: str,
        pr_id: int,
        head_sha: str,
        base_sha: str,
        on_line: Callable[[str], None] | None = None,
    ) -> str:
        del repo, pr_id, head_sha
        argv = claude_argv(self._prefix, max_turns=self._max_turns, base_sha=base_sha)
        try:
            process = subprocess.Popen(
                argv,
                cwd=self._cwd,
                env=self._environ,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as exc:
            raise ClaudeProcessCrashed("no se encontro el ejecutable de claude") from exc
        captured = _LiveStdout(on_line)
        stderr: list[str] = []
        threads = (
            threading.Thread(target=_read_stream, args=(process.stdout, captured.add, None)),
            threading.Thread(target=_read_stream, args=(process.stderr, stderr.append, on_line)),
        )
        for thread in threads:
            thread.start()
        try:
            code = process.wait(timeout=self._timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            for thread in threads:
                thread.join(timeout=2)
            raise ClaudeCeiling(f"claude excedio {self._timeout}s") from exc
        for thread in threads:
            thread.join(timeout=2)
        out = redact_text(captured.text(), self._redact)
        err = redact_text("".join(stderr), self._redact)
        if _hit_turn_ceiling(out) or _hit_turn_ceiling(err):
            raise ClaudeCeiling("claude llego al tope de turnos")
        if code != 0:
            detail = (err or out).strip()
            if _is_quota(detail):
                raise ClaudeQuotaExceeded("cupo de claude agotado")
            raise ClaudeProcessCrashed(detail[-500:] or f"claude salio con {code}")
        return out


def _read_stream(
    stream: object,
    keep: Callable[[str], None],
    on_line: Callable[[str], None] | None,
) -> None:
    if stream is None or not hasattr(stream, "readline"):
        return
    while True:
        line = stream.readline()
        if not line:
            break
        keep(line)
        if on_line is None:
            continue
        with contextlib.suppress(Exception):
            on_line(line.rstrip("\n"))


class _LiveStdout:
    """Separa el JSON final de los eventos que cuentan qué está haciendo Claude."""

    def __init__(self, on_line: Callable[[str], None] | None) -> None:
        self._raw: list[str] = []
        self._result: str | None = None
        self._saw = False
        self._on_line = on_line

    def add(self, line: str) -> None:
        self._raw.append(line)
        text = line.strip()
        if not text.startswith("{"):
            return
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return
        self._saw = True
        if self._on_line is not None:
            with contextlib.suppress(Exception):
                self._on_line(text)
        if event.get("type") == "result" and isinstance(event.get("result"), str):
            self._result = event["result"]

    def text(self) -> str:
        if self._saw and self._result is not None:
            return self._result
        return "".join(self._raw)


def _hit_turn_ceiling(text: str) -> bool:
    return "error_max_turns" in text


def _is_quota(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _QUOTA_MARKERS)
