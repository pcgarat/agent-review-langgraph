"""Qué está haciendo el CLI de Claude, en el volumen que comparten runner y panel.

El runner escribe mientras el proceso vive. El panel lee. No pasa por Redis y
no guarda el token: cada línea se redacta antes de entrar aquí.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def activity_dir(session_file: Path) -> Path:
    return session_file.parent / "activity"


def activity_stem(pr_id: int, head_sha: str) -> str:
    sha = _SAFE.sub("", head_sha)[:16] or "sin-sha"
    return f"{pr_id}-{sha}"


class ClaudeActivity:
    """Un fichero de estado y otro con las líneas, por commit."""

    def __init__(self, session_file: Path, *, pr_id: int, head_sha: str) -> None:
        folder = activity_dir(session_file)
        folder.mkdir(parents=True, exist_ok=True)
        stem = activity_stem(pr_id, head_sha)
        self._status = folder / f"{stem}.json"
        self._log = folder / f"{stem}.log"
        self._started = datetime.now(UTC)

    def start(self) -> None:
        self._write(finished_at=None)
        self._log.write_text("", encoding="utf-8")

    def append(self, line: str) -> None:
        text = line.replace("\x00", "").rstrip()
        if not text:
            return
        with self._log.open("a", encoding="utf-8") as handle:
            handle.write(text[:500] + "\n")

    def finish(self) -> None:
        self._write(finished_at=datetime.now(UTC).isoformat())

    def _write(self, *, finished_at: str | None) -> None:
        payload = {
            "started_at": self._started.isoformat(),
            "finished_at": finished_at,
        }
        self._status.write_text(json.dumps(payload), encoding="utf-8")


def read_activity(session_file: Path, *, pr_id: int, head_sha: str) -> dict[str, object]:
    """Estado y texto para la ficha. Vacío si este commit no ha llamado al CLI."""
    folder = activity_dir(session_file)
    stem = activity_stem(pr_id, head_sha)
    status_path = folder / f"{stem}.json"
    log_path = folder / f"{stem}.log"
    started_at = None
    finished_at = None
    if status_path.is_file():
        try:
            raw = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            started_at = raw.get("started_at") if isinstance(raw.get("started_at"), str) else None
            finished = raw.get("finished_at")
            finished_at = finished if isinstance(finished, str) else None
    text = ""
    if log_path.is_file():
        try:
            text = log_path.read_text(encoding="utf-8")
        except OSError:
            text = ""
    if len(text) > 20_000:
        text = "…\n" + text[-20_000:]
    return {
        "started_at": started_at,
        "finished_at": finished_at,
        "running": started_at is not None and finished_at is None,
        "text": text,
    }


def review_target(review_key: str) -> tuple[int, str] | None:
    """`pr_id` y `head_sha` de una review_key. El repo puede llevar barras."""
    parts = review_key.rsplit(":", 3)
    if len(parts) == 4:
        _repo, pr, sha, _pipeline = parts
    elif len(parts) == 3:
        _repo, pr, sha = parts
    else:
        return None
    if not pr.isdigit() or not sha:
        return None
    return int(pr), sha
