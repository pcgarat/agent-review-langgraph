"""Servicio `teams-bot`: consumo de la outbox y despacho de avisos (T5, 013).

El bot consume `reviews:notices` (`BLPOP`) y traduce cada `ChatNotice` a una
publicacion en el canal, con el transporte de T4 y el render de T2:

- `published` → aviso de texto en el hilo del PR.
- `pause` → tarjeta de decision, publicada o **editada** en el hilo del PR.

El bot **no reanuda** el grafo: solo publica (C4). La decision llega por el
ingress (T7), que reutiliza el `DecisionSubmitter` de T3.

Drenado (C13): al recibir un aviso de pausa se comprueba que la review siga
pausada; un aviso de una pausa ya vencida (el barrido del TTL la reanudo como
`expired`, o alguien decidio por el CLI) se **descarta** a la DLQ sin publicar.
Una reentrega no duplica: el transporte edita la tarjeta si ya existe y abre un
hilo nuevo solo si el mapeo se perdio.
"""

from __future__ import annotations

import asyncio
import logging
from enum import StrEnum
from typing import Any

from agent_review.chat_outbox import ChatOutbox
from agent_review.domain.models import ChatNotice
from agent_review.domain.paused import PauseKind
from agent_review.paused_store import PausedStore
from agent_review.settings import Settings
from agent_review.teams_bot.cards import render_decision_card
from agent_review.teams_bot.threads import ChannelStore
from agent_review.teams_bot.transport import ChannelTransport

logger = logging.getLogger(__name__)

# Espera del bucle cuando la outbox responde "nada" sin haber esperado (Redis caido
# o store en memoria): sin ella el bucle giraria en vacio quemando CPU.
_IDLE_SLEEP_S = 0.1


class DispatchResult(StrEnum):
    """Que ha pasado con un aviso de la outbox."""

    PUBLISHED = "published"
    # Descartado sin publicar (C13): la pausa del aviso ya no esta vigente.
    DISCARDED = "discarded"
    # Aun no se puede entregar: sin destino (C14) o sin saber si la pausa sigue
    # vigente (inbox caido). El aviso vuelve a la cabeza de la cola y el drenado
    # espera antes de reintentar.
    DEFERRED = "deferred"


class _Unknown(Exception):
    """No se pudo leer el inbox: no se sabe si la pausa del aviso sigue vigente."""


