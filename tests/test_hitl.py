from __future__ import annotations

from agent_review.domain.models import (
    DecisionOutcome,
    FindingAction,
    FindingDecision,
    HitlDecision,
    ReviewRequest,
)


def test_pending_actions_flag_en_la_accion() -> None:
    # Freno (escalate) y aprobacion (fix_with_approval) piden persona; report y
    # fix_auto no.
    assert FindingAction.ESCALATE.pending is True
    assert FindingAction.FIX_WITH_APPROVAL.pending is True
    assert FindingAction.REPORT.pending is False
    assert FindingAction.FIX_AUTO.pending is False


def test_hitl_decision_se_serializa_en_el_job() -> None:
    decision = HitlDecision(
        review_key="r:1:abc:7",
        actor="alice",
        items=[FindingDecision(finding_id="f1", outcome=DecisionOutcome.APPROVED, note="ok")],
    )
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="7", decision=decision)
    # La identidad no cambia con la decision: la reanudacion reutiliza el hilo.
    assert request.thread_id == "r:1:abc:7"
    assert request.review_key == "r:1:abc:7"

    roundtrip = ReviewRequest.model_validate_json(request.model_dump_json())
    assert roundtrip.decision is not None
    assert roundtrip.decision.actor == "alice"
    assert roundtrip.decision.outcome_for("f1") is DecisionOutcome.APPROVED


def test_outcome_for_desconocido_es_none() -> None:
    decision = HitlDecision(review_key="r:1:abc", actor="a")
    assert decision.outcome_for("no-existe") is None


def test_decision_id_estable_al_reintentar() -> None:
    # Un reintento del resume reutiliza el mismo objeto: el id no cambia.
    decision = HitlDecision(review_key="r:1:abc", actor="a")
    retry = decision.model_copy(update={"review_key": "r:1:abc"})
    assert decision.decision_id == retry.decision_id
