from __future__ import annotations

from typing import Protocol, runtime_checkable

from reviews.domain.models import Finding


@runtime_checkable
class ReviewerPort(Protocol):
    """Genera hallazgos a partir de un diff.

    Implementaciones: una persona LLM por nodo, o la invocacion de una skill.
    En Fase 0 hay una implementacion nula que no devuelve hallazgos.
    """

    name: str

    def review(
        self, *, repo: str, pr_id: int, diff: str, head_sha: str = ""
    ) -> list[Finding]: ...
