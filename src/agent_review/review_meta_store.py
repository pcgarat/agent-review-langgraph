"""Metadatos de una review (título del PR, etc.).

Almacena en Redis los metadatos que el panel necesita para mostrar información
legible del PR, como el título. La clave es `reviews:meta:<review_key>` y el
valor es un JSON con los campos del `ReviewMeta`.

Se escribe una vez al inicio de la review (cuando el worker obtiene la info del PR)
y se lee en el read model del panel. TTL de 30 días, igual que los checkpoints.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

import redis
from pydantic import BaseModel

from agent_review.redis_utils import build_redis_client

logger = logging.getLogger(__name__)

KEY_PREFIX = "reviews:meta"
DEFAULT_TTL_S = 30 * 24 * 3600


class ReviewMeta(BaseModel):
    """Metadatos de una review que el panel muestra."""

    review_key: str
    pr_id: int
    pr_title: str = ""

    @property
    def display_label(self) -> str:
        """Etiqueta legible: `#ID - título` o solo `#ID` si no hay título."""
        if self.pr_title.strip():
            return f"#{self.pr_id} - {self.pr_title}"
        return f"#{self.pr_id}"


@runtime_checkable
class ReviewMetaStore(Protocol):
    """Puerto para leer/escribir metadatos de una review."""

    def save(self, review_key: str, *, pr_id: int, pr_title: str) -> None:
        """Guarda los metadatos de una review. Idempotente por review_key."""
        ...

    def get(self, review_key: str) -> ReviewMeta | None:
        """Lee los metadatos de una review, o None si no hay."""
        ...

    def get_many(self, review_keys: list[str]) -> dict[str, ReviewMeta]:
        """Lee los metadatos de varias reviews de una vez."""
        ...


class RedisReviewMetaStore:
    """Almacén de metadatos de review en Redis."""

    def __init__(
        self,
        *,
        url: str,
        key_prefix: str = KEY_PREFIX,
        ttl_s: int = DEFAULT_TTL_S,
        client: redis.Redis | None = None,
    ) -> None:
        self._client = client if client is not None else build_redis_client(url)
        self._key_prefix = key_prefix
        self._ttl_s = ttl_s

    def available(self) -> bool:
        try:
            return bool(self._client.ping())
        except redis.exceptions.RedisError:
            return False

    def _entry_key(self, review_key: str) -> str:
        return f"{self._key_prefix}:{review_key}"

    def save(self, review_key: str, *, pr_id: int, pr_title: str) -> None:
        if not self.available():
            return
        meta = ReviewMeta(review_key=review_key, pr_id=pr_id, pr_title=pr_title)
        try:
            self._client.set(
                self._entry_key(review_key),
                meta.model_dump_json(),
                ex=self._ttl_s,
            )
        except redis.exceptions.RedisError as exc:
            logger.warning("No se pudo guardar metadatos de %s: %s", review_key, exc)

    def get(self, review_key: str) -> ReviewMeta | None:
        if not self.available():
            return None
        try:
            raw = self._client.get(self._entry_key(review_key))
        except redis.exceptions.RedisError as exc:
            logger.warning("No se pudo leer metadatos de %s: %s", review_key, exc)
            return None
        if raw is None:
            return None
        try:
            return ReviewMeta.model_validate_json(raw)
        except ValueError:
            logger.warning("Metadatos corruptos para %s", review_key)
            return None

    def get_many(self, review_keys: list[str]) -> dict[str, ReviewMeta]:
        if not self.available() or not review_keys:
            return {}
        result: dict[str, ReviewMeta] = {}
        try:
            keys = [self._entry_key(key) for key in review_keys]
            values = self._client.mget(keys)
        except redis.exceptions.RedisError as exc:
            logger.warning("No se pudo leer metadatos en batch: %s", exc)
            return {}
        for review_key, raw in zip(review_keys, values, strict=True):
            if raw is None:
                continue
            try:
                result[review_key] = ReviewMeta.model_validate_json(raw)
            except ValueError:
                logger.warning("Metadatos corruptos para %s", review_key)
        return result


class InMemoryReviewMetaStore:
    """Store en memoria para tests."""

    def __init__(self) -> None:
        self._items: dict[str, ReviewMeta] = {}

    def save(self, review_key: str, *, pr_id: int, pr_title: str) -> None:
        self._items[review_key] = ReviewMeta(
            review_key=review_key, pr_id=pr_id, pr_title=pr_title
        )

    def get(self, review_key: str) -> ReviewMeta | None:
        return self._items.get(review_key)

    def get_many(self, review_keys: list[str]) -> dict[str, ReviewMeta]:
        return {key: meta for key in review_keys if (meta := self._items.get(key))}
