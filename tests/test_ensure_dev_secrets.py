"""Los secretos que inventa el repo se rellenan una vez; el token de git no."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ensure_dev_secrets.py"
_SPEC = importlib.util.spec_from_file_location("ensure_dev_secrets", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
ensure_dev_secrets = importlib.util.module_from_spec(_SPEC)
sys.modules["ensure_dev_secrets"] = ensure_dev_secrets
_SPEC.loader.exec_module(ensure_dev_secrets)


def test_rellena_placeholders_sin_inventar_el_token_de_git(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "\n".join(
            [
                "# PANEL_SESSION_SECRET=no-tocar",
                "PANEL_SESSION_SECRET=change-me",
                "CLAUDE_RUNNER_TOKEN=",
                'CLAUDE_RUNNER_URL=""',
                "CLAUDE_GIT_TOKEN=change-me",
                "",
            ]
        ),
        encoding="utf-8",
    )
    notes = ensure_dev_secrets.ensure(path)
    text = path.read_text(encoding="utf-8")
    assert "CLAUDE_GIT_TOKEN=change-me" in text
    assert "CLAUDE_RUNNER_URL=http://claude-runner:8098" in text
    assert "# PANEL_SESSION_SECRET=no-tocar" in text
    assert "PANEL_SESSION_SECRET=change-me" not in text
    secret = _value(text, "PANEL_SESSION_SECRET")
    assert secret
    assert any(secret in note for note in notes)
    assert any("TWG_BBC_TOKEN" in note for note in notes)
    again = ensure_dev_secrets.ensure(path)
    assert path.read_text(encoding="utf-8") == text
    assert not any(note.startswith("[nuevo]") for note in again)


def test_no_rota_un_valor_real(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "\n".join(
            [
                "PANEL_SESSION_SECRET=already-real",
                "CLAUDE_RUNNER_TOKEN=also-real",
                "CLAUDE_RUNNER_URL=http://example:9",
                "CLAUDE_GIT_TOKEN=git-real",
                "",
            ]
        ),
        encoding="utf-8",
    )
    before = path.read_text(encoding="utf-8")
    notes = ensure_dev_secrets.ensure(path)
    assert path.read_text(encoding="utf-8") == before
    assert notes == []
    assert "already-real" not in "\n".join(notes)


def test_sin_env_no_crea_el_fichero(tmp_path: Path) -> None:
    missing = tmp_path / ".env"
    notes = ensure_dev_secrets.ensure(missing)
    assert not missing.exists()
    assert "no hay .env" in notes[0]


def _value(text: str, key: str) -> str:
    for line in text.splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    return ""
