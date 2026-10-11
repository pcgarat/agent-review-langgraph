from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_review.domain.models import ToolingSourceStatus


@runtime_checkable
class ToolingOutputSource(Protocol):
    """Provee la salida cruda de una herramienta estatica para un PR.

    En Fase 1 la salida la produce el pipeline de CI y se deposita como artefacto;
    el agente solo la lee. En Fase 3 la producira el sandbox, implementando este
    mismo puerto con otro adaptador.

    Devuelve `(estado, salida)`. `PRESENT` con su contenido; `MISSING` o
    `UNREADABLE` con `None`, para que el reviewer pueda distinguir un analisis
    limpio de una salida que no se pudo leer. El reviewer nunca inventa hallazgos
    a partir de la nada.
    """

    def read(
        self, tool: str, *, repo: str, pr_id: int, head_sha: str
    ) -> tuple[ToolingSourceStatus, str | None]: ...
