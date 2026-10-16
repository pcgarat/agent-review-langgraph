from __future__ import annotations

import logging

import redis

from agent_review.adapters.budget_memory import InMemoryBudget
from agent_review.redis_utils import build_redis_client

logger = logging.getLogger(__name__)

# La clave del fix es distinta de la de la review (017 B11): la aprobacion humana
# puede tardar mas que el TTL de la review y una review cara no debe dejar sin fix
# lo que un humano ya aprobo.
KEY_PREFIX = "budget"
FIX_KEY_PREFIX = "budget:fix"
# TTL de las claves del contador: el mismo que la review. Un contador sin TTL
# creceria sin limite; caducado, una review nueva (o el fix con su clave) empieza
# con el presupuesto entero.
DEFAULT_TTL_S = 8 * 24 * 3600

# Reserva atomica: `KEYS[1]` confirmado, `KEYS[2]` reservado, `ARGV[1]` total,
# `ARGV[2]` tokens, `ARGV[3]` TTL. Devuelve 1 si reserva y 0 si no cabe.
#
# Es la unica forma de cerrar la carrera entre las ramas del fan-out: consultar
# `remaining` y despues reservar en dos viajes deja pasar a N ramas a la vez. El
# script hace comprobacion y escritura en el mismo paso (017 B2/B3).
_RESERVE_LUA = """
local committed = tonumber(redis.call('GET', KEYS[1]) or '0')
local pending = tonumber(redis.call('GET', KEYS[2]) or '0')
local total = tonumber(ARGV[1])
local wanted = tonumber(ARGV[2])
if committed + pending + wanted > total then
  return 0
end
redis.call('INCRBY', KEYS[2], wanted)
redis.call('EXPIRE', KEYS[1], ARGV[3])
redis.call('EXPIRE', KEYS[2], ARGV[3])
return 1
"""

# Confirma un gasto y libera lo reservado que no se gasto (017 B8). Nunca deja el
# pendiente en negativo (un `commit` doble no regala saldo).
_COMMIT_LUA = """
local held = tonumber(redis.call('GET', KEYS[2]) or '0')
local reserved = tonumber(ARGV[1])
local actual = tonumber(ARGV[2])
if reserved > held then reserved = held end
redis.call('DECRBY', KEYS[2], reserved)
if actual > 0 then
  redis.call('INCRBY', KEYS[1], actual)
end
redis.call('EXPIRE', KEYS[1], ARGV[3])
redis.call('EXPIRE', KEYS[2], ARGV[3])
return 1
"""


class RedisBudget:
    """`BudgetPort` sobre Redis: un contador por review, compartido entre procesos.

    Es lo unico que ven a la vez las ramas paralelas del fan-out y el `fix-worker`
    (017 B2). El total es configuracion del despliegue (`LLM_BUDGET_TOKENS_PER_PR`),
    no estado: se pasa en la construccion y el contador solo acumula.

    Si Redis no responde, el adaptador degrada a un contador **en memoria local**: la
    review sigue, sin tope global (X4 de
    [015](../../../docs/iniciativas/015-puesta-en-marcha-real/spec.md) exige Redis en
    `production`), pero no se cae por el presupuesto.
    """

    def __init__(
        self,
        *,
        url: str,
        total: int,
        ttl_s: int = DEFAULT_TTL_S,
        key_prefix: str = KEY_PREFIX,
        client: redis.Redis | None = None,
    ) -> None:
        self._client = client if client is not None else build_redis_client(url)
        self._total = max(0, total)
        self._ttl_s = max(1, int(ttl_s))
        self._prefix = key_prefix
        self._reserve_script = self._client.register_script(_RESERVE_LUA)
        self._commit_script = self._client.register_script(_COMMIT_LUA)
        self._fallback = InMemoryBudget(self._total)

    def available(self) -> bool:
        try:
            return bool(self._client.ping())
        except redis.exceptions.RedisError:
            return False

    def _keys(self, review_key: str) -> tuple[str, str]:
        return f"{self._prefix}:{review_key}", f"{self._prefix}:reserved:{review_key}"

    def reserve(self, review_key: str, tokens: int) -> bool:
        if not self.available():
            return self._fallback.reserve(review_key, tokens)
        committed, pending = self._keys(review_key)
        try:
            result = self._reserve_script(
                keys=[committed, pending],
                args=[self._total, max(0, tokens), self._ttl_s],
            )
        except redis.exceptions.RedisError as exc:
            logger.warning("presupuesto: Redis fallo al reservar (%s); uso el contador local", exc)
            return self._fallback.reserve(review_key, tokens)
        return bool(result)

    def commit(self, review_key: str, reserved: int, actual: int) -> None:
        if not self.available():
            self._fallback.commit(review_key, reserved, actual)
            return
        committed, pending = self._keys(review_key)
        try:
            self._commit_script(
                keys=[committed, pending],
                args=[max(0, reserved), max(0, actual), self._ttl_s],
            )
        except redis.exceptions.RedisError as exc:
            logger.warning("presupuesto: Redis fallo al confirmar (%s); uso el contador local", exc)
            self._fallback.commit(review_key, reserved, actual)

    def remaining(self, review_key: str) -> int:
        if not self.available():
            return self._fallback.remaining(review_key)
        committed, pending = self._keys(review_key)
        try:
            values = self._client.mget(committed, pending)
        except redis.exceptions.RedisError as exc:
            logger.warning("presupuesto: Redis fallo al leer (%s); uso el contador local", exc)
            return self._fallback.remaining(review_key)
        spent = _as_int(values[0]) if values else 0
        held = _as_int(values[1]) if len(values) > 1 else 0
        return max(0, self._total - spent - held)


def _as_int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0
