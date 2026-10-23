from __future__ import annotations

import logging
import time
from collections import deque
from typing import Protocol, runtime_checkable

import redis

from agent_review.domain.models import FixJob, ReviewRequest
from agent_review.redis_utils import build_redis_client, is_transient_redis_error

logger = logging.getLogger(__name__)

QUEUE_KEY = "reviews:jobs"
# Cola de descartes: los jobs que agotan los reintentos no se pierden en el log,
# quedan aqui para reinyectarlos a mano o inspeccionarlos.
DEAD_LETTER_KEY = "reviews:jobs:dead"
# Jobs del motor claude que no pueden correr (sin sesión o sin cupo). Fuera de
# `reviews:jobs` para que el bucle no los vuelva a sacar en la misma vuelta.
PARKED_KEY = "reviews:claude:parked"
NOTICE_KEY_PREFIX = "reviews:claude:notice:"
# `quota` es el job que agotó el cupo: no vuelve solo. `quota-wait` son los
# siguientes, y salen cuando caduca `reviews:claude:notice:quota`.
PARK_REASONS = frozenset({"session", "quota", "quota-wait"})

# Cola de fixes (Fase 3-E7): la wave que el worker de reviews despacha y resuelve
# el worker de fixes. Vive en su propia clave para no mezclarse con las reviews.
FIX_QUEUE_KEY = "reviews:fixes"
FIX_DEAD_LETTER_KEY = "reviews:fixes:dead"
# Set de waves ya resueltas (por `review_key`), con TTL: la reentrega de un job
# de fix tras una caida no vuelve a lanzar la verificacion en el runner (R8).
FIX_DONE_KEY = "reviews:fixes:done"
# TTL del set de resueltas: debe superar con holgura la ventana en que se puede
# reentregar un job (una caida y su recuperacion), sin ser eterno.
DEFAULT_FIX_DONE_TTL_S = 24 * 3600


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

    def take_dead_letter(self, review_key: str) -> dict[str, object] | None:
        """Saca de la DLQ el primer descarte de `review_key`, o `None` si no hay.

        Es la reclamacion del reintento (019 OP31): quien lo saca es el unico que lo
        reencola, asi que dos clics seguidos no encolan dos copias.
        """
        ...

    def park(self, request: ReviewRequest, *, reason: str) -> None:
        """Saca el job de la cola principal a `reviews:claude:parked`."""
        ...

    def parked(self) -> list[ReviewRequest]:
        """Jobs en parking, en el orden en que llegaron."""
        ...

    def release_parked(self, *, reason: str) -> list[ReviewRequest]:
        """Devuelve a `reviews:jobs` los jobs parqueados por `reason`.

        El cupo no sale con la sesión: volver a lanzarlos gastaría el límite.
        """
        ...

    def claim_notice(self, kind: str, *, ttl_s: float | None = None) -> bool:
        """True solo para quien envía el aviso. El segundo no vuelve a encolar.

        `ttl_s` deja que el cupo se vuelva a intentar cuando pase el enfriamiento.
        La falta de sesión no caduca sola: la borra el login.
        """
        ...

    def notice_held(self, kind: str) -> bool:
        """True si ese aviso sigue vigente."""
        ...

    def clear_notice(self, kind: str) -> None:
        """Borra la clave del aviso. Una caducidad posterior puede avisar otra vez."""
        ...


