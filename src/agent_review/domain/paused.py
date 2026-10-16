from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field, field_validator

from agent_review.domain.models import ApprovalRequest, FixWaitRequest, ReviewRequest

logger = logging.getLogger(__name__)

# Se espera una decision humana. Pasado el TTL, la review se reanuda con
# `expired` (se publica "sin decision"), nunca se deja colgada para siempre.
DEFAULT_DECISION_TTL_S = 7 * 24 * 3600.0


class PauseKind(StrEnum):
    """Que espera una review pausada: una decision humana o una verificacion."""

    HUMAN = "human"
    FIX = "fix"


class PausedReview(BaseModel):
    """Review pausada. Hoy espera una decision humana o una wave de fix (E7).

    Espejo de `PendingReview`: el estado durable vive en el checkpoint de
    Postgres; esto es el indice que descubren la API y el CLI.

    `kind` desambigua la carga y es tambien el discriminante de `approval`.
    """

    review_key: str
    request: ReviewRequest
    kind: PauseKind = PauseKind.HUMAN
    # Se elige por `kind`, no por forma: un `ApprovalRequest` (pending) o un
    # `FixWaitRequest` (jobs). El default `human` hace legibles las entradas
    # antiguas de Redis (Fase 2), que no traen `kind`.
    approval: Annotated[ApprovalRequest | FixWaitRequest, Field(discriminator="kind")]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ttl_s: float = DEFAULT_DECISION_TTL_S
    # Cuando el barrido ya encolo la reanudacion `expired`. Sin esta marca, una
    # reanudacion que falla siempre volvia a encolarse en cada barrido (con
    # `attempts=0`) y esquivaba la DLQ hasta que caducaba la clave de Redis.
    expired_enqueued_at: datetime | None = None
    # Veces que el barrido encolo la reanudacion `expired`. Acota el reencolado
    # de una reanudacion perdida (ver `paused_store._is_due`).
    expired_enqueues: int = 0

    @field_validator("approval", mode="before")
    @classmethod
    def _infer_kind_from_payload(cls, value: object) -> object:
        """Una entrada sin `kind` (Fase 2) con `pending` es humana.

        El discriminante de pydantic lo pone `kind` en la envoltura, pero una
        entrada de Redis escrita por la Fase 2 no lo trae. Se rellena aqui para
        que la lectura siga siendo tolerante; una pausa de fix siempre lo lleva.
        """
        if isinstance(value, dict) and "kind" not in value and "pending" in value:
            return {**value, "kind": PauseKind.HUMAN.value}
        return value

    @property
    def key(self) -> str:
        return self.review_key

    @property
    def pause_id(self) -> str:
        """Identidad de *esta* pausa: el hilo y el instante del checkpoint que pauso.

        Varias pausas comparten `review_key` (la pasada degradada y la completa, o
        el grafo que vuelve a interrumpir), asi que el sello de la tarjeta de Teams
        (C11) se ancla a esto. Un re-registro de la misma pausa conserva
        `created_at` (`reconcile_paused`), luego conserva la identidad.
        """
        return f"{self.request.thread_id}@{self.created_at.isoformat()}"

    @property
    def human_request(self) -> ApprovalRequest:
        """La carga humana. Uso: el canal de decision, que solo actua sobre `human`.

        Falla ruidoso si la pausa es de fix: quien llama debe haber distinguido por
        `kind`; un mixup silencioso reanudaria una review de fix con una decision.
        """
        if not isinstance(self.approval, ApprovalRequest):
            raise TypeError(f"La pausa {self.review_key} es de fix, no humana")
        return self.approval

    @property
    def fix_request(self) -> FixWaitRequest:
        """La wave de verificaciones. Uso: registro/encolado de una pausa de fix."""
        if not isinstance(self.approval, FixWaitRequest):
            raise TypeError(f"La pausa {self.review_key} no es de fix")
        return self.approval

    def is_expired(self, *, now: datetime | None = None) -> bool:
        age_s = ((now or datetime.now(UTC)) - self.created_at).total_seconds()
        return age_s >= self.ttl_s
