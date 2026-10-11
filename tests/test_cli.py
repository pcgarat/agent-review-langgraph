from __future__ import annotations

import pytest

from agent_review import cli
from agent_review.decision import DecisionError, build_decision
from agent_review.domain.models import (
    ApprovalRequest,
    DecisionOutcome,
    FindingAction,
    FindingDecision,
    PendingDecision,
    ReviewRequest,
    Severity,
)
from agent_review.paused_store import InMemoryPausedStore
from agent_review.queue import InMemoryReviewQueue

REVIEW_KEY = "cqm/emotivoo-v2:42:abc123:7"


def _request() -> ReviewRequest:
    return ReviewRequest(repo="cqm/emotivoo-v2", pr_id=42, head_sha="abc123", pipeline_id="7")


def _approval() -> ApprovalRequest:
    return ApprovalRequest(
        review_key=REVIEW_KEY,
        repo="cqm/emotivoo-v2",
        pr_id=42,
        head_sha="abc123",
        pending=[
            PendingDecision(
                finding_id="f1",
                proposed_action=FindingAction.ESCALATE,
                reason="Tema protegido",
                matched_rule="default",
                policy_version="1",
                title="T",
                file="a.php",
                line=3,
                severity=Severity.P1,
            )
        ],
        command_hint=f"agent_review decide {REVIEW_KEY} --actor alice --approve f1",
    )


@pytest.fixture
def stores(monkeypatch) -> tuple[InMemoryPausedStore, InMemoryReviewQueue]:
    paused = InMemoryPausedStore()
    queue = InMemoryReviewQueue()
    monkeypatch.setattr(cli, "build_paused", lambda settings: paused)
    monkeypatch.setattr(cli, "build_queue", lambda settings: queue)
    return paused, queue


def test_cli_list_sin_pausas(stores, capsys) -> None:
    assert cli.main(["list"]) == 0
    assert "No hay reviews pausadas" in capsys.readouterr().out


def test_cli_list_muestra_pendientes(stores, capsys) -> None:
    paused, _ = stores
    paused.pause(_request(), _approval())
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert REVIEW_KEY in out
    assert "f1" in out and "T" in out


def test_cli_decide_encola_la_reanudacion(stores, capsys) -> None:
    paused, queue = stores
    paused.pause(_request(), _approval())

    code = cli.main(["decide", REVIEW_KEY, "--actor", "alice", "--approve", "f1"])

    assert code == 0
    job = queue.dequeue()
    assert job is not None and job.decision is not None
    assert job.decision.review_key == REVIEW_KEY
    assert job.decision.actor == "alice"
    assert job.decision.outcome_for("f1") is DecisionOutcome.APPROVED
    assert "encolada" in capsys.readouterr().out


def test_cli_decide_review_inexistente(stores, capsys) -> None:
    code = cli.main(["decide", "no-existe", "--actor", "alice", "--approve", "f1"])
    assert code == 1
    assert "No hay una review pausada" in capsys.readouterr().err


def test_cli_decide_finding_ajeno_no_encola(stores, capsys) -> None:
    paused, queue = stores
    paused.pause(_request(), _approval())

    code = cli.main(["decide", REVIEW_KEY, "--actor", "alice", "--approve", "otro"])

    assert code == 2
    assert "ajeno" in capsys.readouterr().err
    assert len(queue) == 0


def test_cli_decide_nota_solo_en_rechazo(stores) -> None:
    paused, queue = stores
    paused.pause(_request(), _approval())

    code = cli.main(
        ["decide", REVIEW_KEY, "--actor", "alice", "--reject", "f1", "--note", "falso positivo"]
    )

    assert code == 0
    job = queue.dequeue()
    assert job is not None and job.decision is not None
    item = job.decision.items[0]
    assert item.outcome is DecisionOutcome.REJECTED
    assert item.note == "falso positivo"


def test_build_decision_sin_actor_falla() -> None:
    paused = InMemoryPausedStore().pause(_request(), _approval())
    with pytest.raises(DecisionError, match="actor"):
        build_decision(paused, actor="  ", items=[], reviewed_key=REVIEW_KEY)


def test_build_decision_vacia_falla() -> None:
    paused = InMemoryPausedStore().pause(_request(), _approval())
    with pytest.raises(DecisionError, match="ninguna decision"):
        build_decision(paused, actor="alice", items=[], reviewed_key=REVIEW_KEY)


def test_build_decision_rechaza_expired() -> None:
    # `expired` lo emite el barrido del TTL, no una persona.
    paused = InMemoryPausedStore().pause(_request(), _approval())
    items = [FindingDecision(finding_id="f1", outcome=DecisionOutcome.EXPIRED)]
    with pytest.raises(DecisionError, match="expired"):
        build_decision(paused, actor="alice", items=items, reviewed_key=REVIEW_KEY)


def test_build_decision_rechaza_review_key_ajena() -> None:
    paused = InMemoryPausedStore().pause(_request(), _approval())
    items = [FindingDecision(finding_id="f1", outcome=DecisionOutcome.APPROVED)]
    with pytest.raises(DecisionError, match="pausada es"):
        build_decision(paused, actor="alice", items=items, reviewed_key="otra:1:x")
