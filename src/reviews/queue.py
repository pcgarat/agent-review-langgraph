from __future__ import annotations

import logging
from collections import deque
from typing import Protocol, runtime_checkable

import redis

from reviews.domain.models import ReviewRequest

logger = logging.getLogger(__name__)

QUEUE_KEY = "reviews:jobs"


@runtime_checkable
class ReviewQueue(Protocol):
    def enqueue(self, request: ReviewRequest) -> None: ...

    def dequeue(self, *, timeout_s: float = 5.0) -> ReviewRequest | None:
        """Saca el siguiente trabajo o None si no hay nada en el timeout."""
        ...


class RedisReviewQueue:
    def __init__(self, *, url: str, key: str = QUEUE_KEY) -> None:
        self._client = redis.Redis.from_url(url, decode_responses=True)
        self._key = key

    def available(self) -> bool:
        try:
            return bool(self._client.ping())
        except redis.exceptions.RedisError:
            return False

    def enqueue(self, request: ReviewRequest) -> None:
        self._client.rpush(self._key, request.model_dump_json())

    def dequeue(self, *, timeout_s: float = 5.0) -> ReviewRequest | None:
        item = self._client.blpop(self._key, timeout=max(1, int(timeout_s)))
        if item is None:
            return None
        _, payload = item
        return ReviewRequest.model_validate_json(payload)


class InMemoryReviewQueue:
    """Cola compartible en tests y fallback sin Redis."""

    def __init__(self) -> None:
        self._items: deque[str] = deque()

    def enqueue(self, request: ReviewRequest) -> None:
        self._items.append(request.model_dump_json())

    def dequeue(self, *, timeout_s: float = 5.0) -> ReviewRequest | None:
        del timeout_s  # sin espera en memoria
        if not self._items:
            return None
        return ReviewRequest.model_validate_json(self._items.popleft())

    def __len__(self) -> int:
        return len(self._items)
