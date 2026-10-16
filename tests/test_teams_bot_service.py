"""T5 (iniciativa 013): servicio `teams-bot` — consumo de la outbox y despacho.

Se prueba con outbox, transporte y store falsos: lo que importa es la politica de
drenado (C13) y el despacho por `kind`, no el SDK. La outbox es la real
(`InMemoryChatOutbox`), para afirmar que un aviso descartado acaba en su DLQ.
"""

from __future__ import annotations

from agent_review.chat_outbox import InMemoryChatOutbox
from agent_review.domain.models import (
    ApprovalRequest,
    ChatNotice,
    FindingAction,
    PendingDecision,
    ReviewRequest,
    Severity,
)
from agent_review.domain.paused import PauseKind
from agent_review.paused_store import InMemoryPausedStore
from agent_review.teams_bot.service import DispatchResult, TeamsBotService
from agent_review.teams_bot.threads import ChannelContext, InMemoryChannelStore
from agent_review.teams_bot.transport import ChannelTransport


class FakeChannel:
    """Cliente del canal en memoria: registra publicaciones y permite fallar."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sends: list[object] = []
        self.replies: list[object] = []
        self.updates: list[object] = []
        self.fail = fail
        self._next = 200

    async def send(self, conversation_id: str, payload: object, *, service_url: str = "") -> str:
        if self.fail:
            raise ConnectionError("Teams caido")
        self.sends.append(payload)
        self._next += 1
        return str(self._next)

    async def reply(
        self,
        conversation_id: str,
        root_message_id: str,
        payload: object,
        *,
        service_url: str = "",
    ) -> str:
        if self.fail:
            raise ConnectionError("Teams caido")
        self.replies.append(payload)
        self._next += 1
        return str(self._next)

    async def update(
        self, conversation_id: str, message_id: str, payload: object, *, service_url: str = ""
    ) -> None:
        if self.fail:
            raise ConnectionError("Teams caido")
        self.updates.append(payload)


def _approval(review_key: str) -> ApprovalRequest:
    return ApprovalRequest(
        review_key=review_key,
        repo="cqm/emotivoo-v2",
        pr_id=7,
        head_sha="abc",
        pending=[
            PendingDecision(
                finding_id="f1",
                proposed_action=FindingAction.ESCALATE,
                reason="Tema protegido",
                matched_rule="rule:3",
                policy_version="1",
                title="Escritura sin comprobar",
                file="a.php",
                line=3,
                severity=Severity.P1,
            )
        ],
    )


def _services(
    *,
    paused: InMemoryPausedStore | None = None,
    channel: FakeChannel | None = None,
    store: InMemoryChannelStore | None = None,
    with_channel: bool = True,
) -> tuple[TeamsBotService, InMemoryChatOutbox, FakeChannel, InMemoryChannelStore]:
    outbox = InMemoryChatOutbox()
    client = channel or FakeChannel()
    channel_store = store or InMemoryChannelStore()
    if with_channel and channel_store.context() is None:
        channel_store.remember_context(ChannelContext(conversation_id="c", service_url="s"))
    service = TeamsBotService(
        outbox=outbox,
        transport=ChannelTransport(client=client, store=channel_store),
        paused=paused or InMemoryPausedStore(),
        poll_s=0.01,
    )
    return service, outbox, client, channel_store


def _pause_notice(review_key: str) -> ChatNotice:
    return ChatNotice(
        kind="pause",
        review_key=review_key,
        repo="cqm/emotivoo-v2",
        pr_id=7,
        approval=_approval(review_key),
    )


async def test_un_aviso_publicado_deja_el_texto_en_el_hilo() -> None:
    service, outbox, client, _ = _services()
    outbox.enqueue(
        ChatNotice(
            kind="published",
            review_key="k",
            repo="cqm/emotivoo-v2",
            pr_id=7,
            title="Review de cqm/emotivoo-v2#7",
            body="2 hallazgos",
            link="https://pr/7",
        )
    )

    processed = await service.drain()

    assert processed == 1
    assert len(client.sends) == 1, "el primer aviso abre el hilo raiz"
    text = client.sends[0]
    assert "Review de cqm/emotivoo-v2#7" in text
    assert "[Abrir el PR](https://pr/7)" in text


async def test_un_aviso_de_pausa_publica_la_tarjeta_de_decision() -> None:
    paused = InMemoryPausedStore()
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=7, head_sha="abc")
    paused.pause(request, _approval(request.review_key))
    service, outbox, client, _ = _services(paused=paused)
    outbox.enqueue(_pause_notice(request.review_key))

    await service.drain()

    assert len(client.sends) == 1
    card = client.sends[0]
    assert card["type"] == "AdaptiveCard"
    actions = [action.get("data", {}).get("action") for action in card["actions"]]
    assert "teams.submit" in actions, "la tarjeta trae el Enviar (C3)"


async def test_raiz_reply_y_edicion_en_el_mismo_hilo() -> None:
    """C6/C7: el primer aviso abre el hilo; el resto va a reply; la tarjeta se edita."""
    paused = InMemoryPausedStore()
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=7, head_sha="abc")
    paused.pause(request, _approval(request.review_key))
    service, outbox, client, _ = _services(paused=paused)

    # 1) Un aviso de review abre el hilo raiz.
    outbox.enqueue(
        ChatNotice(
            kind="published", review_key="k", repo="cqm/emotivoo-v2", pr_id=7, title="t", body="b"
        )
    )
    await service.drain()
    assert len(client.sends) == 1 and client.replies == []

    # 2) La tarjeta de pausa llega como reply del mismo hilo.
    outbox.enqueue(_pause_notice(request.review_key))
    await service.drain()
    assert len(client.sends) == 1, "no se abre otro hilo"
    assert len(client.replies) == 1

    # 3) Una reentrega de la misma tarjeta edita, no duplica.
    outbox.enqueue(_pause_notice(request.review_key))
    await service.drain()
    assert len(client.replies) == 1, "no se publica otra tarjeta"
    assert len(client.updates) == 1


async def test_un_aviso_de_pausa_vencida_se_descarta_sin_publicar() -> None:
    """C13: la pausa ya no esta en el inbox (expiro o se decidio por el CLI)."""
    service, outbox, client, _ = _services(paused=InMemoryPausedStore())
    outbox.enqueue(_pause_notice("cqm/emotivoo-v2:7:abc"))

    processed = await service.drain()

    assert processed == 1, "el aviso se proceso (descartado), no se reintenta"
    assert client.sends == []
    assert outbox.dead_letter_count == 1
    assert "ya no esta vigente" in str(outbox.dead_letters()[0]["reason"])


async def test_una_pausa_de_fix_no_genera_tarjeta_humana() -> None:
    from agent_review.domain.models import FixWaitRequest

    paused = InMemoryPausedStore()
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc")
    paused.pause(
        request,
        FixWaitRequest(review_key=request.review_key, repo="r", pr_id=1, head_sha="abc"),
        kind=PauseKind.FIX,
    )
    service, outbox, client, _ = _services(paused=paused)
    outbox.enqueue(_pause_notice(request.review_key))

    await service.drain()

    assert client.sends == []
    assert outbox.dead_letter_count == 1, "se descarta: no admite decision humana"
    assert str(outbox.dead_letters()[0]["reason"]).startswith("La pausa ya no esta vigente")


async def test_sin_canal_capturado_el_aviso_vuelve_a_la_cola() -> None:
    """C14: sin canal no hay destino; el aviso se reintenta al arrancar."""
    service, outbox, client, _ = _services(with_channel=False)
    notice = ChatNotice(kind="published", review_key="k", repo="r", pr_id=1, title="t", body="b")
    outbox.enqueue(notice)

    processed = await service.drain()

    assert processed == 0
    assert client.sends == []
    assert len(outbox) == 1, "el aviso sigue en la cola"
    assert outbox.dead_letter_count == 0, "no es un fallo, es falta de destino"


async def test_un_aviso_con_el_bot_caido_se_publica_al_arrancar() -> None:
    """Criterio 8: la outbox conserva el aviso y el bot lo publica al consumir."""
    service, outbox, client, _ = _services()
    outbox.enqueue(
        ChatNotice(kind="published", review_key="k", repo="r", pr_id=1, title="t", body="b")
    )

    # El bot arranca (drena) y publica lo que quedo.
    await service.drain()

    assert len(client.sends) == 1


async def test_un_fallo_de_publicacion_no_pierde_el_aviso() -> None:
    """Teams caido: el aviso va a la DLQ para reinyectar, no se pierde en el log."""
    service, outbox, client, _ = _services(channel=FakeChannel(fail=True))
    outbox.enqueue(
        ChatNotice(kind="published", review_key="k", repo="r", pr_id=1, title="t", body="b")
    )

    await service.drain()

    assert outbox.dead_letter_count == 1
    assert "ConnectionError" in str(outbox.dead_letters()[0]["reason"])


async def test_dispatch_devuelve_el_resultado() -> None:
    service, _, _, _ = _services(with_channel=False)
    notice = ChatNotice(kind="published", review_key="k", repo="r", pr_id=1, title="t", body="b")

    assert await service.dispatch(notice) is DispatchResult.DEFERRED
