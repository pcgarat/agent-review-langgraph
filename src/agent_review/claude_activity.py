"""Qué está haciendo el CLI de Claude, en el volumen que comparten runner y panel.

El runner escribe mientras el proceso vive. El panel lee. No pasa por Redis y
no guarda el token: cada línea se redacta antes de entrar aquí.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from agent_review.adapters.tooling_source import safe_segment


def activity_dir(session_file: Path) -> Path:
    return session_file.parent / "activity"


def activity_stem(repo: str, pr_id: int, head_sha: str) -> str:
    """`repo + pr_id + head_sha` entero. Dos repos no comparten el fichero."""
    name = safe_segment(repo, default="sin-repo")
    sha = safe_segment(head_sha, default="") or "sin-sha"
    return f"{name}-{pr_id}-{sha}"


def activity_paths(
    session_file: Path, *, repo: str, pr_id: int, head_sha: str
) -> tuple[Path, Path]:
    stem = activity_stem(repo, pr_id, head_sha)
    folder = activity_dir(session_file)
    return folder / f"{stem}.json", folder / f"{stem}.log"


class ClaudeActivity:
    """Un fichero de estado y otro con las líneas, por repo y commit."""

    def __init__(self, session_file: Path, *, repo: str, pr_id: int, head_sha: str) -> None:
        folder = activity_dir(session_file)
        folder.mkdir(parents=True, exist_ok=True)
        self._status, self._log = activity_paths(
            session_file, repo=repo, pr_id=pr_id, head_sha=head_sha
        )
        self._started = datetime.now(UTC)

    def start(self) -> None:
        self._write(finished_at=None)
        self._log.write_text("", encoding="utf-8")

    def append(self, line: str) -> None:
        text = line.replace("\x00", "").rstrip()
        if not text:
            return
        with self._log.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")
            handle.flush()

    def finish(self) -> None:
        self._write(finished_at=datetime.now(UTC).isoformat())

    def _write(self, *, finished_at: str | None) -> None:
        payload = {
            "started_at": self._started.isoformat(),
            "finished_at": finished_at,
        }
        self._status.write_text(json.dumps(payload), encoding="utf-8")


def read_activity(session_file: Path, *, repo: str, pr_id: int, head_sha: str) -> dict[str, object]:
    """Estado y texto entero. Vacío si este commit no ha llamado al CLI."""
    status_path, log_path = activity_paths(session_file, repo=repo, pr_id=pr_id, head_sha=head_sha)
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
    return {
        "started_at": started_at,
        "finished_at": finished_at,
        "running": started_at is not None and finished_at is None,
        "text": text,
    }


def review_target(review_key: str) -> tuple[str, int, str] | None:
    """`repo`, `pr_id` y `head_sha` de una review_key. El repo puede llevar barras."""
    parts = review_key.rsplit(":", 3)
    if len(parts) == 4:
        repo, pr, sha, _pipeline = parts
    elif len(parts) == 3:
        repo, pr, sha = parts
    else:
        return None
    if not repo or not pr.isdigit() or not sha:
        return None
    return repo, int(pr), sha


def transcript_events(
    session_file: Path,
    *,
    repo: str,
    pr_id: int,
    head_sha: str,
    poll_s: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
    start_offset: int = 0,
) -> Iterator[str]:
    """SSE: lo ya escrito y después cada línea nueva, hasta `finished_at`.

    Una línea a medias no se emite. Cortar el directo no borra el fichero.

    Cada evento lleva `id:` con el byte del fichero donde empieza su línea, y
    `start_offset` reanuda desde ahí. Sin eso, el navegador reconecta solo y el
    panel recibe el transcript entero otra vez, duplicando lo ya pintado.
    """
    status_path, log_path = activity_paths(session_file, repo=repo, pr_id=pr_id, head_sha=head_sha)
    offset = max(0, start_offset)
    pending = ""
    while True:
        if log_path.is_file():
            with log_path.open("r", encoding="utf-8") as handle:
                handle.seek(offset)
                chunk = handle.read()
                end = handle.tell()
            if chunk:
                # El evento se identifica por el byte donde *empieza*, no por
                # donde acaba: reanudar ahí repite esa línea una vez como mucho
                # si el corte cayó justo en medio.
                cursor = offset
                offset = end
                pending += chunk
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    start = cursor
                    cursor += len(line.encode("utf-8")) + 1
                    if line:
                        yield _sse("line", line, event_id=start)
        if _is_finished(status_path) and not pending:
            yield _sse("end", {})
            return
        sleep(poll_s)


def _is_finished(status_path: Path) -> bool:
    if not status_path.is_file():
        return False
    try:
        raw = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(raw, dict):
        return False
    return isinstance(raw.get("finished_at"), str)


def _sse(name: str, payload: object, *, event_id: int | None = None) -> str:
    """Un frame SSE. Con `event_id`, el navegador manda `Last-Event-ID` al
    reconectar y el panel puede reanudar en vez de repetirlo todo."""
    data = json.dumps(payload, ensure_ascii=False)
    head = "" if event_id is None else f"id: {event_id}\n"
    return f"{head}event: {name}\ndata: {data}\n\n"
