from __future__ import annotations

import logging
import time
from collections import deque
from typing import Protocol, runtime_checkable

import redis

from reviews.domain.models import ReviewRequest
from reviews.redis_utils import build_redis_client, is_transient_redis_error

logger = logging.getLogger(__name__)

QUEUE_KEY = "reviews:jobs"
# Cola de descartes: los jobs que agotan los reintentos no se pierden en el log,
# quedan aqui para reinyectarlos a mano o inspeccionarlos.
DEAD_LETTER_KEY = "reviews:jobs:dead"


@runtime_checkable
class ReviewQueue(Protocol):
    def enqueue(self, request: ReviewRequest) -> None: ...

    def dequeue(self, *, timeout_s: float = 5.0) -> ReviewRequest | None:
        """Saca el siguiente trabajo o None si no hay nada en el timeout."""
        ...

    def dead_letter(self, request: ReviewRequest, *, reason: str) -> None:
        """Registra un job descartado tras agotar los intentos."""
        ...

    def dead_letters(self) -> list[dict[str, object]]:
        """Jobs descartados, para inspeccion o reinyeccion manual."""
        ...


class RedisReviewQueue:
    def __init__(
        self, *, url: str, key: str = QUEUE_KEY, dead_letter_key: str = DEAD_LETTER_KEY
    ) -> None:
        # `blocking=True`: `BLPOP` bloquea y no debe llevar deadline de socket.
        self._client = build_redis_client(url, blocking=True)
        self._key = key
        self._dead_key = dead_letter_key

    def available(self) -> bool:
        try:
            return bool(self._client.ping())
        except redis.exceptions.RedisError:
            return False

    def enqueue(self, request: ReviewRequest) -> None:
        self._client.rpush(self._key, request.model_dump_json())

    def dequeue(self, *, timeout_s: float = 5.0) -> ReviewRequest | None:
        """Siguiente trabajo, o `None` en el timeout. No deja morir al worker.

        La espera venida, un Redis que cae a mitad y un servidor que se reinicia
        son rutinas de un consumidor de cola: se registran y se devuelve `None`
        para reintentar en la siguiente vuelta, no se propaga una excepcion.
        """
        try:
            item = self._client.blpop(self._key, timeout=max(1, int(timeout_s)))
        except Exception as exc:  # noqa: BLE001 - el poll no debe tumbar el worker
            if not is_transient_redis_error(exc):
                raise
            logger.warning("Cola no disponible al leer; se reintenta en el proximo poll: %s", exc)
            return None
        if item is None:
            return None
        _, payload = item
        return ReviewRequest.model_validate_json(payload)

    def dead_letter(self, request: ReviewRequest, *, reason: str) -> None:
        self._client.rpush(self._dead_key, _dead_letter_payload(request, reason=reason))

    def dead_letters(self) -> list[dict[str, object]]:
        import json

        raw = self._client.lrange(self._dead_key, 0, -1)
        parsed: list[dict[str, object]] = []
        for item in raw:
            try:
                entry = json.loads(item)
            except ValueError:
                logger.warning("Entrada ilegible en la DLQ: %s", item)
                continue
            if isinstance(entry, dict):
                parsed.append(entry)
        return parsed


class InMemoryReviewQueue:
    """Cola compartible en tests y fallback sin Redis."""

    def __init__(self) -> None:
        self._items: deque[str] = deque()
        self._dead: list[str] = []

    def enqueue(self, request: ReviewRequest) -> None:
        self._items.append(request.model_dump_json())

    def dequeue(self, *, timeout_s: float = 5.0) -> ReviewRequest | None:
        del timeout_s  # sin espera en memoria
        if not self._items:
            return None
        return ReviewRequest.model_validate_json(self._items.popleft())

    def dead_letter(self, request: ReviewRequest, *, reason: str) -> None:
        self._dead.append(_dead_letter_payload(request, reason=reason))

    def dead_letters(self) -> list[dict[str, object]]:
        import json

        return [json.loads(item) for item in self._dead]

    def __len__(self) -> int:
        return len(self._items)

    @property
    def dead_letter_count(self) -> int:
        return len(self._dead)


def _dead_letter_payload(request: ReviewRequest, *, reason: str) -> str:
    import json

    return json.dumps(
        {
            "review_key": request.review_key,
            "attempts": request.attempts,
            "reason": reason,
            "discarded_at": time.time(),
            "request": request.model_dump(),
        }
    )
