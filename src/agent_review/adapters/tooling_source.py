from __future__ import annotations

import logging
import re
from pathlib import Path

from agent_review.domain.models import ToolingSourceStatus

logger = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_PLACEHOLDERS = ("tool", "slug", "pr_id", "head_sha")


def validate_tooling_template(*, template: str, root: str | Path = ".") -> None:
    """Construye la fuente sin leer nada: falla ruidoso si la plantilla no resuelve.

    Pensado para el arranque (D2): un cambio en el CI que rompa la ruta del
    artefacto debe fallar al levantar el servicio, no al procesar el primer PR.
    """
    FileToolingOutputSource(template=template, root=root)


def safe_segment(value: object, *, default: str = "unknown") -> str:
    """Convierte un valor en un segmento de ruta sin traversal ni separadores."""
    cleaned = _clean(value)
    return cleaned or default


def _clean(value: object) -> str:
    return _UNSAFE.sub("_", str(value)).strip("._-")


def _required_segment(name: str, value: object) -> str:
    """Como `safe_segment`, pero falla ruidoso en vez de inventar `unknown`."""
    cleaned = _clean(value)
    if not cleaned:
        raise ValueError(f"El placeholder {name!r} no puede estar vacio")
    return cleaned


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
        if not self._template.strip():
            raise ValueError("La plantilla del artefacto no puede estar vacia")
        if Path(self._template).is_absolute():
            raise ValueError(
                f"La plantilla del artefacto no puede ser absoluta: {self._template!r}"
            )
        # Valores representativos de los tipos reales (`pr_id` es int, `head_sha`
        # str), para que un format-spec incompatible falle aqui y no en runtime.
        try:
            rendered = self._template.format(tool="x", slug="x", pr_id=0, head_sha="sha")
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(
                f"Plantilla de artefacto invalida {self._template!r}: {exc}. "
                f"Placeholders admitidos: {_PLACEHOLDERS}"
            ) from exc
        self._assert_contained(Path(rendered))

    def _assert_contained(self, candidate: Path) -> None:
        root = self._root.resolve()
        resolved = (root / candidate).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"La plantilla escapa del root {self._root!r}: {self._template!r}")

    def path_for(self, tool: str, *, repo: str, pr_id: int, head_sha: str) -> Path:
        try:
            relative = self._template.format(
                tool=_required_segment("tool", tool),
                slug=safe_segment(repo),
                pr_id=int(pr_id),
                head_sha=_required_segment("head_sha", head_sha),
            )
        except ValueError as exc:
            raise ValueError(f"No se pudo resolver la ruta del artefacto: {exc}") from exc
        return self._root / Path(relative)

    def read(
        self, tool: str, *, repo: str, pr_id: int, head_sha: str
    ) -> tuple[ToolingSourceStatus, str | None]:
        # - `path_for` falla ruidoso ante segmentos invalidos: una ruta irresoluble
        #   no debe confundirse con "sin hallazgos", asi que la elevamos. - un
        #   artefacto ausente o ilegible si se reporta como estado, no como exito.
        path = self.path_for(tool, repo=repo, pr_id=pr_id, head_sha=head_sha)
        try:
            return ToolingSourceStatus.PRESENT, path.read_text(encoding="utf-8")
        except OSError:
            logger.error("Salida de %s ausente en %s", tool, path)
            return ToolingSourceStatus.MISSING, None
        except UnicodeDecodeError:
            logger.error("Salida de %s ilegible (no UTF-8) en %s", tool, path)
            return ToolingSourceStatus.UNREADABLE, None


class StaticToolingOutputSource:
    """Salida fija en memoria, para tests e inyeccion manual."""

    def __init__(
        self, payload: str, *, status: ToolingSourceStatus = ToolingSourceStatus.PRESENT
    ) -> None:
        self._payload = payload
        self._status = status

    def read(
        self, tool: str, *, repo: str, pr_id: int, head_sha: str
    ) -> tuple[ToolingSourceStatus, str | None]:
        del tool, repo, pr_id, head_sha
        if self._status is not ToolingSourceStatus.PRESENT:
            return self._status, None
        return ToolingSourceStatus.PRESENT, self._payload
