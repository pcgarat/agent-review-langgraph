"""Marca de cancelación cooperativa de una review (019 W4/W6).

El panel la **escribe** (`SET reviews:cancel:<review_key>`, con TTL); el worker solo
la **lee**: desde la frontera de cada nodo (el *seam* de `graph.wrapper`), que es el
único punto donde LangGraph permite abortar sin cortar un nodo a mitad (W4/R5), y al
desencolar cada job.

La marca **no** se retira al abortar: queda como *tombstone* hasta su TTL
(`CANCEL_TTL_S`) para que un job rezagado de la misma review (la reanudación del
fix-worker, una decisión encolada) se aborte en vez de re-ejecutar la review desde
cero sobre un hilo ya borrado. Solo «Reintentar» desde el panel la retira.

La lectura es best-effort —si Redis no responde, `is_cancelled` devuelve `False` y la
review sigue, porque un fallo del panel no puede tumbar lo que observa (criterio 8
de la spec)—.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

import redis

from agent_review.errors import PermanentError, cancel_key
from agent_review.redis_utils import build_redis_client
from agent_review.settings import Settings

logger = logging.getLogger(__name__)


@runtime_checkable
class CancellationPort(Protocol):
    """Lee la marca de cancelación de una review (019 W4).

    Lo implementa el worker sobre Redis; el grafo solo conoce el `CancelCheck`
    (un callable), no este puerto, para no depender de Redis aguas arriba.
    """

    def is_cancelled(self, review_key: str) -> bool: ...


class RedisCancellation:
    """Marca de cancelación de una review en Redis (019 W4)."""

    def __init__(
        self,
        *,
        url: str,
        client: redis.Redis | None = None,
        ttl_s: float | None = None,
    ) -> None:
        self._client = client if client is not None else build_redis_client(url)
        self._ttl_s = int(ttl_s) if ttl_s else None

    def is_cancelled(self, review_key: str) -> bool:
        """¿Tiene la review marca de cancelación? `False` si Redis no responde.

        Es el `CancelCheck` que el worker consume: al ser best-effort, un fallo de
        Redis no aborta nada; la review simplemente sigue corriendo.
        """
        try:
            return bool(self._client.exists(cancel_key(review_key)))
        except redis.exceptions.RedisError as exc:
            logger.debug("cancelación: fallo al consultar %s (%s); se sigue", review_key, exc)
            return False

    def mark(self, review_key: str) -> None:
        """Pone la marca de cancelación con su TTL (019 OP29). La escribe el panel.

        Ruidoso si falla, al contrario que la lectura: quien cancela necesita saber
        que la marca no llegó, no un `200` sobre una cancelación que no ocurrirá.
        """
        try:
            self._client.set(cancel_key(review_key), "1", ex=self._ttl_s)
        except redis.exceptions.RedisError as exc:
            raise PermanentError(f"no se pudo marcar la cancelación de {review_key}") from exc

    def clear(self, review_key: str) -> None:
        """Retira la marca. Lo hace el panel al reintentar una review cancelada.

        Ruidoso como `mark`: si la marca sigue puesta, el reintento se abortaría al
        desencolar, y quien reintenta debe enterarse ahora, no con un `200` vacío.
        """
        try:
            self._client.delete(cancel_key(review_key))
        except redis.exceptions.RedisError as exc:
            raise PermanentError(f"no se pudo retirar la cancelación de {review_key}") from exc


def build_cancellation(settings: Settings) -> RedisCancellation | None:
    """Adaptador de cancelación si hay Redis; `None` sin él.

    Mismo criterio que `build_budget`/`build_progress`: la elección es por
    configuración y el sistema sin Redis se comporta como hoy (W2).
    """
    if not settings.redis_url.strip():
        return None
    return RedisCancellation(url=settings.redis_url, ttl_s=settings.cancel_ttl_s)