class RedisReviewQueue:
    def __init__(
        self,
        *,
        url: str,
        key: str = QUEUE_KEY,
        dead_letter_key: str = DEAD_LETTER_KEY,
        parked_key: str = PARKED_KEY,
    ) -> None:
        # `blocking=True`: `BLPOP` bloquea y no debe llevar deadline de socket.
        self._client = build_redis_client(url, blocking=True)
        self._key = key
        self._dead_key = dead_letter_key
        self._parked_key = parked_key

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

    def take_dead_letter(self, review_key: str) -> dict[str, object] | None:
        """`LREM` de la entrada cruda: atomico, solo un llamante concurrente la gana."""
        for item in self._client.lrange(self._dead_key, 0, -1):
            raw = item.decode() if isinstance(item, bytes) else str(item)
            entry = _parse_dead_letter(raw)
            if entry is None or str(entry.get("review_key", "")) != review_key:
                continue
            if self._client.lrem(self._dead_key, 1, raw):
                return entry
        return None

    def park(self, request: ReviewRequest, *, reason: str) -> None:
        _check_park_reason(reason)
        self._client.rpush(self._parked_key, _park_payload(request, reason=reason))

    def parked(self) -> list[ReviewRequest]:
        return [
            request
            for item in self._client.lrange(self._parked_key, 0, -1)
            if (request := _parked_request(item)) is not None
        ]

    def release_parked(self, *, reason: str) -> list[ReviewRequest]:
        _check_park_reason(reason)
        # Un solo script: entre LREM y RPUSH una caída perdía el job.
        raw_items = self._client.eval(
            _RELEASE_PARKED,
            2,
            self._parked_key,
            self._key,
            reason,
        )
        released: list[ReviewRequest] = []
        for item in raw_items or []:
            raw = item.decode() if isinstance(item, bytes) else str(item)
            try:
                released.append(ReviewRequest.model_validate_json(raw))
            except ValueError:
                logger.warning("Job ilegible al soltar el parking de Claude")
        return released

    def claim_notice(self, kind: str, *, ttl_s: float | None = None) -> bool:
        _check_park_reason(kind)
        if ttl_s is None:
            return bool(self._client.set(_notice_key(kind), "1", nx=True))
        return bool(self._client.set(_notice_key(kind), "1", nx=True, ex=max(1, int(ttl_s))))

    def notice_held(self, kind: str) -> bool:
        _check_park_reason(kind)
        return bool(self._client.exists(_notice_key(kind)))

    def clear_notice(self, kind: str) -> None:
        _check_park_reason(kind)
        self._client.delete(_notice_key(kind))


class InMemoryReviewQueue:
    """Cola compartible en tests y fallback sin Redis."""

    def __init__(self) -> None:
        self._items: deque[str] = deque()
        self._dead: list[str] = []
        self._parked: deque[str] = deque()
        self._notices: dict[str, float | None] = {}

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

    def take_dead_letter(self, review_key: str) -> dict[str, object] | None:
        for index, item in enumerate(self._dead):
            entry = _parse_dead_letter(item)
            if entry is not None and str(entry.get("review_key", "")) == review_key:
                del self._dead[index]
                return entry
        return None

    def park(self, request: ReviewRequest, *, reason: str) -> None:
        _check_park_reason(reason)
        self._parked.append(_park_payload(request, reason=reason))

    def parked(self) -> list[ReviewRequest]:
        return [request for item in self._parked if (request := _parked_request(item)) is not None]

    def release_parked(self, *, reason: str) -> list[ReviewRequest]:
        _check_park_reason(reason)
        kept: deque[str] = deque()
        released: list[ReviewRequest] = []
        while self._parked:
            raw = self._parked.popleft()
            parsed = _parse_park(raw)
            if parsed is None or parsed[0] != reason:
                kept.append(raw)
                continue
            self._items.append(parsed[1].model_dump_json())
            released.append(parsed[1])
        self._parked.extend(kept)
        return released

    def claim_notice(self, kind: str, *, ttl_s: float | None = None) -> bool:
        _check_park_reason(kind)
        if self.notice_held(kind):
            return False
        expires = None if ttl_s is None else time.time() + ttl_s
        self._notices[kind] = expires
        return True

    def notice_held(self, kind: str) -> bool:
        _check_park_reason(kind)
        expires = self._notices.get(kind)
        if kind not in self._notices:
            return False
        if expires is not None and expires <= time.time():
            del self._notices[kind]
            return False
        return True

    def clear_notice(self, kind: str) -> None:
        _check_park_reason(kind)
        self._notices.pop(kind, None)

    def __len__(self) -> int:
        return len(self._items)

    @property
    def dead_letter_count(self) -> int:
        return len(self._dead)


