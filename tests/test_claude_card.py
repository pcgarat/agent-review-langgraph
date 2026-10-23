"""Tarjeta de publicación del motor claude (025 H1–H2).

Los lotes se traducen a una `HitlDecision` completa antes de encolar. Sin
`pause_kind`, la tarjeta sigue siendo Aprobar, Rechazar y Anotar.
"""

from __future__ import annotations

from typing import Any, Literal

from agent_review.domain.models import (
    ApprovalRequest,
    DecisionOutcome,
    FindingAction,
    PendingDecision,
    ReviewRequest,
    Severity,
)
from agent_review.graph.build import render_pause_notice
from agent_review.paused_store import InMemoryPausedStore
from agent_review.queue import InMemoryReviewQueue
from agent_review.teams_bot.card_service import DecisionCardService
from agent_review.teams_bot.cards import (
    DECISION_ACTION,
    PUBLISH_BATCH_ACTION,
    SUBMIT_ACTION,
    PublishBatch,
    render_decision_card,
)
from agent_review.teams_bot.decisions import DecisionSubmitter, SubmitStatus
from agent_review.teams_bot.ingress import CardIngress
from agent_review.teams_bot.state import InMemoryCardStore
from agent_review.teams_bot.threads import ChannelContext, InMemoryChannelStore
from agent_review.teams_bot.transport import ChannelTransport

_SECRETO = "sk-ant-api03-no-debe-salir"


def _pending(
    finding_id: str,
    severity: Severity,
    *,
    matched_rule: str = "claude",
    policy_version: str = "claude",
) -> PendingDecision:
    return PendingDecision(
        finding_id=finding_id,
        proposed_action=FindingAction.PUBLISH,
        reason="elegir si se publica",
        matched_rule=matched_rule,
        policy_version=policy_version,
        title=f"Hallazgo {finding_id}",
        file="src/Pay.php",
        line=4,
        severity=severity,
        why_it_matters="El cobro queda a medias si el PSP falla",
    )


def _lote() -> list[PendingDecision]:
    return [
        _pending("p0", Severity.P0),
        _pending("p1", Severity.P1),
        _pending("p2", Severity.P2),
    ]


def _approval(
    pending: list[PendingDecision] | None = None,
    *,
    pause_kind: Literal["publication"] | None = "publication",
    command_hint: str = "",
) -> ApprovalRequest:
    items = _lote() if pending is None else pending
    return ApprovalRequest(
        review_key="cqm/emotivoo-v2:1:abc",
        repo="cqm/emotivoo-v2",
        pr_id=1,
        head_sha="abc",
        pending=items,
        pause_kind=pause_kind,
        command_hint=command_hint,
    )


def _pausa(
    approval: ApprovalRequest,
) -> tuple[InMemoryPausedStore, InMemoryReviewQueue, InMemoryCardStore, str]:
    request = ReviewRequest(
        repo=approval.repo,
        pr_id=approval.pr_id,
        head_sha=approval.head_sha,
        pipeline_id="7",
    )
    stored = approval.model_copy(update={"review_key": request.review_key})
    paused = InMemoryPausedStore()
    paused.pause(request, stored)
    return paused, InMemoryReviewQueue(), InMemoryCardStore(), request.review_key


def _actions(card: dict[str, Any]) -> list[dict[str, Any]]:
    return list(card["actions"])


def _texts(card: dict[str, Any]) -> str:
    chunks: list[str] = []
    for block in card["body"]:
        if block["type"] == "Container":
            chunks.extend(item.get("text", "") for item in block["items"])
        else:
            chunks.append(block.get("text", ""))
    return "\n".join(chunks)


def _titles(card: dict[str, Any]) -> list[str]:
    return [str(action.get("title", "")) for action in _actions(card)]


def test_los_tres_lotes_cubren_todos_los_pendientes() -> None:
    esperado = {
        PublishBatch.BLOCKERS: {
            "p0": DecisionOutcome.APPROVED,
            "p1": DecisionOutcome.APPROVED,
            "p2": DecisionOutcome.DROPPED,
        },
        PublishBatch.ALL: {
            "p0": DecisionOutcome.APPROVED,
            "p1": DecisionOutcome.APPROVED,
            "p2": DecisionOutcome.APPROVED,
        },
        PublishBatch.NONE: {
            "p0": DecisionOutcome.DROPPED,
            "p1": DecisionOutcome.DROPPED,
            "p2": DecisionOutcome.DROPPED,
        },
    }
    card = render_decision_card(_approval())
    lotes = [
        action
        for action in _actions(card)
        if action.get("data", {}).get("action") == PUBLISH_BATCH_ACTION
    ]
    assert [action["data"]["batch"] for action in lotes] == ["blockers", "all", "none"]

    for batch, outcomes in esperado.items():
        approval = _approval()
        paused, queue, cards, key = _pausa(approval)
        outcome = DecisionSubmitter(paused=paused, queue=queue, cards=cards).submit_batch(
            key, actor="Ana", batch=batch.value
        )
        assert outcome.status is SubmitStatus.ENQUEUED
        assert outcome.decision is not None
        assert {item.finding_id for item in outcome.decision.items} == set(outcomes)
        assert {item.finding_id: item.outcome for item in outcome.decision.items} == outcomes
        job = queue.dequeue()
        assert job is not None and job.decision is not None
        assert job.decision.items == outcome.decision.items


