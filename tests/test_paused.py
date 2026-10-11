from __future__ import annotations

from datetime import UTC, datetime, timedelta

from reviews.domain.models import (
    ApprovalRequest,
    FindingAction,
    PendingDecision,
    ReviewRequest,
    Severity,
)
from reviews.paused_store import InMemoryPausedStore


def _approval(review_key: str) -> ApprovalRequest:
    return ApprovalRequest(
        review_key=review_key,
        repo="r",
        pr_id=1,
        head_sha="abc",
        pending=[
            PendingDecision(
                finding_id="f1",
                proposed_action=FindingAction.ESCALATE,
                reason="Tema protegido",
                matched_rule="rule:0",
                policy_version="1",
                title="T",
                file="a.php",
                line=3,
                severity=Severity.P1,
            )
        ],
        command_hint="reviews decide r:1:abc ...",
    )


def _request(pipeline: str = "7") -> ReviewRequest:
    return ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id=pipeline)


def test_pause_se_descubre_y_se_borra_al_reanudar() -> None:
    store = InMemoryPausedStore()
    request = _request()
    store.pause(request, _approval(request.review_key))

    paused = store.get(request.review_key)
    assert paused is not None
    assert paused.approval.pending[0].finding_id == "f1"
    assert [r.key for r in store.all()] == [request.review_key]

    store.resume(request.review_key)
    assert store.get(request.review_key) is None
    assert store.all() == []


def test_sweep_solo_vencidas() -> None:
    store = InMemoryPausedStore(ttl_s=60.0)
    request = _request()
    store.pause(request, _approval(request.review_key))

    # Recien creada: no vencida.
    assert store.sweep() == []

    # Un reloj avanzado mas alla del TTL la vence.
    future = datetime.now(UTC) + timedelta(seconds=120)
    vencidas = store.sweep(now=future)
    assert [r.key for r in vencidas] == [request.review_key]
    # El sweep no borra: la reanudacion es quien limpia.
    assert store.get(request.review_key) is not None
