"""Ingress del Bot Framework: acciones de la tarjeta -> marcas y decision (T7, 013).

Es la logica **pura** del ingress: recibe el `data` de la accion (ya validada su
procedencia por el SDK, que verifica el JWT del Bot Framework) y produce la
tarjeta que Teams repinta para quien pulso. No importa `microsoft_teams`: el
wiring del SDK vive en `sdk_handlers.py`, asi esta parte es testeable sin Teams.

Una accion llega de dos formas y ambas se despachan por `data.action` (T0):

- `Action.Execute` -> `adaptiveCard/action` (clientes modernos).
- `Action.Submit` -> un mensaje con el `data` en `value` (fallback de clientes
  antiguos, que el SDK entrega por `on_message`).

Tres acciones: marcar un hallazgo (`teams.decision`), enviar la decision
(`teams.submit`) y, en una pausa `publication`, un lote (`teams.publish_batch`).
Enviar y el lote reutilizan el `DecisionSubmitter`: el bot solo encola, nunca
reanuda (C4). El grafo no ve el botón.

La accion **no puede tumbar el endpoint**: un fallo de infraestructura (Redis caido
al marcar o al encolar) se convierte en un aviso para que la persona reintente, no
en un `500`. Un `500` haria que Teams reintentara la accion a ciegas.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from agent_review.domain.models import ApprovalRequest, DecisionOutcome
from agent_review.domain.paused import PausedReview, PauseKind
from agent_review.paused_store import PausedStore
from agent_review.teams_bot.card_service import DecisionCardService
from agent_review.teams_bot.cards import (
    DECISION_ACTION,
    PUBLISH_BATCH_ACTION,
    SUBMIT_ACTION,
    render_decision_card,
)
from agent_review.teams_bot.decisions import DecisionSubmitter, SubmitOutcome, SubmitStatus
from agent_review.teams_bot.transport import ChannelTransport

logger = logging.getLogger(__name__)

_GONE = "La review ya no esta pendiente de decision; la tarjeta quedo obsoleta."
_RETRY = "Vuelve a intentarlo en unos segundos."


@dataclass(frozen=True)
class CardUpdate:
    """Resultado de una accion: la tarjeta a repintar y el texto para quien pulso.

    `card` es lo que Teams muestra tras la accion (para quien pulso: C8). Es `None`
    cuando no hay tarjeta que repintar (la review dejo de estar pausada o el estado
    no se pudo leer): entonces solo hay `notice`. `notice` es el texto breve de
    confirmacion o de aviso; vacio si no hay nada que decir.
    """

    card: dict[str, Any] | None = None
    notice: str = ""


class CardIngress:
    """Traduce las acciones de la tarjeta a marcas y a la decision encolada."""

    def __init__(
        self,
        *,
        paused: PausedStore,
        cards: DecisionCardService,
        submitter: DecisionSubmitter,
        transport: ChannelTransport,
    ) -> None:
        self._paused = paused
        self._cards = cards
        self._submitter = submitter
        self._transport = transport

    async def handle(self, data: dict[str, Any], *, user_id: str, actor: str) -> CardUpdate | None:
        """Despacha una accion de tarjeta. `None` si el `data.action` es ajeno.

        `user_id` identifica al que pulsa en el estado por usuario (C8) y `actor`
        es quien queda auditado en la decision (C5). El cuerpo va bajo una unica
        guarda: es la frontera con el canal, y una accion no debe responder un error
        de servidor a Teams.
        """
        action = str(data.get("action", ""))
        review_key = str(data.get("review_key", ""))
        if not review_key:
            logger.warning("Accion de tarjeta sin review_key: %s", data)
            return None
        try:
            if action == DECISION_ACTION:
                return self._mark(review_key, data, user_id=user_id)
            if action == SUBMIT_ACTION:
                return await self._submit(review_key, user_id=user_id, actor=actor)
            if action == PUBLISH_BATCH_ACTION:
                return await self._submit_batch(review_key, data, user_id=user_id, actor=actor)
        except Exception as exc:  # noqa: BLE001 - el canal nunca recibe un 500
            logger.warning("Fallo al aplicar la accion %s de %s: %s", action, review_key, exc)
            return CardUpdate(notice=f"No se pudo registrar la accion. {_RETRY}")
        return None

    def _mark(self, review_key: str, data: dict[str, Any], *, user_id: str) -> CardUpdate:
        paused = self._human_pause(review_key)
        if paused is None:
            return CardUpdate(notice=_GONE)
        approval = paused.human_request
        outcome = _outcome(data.get("outcome"))
        if outcome is None:
            logger.warning("Marca sin outcome valido para %s: %s", review_key, data)
            return CardUpdate(notice="Esa marca no es valida.")
        card = self._cards.apply_mark(
            approval,
            user_id=user_id,
            finding_id=str(data.get("finding_id", "")),
            outcome=outcome,
            pause=paused.pause_id,
        )
        return CardUpdate(card=card)

    async def _submit(self, review_key: str, *, user_id: str, actor: str) -> CardUpdate:
        paused = self._human_pause(review_key)
        if paused is None:
            return CardUpdate(notice=_GONE)
        outcome = self._submitter.submit(review_key, user_id=user_id, actor=actor)
        return await self._respond(
            review_key, paused.human_request, outcome, user_id=user_id, pause=paused.pause_id
        )

    async def _submit_batch(
        self, review_key: str, data: dict[str, Any], *, user_id: str, actor: str
    ) -> CardUpdate:
        paused = self._human_pause(review_key)
        if paused is None:
            return CardUpdate(notice=_GONE)
        outcome = self._submitter.submit_batch(
            review_key, actor=actor, batch=str(data.get("batch", ""))
        )
        return await self._respond(
            review_key, paused.human_request, outcome, user_id=user_id, pause=paused.pause_id
        )

    async def _respond(
        self,
        review_key: str,
        approval: ApprovalRequest,
        outcome: SubmitOutcome,
        *,
        user_id: str,
        pause: str,
    ) -> CardUpdate:
        if outcome.status is SubmitStatus.ENQUEUED:
            # La pregunta desaparece del canal. Es cosmetico: la decision ya esta
            # encolada, asi que un fallo de Teams no la pierde.
            await self._drop_card(review_key)
            return CardUpdate(notice="Decision enviada")
        if outcome.status is SubmitStatus.ALREADY_DECIDED:
            await self._drop_card(review_key)
            return CardUpdate(notice=f"La decision ya la tomo {outcome.actor}")
        if outcome.status is SubmitStatus.INCOMPLETE:
            # Vuelve la tarjeta con lo marcado y el aviso de que falta algo.
            return CardUpdate(
                card=self._cards.render(approval, user_id=user_id, pause=pause),
                notice=outcome.reason or "Faltan hallazgos por decidir",
            )
        return CardUpdate(
            card=render_decision_card(approval),
            notice=outcome.reason or "La review ya no esta pausada",
        )

    async def _drop_card(self, review_key: str) -> None:
        """Borra el mensaje de la pregunta. Si Teams falla, el barrido lo reintenta."""
        try:
            await self._transport.delete_card(review_key)
        except Exception as exc:  # noqa: BLE001 - borrar no puede tumbar la decision
            logger.warning("No se pudo retirar la tarjeta de %s: %s", review_key, exc)

    def _human_pause(self, review_key: str) -> PausedReview | None:
        """La pausa humana vigente de `review_key`, o `None` si no la hay.

        El bot lee el inbox para **aplicar** la accion (spec §5.2), no para pintar
        la tarjeta (esa la trae el aviso: C12). Un fallo de Redis no se confunde con
        "no esta pausada": sube a la guarda de `handle`, que avisa del reintento.
        """
        paused = self._paused.get(review_key)
        if paused is None or paused.kind is not PauseKind.HUMAN:
            logger.info("Accion sobre %s ignorada: no es una pausa humana vigente", review_key)
            return None
        return paused


def _outcome(raw: object) -> DecisionOutcome | None:
    """`outcome` del `data`, ignorando `expired` (no es una decision humana)."""
    try:
        outcome = DecisionOutcome(str(raw))
    except ValueError:
        return None
    return None if outcome is DecisionOutcome.EXPIRED else outcome
