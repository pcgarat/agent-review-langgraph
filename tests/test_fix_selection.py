from __future__ import annotations

from reviews.domain.models import (
    AutofixClass,
    Decision,
    DecisionOutcome,
    Finding,
    FindingAction,
    FindingDecision,
    HitlDecision,
    Owner,
    Severity,
)
from reviews.fix.selection import select_candidates


def _finding(fid: str, *, line: int = 1) -> Finding:
    return Finding(
        id=fid,
        title=f"Titulo {fid}",
        severity=Severity.P2,
        file="src/Foo.php",
        line=line,
        why_it_matters="Rompe",
        autofix_class=AutofixClass.GATED_AUTO,
        owner=Owner.HUMAN,
        requires_verification=True,
        confidence=75,
        evidence=["src/Foo.php:1 x"],
    )


def _decision(fid: str, action: FindingAction) -> Decision:
    return Decision(
        finding_id=fid, action=action, reason="r", matched_rule="rule:0", policy_version="1"
    )


def _human(*pairs: tuple[str, DecisionOutcome]) -> HitlDecision:
    return HitlDecision(
        review_key="k",
        actor="alice",
        items=[FindingDecision(finding_id=fid, outcome=outcome) for fid, outcome in pairs],
    )


def test_solo_los_aprobados_con_accion_fix_entran() -> None:
    findings = [_finding("f1"), _finding("f2"), _finding("f3")]
    decisions = [
        _decision("f1", FindingAction.FIX_WITH_APPROVAL),
        _decision("f2", FindingAction.ESCALATE),
        _decision("f3", FindingAction.REPORT),
    ]
    human = _human(
        ("f1", DecisionOutcome.APPROVED),
        ("f2", DecisionOutcome.APPROVED),  # aprobar un escalate NO genera fix
        ("f3", DecisionOutcome.APPROVED),
    )
    candidates, skipped = select_candidates(findings, decisions, human, max_fixes=3)
    assert [c.finding.id for c in candidates] == ["f1"]
    assert skipped == []


def test_no_aprobado_no_genera_fix() -> None:
    findings = [_finding("f1")]
    decisions = [_decision("f1", FindingAction.FIX_WITH_APPROVAL)]
    human = _human(("f1", DecisionOutcome.REJECTED))
    candidates, _ = select_candidates(findings, decisions, human, max_fixes=3)
    assert candidates == []


def test_sin_decision_humana_no_hay_fix() -> None:
    findings = [_finding("f1")]
    decisions = [_decision("f1", FindingAction.FIX_AUTO)]
    candidates, _ = select_candidates(findings, decisions, None, max_fixes=3)
    assert candidates == []


def test_tope_de_fixes_marca_los_excedentes_como_saltados() -> None:
    findings = [_finding("f1"), _finding("f2"), _finding("f3")]
    decisions = [_decision(fid, FindingAction.FIX_WITH_APPROVAL) for fid in ("f1", "f2", "f3")]
    human = _human(
        ("f1", DecisionOutcome.APPROVED),
        ("f2", DecisionOutcome.APPROVED),
        ("f3", DecisionOutcome.APPROVED),
    )
    candidates, skipped = select_candidates(findings, decisions, human, max_fixes=2)
    assert [c.finding.id for c in candidates] == ["f1", "f2"]
    assert skipped == ["f3"]
