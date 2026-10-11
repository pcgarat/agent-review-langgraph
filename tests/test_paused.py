from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agent_review.domain.models import (
    ApprovalRequest,
    FindingAction,
    FixWaitRequest,
    PendingDecision,
    ReviewRequest,
    Severity,
    VerifyJob,
)
from agent_review.domain.paused import PausedReview, PauseKind
from agent_review.paused_store import InMemoryPausedStore


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
        command_hint="agent_review decide r:1:abc ...",
    )


def _request(pipeline: str = "7") -> ReviewRequest:
    return ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id=pipeline)


def _fix_wait(review_key: str) -> FixWaitRequest:
    return FixWaitRequest(
        review_key=review_key,
        repo="r",
        pr_id=1,
        head_sha="abc",
        jobs=[
            VerifyJob(
                run_key="r:1:abc:7:f1:1",
                finding_id="f1",
                attempt=1,
                repo="r",
                head_sha="abc",
                patch="diff --git a/src/Foo.php b/src/Foo.php",
            )
        ],
    )


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


def test_una_pausa_humana_es_kind_human_por_defecto() -> None:
    store = InMemoryPausedStore()
    request = _request()
    store.pause(request, _approval(request.review_key))
    paused = store.get(request.review_key)
    assert paused is not None
    assert paused.kind is PauseKind.HUMAN
    assert isinstance(paused.approval, ApprovalRequest)


def test_una_entrada_legada_de_redis_sin_kind_se_lee_como_humana() -> None:
    """Compatibilidad: una pausa de Fase 2 (sin `kind`) no se rompe al deserializar."""
    request = _request()
    legacy = {
        "review_key": request.review_key,
        "request": request.model_dump(mode="json"),
        "approval": _approval(request.review_key).model_dump(mode="json"),
        "created_at": datetime.now(UTC).isoformat(),
        "ttl_s": 60.0,
    }
    paused = PausedReview.model_validate(legacy)
    assert paused.kind is PauseKind.HUMAN
    assert isinstance(paused.approval, ApprovalRequest)
    assert paused.approval.pending[0].finding_id == "f1"


def test_una_pausa_de_fix_se_distingue_de_la_humana() -> None:
    store = InMemoryPausedStore()
    request = _request()
    store.pause(request, _fix_wait(request.review_key), kind=PauseKind.FIX)
    paused = store.get(request.review_key)
    assert paused is not None
    assert paused.kind is PauseKind.FIX
    assert isinstance(paused.approval, FixWaitRequest)
    assert paused.approval.jobs[0].finding_id == "f1"


def test_el_approval_se_elige_por_kind_no_por_forma() -> None:
    """Un dict con `jobs` debe deserializar como `FixWaitRequest`; uno con
    `pending`, como `ApprovalRequest` (el `kind` es el discriminante)."""
    human = PausedReview.model_validate(
        {
            "review_key": "k",
            "request": _request().model_dump(mode="json"),
            "kind": "human",
            "approval": _approval("k").model_dump(mode="json"),
            "created_at": datetime.now(UTC).isoformat(),
            "ttl_s": 60.0,
        }
    )
    assert isinstance(human.approval, ApprovalRequest)
    fix = PausedReview.model_validate(
        {
            "review_key": "k",
            "request": _request().model_dump(mode="json"),
            "kind": "fix",
            "approval": _fix_wait("k").model_dump(mode="json"),
            "created_at": datetime.now(UTC).isoformat(),
            "ttl_s": 60.0,
        }
    )
    assert isinstance(fix.approval, FixWaitRequest)
