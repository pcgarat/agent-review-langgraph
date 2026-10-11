from __future__ import annotations

import logging
from datetime import datetime
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
    def pause(self, request: ReviewRequest, approval: ApprovalRequest) -> PausedReview: ...

    def get(self, review_key: str) -> PausedReview | None: ...

    def all(self) -> list[PausedReview]: ...

    def resume(self, review_key: str) -> None: ...

    def sweep(self, *, now: datetime | None = None) -> list[PausedReview]: ...


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

    def pause(self, request: ReviewRequest, approval: ApprovalRequest) -> PausedReview:
        if not self.available():
            return self._fallback.pause(request, approval)
        review = PausedReview(review_key=request.review_key, request=request, approval=approval)
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
        redis-encolado sea idempotente si el worker reintenta.
        """
        if not self.available():
            return self._fallback.sweep(now=now)
        return [review for review in self.all() if review.is_expired(now=now)]


class InMemoryPausedStore:
    """Store de apoyo para tests y para correr sin Redis."""

    def __init__(self, *, ttl_s: float | None = None) -> None:
        self._items: dict[str, PausedReview] = {}
        self._ttl_s = ttl_s

    def pause(self, request: ReviewRequest, approval: ApprovalRequest) -> PausedReview:
        review = PausedReview(review_key=request.review_key, request=request, approval=approval)
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
        return [review for review in self._items.values() if review.is_expired(now=now)]
