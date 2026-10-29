from __future__ import annotations

import pytest
import redis

from agent_review.chat_outbox import (
    InMemoryChatOutbox,
    OutboxChatAdapter,
    RedisChatOutbox,
)
from agent_review.domain.models import (
    ApprovalRequest,
    ChatNotice,
    FindingAction,
    PendingDecision,
    Severity,
)


def _approval(review_key: str = "r:1:abc") -> ApprovalRequest:
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
                matched_rule="default",
                policy_version="1",
                title="Título",
                file="a.php",
                line=3,
                severity=Severity.P1,
            )
        ],
    )


def test_notify_encola_un_aviso_de_review() -> None:
    outbox = InMemoryChatOutbox()
    OutboxChatAdapter(outbox=outbox).notify(
        repo="cqm/emotivoo-v2",
        pr_id=9,
        review_key="cqm/emotivoo-v2:9:deadbeef",
        title="Review de cqm/emotivoo-v2#9",
        body="2 hallazgos",
        link="https://pr/9",
    )

    notice = outbox.dequeue()
    assert notice is not None
    assert notice.kind == "published"
    assert notice.repo == "cqm/emotivoo-v2" and notice.pr_id == 9
    assert notice.review_key == "cqm/emotivoo-v2:9:deadbeef"
    assert notice.title == "Review de cqm/emotivoo-v2#9"
    assert notice.body == "2 hallazgos"
    assert notice.link == "https://pr/9"
    assert notice.approval is None


def test_notify_decision_pending_encola_la_approval_estructurada() -> None:
    """C12: el aviso de pausa lleva la `ApprovalRequest`, no su render en texto."""
    outbox = InMemoryChatOutbox()
    approval = _approval()
    OutboxChatAdapter(outbox=outbox).notify_decision_pending(
        repo="r",
        pr_id=1,
        review_key=approval.review_key,
        approval=approval,
        link="https://pr/1",
    )

    notice = outbox.dequeue()
    assert notice is not None
    assert notice.kind == "pause"
    assert notice.approval == approval
    # La identidad del PR viaja siempre: es con la que el canal resuelve el hilo.
    assert (notice.repo, notice.pr_id, notice.review_key) == ("r", 1, approval.review_key)
    assert notice.body == ""


def test_aviso_de_pausa_sin_approval_falla_al_construirlo() -> None:
    """Un aviso incompleto no llega a la outbox: se detecta al construirlo."""
    with pytest.raises(ValueError, match="ApprovalRequest"):
        ChatNotice(kind="pause", review_key="k", repo="r", pr_id=1)


def test_un_aviso_withdraw_no_necesita_titulo_ni_approval() -> None:
    notice = ChatNotice(kind="withdraw", review_key="k", repo="r", pr_id=1)
    assert notice.kind == "withdraw"
    assert notice.approval is None
    assert notice.title == ""


def test_aviso_de_review_sin_titulo_falla_al_construirlo() -> None:
    with pytest.raises(ValueError, match="titulo"):
        ChatNotice(kind="published", review_key="k", repo="r", pr_id=1, body="x")


def test_la_cola_en_memoria_es_fifo() -> None:
    outbox = InMemoryChatOutbox()
    adapter = OutboxChatAdapter(outbox=outbox)
    adapter.notify(repo="r", pr_id=1, review_key="k1", title="uno", body="a")
    adapter.notify(repo="r", pr_id=2, review_key="k2", title="dos", body="b")

    assert len(outbox) == 2
    assert outbox.dequeue().title == "uno"
    assert outbox.dequeue().title == "dos"
    assert outbox.dequeue() is None


def test_la_dlq_guarda_el_aviso_descartado() -> None:
    outbox = InMemoryChatOutbox()
    outbox.dead_letter(
        ChatNotice(kind="published", review_key="k", repo="r", pr_id=1, title="t", body="b"),
        reason="pausa ya vencida",
    )

    assert outbox.dead_letter_count == 1
    entry = outbox.dead_letters()[0]
    assert entry["reason"] == "pausa ya vencida"
    assert entry["kind"] == "published"


def test_redis_caido_no_tumba_el_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un Redis caído en la outbox no propaga: el respaldo real es el CLI (C9)."""
    outbox = RedisChatOutbox(url="redis://localhost:6379/0")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise redis.exceptions.ConnectionError("servidor reiniciando")

    monkeypatch.setattr(outbox._client, "rpush", _boom)
    # No lanza: el aviso se pierde y se registra.
    OutboxChatAdapter(outbox=outbox).notify(repo="r", pr_id=1, review_key="k", title="t", body="b")


def test_outbox_propaga_errores_no_transitorios(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un bug del canal (no de red) no se traga: es un fallo que hay que ver."""
    outbox = RedisChatOutbox(url="redis://localhost:6379/0")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise ValueError("payload corrupto")

    monkeypatch.setattr(outbox._client, "rpush", _boom)
    with pytest.raises(ValueError):
        OutboxChatAdapter(outbox=outbox).notify(
            repo="r", pr_id=1, review_key="k", title="t", body="b"
        )