def test_una_marca_suelta_no_envia() -> None:
    approval = _approval()
    paused, queue, cards, key = _pausa(approval)
    stored = paused.get(key)
    assert stored is not None
    service = DecisionCardService(store=cards)
    card = service.apply_mark(
        stored.human_request,
        user_id="u1",
        finding_id="p0",
        outcome=DecisionOutcome.APPROVED,
        pause=stored.pause_id,
    )
    submit = next(action for action in _actions(card) if action["data"]["action"] == SUBMIT_ACTION)
    assert submit["isEnabled"] is False

    outcome = DecisionSubmitter(paused=paused, queue=queue, cards=cards).submit(
        key, user_id="u1", actor="Ana"
    )
    assert outcome.status is SubmitStatus.INCOMPLETE
    assert queue.dequeue() is None


def test_el_aviso_de_pausa_no_lleva_secreto() -> None:
    approval = _approval(
        pending=[
            _pending("p0", Severity.P0, matched_rule=_SECRETO, policy_version=_SECRETO),
        ],
        command_hint=f"agent_review decide x --token {_SECRETO}",
    )
    card = render_decision_card(approval)
    notice = render_pause_notice(approval)
    visible = _texts(card) + "\n" + notice + "\n" + " ".join(_titles(card))
    assert _SECRETO not in visible
    assert "Hallazgo p0" in visible
    assert "agent_review decide" not in notice


def test_sin_pause_kind_la_tarjeta_sigue_con_aprobar_rechazar_y_anotar() -> None:
    card = render_decision_card(_approval(pause_kind=None))
    titles = " ".join(_titles(card))
    assert "Aprobar" in titles
    assert "Rechazar" in titles
    assert "Anotar" in titles
    assert "Solo bloqueantes" not in titles
    assert all(
        action.get("data", {}).get("action") != PUBLISH_BATCH_ACTION for action in _actions(card)
    )
    decisions = [
        action
        for action in _actions(card)
        if action.get("data", {}).get("action") == DECISION_ACTION
    ]
    assert len(decisions) == 9, "3 verbos por cada uno de los 3 pendientes"


def test_la_tarjeta_de_publicacion_no_ofrece_rechazar_ni_anotar() -> None:
    card = render_decision_card(_approval())
    titles = " ".join(_titles(card))
    assert "Rechazar" not in titles
    assert "Anotar" not in titles
    assert "Publicar" in titles
    assert "No publicar" in titles


def test_un_lote_sobre_la_pausa_llm_no_encola() -> None:
    paused, queue, cards, key = _pausa(_approval(pause_kind=None))
    outcome = DecisionSubmitter(paused=paused, queue=queue, cards=cards).submit_batch(
        key, actor="Ana", batch=PublishBatch.ALL.value
    )
    assert outcome.status is SubmitStatus.INCOMPLETE
    assert queue.dequeue() is None


def test_una_marca_dropped_en_la_tarjeta_llm_no_se_guarda() -> None:
    paused, _queue, cards, key = _pausa(_approval(pause_kind=None))
    stored = paused.get(key)
    assert stored is not None
    service = DecisionCardService(store=cards)
    service.apply_mark(
        stored.human_request,
        user_id="u1",
        finding_id="p0",
        outcome=DecisionOutcome.DROPPED,
    )
    assert cards.marks(key, user_id="u1") == {}


class _Canal:
    async def send(self, conversation_id: str, payload: Any, *, service_url: str = "") -> str:
        del conversation_id, payload, service_url
        return "1"

    async def reply(
        self, conversation_id: str, root_message_id: str, payload: Any, *, service_url: str = ""
    ) -> str:
        del conversation_id, root_message_id, payload, service_url
        return "2"

    async def update(
        self, conversation_id: str, message_id: str, payload: Any, *, service_url: str = ""
    ) -> None:
        del conversation_id, message_id, payload, service_url


async def test_el_bot_traduce_el_lote_de_la_tarjeta_antes_de_encolar() -> None:
    approval = _approval()
    paused, queue, cards, key = _pausa(approval)
    stored = paused.get(key)
    assert stored is not None
    card = render_decision_card(stored.human_request)
    lote = next(
        action
        for action in _actions(card)
        if action["data"].get("batch") == PublishBatch.BLOCKERS.value
    )
    store = InMemoryChannelStore()
    store.remember_context(ChannelContext(conversation_id="19:canal", service_url="s"))
    ingress = CardIngress(
        paused=paused,
        cards=DecisionCardService(store=cards),
        submitter=DecisionSubmitter(paused=paused, queue=queue, cards=cards),
        transport=ChannelTransport(client=_Canal(), store=store),
    )

    update = await ingress.handle(lote["data"], user_id="u1", actor="Ana")

    assert update is not None
    assert update.notice == "Decision enviada"
    job = queue.dequeue()
    assert job is not None and job.decision is not None
    assert {item.finding_id: item.outcome for item in job.decision.items} == {
        "p0": DecisionOutcome.APPROVED,
        "p1": DecisionOutcome.APPROVED,
        "p2": DecisionOutcome.DROPPED,
    }
    assert "blockers" not in str(job.decision.model_dump())
