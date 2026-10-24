#!/usr/bin/env python3
"""Rellena en `.env` los secretos que este repo inventa, si siguen en blanco.

Solo actúa sobre un valor vacío o `change-me`. Un valor ya puesto no se rota.
`CLAUDE_GIT_TOKEN` no se rellena: si está vacío, el runner clona con
`GITHUB_TOKEN` o `TWG_BBC_TOKEN`.

`make up`, `make panel`, `make claude` y `make panel-claude` lo ejecutan antes
de Compose. La primera vez que crea `PANEL_SESSION_SECRET` lo imprime, porque
el panel pasa a exigirlo en la cookie y no hay formulario de login.
"""

from __future__ import annotations

import os
import secrets
import stat
import sys
from pathlib import Path

from agent_review.settings import is_missing

ROOT = Path(__file__).resolve().parents[1]
_RUNNER_URL = "http://claude-runner:8098"
_GENERATED = ("PANEL_SESSION_SECRET", "CLAUDE_RUNNER_TOKEN")


def ensure(path: Path) -> list[str]:
    """Actualiza `path` y devuelve las líneas que hay que mostrar."""
    if not path.is_file():
        return ["[aviso] no hay .env; nada que rellenar (make bootstrap)"]
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    notes: list[str] = []
    notes.extend(_fill_url(lines))
    for key in _GENERATED:
        notes.extend(_fill_secret(lines, key))
    notes.extend(_git_token_note(lines))
    updated = "\n".join(lines)
    if lines:
        updated += "\n"
    if updated != original:
        path.write_text(updated, encoding="utf-8")
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return notes


def _fill_url(lines: list[str]) -> list[str]:
    index, value = _effective(lines, "CLAUDE_RUNNER_URL")
    if value and not is_missing(value):
        return []
    _write(lines, "CLAUDE_RUNNER_URL", _RUNNER_URL, index)
    return [f"[nuevo] CLAUDE_RUNNER_URL={_RUNNER_URL}"]


def _fill_secret(lines: list[str], key: str) -> list[str]:
    index, value = _effective(lines, key)
    if value and not is_missing(value):
        return []
    generated = secrets.token_hex(32)
    _write(lines, key, generated, index)
    if key == "PANEL_SESSION_SECRET":
        return [
            "[nuevo] PANEL_SESSION_SECRET guardado en .env",
            "        el panel lo exige en la cookie panel_session:",
            f'        document.cookie = "panel_session={generated}; Path=/"',
        ]
    return [f"[nuevo] {key} guardado en .env"]


def _git_token_note(lines: list[str]) -> list[str]:
    _, value = _effective(lines, "CLAUDE_GIT_TOKEN")
    if value and not is_missing(value):
        return []
    provider = _effective(lines, "VCS_PROVIDER")[1] or "twg"
    other = "GITHUB_TOKEN" if provider == "github" else "TWG_BBC_TOKEN"
    _, other_value = _effective(lines, other)
    if other_value and not is_missing(other_value):
        return []
    return [f"[aviso] {other} sigue sin configurar: el checkout de Claude lo reutiliza"]


def _effective(lines: list[str], key: str) -> tuple[int | None, str]:
    """Índice y valor de la última asignación. Los comentarios no cuentan."""
    found: tuple[int | None, str] = (None, "")
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, raw = stripped.split("=", 1)
        if name.strip() != key:
            continue
        found = (index, _unquote(raw.strip()))
    return found


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _write(lines: list[str], key: str, value: str, index: int | None) -> None:
    assignment = f"{key}={value}"
    if index is None:
        lines.append(assignment)
        return
    lines[index] = assignment


def main() -> int:
    path = Path(os.environ.get("ENV_FILE", ROOT / ".env"))
    for note in ensure(path):
        print(note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
