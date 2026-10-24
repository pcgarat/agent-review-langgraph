"""Bandeja de decisiones: la misma pregunta que Teams, en el panel.

La primera respuesta sella la pausa. La segunda no encola otra reanudacion, y si
Teams esta en el camino se pide retirar la tarjeta.
"""

from __future__ import annotations

from typing import Literal

import pytest
from fastapi.testclient import TestClient

from agent_review.chat_outbox import InMemoryChatOutbox
from agent_review.domain.models import (
    ApprovalRequest,
    DecisionOutcome,
    FindingAction,
    FindingDecision,
    FixWaitRequest,
    PendingDecision,
    ReviewRequest,
    Severity,
)
from agent_review.domain.paused import PauseKind
from agent_review.paused_store import InMemoryPausedStore
from agent_review.queue import InMemoryReviewQueue
from agent_review.teams_bot.decisions import DecisionSubmitter, SubmitStatus
from agent_review.teams_bot.state import InMemoryCardStore
from agent_review.teams_bot.withdrawal import OutboxPauseWithdrawal
from agent_review.web import main as web_main
from agent_review.web.bandeja import BandejaService
from agent_review.web.commands import CommandEffect, CommandError, CommandService


def _pending(finding_id: str, *, severity: Severity = Severity.P1) -> PendingDecision:
    return PendingDecision(
        finding_id=finding_id,
        proposed_action=FindingAction.ESCALATE,
        reason="Tema protegido",
        matched_rule="rule:3",
        policy_version="1",
        title=f"Hallazgo {finding_id}",
        file="src/Pay.php",
        line=12,
        severity=severity,
        why_it_matters="importa",
        code_url=f"https://code/{finding_id}",
    )


def _approval(
    review_key: str,
    *ids: str,
    pause_kind: Literal["publication"] | None = None,
) -> ApprovalRequest:
    severities = {"f0": Severity.P0, "f2": Severity.P2}
    return ApprovalRequest(
        review_key=review_key,
        repo="cqm/emotivoo-v2",
        pr_id=7,
        head_sha="abc",
        pause_kind=pause_kind,
        pending=[_pending(item, severity=severities.get(item, Severity.P1)) for item in ids],
    )


class _Marker:
    def mark(self, review_key: str) -> None:
        del review_key

    def clear(self, review_key: str) -> None:
        del review_key


class _World:
    def __init__(
        self, *, withdraw: bool = False, pause_kind: Literal["publication"] | None = None
    ) -> None:
        self.paused = InMemoryPausedStore()
        self.queue = InMemoryReviewQueue()
        self.cards = InMemoryCardStore()
        self.outbox = InMemoryChatOutbox()
        withdrawal = OutboxPauseWithdrawal(self.outbox) if withdraw else None
        self.submitter = DecisionSubmitter(
            paused=self.paused,
            queue=self.queue,
            cards=self.cards,
            withdrawal=withdrawal,
        )
        self.bandeja = BandejaService(
            paused=self.paused,
            cards=self.cards,
            submitter=self.submitter,
            queue=self.queue,
            teams_enabled=withdraw,
        )
        self.commands = CommandService(
            queue=self.queue,
            paused=self.paused,
            cancellation=_Marker(),
            submitter=self.submitter,
        )
        self.request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=7, head_sha="abc")
        self.paused.pause(
            self.request,
            _approval(self.request.review_key, "f1", pause_kind=pause_kind),
        )
        self.key = self.request.review_key


def test_la_bandeja_lista_la_pausa_humana_y_no_la_de_fix() -> None:
    world = _World()
    fix = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=8, head_sha="def")
    world.paused.pause(
        fix,
        FixWaitRequest(review_key=fix.review_key, repo=fix.repo, pr_id=fix.pr_id, head_sha="def"),
        kind=PauseKind.FIX,
    )

    view = world.bandeja.view()

    assert view.teams_enabled is False
    assert [item.review_key for item in view.items] == [world.key]
    pending = view.items[0].pending[0]
    assert pending.finding_id == "f1"
    assert pending.code_url == "https://code/f1"
    assert pending.why_it_matters == "importa"


def test_una_pausa_ya_sellada_no_se_lista() -> None:
    world = _World()
    stored = world.paused.get(world.key)
    assert stored is not None
    world.cards.close(world.key, actor="Ana", pause=stored.pause_id)

    assert world.bandeja.view().items == []


