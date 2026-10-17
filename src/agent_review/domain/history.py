from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from agent_review.domain.models import ReviewOutcome, SkipReason


class ReviewEnd(StrEnum):
    """Cómo terminó una review, para el histórico del panel (021 R9)."""

    COMPLETE = "completa"
    PARTIAL = "parcial"  # codigo o configuracion sin leer (012)
    INCOMPLETE = "incompleta"  # alguna fuente estatica sin cubrir


class ReviewSummary(BaseModel):
    """Resumen de una review terminada (021 R9).

    Sale del `ReviewOutcome` al cerrar; el detalle completo sigue en el checkpointer
    (recorrido de 019) y el razonamiento, en su stream mientras dure su TTL.
    """

    review_key: str
    repo: str
    pr_id: int
    head_sha: str
    finished_at: datetime
    end: ReviewEnd
    findings: int = 0
    by_severity: dict[str, int] = Field(default_factory=dict)
    tokens: int = 0
    files_reviewed: int = 0
    files_skipped: int = 0
    reasoning: bool = False

    @classmethod
    def from_outcome(
        cls, outcome: ReviewOutcome, *, finished_at: datetime, reasoning: bool
    ) -> ReviewSummary:
        if outcome.complete:
            end = ReviewEnd.COMPLETE
        elif outcome.unavailable_sources:
            end = ReviewEnd.INCOMPLETE
        else:
            end = ReviewEnd.PARTIAL
        by_severity: dict[str, int] = {}
        for finding in outcome.findings:
            by_severity[finding.severity.value] = by_severity.get(finding.severity.value, 0) + 1
        coverage = outcome.coverage
        skipped = (
            sum(1 for reason in coverage.skipped.values() if reason is not SkipReason.IGNORED)
            if coverage is not None
            else 0
        )
        return cls(
            review_key=outcome.request.review_key,
            repo=outcome.request.repo,
            pr_id=outcome.request.pr_id,
            head_sha=outcome.request.head_sha,
            finished_at=finished_at,
            end=end,
            findings=len(outcome.findings),
            by_severity=dict(sorted(by_severity.items())),
            tokens=outcome.tokens_total,
            files_reviewed=len(coverage.reviewed) if coverage is not None else 0,
            files_skipped=skipped,
            reasoning=reasoning and outcome.tokens_total > 0,
        )
