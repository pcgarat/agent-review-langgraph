from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from reviews.domain.models import (
    Decision,
    DecisionOutcome,
    Finding,
    FindingAction,
    HitlDecision,
)

# Acciones de politica que pueden producir un fix. `escalate` no: aprobar un
# escalado significa "procede fuera del agente", no "arregla esto" (spec §4).
_FIXABLE = frozenset({FindingAction.FIX_WITH_APPROVAL, FindingAction.FIX_AUTO})


@dataclass(frozen=True)
class FixCandidate:
    finding: Finding
    decision: Decision


def _key(finding: Finding) -> str:
    return finding.id or f"{finding.file}:{finding.line}:{finding.title}"


def select_candidates(
    findings: Iterable[Finding],
    decisions: Iterable[Decision],
    human: HitlDecision | None,
    *,
    max_fixes: int,
) -> tuple[list[FixCandidate], list[str]]:
    """Hallazgos aprobados que deben arreglarse, y los saltados por tope.

    Requisitos: `APPROVED` por una persona **y** una accion de politica `fix_*`.
    Los saltados por `max_fixes` se devuelven aparte para publicarse como
    `SKIPPED` con su razon: no desaparecen en silencio.
    """
    by_id = {d.finding_id: d for d in decisions}
    approved = (
        {item.finding_id for item in human.items if item.outcome is DecisionOutcome.APPROVED}
        if human is not None
        else set()
    )

    candidates: list[FixCandidate] = []
    skipped: list[str] = []
    for finding in findings:
        finding_id = _key(finding)
        decision = by_id.get(finding_id)
        if decision is None or decision.action not in _FIXABLE:
            continue
        if finding_id not in approved:
            continue
        if len(candidates) >= max_fixes:
            skipped.append(finding_id)
            continue
        candidates.append(FixCandidate(finding=finding, decision=decision))
    return candidates, skipped
