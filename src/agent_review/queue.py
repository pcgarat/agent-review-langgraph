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

    def take_dead_letter(self, review_key: str) -> dict[str, object] | None:
        for index, item in enumerate(self._dead):
            entry = _parse_dead_letter(item)
            if entry is not None and str(entry.get("review_key", "")) == review_key:
                del self._dead[index]
                return entry
        return None

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