def _parse_dead_letter(item: str | bytes) -> dict[str, object] | None:
    """Entrada de la DLQ como `dict`, o `None` si es ilegible."""
    import json

    try:
        entry = json.loads(item)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


# Mueve, en una operación, las entradas cuyo `reason` coincide. El payload es
# `{"reason": "<motivo>", "request": <ReviewRequest>}` (json.dumps).
_RELEASE_PARKED = """
local reason = ARGV[1]
local prefix = '{"reason": "' .. reason .. '"'
local items = redis.call('LRANGE', KEYS[1], 0, -1)
local kept = {}
local released = {}
for _, raw in ipairs(items) do
  if string.sub(raw, 1, #prefix) == prefix then
    local at = string.find(raw, '"request":', 1, true)
    local brace = at and string.find(raw, '{', at, true) or nil
    if brace then
      table.insert(released, string.sub(raw, brace, #raw - 1))
    else
      table.insert(kept, raw)
    end
  else
    table.insert(kept, raw)
  end
end
redis.call('DEL', KEYS[1])
if #kept > 0 then
  redis.call('RPUSH', KEYS[1], unpack(kept))
end
if #released > 0 then
  redis.call('RPUSH', KEYS[2], unpack(released))
end
return released
"""


def _check_park_reason(reason: str) -> None:
    if reason not in PARK_REASONS:
        raise ValueError(f"motivo de parking desconocido: {reason}")


def _notice_key(kind: str) -> str:
    return f"{NOTICE_KEY_PREFIX}{kind}"


def _park_payload(request: ReviewRequest, *, reason: str) -> str:
    import json

    return json.dumps({"reason": reason, "request": request.model_dump(mode="json")})


def _parse_park(item: str | bytes) -> tuple[str, ReviewRequest] | None:
    import json

    raw = item.decode() if isinstance(item, bytes) else item
    try:
        entry = json.loads(raw)
    except ValueError:
        logger.warning("Entrada ilegible en el parking de Claude")
        return None
    if not isinstance(entry, dict):
        return None
    reason = entry.get("reason")
    payload = entry.get("request")
    if not isinstance(reason, str) or reason not in PARK_REASONS or not isinstance(payload, dict):
        return None
    try:
        return reason, ReviewRequest.model_validate(payload)
    except ValueError:
        logger.warning("Job ilegible en el parking de Claude")
        return None


def _parked_request(item: str | bytes) -> ReviewRequest | None:
    parsed = _parse_park(item)
    if parsed is None:
        return None
    return parsed[1]


def _dead_letter_payload(request: ReviewRequest, *, reason: str) -> str:
    import json

    return json.dumps(
        {
            "review_key": request.review_key,
            "attempts": request.attempts,
            "reason": reason,
            "discarded_at": time.time(),
            # `mode="json"`: una reanudacion lleva `HitlDecision.decided_at`
            # (`datetime`), que `json.dumps` no serializa.
            "request": request.model_dump(mode="json"),
        }
    )


@runtime_checkable
class FixQueue(Protocol):
    """Cola de waves de verificacion. Mismo contrato que `ReviewQueue`.

    Late el `FixJob` (la wave y la reanudacion) por `review_key`; una clave propia
    para que un job de fix no se mezcle con la cola de reviews. El `done` set
    (`mark_done`/`is_done`) es la guarda de idempotencia del worker de fixes: un
    job reentregado tras una caida no relanza la verificacion en el runner.
    """

    def enqueue(self, job: FixJob) -> None: ...

    def dequeue(self, *, timeout_s: float = 5.0) -> FixJob | None: ...

    def dead_letter(self, job: FixJob, *, reason: str) -> None: ...

    def dead_letters(self) -> list[dict[str, object]]: ...

    def mark_done(self, run_key: str) -> None:
        """Marca una verificacion como resuelta (TTL); una reentrega no la repite."""
        ...

    def is_done(self, run_key: str) -> bool: ...


