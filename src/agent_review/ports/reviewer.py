from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_review.domain.models import ReviewerResult


@runtime_checkable
class ReviewerPort(Protocol):
    """Genera hallazgos (y el estado de su fuente) a partir de un diff.

    Implementaciones: una persona LLM por nodo, la invocacion de una skill, o la
    persona estatica que lee la salida de una herramienta. En Fase 0 hay una
    implementacion nula que no devuelve hallazgos.

    Devuelve un `ReviewerResult`: los hallazgos mas, si la persona depende de una
    fuente externa, el estado de esa fuente (`tooling_status`). Una persona sin
    fuente externa deja `tooling_status` en `None`.

    `review_key` identifica la ejecucion
    ([ADR-0005](../../../docs/adr/0005-identidad-de-la-ejecucion.md)): las personas
    con presupuesto compartido lo necesitan para reservar y confirmar contra el
    contador de esa review (017 B1). Las personas sin coste lo ignoran.

    `grounded` (bool, opcional, por defecto `False`): si la hipotesis nace de una
    herramienta que ya corrio (009 V7). Quien no lo declara se lee como no
    grounded (`getattr(reviewer, "grounded", False)`). Solo `ToolingReviewer` lo
    pone a `True`; el merge estampa `grounded_findings` y L2 no vuelve a verificar
    esos hallazgos.
    """

    name: str

    def review(
        self, *, repo: str, pr_id: int, diff: str, head_sha: str = "", review_key: str = ""
    ) -> ReviewerResult: ...
