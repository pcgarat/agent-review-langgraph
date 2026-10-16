"""Marca de cancelación cooperativa de una review (019 W4/W6).

El panel la **escribe** (`SET reviews:cancel:<review_key>`); el worker solo la
**lee** desde la frontera de cada nodo (el *seam* de `graph.wrapper`), que es el
único punto donde LangGraph permite abortar sin cortar un nodo a mitad (W4/R5).

Este módulo es el lado de lectura: un `RedisCancellation` best-effort —si Redis no
responde, `is_cancelled` devuelve `False` y la review sigue, porque un fallo del
panel no puede tumbar lo que observa (criterio 8 de la spec)—.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

import redis

from agent_review.errors import cancel_key
from agent_review.redis_utils import build_redis_client
from agent_review.settings import Settings

logger = logging.getLogger(__name__)


@runtime_checkable
class CancellationPort(Protocol):
    """Lee (y retira) la marca de cancelación de una review (019 W4).

    Lo implementa el worker sobre Redis; el grafo solo conoce el `CancelCheck`
    (un callable), no este puerto, para no depender de Redis aguas arriba.
    """

    def is_cancelled(self, review_key: str) -> bool: ...

    def clear(self, review_key: str) -> None: ...


class RedisCancellation:
    """Lee la marca de cancelación de una review en Redis (019 W4)."""

    def __init__(self, *, url: str, client: redis.Redis | None = None) -> None:
        self._client = client if client is not None else build_redis_client(url)

    def is_cancelled(self, review_key: str) -> bool:
        """¿Tiene la review marca de cancelación? `False` si Redis no responde.

        Es el `CancelCheck` que instala el worker: al ser best-effort, un fallo de
        Redis no aborta nada; la review simplemente sigue corriendo.
        """
        try:
            return bool(self._client.exists(cancel_key(review_key)))
        except redis.exceptions.RedisError as exc:
            logger.debug("cancelación: fallo al consultar %s (%s); se sigue", review_key, exc)
            return False

    def clear(self, review_key: str) -> None:
        """Retira la marca (el worker la consume al abortar)."""
        try:
            self._client.delete(cancel_key(review_key))
        except redis.exceptions.RedisError as exc:
            logger.debug("cancelación: fallo al limpiar %s (%s); se salta", review_key, exc)


def build_cancellation(settings: Settings) -> RedisCancellation | None:
    """Adaptador de cancelación si hay Redis; `None` sin él.

    Mismo criterio que `build_budget`/`build_progress`: la elección es por
    configuración y el sistema sin Redis se comporta como hoy (W2).
    """
    if not settings.redis_url.strip():
        return None
    return RedisCancellation(url=settings.redis_url)
