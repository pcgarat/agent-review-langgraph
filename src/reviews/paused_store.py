from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

import redis

from reviews.domain.models import ApprovalRequest, ReviewRequest
from reviews.domain.paused import PausedReview
from reviews.redis_utils import build_redis_client

logger = logging.getLogger(__name__)

KEY_PREFIX = "reviews:paused"
# La entrada del inbox debe sobrevivir mas que el TTL de decision para poder
# expirarla; luego se descarta sola.
DEFAULT_TTL_S = 8 * 24 * 3600


@runtime_checkable
class PausedStore(Protocol):
    def pause(
        self,
        request: ReviewRequest,
        approval: ApprovalRequest,
        *,
        created_at: datetime | None = None,
    ) -> PausedReview: ...

    def get(self, review_key: str) -> PausedReview | None: ...

    def all(self) -> list[PausedReview]: ...

    def resume(self, review_key: str) -> None: ...

    def sweep(self, *, now: datetime | None = None) -> list[PausedReview]: ...

    def mark_expired_enqueued(self, review_key: str, *, now: datetime | None = None) -> None:
        """Anota que su reanudacion `expired` ya esta en la cola (no se reencola)."""
        ...


def _is_due(review: PausedReview, now: datetime | None) -> bool:
    """Vencida y sin reanudacion `expired` encolada todavia."""
    return review.expired_enqueued_at is None and review.is_expired(now=now)


class RedisPausedStore:
    """Inbox de reviews pausadas.

    Si Redis no esta disponible, delega en un store en memoria para no romper el
    arranque en local; en produccion la caida de Redis debe ser ruidosa. Es la
    misma asimetria (aceptada) que la ingesta de Fase 1: la verdad durable es el
    checkpoint de Postgres, esto es el indice de descubrimiento.
    """

    def __init__(
        self,
        *,
        url: str,
        ttl_s: float | None = None,
        key_ttl_s: int = DEFAULT_TTL_S,
        key: str = KEY_PREFIX,
    ) -> None:
        self._client = build_redis_client(url)
        self._key = key
        self._key_ttl_s = key_ttl_s
        self._ttl_s = ttl_s
        self._fallback = InMemoryPausedStore(ttl_s=ttl_s)

    def available(self) -> bool:
        try:
            return bool(self._client.ping())
        except redis.exceptions.RedisError:
            return False

    def _entry_key(self, review_key: str) -> str:
        return f"{self._key}:{review_key}"

    def pause(
        self,
        request: ReviewRequest,
        approval: ApprovalRequest,
        *,
        created_at: datetime | None = None,
    ) -> PausedReview:
        if not self.available():
            return self._fallback.pause(request, approval, created_at=created_at)
        review = PausedReview(review_key=request.review_key, request=request, approval=approval)
        if created_at is not None:
            # Re-registro de una pausa perdida: el TTL cuenta desde la pausa real.
            review.created_at = created_at
        if self._ttl_s is not None:
            review.ttl_s = self._ttl_s
        self._client.set(self._entry_key(review.key), review.model_dump_json(), ex=self._key_ttl_s)
        return review

    def get(self, review_key: str) -> PausedReview | None:
        if not self.available():
            return self._fallback.get(review_key)
        raw = self._client.get(self._entry_key(review_key))
        if raw is None:
            return None
        try:
            return PausedReview.model_validate_json(raw)
        except ValueError:
            logger.error("Entrada de pausa corrupta para %s", review_key)
            return None

    def all(self) -> list[PausedReview]:
        if not self.available():
            return self._fallback.all()
        reviews: list[PausedReview] = []
        for key in self._client.scan_iter(match=f"{self._key}:*"):
            raw = self._client.get(key)
            if raw is None:
                continue
            try:
                reviews.append(PausedReview.model_validate_json(raw))
            except ValueError:
                logger.warning("Entrada de pausa ilegible en %s", key)
        return reviews

    def resume(self, review_key: str) -> None:
        if not self.available():
            self._fallback.resume(review_key)
            return
        self._client.delete(self._entry_key(review_key))

    def sweep(self, *, now: datetime | None = None) -> list[PausedReview]:
        """Devuelve las pausas vencidas (para reanudarlas con `expired`).

        No borra la entrada: la reanudacion la elimina al aplicarse, para que el
        redis-encolado sea idempotente si el worker reintenta. Las que ya tienen la
        reanudacion encolada (`mark_expired_enqueued`) no se devuelven: si esa
        reanudacion falla, su destino es la DLQ, no otra vuelta del barrido.
        """
        if not self.available():
            return self._fallback.sweep(now=now)
        return [review for review in self.all() if _is_due(review, now)]

    def mark_expired_enqueued(self, review_key: str, *, now: datetime | None = None) -> None:
        if not self.available():
            self._fallback.mark_expired_enqueued(review_key, now=now)
            return
        review = self.get(review_key)
        if review is None:
            return
        review.expired_enqueued_at = now or datetime.now(UTC)
        # `keepttl`: la marca no debe alargar la vida de la entrada.
        self._client.set(self._entry_key(review_key), review.model_dump_json(), keepttl=True)


class InMemoryPausedStore:
    """Store de apoyo para tests y para correr sin Redis."""

    def __init__(self, *, ttl_s: float | None = None) -> None:
        self._items: dict[str, PausedReview] = {}
        self._ttl_s = ttl_s

    def pause(
        self,
        request: ReviewRequest,
        approval: ApprovalRequest,
        *,
        created_at: datetime | None = None,
    ) -> PausedReview:
        review = PausedReview(review_key=request.review_key, request=request, approval=approval)
        if created_at is not None:
            review.created_at = created_at
        if self._ttl_s is not None:
            review.ttl_s = self._ttl_s
        self._items[review.key] = review
        return review

    def get(self, review_key: str) -> PausedReview | None:
        return self._items.get(review_key)

    def all(self) -> list[PausedReview]:
        return list(self._items.values())

    def resume(self, review_key: str) -> None:
        self._items.pop(review_key, None)

    def sweep(self, *, now: datetime | None = None) -> list[PausedReview]:
        return [review for review in self._items.values() if _is_due(review, now)]

    def mark_expired_enqueued(self, review_key: str, *, now: datetime | None = None) -> None:
        review = self._items.get(review_key)
        if review is not None:
            review.expired_enqueued_at = now or datetime.now(UTC)
