from __future__ import annotations

import redis


def build_redis_client(url: str, *, blocking: bool = False) -> redis.Redis:
    """Cliente Redis; sin deadline de socket si la operacion puede bloquear.

    redis-py 8 fija un `socket_timeout` de 5 s por defecto. En un cliente que
    bloquea (`BLPOP`), ese deadline compite con el timeout del propio comando y
    lo aborta antes: con `BLPOP timeout=5` el socket vencia primero y el poll de
    una cola vacia elevaba `TimeoutError`, matando al worker en cada vuelta. Un
    cliente que nunca bloquea si puede conservar el deadline (falla rapido ante
    un servidor colgado).
    """
    kwargs: dict[str, object] = {"decode_responses": True}
    if blocking:
        kwargs["socket_timeout"] = None
    return redis.Redis.from_url(url, **kwargs)


def is_transient_redis_error(exc: BaseException) -> bool:
    """Un fallo recuperable de Redis (servidor reiniciando, red), no de datos."""
    return isinstance(exc, (redis.exceptions.RedisError, TimeoutError))
