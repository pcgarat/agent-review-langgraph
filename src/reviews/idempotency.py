from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

import redis

from reviews.redis_utils import build_redis_client

logger = logging.getLogger(__name__)


@runtime_checkable
class IdempotencyStore(Protocol):
    def claim(self, key: str) -> bool:
        """Devuelve True la primera vez que se ve la clave; False si ya existia."""
        ...

    def release(self, key: str) -> None:
        """Deshace un `claim` cuyo trabajo fallo, para que el reintento no sea `duplicado`."""
        ...


class RedisIdempotencyStore:
    """Idempotencia por repo:pr:head_sha.

    Un push nuevo genera un head_sha nuevo y por tanto una review nueva; repetir
    el mismo evento no reprocesa nada.
    """

    def __init__(self, *, url: str, ttl_seconds: int = 60 * 60 * 24 * 7) -> None:
        self._client = build_redis_client(url)
        self._ttl = ttl_seconds

    def available(self) -> bool:
        try:
            return bool(self._client.ping())
        except redis.exceptions.RedisError:
            return False

    def claim(self, key: str) -> bool:
        return bool(self._client.set(key, "1", nx=True, ex=self._ttl))

    def release(self, key: str) -> None:
        self._client.delete(key)


class InMemoryIdempotencyStore:
    """Implementacion de apoyo para tests y para correr sin Redis."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def claim(self, key: str) -> bool:
        if key in self._seen:
            logger.debug("Clave ya procesada: %s", key)
            return False
        self._seen.add(key)
        return True

    def release(self, key: str) -> None:
        self._seen.discard(key)
