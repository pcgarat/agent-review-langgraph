"""Ingress del Bot Framework: acciones de la tarjeta -> marcas y decision (T7, 013).

Es la logica **pura** del ingress: recibe el `data` de la accion (ya validada su
procedencia por el SDK, que verifica el JWT del Bot Framework) y produce la
tarjeta que Teams repinta para quien pulso. No importa `microsoft_teams`: el
wiring del SDK vive en `sdk_handlers.py`, asi esta parte es testeable sin Teams.

Una accion llega de dos formas y ambas se despachan por `data.action` (T0):

- `Action.Execute` -> `adaptiveCard/action` (clientes modernos).
- `Action.Submit` -> un mensaje con el `data` en `value` (fallback de clientes
  antiguos, que el SDK entrega por `on_message`).

Dos acciones: marcar un hallazgo (`teams.decision`) y enviar la decision
(`teams.submit`). Enviar reutiliza el `DecisionSubmitter` de T3: el bot solo
encola, nunca reanuda (C4).

La accion **no puede tumbar el endpoint**: un fallo de infraestructura (Redis caido
al marcar o al encolar) se convierte en un aviso para que la persona reintente, no
en un `500`. Un `500` haria que Teams reintentara la accion a ciegas.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from agent_review.domain.models import ApprovalRequest, DecisionOutcome
from agent_review.domain.paused import PauseKind
from agent_review.paused_store import PausedStore
from agent_review.teams_bot.card_service import DecisionCardService
from agent_review.teams_bot.cards import (
    DECISION_ACTION,
    SUBMIT_ACTION,
    render_decided_card,
    render_decision_card,
)
from agent_review.teams_bot.decisions import DecisionSubmitter, SubmitStatus
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
        except Exception as exc:  # noqa: BLE001 - el canal nunca recibe un 500
            logger.warning("Fallo al aplicar la accion %s de %s: %s", action, review_key, exc)
            return CardUpdate(notice=f"No se pudo registrar la accion. {_RETRY}")
        return None

    def _mark(self, review_key: str, data: dict[str, Any], *, user_id: str) -> CardUpdate:
        approval = self._human_approval(review_key)
        if approval is None:
            return CardUpdate(notice=_GONE)
        outcome = _outcome(data.get("outcome"))
        if outcome is None:
            logger.warning("Marca sin outcome valido para %s: %s", review_key, data)
            return CardUpdate(notice="Esa marca no es valida.")
        card = self._cards.apply_mark(
            approval,
            user_id=user_id,
            finding_id=str(data.get("finding_id", "")),
            outcome=outcome,
        )
        return CardUpdate(card=card)

    async def _submit(self, review_key: str, *, user_id: str, actor: str) -> CardUpdate:
        approval = self._human_approval(review_key)
        if approval is None:
            return CardUpdate(notice=_GONE)
        outcome = self._submitter.submit(review_key, user_id=user_id, actor=actor)
        if outcome.status is SubmitStatus.ENQUEUED:
            decided = render_decided_card(approval, actor=actor)
            # C7/C11: la tarjeta del canal pasa a «decidido por X». Es cosmetico:
            # la decision ya esta encolada, asi que un fallo de Teams no la pierde.
            await self._edit_card(review_key, approval, decided)
            return CardUpdate(card=decided, notice="Decision enviada")
        if outcome.status is SubmitStatus.ALREADY_DECIDED:
            return CardUpdate(
                card=render_decided_card(approval, actor=outcome.actor),
                notice=f"La decision ya la tomo {outcome.actor}",
            )
        if outcome.status is SubmitStatus.INCOMPLETE:
            # Vuelve la tarjeta con lo marcado y el aviso de que falta algo.
            return CardUpdate(
                card=self._cards.render(approval, user_id=user_id),
                notice=outcome.reason or "Faltan hallazgos por decidir",
            )
        return CardUpdate(
            card=render_decision_card(approval),
            notice=outcome.reason or "La review ya no esta pausada",
        )

    async def _edit_card(
        self, review_key: str, approval: ApprovalRequest, card: dict[str, Any]
    ) -> None:
        """Edita el mensaje de la tarjeta para el resto del canal (C7/C11).

        Cosmetico a proposito: si Teams falla, la decision ya esta encolada y no se
        pierde por no repintar el mensaje.
        """
        try:
            await self._transport.post_card(
                review_key, repo=approval.repo, pr_id=approval.pr_id, card=card
            )
        except Exception as exc:  # noqa: BLE001 - repintar no puede tumbar la decision
            logger.warning("No se pudo editar la tarjeta de %s: %s", review_key, exc)

    def _human_approval(self, review_key: str) -> ApprovalRequest | None:
        """La `ApprovalRequest` de la pausa, o `None` si no es una pausa humana.

        El bot lee el inbox para **aplicar** la accion (spec §5.2), no para pintar
        la tarjeta (esa la trae el aviso: C12). Un fallo de Redis no se confunde con
        "no esta pausada": sube a la guarda de `handle`, que avisa del reintento.
        """
        paused = self._paused.get(review_key)
        if paused is None or paused.kind is not PauseKind.HUMAN:
            logger.info("Accion sobre %s ignorada: no es una pausa humana vigente", review_key)
            return None
        return paused.human_request


def _outcome(raw: object) -> DecisionOutcome | None:
    """`outcome` del `data`, ignorando `expired` (no es una decision humana)."""
    try:
        outcome = DecisionOutcome(str(raw))
    except ValueError:
        return None
    return None if outcome is DecisionOutcome.EXPIRED else outcome
