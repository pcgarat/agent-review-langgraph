from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_PLACEHOLDERS = ("tool", "slug", "pr_id", "head_sha")


def safe_segment(value: object, *, default: str = "unknown") -> str:
    """Convierte un valor en un segmento de ruta sin traversal ni separadores."""
    cleaned = _UNSAFE.sub("_", str(value)).strip("._-")
    return cleaned or default


class FileToolingOutputSource:
    """Lee la salida de una herramienta desde un artefacto en disco.

    Fase 1: el artefacto lo deposita el pipeline de CI (o una persona, en local);
    el agente no ejecuta la herramienta. La plantilla se resuelve por review, no
    en construccion, para que un mismo worker sirva PRs distintos.
    """

    def __init__(self, *, template: str, root: str | Path = ".") -> None:
        self._root = Path(root)
        self._template = template
        self._validate()

    def _validate(self) -> None:
        try:
            self._template.format(**{field: "x" for field in _PLACEHOLDERS})
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(
                f"Plantilla de artefacto invalida {self._template!r}: {exc}. "
                f"Placeholders admitidos: {_PLACEHOLDERS}"
            ) from exc

    def path_for(self, tool: str, *, repo: str, pr_id: int, head_sha: str) -> Path:
        relative = self._template.format(
            tool=safe_segment(tool),
            slug=safe_segment(repo),
            pr_id=int(pr_id),
            head_sha=safe_segment(head_sha),
        )
        path = Path(relative)
        return path if path.is_absolute() else self._root / path

    def read(self, tool: str, *, repo: str, pr_id: int, head_sha: str) -> str | None:
        path = self.path_for(tool, repo=repo, pr_id=pr_id, head_sha=head_sha)
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("Salida de %s no disponible en %s", tool, path)
            return None


class StaticToolingOutputSource:
    """Salida fija en memoria, para tests e inyeccion manual."""

    def __init__(self, payload: str) -> None:
        self._payload = payload

    def read(self, tool: str, *, repo: str, pr_id: int, head_sha: str) -> str | None:
        del tool, repo, pr_id, head_sha
        return self._payload
