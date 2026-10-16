from __future__ import annotations

import logging
from datetime import UTC, datetime

import redis

from agent_review.redis_utils import build_redis_client

logger = logging.getLogger(__name__)

# Clave del progreso: un hash por review, un campo por nodo en curso (W10). El
# panel la lee de un tirón con `HGETALL reviews:active:<review_key>`.
KEY_PREFIX = "reviews:active"
# TTL por defecto: por encima del peor caso de un nodo con reintentos (el LLM
# medido es ~15 s; el sandbox no bloquea al worker). Una marca caducada no se
# confunde con un trabajo vivo: es el respaldo si el worker muere a mitad de nodo.
DEFAULT_TTL_S = 3600.0

# `start_node`: alta del nodo o cuenta de rama en curso, sin mover `started_at`.
#
# El campo guarda `{node, started_at, branches}`. Una rama paralela del fan-out que
# arranca después **cuenta** pero no pisa el sello: el «en este nodo desde» es el de
# la primera rama, no el de la última. Todo ocurre en el script para no depender de
# un `HGET`+`HSET` que dos ramas colarían a la vez.
_START_LUA = """
local raw = redis.call('HGET', KEYS[1], ARGV[1])
local started = ARGV[2]
local branches = 1
if raw then
  local existing = cjson.decode(raw)
  started = existing['started_at']
  branches = (tonumber(existing['branches']) or 1) + 1
end
redis.call('HSET', KEYS[1], ARGV[1],
  cjson.encode({node=ARGV[1], started_at=started, branches=branches}))
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 1
"""

# `finish_node`: baja una rama en curso y borra el campo si era la última.
#
# Un contador que baja a 0 se elimina (un campo zombi a 0 se leería como «en
# curso»); si quedan ramas del fan-out, se conserva el sello de la primera.
_FINISH_LUA = """
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if raw then
  local existing = cjson.decode(raw)
  local branches = (tonumber(existing['branches']) or 1) - 1
  if branches > 0 then
    redis.call('HSET', KEYS[1], ARGV[1],
      cjson.encode({node=ARGV[1], started_at=existing['started_at'], branches=branches}))
  else
    redis.call('HDEL', KEYS[1], ARGV[1])
  end
  if redis.call('HLEN', KEYS[1]) > 0 then
    redis.call('EXPIRE', KEYS[1], ARGV[2])
  end
end
return 1
"""


class RedisProgress:
    """`ProgressPort` sobre un hash de Redis: un nodo en curso por campo (W10).

    El hash `reviews:active:<review_key>` lleva un campo por nodo en curso, con su
    `started_at` y su número de ramas. Un nodo con varias ramas (el fan-out de
    `persona`) cuenta ramas: la marca desaparece cuando termina la última, no la
    primera.

    Best-effort por diseño (criterio 8): si Redis no responde, cada operación se
    **salta** con un `debug` en el log. No se comprueba con `ping` antes de cada
    llamada —el fan-out multiplicaría los viajes—, sino que se intenta el script y
    se captura el fallo. El progreso es observabilidad, no estado del que dependa
    una review.
    """

    def __init__(
        self,
        *,
        url: str,
        ttl_s: float = DEFAULT_TTL_S,
        key_prefix: str = KEY_PREFIX,
        client: redis.Redis | None = None,
    ) -> None:
        self._client = client if client is not None else build_redis_client(url)
        self._ttl_s = max(1, int(ttl_s))
        self._prefix = key_prefix
        self._start_script = self._client.register_script(_START_LUA)
        self._finish_script = self._client.register_script(_FINISH_LUA)

    def _key(self, review_key: str) -> str:
        return f"{self._prefix}:{review_key}"

    def start_node(self, review_key: str, node: str, *, at: datetime | None = None) -> None:
        started = (at or datetime.now(UTC)).isoformat()
        try:
            self._start_script(keys=[self._key(review_key)], args=[node, started, self._ttl_s])
        except redis.exceptions.RedisError as exc:
            logger.debug("progreso: fallo al anotar %s (%s); se salta", node, exc)

    def finish_node(self, review_key: str, node: str) -> None:
        try:
            self._finish_script(keys=[self._key(review_key)], args=[node, self._ttl_s])
        except redis.exceptions.RedisError as exc:
            logger.debug("progreso: fallo al cerrar %s (%s); se salta", node, exc)

    def clear(self, review_key: str) -> None:
        try:
            self._client.delete(self._key(review_key))
        except redis.exceptions.RedisError as exc:
            logger.debug("progreso: fallo al limpiar %s (%s); se salta", review_key, exc)
