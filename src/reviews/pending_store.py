from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol, runtime_checkable

import redis

from reviews.domain.models import ReviewRequest
from reviews.domain.pending import InMemoryPendingStore, PendingReview, PendingStatus
from reviews.redis_utils import build_redis_client

logger = logging.getLogger(__name__)

KEY_PREFIX = "reviews:pending"
# La ventana de vida del registro debe superar el TTL de la review para poder
# degradarla, pero no ser eterna: los zombies se descartan solos.
DEFAULT_TTL_S = 7 * 24 * 3600


@runtime_checkable
class PendingStore(Protocol):
    def observe(self, request: ReviewRequest, tool: str, payload: str) -> PendingStatus: ...

    def get(self, key: str) -> PendingReview | None: ...

    def sweep(self, *, now: datetime | None = None) -> list[PendingReview]: ...


class RedisPendingStore:
    """Registra, por review, que artefactos del CI han llegado.

    Si Redis no esta disponible, delega en un store en memoria para no romper el
    arranque en local; en produccion el fallo de Redis debe ser ruidoso.
    """

    def __init__(
        self,
        *,
        url: str,
        expected_tools: list[str] | None = None,
        ttl_ready_s: float = 1200.0,
        ttl_pending_s: float = 24 * 3600.0,
        key_ttl_s: int = DEFAULT_TTL_S,
        key: str = KEY_PREFIX,
    ) -> None:
        self._client = build_redis_client(url)
        self._key = key
        self._expected = expected_tools or []
        self._ttl_ready_s = ttl_ready_s
        self._ttl_pending_s = ttl_pending_s
        self._key_ttl_s = key_ttl_s
        self._fallback = InMemoryPendingStore(expected_tools=self._expected)

    def available(self) -> bool:
        try:
            return bool(self._client.ping())
        except redis.exceptions.RedisError:
            return False

    def _entry_key(self, key: str) -> str:
        return f"{self._key}:{key}"

    def observe(self, request: ReviewRequest, tool: str, payload: str) -> PendingStatus:
        if not self.available():
            return self._fallback.observe(request, tool, payload)
        entry_key = self._entry_key(request.idempotency_key)
        raw = self._client.get(entry_key)
        if raw is None:
            review = PendingReview(
                request=request,
                expected_tools=list(self._expected),
                ttl_ready_s=self._ttl_ready_s,
                ttl_pending_s=self._ttl_pending_s,
            )
        else:
            review = PendingReview.model_validate_json(raw)
        status = review.observe(tool, payload)
        self._client.set(entry_key, review.model_dump_json(), ex=self._key_ttl_s)
        return status

    def get(self, key: str) -> PendingReview | None:
        if not self.available():
            return self._fallback.get(key)
        raw = self._client.get(self._entry_key(key))
        if raw is None:
            return None
        try:
            return PendingReview.model_validate_json(raw)
        except ValueError:
            logger.error("Registro pending corrupto para %s", key)
            return None

    def sweep(self, *, now: datetime | None = None) -> list[PendingReview]:
        if not self.available():
            return self._fallback.sweep(now=now)
        resolved: list[PendingReview] = []
        for key in self._client.scan_iter(match=f"{self._key}:*"):
            raw = self._client.get(key)
            if raw is None:
                continue
            review = PendingReview.model_validate_json(raw)
            if review.resolve_timeout(now=now) is not None:
                self._client.set(key, review.model_dump_json(), ex=self._key_ttl_s)
                resolved.append(review)
        return resolved