class TeamsBotService:
    """Consume la outbox y publica en el canal. No toca el grafo ni la cola de jobs."""

    def __init__(
        self,
        *,
        outbox: ChatOutbox,
        transport: ChannelTransport,
        paused: PausedStore,
        poll_s: float = 5.0,
    ) -> None:
        self._outbox = outbox
        self._transport = transport
        self._paused = paused
        self._poll_s = poll_s

    async def run_forever(self) -> None:
        """Bucle de consumo: drena la outbox y vuelve a esperar.

        La lectura es bloqueante con timeout (`BLPOP`), asi que la espera vive en
        `drain`; el `sleep` corto solo cubre el caso en que la outbox responde
        "nada" al instante (Redis caido), para no girar en vacio. Un aviso aplazado
        o un fallo esperan `poll_s` antes de reintentar: reintentar al momento solo
        repetiria lo mismo.
        """
        while True:
            try:
                processed, deferred = await self.drain()
            except Exception:  # noqa: BLE001 - el bucle no puede morir por un aviso
                logger.exception("Fallo en el drenado de la outbox; se reintenta")
                await asyncio.sleep(self._poll_s)
                continue
            if deferred:
                await asyncio.sleep(self._poll_s)
            elif processed == 0:
                await asyncio.sleep(_IDLE_SLEEP_S)

    async def drain(self) -> tuple[int, bool]:
        """Publica los avisos pendientes. Devuelve `(procesados, aplazado)`.

        Drena la cola entera para publicar al arrancar lo que quedo pendiente con el
        bot caido (criterio 8). Se detiene en el primer aviso que no se puede
        entregar todavia (`DEFERRED`): no tiene sentido seguir sacando avisos si no
        hay destino, y ese aviso ya volvio a la cabeza de la cola.

        El `BLPOP` es sincrono y bloquea hasta `poll_s`: corre en un hilo para no
        congelar el bucle de eventos que comparte con el *messaging endpoint*
        (`serve_bot`); si no, una accion de tarjeta esperaria al timeout del poll.
        """
        processed = 0
        while True:
            notice = await asyncio.to_thread(self._outbox.dequeue, timeout_s=self._poll_s)
            if notice is None:
                return processed, False
            try:
                result = await self.dispatch(notice)
            except BaseException:
                # El aviso ya salio de la cola: un fallo inesperado no debe perderlo.
                self._requeue(notice)
                raise
            if result is DispatchResult.DEFERRED:
                return processed, True
            processed += 1

    async def dispatch(self, notice: ChatNotice) -> DispatchResult:
        """Publica un aviso, lo descarta o lo devuelve a la cola.

        Un fallo de publicacion (Teams caido, payload rechazado) no se pierde: va a
        la DLQ para reinyectar, porque el canal es UI y la verdad sigue en Postgres
        y el inbox. Un aviso que aun no se puede entregar (sin canal, o sin poder
        leer el inbox) no es un fallo: vuelve a la cabeza de la cola.
        """
        if notice.kind == "pause":
            try:
                still_paused = self._still_paused(notice)
            except _Unknown as exc:
                self._outbox.requeue(notice)
                logger.warning("Aviso de %s aplazado: %s", notice.review_key, exc)
                return DispatchResult.DEFERRED
            if not still_paused:
                self._outbox.dead_letter(notice, reason="La pausa ya no esta vigente (C13)")
                logger.info(
                    "Aviso de pausa de %s descartado: la review ya no esta pausada",
                    notice.review_key,
                )
                return DispatchResult.DISCARDED
        try:
            posted = await self._publish(notice)
        except Exception as exc:  # noqa: BLE001 - Teams no tumba el bot
            reason = f"Fallo al publicar: {type(exc).__name__}: {exc}"
            self._outbox.dead_letter(notice, reason=reason)
            logger.warning("No se pudo publicar el aviso de %s: %s", notice.review_key, exc)
            return DispatchResult.DISCARDED
        if not posted:
            # No hay canal capturado (C14): vuelve a la cola, no es un fallo.
            self._outbox.requeue(notice)
            logger.info("Aviso de %s sin destino; se reintentara", notice.review_key)
            return DispatchResult.DEFERRED
        return DispatchResult.PUBLISHED

    def _requeue(self, notice: ChatNotice) -> None:
        try:
            self._outbox.requeue(notice)
        except Exception as exc:  # noqa: BLE001 - se registra para reinyectar a mano
            logger.error("Aviso de %s perdido al reencolar: %s", notice.review_key, exc)

    async def _publish(self, notice: ChatNotice) -> bool:
        if notice.kind == "pause":
            assert notice.approval is not None  # lo valida `ChatNotice`
            return await self._transport.post_card(
                notice.review_key,
                repo=notice.repo,
                pr_id=notice.pr_id,
                card=render_decision_card(notice.approval, link=notice.link),
            )
        return await self._transport.post_text(
            repo=notice.repo, pr_id=notice.pr_id, text=_text_notice(notice)
        )

    def _still_paused(self, notice: ChatNotice) -> bool:
        """C13: `True` si la pausa humana del aviso sigue vigente en el inbox.

        Un inbox que no responde no es "no esta pausada": `RedisPausedStore` cae a
        un store en memoria vacio, y tomarlo por cierto descartaria una tarjeta
        viva. Ese caso sube como `_Unknown` y el aviso se aplaza.
        """
        available = getattr(self._paused, "available", None)
        if callable(available) and not available():
            raise _Unknown("el inbox de pausas no responde")
        try:
            paused = self._paused.get(notice.review_key)
        except Exception as exc:  # noqa: BLE001 - cualquier fallo de lectura es "no se sabe"
            raise _Unknown(f"no se pudo leer el inbox: {exc}") from exc
        return paused is not None and paused.kind is PauseKind.HUMAN


def build_teams_bot(
    settings: Settings,
    *,
    client: Any,
    outbox: ChatOutbox,
    store: ChannelStore,
    paused: PausedStore,
) -> TeamsBotService:
    """Monta el servicio con el cliente del SDK (T6) y los stores reales."""
    transport = ChannelTransport(client=client, store=store)
    return TeamsBotService(
        outbox=outbox, transport=transport, paused=paused, poll_s=settings.teams_bot_poll_s
    )


def _text_notice(notice: ChatNotice) -> str:
    """Aviso de texto de una review publicada, con su enlace si lo lleva."""
    text = f"**{notice.title}**\n{notice.body}".strip()
    if notice.link:
        text += f"\n\n[Abrir el PR]({notice.link})"
    return text
