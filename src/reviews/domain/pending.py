from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from reviews.domain.models import ReviewRequest

logger = logging.getLogger(__name__)


class PendingStatus(StrEnum):
    """Ciclo de vida de una review a la espera de los artefactos del CI."""

    PENDING = "pending"
    READY = "ready"
    DEGRADED = "degraded"


class PendingReview(BaseModel):
    """Review en curso de recoleccion.

    No se encola hasta que estan los N artefactos que declara el perfil (barrera)
    o hasta que expira el TTL (review degradada, con lo que haya). Es el punto
    donde se rompe la carrera webhook-vs-CI.
    """

    request: ReviewRequest
    expected_tools: list[str] = Field(default_factory=list)
    received: dict[str, str] = Field(default_factory=dict)
    status: PendingStatus = PendingStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # El reloj se inyecta en `resolve_timeout` para que la espera sea testeable
    # sin dormir ni depender del reloj de pared.
    ttl_ready_s: float = 1200.0
    ttl_pending_s: float = 24 * 3600.0

    @property
    def key(self) -> str:
        return self.request.idempotency_key

    @property
    def missing(self) -> list[str]:
        return [tool for tool in self.expected_tools if tool not in self.received]

    def observe(self, tool: str, payload: str, *, now: datetime | None = None) -> PendingStatus:
        """Registra un artefacto. No retrocede estados ya resueltos."""
        now = now or datetime.now(UTC)
        if tool in self.received:
            logger.debug("Artefacto %s ya recibido para %s", tool, self.key)
            return self.status
        self.received[tool] = payload
        self.updated_at = now
        if not self.missing:
            self.status = PendingStatus.READY
        return self.status

    def is_expired(self, *, now: datetime | None = None) -> bool:
        """¿Vencio la espera? El listón es distinto si llego algo o nada."""
        if self.status is not PendingStatus.PENDING:
            return False
        age_s = ((now or datetime.now(UTC)) - self.created_at).total_seconds()
        limit = self.ttl_ready_s if self.received else self.ttl_pending_s
        return age_s >= limit

    def resolve_timeout(self, *, now: datetime | None = None) -> PendingStatus | None:
        """Degrada una espera vencida. Devuelve el estado nuevo, o None si sigue esperando."""
        now = now or datetime.now(UTC)
        if not self.is_expired(now=now):
            return None
        self.status = PendingStatus.DEGRADED
        self.updated_at = now
        return self.status


class InMemoryPendingStore:
    """Store de apoyo para tests y para correr sin Redis."""

    def __init__(self, *, expected_tools: list[str] | None = None) -> None:
        self._items: dict[str, PendingReview] = {}
        self._expected = expected_tools or []

    def observe(
        self,
        request: ReviewRequest,
        tool: str,
        payload: str,
        *,
        ttl_ready_s: float | None = None,
        ttl_pending_s: float | None = None,
    ) -> PendingStatus:
        review = self._items.get(request.idempotency_key)
        if review is None:
            review = PendingReview(request=request, expected_tools=list(self._expected))
            if ttl_ready_s is not None:
                review.ttl_ready_s = ttl_ready_s
            if ttl_pending_s is not None:
                review.ttl_pending_s = ttl_pending_s
            self._items[review.key] = review
        return review.observe(tool, payload)

    def get(self, key: str) -> PendingReview | None:
        return self._items.get(key)

    def ready(self) -> list[PendingReview]:
        return [r for r in self._items.values() if r.status is PendingStatus.READY]

    def sweep(self, *, now: datetime | None = None) -> list[PendingReview]:
        """Aplica TTLs y devuelve las reviews que quedan resueltas (degradadas)."""
        resolved: list[PendingReview] = []
        for review in self._items.values():
            if review.resolve_timeout(now=now) is not None:
                resolved.append(review)
        return resolved
