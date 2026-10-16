"""Comandos del panel (iniciativa 019, OP29-OP32, W6).

El panel es CQRS-lite: **encola** comandos y nunca muta el estado del grafo (W3).
Toda la semantica de negocio vive aqui, en un puerto pequeno (`ReviewCommands`), de
modo que los endpoints solo validan la sesion y traducen el resultado.

Las tres operaciones son distintas a proposito (spec §5.5):

- **Cancelar**: una review que no ha publicado se aborta limpiamente; una que **ya
  publicado** solo puede cancelar lo pendiente (los comentarios del PR se quedan).
  Para lo que corre o espera en cola basta la marca `reviews:cancel:<key>`; para una
  review **pausada** se encola el `request` marcado, porque no hay ningun nodo que
  mire la marca. Una pausa de **fix** no lleva marca: su cancelacion es una
  reanudacion normal (la wave como no verificable) y la marca la abortaria.
- **Reintentar**: retira la marca (si la review se cancelo) y reencola el `request`
  de un descarte de la DLQ con `attempts=0`.
- **Reanudar**: reencola la reanudacion de una pausa humana. Reanudar **es** decidir:
  una pausa humana espera a una persona, asi que reanudar sin decidir equivale a
  resolver los pendientes como `expired` (lo que ya hace el TTL, pero a peticion).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from agent_review.domain.models import (
    DecisionOutcome,
    FindingDecision,
    FixResolution,
    FixResolutions,
    HitlDecision,
    ReviewRequest,
)
from agent_review.domain.paused import PausedReview, PauseKind
from agent_review.paused_store import PausedStore
from agent_review.queue import ReviewQueue

logger = logging.getLogger(__name__)

# Actor de auditoria cuando la sesion no identifica a nadie (dev/test).
DEFAULT_ACTOR = "panel"


class CommandError(Exception):
    """Un comando no aplica al estado actual de la review; la API responde `409`."""


class CommandEffect(StrEnum):
    """Que hizo el comando, para que la UI lo explique sin volver a leer el estado."""

    CANCELLED = "cancelled"  # aborta sin publicar (en cola, en vuelo o pausa humana)
    CANCELLED_PENDING = "cancelled_pending"  # ya habia publicado: solo lo pendiente
    RETRIED = "retried"
    RESUMED = "resumed"


class CancellationMarker(Protocol):
    """Escribe la marca de cancelacion (OP26). El worker la lee en la frontera.

    `clear` la retira al reintentar: la marca sobrevive al aborto como *tombstone*
    y, sin retirarla, el reintento se abortaria al desencolar.
    """

    def mark(self, review_key: str) -> None: ...

    def clear(self, review_key: str) -> None: ...


class ReviewCommands(Protocol):
    """Los tres comandos del panel, ya resueltos contra el estado (W6)."""

    def cancel(self, review_key: str, *, actor: str) -> CommandEffect: ...

    def retry(self, review_key: str, *, actor: str) -> CommandEffect: ...

    def resume(self, review_key: str, *, actor: str) -> CommandEffect: ...


class CommandService:
    """Semantica de los comandos sobre la cola, el inbox y la marca (OP29-OP32)."""

    def __init__(
        self,
        *,
        queue: ReviewQueue,
        paused: PausedStore,
        cancellation: CancellationMarker,
    ) -> None:
        self._queue = queue
        self._paused = paused
        self._cancellation = cancellation

    def cancel(self, review_key: str, *, actor: str) -> CommandEffect:
        """Cancela una review, con la semantica por estado de la spec §5.4.

        Una pausa de fix es **posterior a publicar**: cancelarla es «cancelar lo
        pendiente» (el fix queda no verificable), nunca deshacer lo publicado. Una
        pausa humana es previa a publicar: cancelar es limpio y no deja comentarios.

        La pausa de fix **no** se marca: el worker abortaria con la marca el propio
        job de cancelacion (borrando el hilo) y la reanudacion tardia del fix-worker
        re-ejecutaria la review desde cero. Sin marca, el job resuelve la wave y el
        hilo queda completado; la reanudacion real que llegue despues es un no-op.
        """
        paused = self._paused.get(review_key)
        if paused is not None and paused.kind is PauseKind.FIX:
            self._queue.enqueue(_fix_cancellation(paused))
            logger.info("Cancelado lo pendiente de %s (actor=%s)", review_key, actor)
            return CommandEffect.CANCELLED_PENDING
        self._cancellation.mark(review_key)
        if paused is None:
            # En cola o en vuelo: la marca basta, la frontera de nodo aborta.
            logger.info("Cancelacion marcada para %s (actor=%s)", review_key, actor)
            return CommandEffect.CANCELLED
        self._queue.enqueue(paused.request.model_copy(update={"cancelled": True}))
        logger.info("Cancelada la review pausada %s (actor=%s)", review_key, actor)
        return CommandEffect.CANCELLED

    def retry(self, review_key: str, *, actor: str) -> CommandEffect:
        """Reencola un descarte de la DLQ con `attempts=0` (spec §5.5).

        Reintentar es la orden explicita de que la review vuelva a correr, asi que
        retira antes una marca de cancelacion que pudiera quedar: si no, el worker
        abortaria el reintento al desencolarlo y el panel habria dicho `RETRIED`.
        """
        entry = _find_dead(self._queue, review_key)
        if entry is None:
            raise CommandError(f"{review_key} no esta en la DLQ")
        request = ReviewRequest.model_validate(entry["request"])
        self._cancellation.clear(review_key)
        self._queue.enqueue(request.model_copy(update={"attempts": 0, "cancelled": False}))
        logger.info("Reintento encolado para %s (actor=%s)", review_key, actor)
        return CommandEffect.RETRIED

    def resume(self, review_key: str, *, actor: str) -> CommandEffect:
        """Reencola la reanudacion de una pausa humana (spec §5.5).

        No inventa aprobaciones: resuelve los pendientes como `expired`, que es el
        mismo camino que sigue el TTL cuando nadie decide, y deja el `actor` para que
        la reanudacion quede auditada como una accion del panel.
        """
        paused = self._paused.get(review_key)
        if paused is None:
            raise CommandError(f"{review_key} no esta pausada")
        if paused.kind is not PauseKind.HUMAN:
            raise CommandError(f"{review_key} no es una pausa humana")
        decision = HitlDecision(
            review_key=review_key,
            actor=actor,
            items=[
                FindingDecision(
                    finding_id=item.finding_id,
                    outcome=DecisionOutcome.EXPIRED,
                    note="reanudada desde el panel sin decision explicita",
                )
                for item in paused.human_request.pending
            ],
        )
        self._queue.enqueue(paused.request.model_copy(update={"decision": decision}))
        logger.info("Reanudacion encolada para %s (actor=%s)", review_key, actor)
        return CommandEffect.RESUMED


def _fix_cancellation(paused: PausedReview) -> ReviewRequest:
    """Reanudacion de una wave cancelada: todos los jobs como no verificables.

    Es lo que ya hace el TTL de la wave (`FIX_VERIFY_TTL_S`), pero a peticion. No se
    mata el contenedor del PR (riesgo R3): se descarta la reanudacion y el runner
    acaba solo.
    """
    stamp = datetime.now(UTC).isoformat()
    items = [
        FixResolution(
            run_key=job.run_key,
            outcome="error",
            error=f"wave cancelada desde el panel a las {stamp}",
        )
        for job in paused.fix_request.jobs
    ]
    return paused.request.model_copy(update={"fix_resolution": FixResolutions(items=items)})


def _find_dead(queue: ReviewQueue, review_key: str) -> dict[str, object] | None:
    for entry in queue.dead_letters():
        request = entry.get("request")
        if str(entry.get("review_key", "")) == review_key and isinstance(request, dict):
            return entry
    return None


def build_command_service(
    *, queue: ReviewQueue, paused: PausedStore, cancellation: CancellationMarker
) -> CommandService:
    """Raiz de composicion del servicio de comandos del panel (OP29-OP32)."""
    return CommandService(queue=queue, paused=paused, cancellation=cancellation)
