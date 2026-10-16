"""Estado de la tarjeta de decision de Teams (C8, iniciativa 013).

Guarda, por `review_key` y por **usuario**, lo que cada uno ha marcado en la
tarjeta. `Action.Execute` repinta la tarjeta solo para quien pulsa, asi que el
estado es por usuario: dos personas marcan en paralelo sin pisarse y cada una ve
lo suyo. La verdad durable sigue siendo el checkpoint del grafo; esto es estado
de UI y puede perderse (se cae a "nada marcado", no a un estado corrupto).

Clave: `reviews:teams:card:<review_key>` -> hash `user_id -> outcome`.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

import redis

from agent_review.domain.models import DecisionOutcome
from agent_review.redis_utils import build_redis_client

logger = logging.getLogger(__name__)

CARD_KEY_PREFIX = "reviews:teams:card"
# La tarjeta de una pausa vive menos que la pausa: pasado `DECISION_TTL_S` la
# review se reanuda como `expired` y el estado de la tarjeta ya no sirve.
DEFAULT_TTL_S = 8 * 24 * 3600


@runtime_checkable
class CardStore(Protocol):
    """Marcas por usuario de una tarjeta de decision."""

    def marks(self, review_key: str, *, user_id: str) -> dict[str, DecisionOutcome]:
        """Lo marcado por un usuario (vacio si no ha marcado nada)."""
        ...

    def mark(
        self, review_key: str, *, user_id: str, finding_id: str, outcome: DecisionOutcome
    ) -> None: ...

    def final(self, review_key: str) -> str | None:
        """Quien decidio primero (C11), o `None` si la decision sigue abierta."""
        ...

    def close(self, review_key: str, *, actor: str) -> bool:
        """Sella la decision. `True` si esta llamada gano (la primera es final, C11)."""
        ...

    def release(self, review_key: str) -> None:
        """Deshace un sello cuyo encolado fallo, para no perder la decision."""
        ...


def card_key(review_key: str) -> str:
    return f"{CARD_KEY_PREFIX}:{review_key}"


_FINAL_SUFFIX = ":final"


class RedisCardStore:
    """Estado de tarjeta en Redis. Un hash por review y una clave de cierre.

    Un fallo de Redis no propaga: el estado es de UI y perderlo solo cuesta que
    la persona vuelva a marcar, nunca una decision corrupta.
    """

    def __init__(
        self,
        *,
        url: str,
        ttl_s: int = DEFAULT_TTL_S,
        key_prefix: str = CARD_KEY_PREFIX,
    ) -> None:
        self._client = build_redis_client(url)
        self._ttl_s = ttl_s
        self._prefix = key_prefix

    def available(self) -> bool:
        try:
            return bool(self._client.ping())
        except redis.exceptions.RedisError:
            return False

    def _key(self, review_key: str) -> str:
        return f"{self._prefix}:{review_key}"

    def _final_key(self, review_key: str) -> str:
        return f"{self._key(review_key)}{_FINAL_SUFFIX}"

    def marks(self, review_key: str, *, user_id: str) -> dict[str, DecisionOutcome]:
        raw = self._client.hgetall(self._key(review_key))
        return _parse_marks(raw, user_id=user_id)

    def mark(
        self, review_key: str, *, user_id: str, finding_id: str, outcome: DecisionOutcome
    ) -> None:
        field = f"{user_id}|{finding_id}"
        pipe = self._client.pipeline()
        pipe.hset(self._key(review_key), field, outcome.value)
        pipe.expire(self._key(review_key), self._ttl_s)
        pipe.execute()

    def final(self, review_key: str) -> str | None:
        raw = self._client.get(self._final_key(review_key))
        if raw is None:
            return None
        return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)

    def close(self, review_key: str, *, actor: str) -> bool:
        """Marca la tarjeta como decidida. `SETNX`: gana la primera decision.

        El booleano es la guarda de C11: quien no gana no debe encolar la suya.
        """
        return bool(self._client.set(self._final_key(review_key), actor, ex=self._ttl_s, nx=True))

    def release(self, review_key: str) -> None:
        self._client.delete(self._final_key(review_key))


class InMemoryCardStore:
    """Store de apoyo para tests y para correr sin Redis."""

    def __init__(self) -> None:
        self._marks: dict[str, dict[str, str]] = {}
        self._final: dict[str, str] = {}

    def marks(self, review_key: str, *, user_id: str) -> dict[str, DecisionOutcome]:
        stored = self._marks.get(review_key, {})
        result: dict[str, DecisionOutcome] = {}
        prefix = f"{user_id}|"
        for field, value in stored.items():
            if field.startswith(prefix):
                result[field[len(prefix) :]] = DecisionOutcome(value)
        return result

    def mark(
        self, review_key: str, *, user_id: str, finding_id: str, outcome: DecisionOutcome
    ) -> None:
        self._marks.setdefault(review_key, {})[f"{user_id}|{finding_id}"] = outcome.value

    def final(self, review_key: str) -> str | None:
        return self._final.get(review_key)

    def close(self, review_key: str, *, actor: str) -> bool:
        if review_key in self._final:
            return False
        self._final[review_key] = actor
        return True

    def release(self, review_key: str) -> None:
        self._final.pop(review_key, None)


def _parse_marks(
    raw: dict[bytes | str, bytes | str], *, user_id: str
) -> dict[str, DecisionOutcome]:
    """Filtra el hash por usuario. Las claves ajenas o ilegibles se ignoran."""
    prefix = f"{user_id}|"
    result: dict[str, DecisionOutcome] = {}
    for field, value in raw.items():
        name = field.decode("utf-8") if isinstance(field, bytes) else str(field)
        if not name.startswith(prefix):
            continue
        text = value.decode("utf-8") if isinstance(value, bytes) else str(value)
        try:
            result[name[len(prefix) :]] = DecisionOutcome(text)
        except ValueError:
            logger.warning("Marca ilegible en la tarjeta (%s=%s); se ignora", name, text)
    return result
