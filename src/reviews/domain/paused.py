from __future__ import annotations

import logging
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from reviews.domain.models import ApprovalRequest, ReviewRequest

logger = logging.getLogger(__name__)

# Se espera una decision humana. Pasado el TTL, la review se reanuda con
# `expired` (se publica "sin decision"), nunca se deja colgada para siempre.
DEFAULT_DECISION_TTL_S = 7 * 24 * 3600.0


class PausedReview(BaseModel):
    """Review pausada esperando una decision humana.

    Espejo de `PendingReview`: el estado durable vive en el checkpoint de
    Postgres; esto es el indice que descubren la API y el CLI.
    """

    review_key: str
    request: ReviewRequest
    approval: ApprovalRequest
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ttl_s: float = DEFAULT_DECISION_TTL_S

    @property
    def key(self) -> str:
        return self.review_key

    def is_expired(self, *, now: datetime | None = None) -> bool:
        age_s = ((now or datetime.now(UTC)) - self.created_at).total_seconds()
        return age_s >= self.ttl_s
