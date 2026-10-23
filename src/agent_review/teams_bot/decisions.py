"""Decision de la tarjeta de Teams: traduccion, validacion y encolado (T3, 013).

El bot no reanuda el grafo: traduce lo marcado en la tarjeta a una `HitlDecision`
con el mismo `build_decision` que usan el CLI y `POST /decision`, y la **encola**
en `reviews:jobs`. El worker hace el `Command(resume=...)` sobre el mismo hilo
([ADR-0008](../../../docs/adr/0008-hitl-por-cli-y-http-con-cola-unica.md)); el bot
solo es un cliente mas del canal de decision (C4).

La **primera decision es final** (C11) y se sella con `CardStore.close`, cuyo
`SETNX` en Redis es el mutex: dos personas pulsando Enviar a la vez no encolan dos
reanudaciones; la que no gana ve la tarjeta ya decidida. El sello es de **la pausa**
(`PausedReview.pause_id`), no del `review_key`: la pasada degradada y la completa
comparten `review_key`, y decidir una no debe cerrar la tarjeta de la siguiente.
Si el encolado falla tras ganar el sello, se suelta (`release`) para que la
persona pueda reintentar en Teams: sin la vuelta atras, un fallo transitorio de
Redis dejaria la tarjeta cerrada y la decision sin llegar a la cola.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from agent_review.decision import DecisionError, build_decision
from agent_review.domain.models import (
    ApprovalRequest,
    DecisionOutcome,
    FindingDecision,
    HitlDecision,
    Severity,
)
from agent_review.domain.paused import PausedReview, PauseKind
from agent_review.paused_store import PausedStore
from agent_review.queue import ReviewQueue
from agent_review.teams_bot.cards import PublishBatch, allowed_outcomes
from agent_review.teams_bot.state import CardStore
from agent_review.teams_bot.withdrawal import PauseWithdrawal

logger = logging.getLogger(__name__)

# Corte de «bloqueante» de la decision 7: P0 y P1 se publican; el resto, no.
_BLOCKING = frozenset({Severity.P0, Severity.P1})


class SubmitStatus(StrEnum):
    """Que ha pasado al enviar la decision de una tarjeta."""

    ENQUEUED = "enqueued"
    # Alguna marca falta o sobra: `build_decision` la rechazo (misma regla que el
    # 409 de la API). La review sigue pausada y la tarjeta, abierta.
    INCOMPLETE = "incomplete"
    # Otro pulso Enviar antes (C11): no se encola una segunda reanudacion.
    ALREADY_DECIDED = "already_decided"
    # La review ya no consta pausada: un `expired` del TTL, una decision por el
    # CLI o una reanudacion ya aplicada. La tarjeta quedo obsoleta.
    NOT_PAUSED = "not_paused"


@dataclass(frozen=True)
class SubmitOutcome:
    """Resultado de enviar una decision, para que el ingress responda en el canal."""

    status: SubmitStatus
    # Quien decidio. En `ALREADY_DECIDED` es quien gano el sello, no quien pulso.
    actor: str = ""
    reason: str = ""
    decision: HitlDecision | None = field(default=None)


class DecisionSubmitter:
    """Convierte las marcas de una tarjeta en la reanudacion de una review pausada."""

    def __init__(
        self,
        *,
        paused: PausedStore,
        queue: ReviewQueue,
        cards: CardStore,
        withdrawal: PauseWithdrawal | None = None,
    ) -> None:
        self._paused = paused
        self._queue = queue
        self._cards = cards
        self._withdrawal = withdrawal

    def submit(self, review_key: str, *, user_id: str, actor: str) -> SubmitOutcome:
        """Encola la decision de `user_id` sobre `review_key`, o explica por que no.

        `user_id` es la identidad con la que se marco en la tarjeta (el estado es
        por usuario: C8) y `actor` es quien queda auditado en la decision. El bot
        pasa el id de Entra ID y el nombre visible, respectivamente (C5).
        """
        paused, blocked = self._open(review_key)
        if blocked is not None:
            return blocked
        assert paused is not None

        approval = paused.human_request
        marks = self._cards.marks(review_key, user_id=user_id)
        allowed = allowed_outcomes(approval)
        pending = {item.finding_id for item in approval.pending}
        illegal = sorted(
            finding_id
            for finding_id, outcome in marks.items()
            if finding_id in pending and outcome not in allowed
        )
        if illegal:
            logger.warning(
                "Marca ajena a la tarjeta de %s sobre %s: %s", actor, review_key, illegal
            )
            return SubmitOutcome(
                SubmitStatus.INCOMPLETE,
                actor=actor,
                reason="Marca no valida para esta pausa: " + ", ".join(illegal),
            )
        return self._decide(
            paused,
            actor=actor,
            items=_items_from_marks(marks, approval),
            origin="Teams",
        )

    def submit_items(
        self,
        review_key: str,
        *,
        actor: str,
        items: list[FindingDecision],
        origin: str,
    ) -> SubmitOutcome:
        """Encola una decision ya armada (bandeja o formulario del panel).

        Misma validacion y mismo sello que la tarjeta: quien llega segundo no
        encola otra reanudacion.
        """
        paused, blocked = self._open(review_key)
        if blocked is not None:
            return blocked
        assert paused is not None
        return self._decide(paused, actor=actor, items=items, origin=origin)

    def submit_batch(
        self, review_key: str, *, actor: str, batch: str, origin: str = "Teams"
    ) -> SubmitOutcome:
        """Traduce un lote a una `HitlDecision` que cubre todos los pendientes.

        El grafo no ve el botón: solo el resultado por hallazgo. Un lote sobre una
        pausa que no es `publication` no se encola: en el motor `llm`, `approved`
        autoriza un arreglo.
        """
        paused, blocked = self._open(review_key)
        if blocked is not None:
            return blocked
        assert paused is not None

        approval = paused.human_request
        if approval.pause_kind != "publication":
            logger.warning("Lote de publicacion sobre una pausa que no lo admite: %s", review_key)
            return SubmitOutcome(
                SubmitStatus.INCOMPLETE,
                actor=actor,
                reason="Esta pausa no admite un lote de publicacion",
            )
        try:
            kind = PublishBatch(batch)
        except ValueError:
            return SubmitOutcome(
                SubmitStatus.INCOMPLETE,
                actor=actor,
                reason="Esa accion de lote no es valida",
            )
        return self._decide(
            paused,
            actor=actor,
            items=items_for_publish_batch(approval, kind),
            origin=origin,
        )

    def _open(self, review_key: str) -> tuple[PausedReview | None, SubmitOutcome | None]:
        paused = self._paused.get(review_key)
        if paused is None:
            logger.info("Decision de Teams para %s ignorada: no consta pausada", review_key)
            return None, SubmitOutcome(
                SubmitStatus.NOT_PAUSED, reason="La review ya no esta pausada"
            )
        if paused.kind is not PauseKind.HUMAN:
            logger.info("Decision de Teams para %s ignorada: la pausa es de fix", review_key)
            return None, SubmitOutcome(
                SubmitStatus.NOT_PAUSED,
                reason="La pausa no admite decision humana (espera verificacion de fixes)",
            )
        decided_by = self._cards.final(review_key, pause=paused.pause_id)
        if decided_by is not None:
            return None, SubmitOutcome(SubmitStatus.ALREADY_DECIDED, actor=decided_by)
        return paused, None

    def _decide(
        self,
        paused: PausedReview,
        *,
        actor: str,
        items: list[FindingDecision],
        origin: str,
    ) -> SubmitOutcome:
        review_key = paused.review_key
        try:
            decision = build_decision(
                paused,
                actor=actor,
                items=items,
                reviewed_key=review_key,
            )
        except DecisionError as exc:
            logger.warning("Decision incompleta de %s sobre %s: %s", actor, review_key, exc)
            return SubmitOutcome(SubmitStatus.INCOMPLETE, actor=actor, reason=str(exc))

        pause = paused.pause_id
        # Sello de C11 *antes* de encolar: si dos personas pulsan a la vez, la
        # primera gana el `SETNX` y solo ella encola.
        if not self._cards.close(review_key, actor=actor, pause=pause):
            return SubmitOutcome(
                SubmitStatus.ALREADY_DECIDED,
                actor=self._cards.final(review_key, pause=pause) or actor,
            )
        request = paused.request.model_copy(update={"decision": decision})
        try:
            self._queue.enqueue(request)
        except Exception:
            self._cards.release(review_key, pause=pause)
            raise
        self._ask_withdrawal(paused)
        logger.info(
            "Decision encolada desde %s para %s: %d hallazgo(s), actor=%s",
            origin,
            review_key,
            len(decision.items),
            decision.actor,
        )
        return SubmitOutcome(SubmitStatus.ENQUEUED, actor=actor, decision=decision)

    def _ask_withdrawal(self, paused: PausedReview) -> None:
        """Avisa al canal de que retire la tarjeta. Un fallo no deshace el sello.

        La decision ya esta en la cola: perder el aviso solo deja la pregunta
        visible hasta que el bot, en su barrido, vea la pausa cerrada.
        """
        if self._withdrawal is None:
            return
        try:
            self._withdrawal.withdraw(
                review_key=paused.review_key,
                repo=paused.request.repo,
                pr_id=paused.request.pr_id,
            )
        except Exception as exc:  # noqa: BLE001 - retirar la tarjeta es cosmetico
            logger.warning(
                "No se pudo pedir la retirada de la tarjeta de %s: %s", paused.review_key, exc
            )


def items_for_publish_batch(
    approval: ApprovalRequest, batch: PublishBatch
) -> list[FindingDecision]:
    """Un ítem por pendiente. El lote no deja huecos: `build_decision` lo exige."""
    items: list[FindingDecision] = []
    for pending in approval.pending:
        if batch is PublishBatch.ALL:
            outcome = DecisionOutcome.APPROVED
        elif batch is PublishBatch.NONE:
            outcome = DecisionOutcome.DROPPED
        else:
            outcome = (
                DecisionOutcome.APPROVED
                if pending.severity in _BLOCKING
                else DecisionOutcome.DROPPED
            )
        items.append(FindingDecision(finding_id=pending.finding_id, outcome=outcome))
    return items


def _items_from_marks(
    marks: Mapping[str, DecisionOutcome], approval: ApprovalRequest
) -> list[FindingDecision]:
    """Marcas de la tarjeta -> hallazgos decididos, en orden estable.

    `marks` es el hash del estado de la tarjeta (`finding_id -> outcome`); se
    ordena por id para que la decision no dependa del orden en que Redis devuelve
    el hash. Solo se traducen los **pendientes de esta pausa**: la tarjeta solo
    tiene botones para ellos, pero el hash puede arrastrar una marca de una pausa
    anterior del mismo `review_key` (el estado de UI no se limpia al re-pausar).
    Un `finding_id` que ya no es pendiente no debe entrar en la decision.
    """
    pending = {item.finding_id for item in approval.pending}
    return [
        FindingDecision(finding_id=finding_id, outcome=marks[finding_id])
        for finding_id in sorted(marks)
        if finding_id in pending
    ]