def test_la_primera_respuesta_encola_y_la_segunda_no() -> None:
    world = _World(withdraw=True)
    first = world.bandeja.submit(
        world.key,
        actor="Ana",
        items=[FindingDecision(finding_id="f1", outcome=DecisionOutcome.APPROVED)],
    )
    second = world.bandeja.submit(
        world.key,
        actor="Luis",
        items=[FindingDecision(finding_id="f1", outcome=DecisionOutcome.REJECTED)],
    )

    assert first.status is SubmitStatus.ENQUEUED
    assert second.status is SubmitStatus.ALREADY_DECIDED
    assert second.actor == "Ana"
    job = world.queue.dequeue()
    assert job is not None and job.decision is not None
    assert job.decision.actor == "Ana"
    assert world.queue.dequeue() is None
    notice = world.outbox.dequeue()
    assert notice is not None and notice.kind == "withdraw"
    assert notice.review_key == world.key
    assert world.bandeja.view().items == []


def test_sin_teams_no_se_pide_retirar_una_tarjeta() -> None:
    world = _World(withdraw=False)
    world.bandeja.submit(
        world.key,
        actor="Ana",
        items=[FindingDecision(finding_id="f1", outcome=DecisionOutcome.NOTED)],
    )
    assert world.outbox.dequeue() is None


def test_el_formulario_del_detalle_comparte_el_sello() -> None:
    world = _World()
    effect = world.commands.decide(
        world.key,
        actor="Ana",
        items=[FindingDecision(finding_id="f1", outcome=DecisionOutcome.APPROVED)],
    )
    assert effect is CommandEffect.DECIDED
    with pytest.raises(CommandError, match="Ana"):
        world.commands.decide(
            world.key,
            actor="Luis",
            items=[FindingDecision(finding_id="f1", outcome=DecisionOutcome.REJECTED)],
        )
    assert world.bandeja.view().items == []
    assert world.queue.dequeue() is not None
    assert world.queue.dequeue() is None


def test_un_lote_de_publicacion_cubre_todos_los_pendientes() -> None:
    world = _World(pause_kind="publication")
    world.paused.resume(world.key)
    request = world.request
    world.paused.pause(request, _approval(request.review_key, "f0", "f2", pause_kind="publication"))
    outcome = world.bandeja.submit(world.key, actor="Ana", batch="blockers")

    assert outcome.status is SubmitStatus.ENQUEUED
    job = world.queue.dequeue()
    assert job is not None and job.decision is not None
    by_id = {item.finding_id: item.outcome for item in job.decision.items}
    assert by_id == {"f0": DecisionOutcome.APPROVED, "f2": DecisionOutcome.DROPPED}


def test_un_lote_sobre_una_pausa_de_arreglo_no_se_encola() -> None:
    world = _World()
    outcome = world.bandeja.submit(world.key, actor="Ana", batch="all")
    assert outcome.status is SubmitStatus.INCOMPLETE
    assert world.queue.dequeue() is None


@pytest.fixture
def client() -> TestClient:
    return TestClient(web_main.app)


def test_get_lista_y_post_responde(client: TestClient) -> None:
    world = _World()
    with client as http:
        web_main.app.state.bandeja = world.bandeja
        listed = http.get("/api/bandeja", headers={"X-Panel-Session": "ana:lector"})
        assert listed.status_code == 200
        body = listed.json()
        assert body["items"][0]["review_key"] == world.key
        answered = http.post(
            f"/api/bandeja/{world.key}/decision",
            headers={"X-Panel-Session": "ana:operador"},
            json={"items": [{"finding_id": "f1", "outcome": "approved"}]},
        )
    assert answered.status_code == 200
    assert answered.json()["effect"] == "enqueued"
    assert answered.json()["actor"] == "ana"
    again = world.bandeja.view()
    assert again.items == []


def test_el_lector_no_responde(client: TestClient) -> None:
    world = _World()
    with client as http:
        web_main.app.state.bandeja = world.bandeja
        response = http.post(
            f"/api/bandeja/{world.key}/decision",
            headers={"X-Panel-Session": "ana:lector"},
            json={"items": [{"finding_id": "f1", "outcome": "approved"}]},
        )
    assert response.status_code == 403
    assert world.queue.dequeue() is None


def test_responder_cuando_otro_ya_gano_es_409(client: TestClient) -> None:
    world = _World()
    world.bandeja.submit(
        world.key,
        actor="Ana",
        items=[FindingDecision(finding_id="f1", outcome=DecisionOutcome.APPROVED)],
    )
    with client as http:
        web_main.app.state.bandeja = world.bandeja
        response = http.post(
            f"/api/bandeja/{world.key}/decision",
            headers={"X-Panel-Session": "luis:operador"},
            json={"items": [{"finding_id": "f1", "outcome": "rejected"}]},
        )
    assert response.status_code == 409
    assert "Ana" in response.json()["detail"]


def test_un_cuerpo_sin_items_ni_lote_es_400(client: TestClient) -> None:
    world = _World()
    with client as http:
        web_main.app.state.bandeja = world.bandeja
        response = http.post(
            f"/api/bandeja/{world.key}/decision",
            headers={"X-Panel-Session": "ana:operador"},
            json={},
        )
    assert response.status_code == 400