class RedisFixQueue:
    def __init__(
        self,
        *,
        url: str,
        key: str = FIX_QUEUE_KEY,
        dead_letter_key: str = FIX_DEAD_LETTER_KEY,
        done_key: str = FIX_DONE_KEY,
        done_ttl_s: int = DEFAULT_FIX_DONE_TTL_S,
    ) -> None:
        self._client = build_redis_client(url, blocking=True)
        self._key = key
        self._dead_key = dead_letter_key
        self._done_key = done_key
        self._done_ttl_s = done_ttl_s

    def available(self) -> bool:
        try:
            return bool(self._client.ping())
        except redis.exceptions.RedisError:
            return False

    def enqueue(self, job: FixJob) -> None:
        self._client.rpush(self._key, job.model_dump_json())

    def dequeue(self, *, timeout_s: float = 5.0) -> FixJob | None:
        try:
            item = self._client.blpop(self._key, timeout=max(1, int(timeout_s)))
        except Exception as exc:  # noqa: BLE001 - el poll no debe tumbar al worker
            if not is_transient_redis_error(exc):
                raise
            logger.warning("Cola de fixes no disponible al leer; se reintenta: %s", exc)
            return None
        if item is None:
            return None
        _, payload = item
        return FixJob.model_validate_json(payload)

    def dead_letter(self, job: FixJob, *, reason: str) -> None:
        self._client.rpush(self._dead_key, _fix_dead_letter_payload(job, reason=reason))

    def dead_letters(self) -> list[dict[str, object]]:
        import json

        parsed: list[dict[str, object]] = []
        for item in self._client.lrange(self._dead_key, 0, -1):
            try:
                entry = json.loads(item)
            except ValueError:
                logger.warning("Entrada ilegible en la DLQ de fixes: %s", item)
                continue
            if isinstance(entry, dict):
                parsed.append(entry)
        return parsed

    def mark_done(self, run_key: str) -> None:
        self._client.sadd(self._done_key, run_key)
        self._client.expire(self._done_key, self._done_ttl_s)

    def is_done(self, run_key: str) -> bool:
        return bool(self._client.sismember(self._done_key, run_key))


class InMemoryFixQueue:
    """FixQueue para tests y fallback sin Redis."""

    def __init__(self) -> None:
        self._items: deque[str] = deque()
        self._dead: list[str] = []
        self._done: set[str] = set()

    def enqueue(self, job: FixJob) -> None:
        self._items.append(job.model_dump_json())

    def dequeue(self, *, timeout_s: float = 5.0) -> FixJob | None:
        del timeout_s  # sin espera en memoria
        if not self._items:
            return None
        return FixJob.model_validate_json(self._items.popleft())

    def dead_letter(self, job: FixJob, *, reason: str) -> None:
        self._dead.append(_fix_dead_letter_payload(job, reason=reason))

    def dead_letters(self) -> list[dict[str, object]]:
        import json

        return [json.loads(item) for item in self._dead]

    def mark_done(self, run_key: str) -> None:
        self._done.add(run_key)

    def is_done(self, run_key: str) -> bool:
        return run_key in self._done

    def __len__(self) -> int:
        return len(self._items)

    @property
    def dead_letter_count(self) -> int:
        return len(self._dead)


def _fix_dead_letter_payload(job: FixJob, *, reason: str) -> str:
    import json

    return json.dumps(
        {
            "review_key": job.review_key,
            "reason": reason,
            "discarded_at": time.time(),
            "job": job.model_dump(mode="json"),
        }
    )
