from __future__ import annotations

import logging
import socket
import time
from collections import deque
from collections.abc import Iterable
from typing import Literal, Protocol, cast, get_args, runtime_checkable
from uuid import uuid4

import redis

from agent_review.domain.models import FixJob, ReviewRequest
from agent_review.redis_utils import build_redis_client, is_transient_redis_error

logger = logging.getLogger(__name__)

QUEUE_KEY = "reviews:jobs"
# Cola de descartes: los jobs que agotan los reintentos no se pierden en el log,
# quedan aqui para reinyectarlos a mano o inspeccionarlos.
DEAD_LETTER_KEY = "reviews:jobs:dead"
# Log de los intentos que aún no han llegado a la DLQ. Al descartar, el texto
# viaja en la entrada (`error`) y esta clave se borra. Si el job termina bien,
# también. El TTL cubre un worker que muere entre el fallo y el descarte.
FAILURE_LOG_PREFIX = "reviews:failure:"
# Anotaciones que sobreviven al cierre de la review. El log de intentos se borra
# al terminar; este se queda para la ficha del panel.
JOURNAL_PREFIX = "reviews:journal:"
GENERATION_PREFIX = "reviews:generation:"
_FAILURE_LOG_TTL_S = 7 * 24 * 3600
_FAILURE_LOG_KEEP = 8
_FAILURE_LOG_LIMIT = 12_000
_JOURNAL_TTL_S = 10 * 24 * 3600
_JOURNAL_KEEP = 100
# Jobs del motor claude que no pueden correr (sin sesión o sin cupo). Fuera de
# `reviews:jobs` para que el bucle no los vuelva a sacar en la misma vuelta.
PARKED_KEY = "reviews:claude:parked"
NOTICE_KEY_PREFIX = "reviews:claude:notice:"
# `quota` es el job que agotó el cupo: no vuelve solo. `quota-wait` son los
# siguientes, y salen cuando caduca `reviews:claude:notice:quota`.
# `ParkReason` es el tipo y `PARK_REASONS` la lista de validación: una fuente
# única para el worker y para el panel, que lo publica en la API (030).
ParkReason = Literal["session", "quota", "quota-wait", "ceiling"]
PARK_REASONS = frozenset(get_args(ParkReason))

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
# Un job en la lista de procesamiento vuelve a la cola si su dueno no lo confirma
# en esta ventana. Cubre el worker que murio en otra maquina; el reinicio del
# mismo proceso lo reclama al momento porque el identificador de arranque no
# coincide. Seis horas superan una review larga sin reentregar una que sigue viva.
JOB_VISIBILITY_S = 6 * 3600


@runtime_checkable
class ReviewQueue(Protocol):
    def enqueue(self, request: ReviewRequest) -> None: ...

    def dequeue(self, *, timeout_s: float = 5.0) -> ReviewRequest | None:
        """Saca el siguiente trabajo o None si no hay nada en el timeout."""
        ...

    def ack(self) -> None:
        """Confirma el job que `dequeue` dejo en la lista de procesamiento."""
        ...

    def reclaim_inflight(self) -> int:
        """Devuelve a la cola los jobs cuyo dueno ya no vive. `0` si no hay."""
        ...

    def dead_letter(self, request: ReviewRequest, *, reason: str, error: str = "") -> None:
        """Registra un job descartado tras agotar los intentos.

        `error` es el log de los intentos (traceback incluido). Vacío en un
        descarte que no llegó a registrar el fallo.
        """
        ...

    def note_failure(self, review_key: str, text: str) -> None:
        """Añade un intento fallido al log de esa review, hasta que se descarte o acabe."""
        ...

    def failure_log(self, review_key: str) -> str:
        """Log acumulado de los intentos, o vacío si no hay."""
        ...

    def clear_failure(self, review_key: str) -> None:
        """Borra el log acumulado. El descarte ya lo copió, o el job terminó."""
        ...

    def append_journal(self, review_key: str, text: str) -> None:
        """Añade una línea al log de la review. No se borra al terminar."""
        ...

    def journal(self, review_key: str) -> list[str]:
        """Líneas del log, en orden. Vacío si no hay."""
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

    def parked_entries(self) -> list[tuple[ParkReason, ReviewRequest]]:
        """Jobs en parking con su motivo, en el orden en que llegaron.

        `parked()` descarta el motivo y el panel lo necesita para decir que
        desatasca cada job: no es lo mismo esperar un token que haber agotado el
        cupo. El motivo sale del payload, validado contra `PARK_REASONS`.
        """
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

    def notice_ttl_s(self, kind: str) -> float | None:
        """Segundos que le quedan al aviso, o `None` si no caduca o no existe."""
        ...

    def clear_notice(self, kind: str) -> None:
        """Borra la clave del aviso. Una caducidad posterior puede avisar otra vez."""
        ...

    def generation(self, review_key: str) -> int:
        """Generacion actual de esa ejecucion. `0` si nadie la ha subido."""
        ...

    def bump_generation(self, review_key: str) -> int:
        """Sube la generacion y devuelve el valor nuevo."""
        ...

    def withdraw(self, review_key: str) -> int:
        """Saca de la cola, el parking y la DLQ los jobs de esa ejecucion.

        No toca el que el worker ya tiene en la lista de procesamiento: ese
        termina, y el job de borrado que va detras limpia lo que haya escrito.
        """
        ...


class _Inflight:
    """Saca un job a una lista propia y lo devuelve a la cola si nadie lo confirma.

    `BLMOVE` (equivalente fiable de `BLPOP` + `RPUSH`) deja el payload en
    `{cola}:processing:{worker}` hasta `ack`. El latido guarda el arranque de
    este proceso: un reinicio, con otro arranque, reclama su lista al momento.
    Otra maquina solo reclama un latido mas viejo que `JOB_VISIBILITY_S`.
    """

    def __init__(
        self,
        owner: RedisReviewQueue | RedisFixQueue,
        *,
        source: str,
        dead_key: str,
        worker_id: str | None,
    ) -> None:
        self._owner = owner
        self._source = source
        self._dead = dead_key
        self._worker_id = worker_id or socket.gethostname()
        self._boot = uuid4().hex
        self._processing = f"{source}:processing:{self._worker_id}"
        self._hb = f"{self._processing}:hb"
        self._inflight: str | None = None

    def pop(self, timeout_s: float) -> str | None:
        client = self._owner._client
        try:
            client.set(self._hb, f"{self._boot}:{time.time()}", ex=JOB_VISIBILITY_S)
            item = client.blmove(
                self._source,
                self._processing,
                max(1, int(timeout_s)),
                "LEFT",
                "RIGHT",
            )
        except Exception as exc:  # noqa: BLE001 - el poll no debe tumbar el worker
            if not is_transient_redis_error(exc):
                raise
            logger.warning("Cola no disponible al leer; se reintenta en el proximo poll: %s", exc)
            return None
        if item is None:
            self._clear_hb_if_idle()
            return None
        self._inflight = item if isinstance(item, str) else item.decode()
        return self._inflight

    def ack(self) -> None:
        if self._inflight is None:
            return
        self._owner._client.lrem(self._processing, 1, self._inflight)
        self._inflight = None
        self._clear_hb_if_idle()

    def discard(self, payload: str, *, reason: str) -> None:
        self._owner._client.rpush(self._dead, _raw_dead_letter_payload(payload, reason=reason))
        if self._inflight == payload:
            self.ack()
            return
        self._owner._client.lrem(self._processing, 1, payload)

    def reclaim(self) -> int:
        client = self._owner._client
        moved = 0
        prefix = f"{self._source}:processing:"
        keys = [self._processing]
        for key in client.scan_iter(match=f"{prefix}*"):
            if key == self._processing or str(key).endswith(":hb"):
                continue
            keys.append(str(key))
        for key in keys:
            if self._should_reclaim(key, own=key == self._processing):
                moved += self._move_back(key)
        return moved

    def _should_reclaim(self, key: str, *, own: bool) -> bool:
        client = self._owner._client
        if not client.llen(key):
            return False
        hb = client.get(f"{key}:hb")
        if not hb:
            return True
        boot, _, ts = str(hb).partition(":")
        if own:
            return boot != self._boot
        try:
            return time.time() - float(ts) >= JOB_VISIBILITY_S
        except ValueError:
            return True

    def _move_back(self, key: str) -> int:
        client = self._owner._client
        moved = 0
        while True:
            item = client.lmove(key, self._source, "LEFT", "RIGHT")
            if item is None:
                break
            moved += 1
        client.delete(f"{key}:hb")
        return moved

    def _clear_hb_if_idle(self) -> None:
        client = self._owner._client
        if not client.llen(self._processing):
            client.delete(self._hb)


class RedisReviewQueue:
    def __init__(
        self,
        *,
        url: str,
        key: str = QUEUE_KEY,
        dead_letter_key: str = DEAD_LETTER_KEY,
        parked_key: str = PARKED_KEY,
        worker_id: str | None = None,
    ) -> None:
        # `blocking=True`: el pop bloquea y no debe llevar deadline de socket.
        self._client = build_redis_client(url, blocking=True)
        self._key = key
        self._dead_key = dead_letter_key
        self._parked_key = parked_key
        self._flight = _Inflight(self, source=key, dead_key=dead_letter_key, worker_id=worker_id)

    def available(self) -> bool:
        try:
            return bool(self._client.ping())
        except redis.exceptions.RedisError:
            return False

    def enqueue(self, request: ReviewRequest) -> None:
        self._client.rpush(self._key, request.model_dump_json())

    def dequeue(self, *, timeout_s: float = 5.0) -> ReviewRequest | None:
        """Siguiente trabajo, o `None` en el timeout. No deja morir al worker.

        Mueve el job a la lista de procesamiento (`BLMOVE`) y solo lo quita
        `ack`. Una caida del proceso lo deja ahi para que el arranque lo
        devuelva a la cola. Un payload ilegible va a la DLQ en crudo: no tumba
        el proceso ni se pierde.
        """
        payload = self._flight.pop(timeout_s)
        if payload is None:
            return None
        try:
            return ReviewRequest.model_validate_json(payload)
        except ValueError:
            logger.error("Job ilegible en %s; se manda a la DLQ", self._key)
            self._flight.discard(payload, reason="Job ilegible")
            return None

    def ack(self) -> None:
        self._flight.ack()

    def reclaim_inflight(self) -> int:
        return self._flight.reclaim()

    def dead_letter(self, request: ReviewRequest, *, reason: str, error: str = "") -> None:
        self._client.rpush(
            self._dead_key, _dead_letter_payload(request, reason=reason, error=error)
        )

    def note_failure(self, review_key: str, text: str) -> None:
        note = text.strip()
        if not note:
            return
        key = _failure_key(review_key)
        pipe = self._client.pipeline()
        pipe.rpush(key, note[-4000:])
        pipe.ltrim(key, -_FAILURE_LOG_KEEP, -1)
        pipe.expire(key, _FAILURE_LOG_TTL_S)
        pipe.execute()

    def failure_log(self, review_key: str) -> str:
        raw = self._client.lrange(_failure_key(review_key), 0, -1)
        parts = [_as_text(item).strip() for item in raw]
        return _clip_log("\n\n".join(part for part in parts if part))

    def clear_failure(self, review_key: str) -> None:
        self._client.delete(_failure_key(review_key))

    def append_journal(self, review_key: str, text: str) -> None:
        line = text.strip()
        if not line:
            return
        key = _journal_key(review_key)
        pipe = self._client.pipeline()
        pipe.rpush(key, line[-4000:])
        pipe.ltrim(key, -_JOURNAL_KEEP, -1)
        pipe.expire(key, _JOURNAL_TTL_S)
        pipe.execute()

    def journal(self, review_key: str) -> list[str]:
        raw = self._client.lrange(_journal_key(review_key), 0, -1)
        return [text for item in raw if (text := _as_text(item).strip())]

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

    def parked_entries(self) -> list[tuple[ParkReason, ReviewRequest]]:
        return parse_parked(self._client.lrange(self._parked_key, 0, -1))

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

    def notice_ttl_s(self, kind: str) -> float | None:
        """Segundos que le quedan. `None` si no existe o no caduca (`TTL` -1)."""
        _check_park_reason(kind)
        ttl = self._client.ttl(_notice_key(kind))
        return float(ttl) if isinstance(ttl, int) and ttl >= 0 else None

    def clear_notice(self, kind: str) -> None:
        _check_park_reason(kind)
        self._client.delete(_notice_key(kind))

    def generation(self, review_key: str) -> int:
        raw = self._client.get(_generation_key(review_key))
        return _generation_value(raw)

    def bump_generation(self, review_key: str) -> int:
        bumped = self._client.incr(_generation_key(review_key))
        return bumped if isinstance(bumped, int) else int(str(bumped))

    def withdraw(self, review_key: str) -> int:
        removed = _drop_matching(self._client, self._key, review_key)
        removed += _drop_matching(self._client, self._parked_key, review_key)
        removed += _drop_matching(self._client, self._dead_key, review_key)
        self._client.delete(
            _failure_key(review_key),
            _journal_key(review_key),
            f"reviews:reasoning:{review_key}",
            f"reviews:active:{review_key}",
            f"reviews:teams:card:{review_key}",
        )
        for extra in self._client.scan_iter(match=f"reviews:teams:card:{review_key}:final*"):
            self._client.delete(extra)
        return removed


class InMemoryReviewQueue:
    """Cola compartible en tests y fallback sin Redis."""

    def __init__(self) -> None:
        self._items: deque[str] = deque()
        self._dead: list[str] = []
        self._parked: deque[str] = deque()
        self._notices: dict[str, float | None] = {}
        self._failures: dict[str, list[str]] = {}
        self._journal: dict[str, list[str]] = {}
        self._generation: dict[str, int] = {}

    def enqueue(self, request: ReviewRequest) -> None:
        self._items.append(request.model_dump_json())

    def dequeue(self, *, timeout_s: float = 5.0) -> ReviewRequest | None:
        del timeout_s  # sin espera en memoria
        if not self._items:
            return None
        payload = self._items.popleft()
        try:
            return ReviewRequest.model_validate_json(payload)
        except ValueError:
            self._dead.append(_raw_dead_letter_payload(payload, reason="Job ilegible"))
            return None

    def ack(self) -> None:
        return None

    def reclaim_inflight(self) -> int:
        return 0

    def dead_letter(self, request: ReviewRequest, *, reason: str, error: str = "") -> None:
        self._dead.append(_dead_letter_payload(request, reason=reason, error=error))

    def note_failure(self, review_key: str, text: str) -> None:
        note = text.strip()
        if not note:
            return
        notes = self._failures.setdefault(review_key, [])
        notes.append(note[-4000:])
        del notes[:-_FAILURE_LOG_KEEP]

    def failure_log(self, review_key: str) -> str:
        return _clip_log("\n\n".join(self._failures.get(review_key, [])))

    def clear_failure(self, review_key: str) -> None:
        self._failures.pop(review_key, None)

    def append_journal(self, review_key: str, text: str) -> None:
        line = text.strip()
        if not line:
            return
        lines = self._journal.setdefault(review_key, [])
        lines.append(line[-4000:])
        del lines[:-_JOURNAL_KEEP]

    def journal(self, review_key: str) -> list[str]:
        return list(self._journal.get(review_key, []))

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

    def parked_entries(self) -> list[tuple[ParkReason, ReviewRequest]]:
        return parse_parked(self._parked)

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

    def notice_ttl_s(self, kind: str) -> float | None:
        """Segundos que le quedan. `None` si no existe o no caduca."""
        _check_park_reason(kind)
        expires = self._notices.get(kind)
        if kind not in self._notices or expires is None:
            return None
        return max(0.0, expires - time.time())

    def clear_notice(self, kind: str) -> None:
        _check_park_reason(kind)
        self._notices.pop(kind, None)

    def generation(self, review_key: str) -> int:
        return self._generation.get(review_key, 0)

    def bump_generation(self, review_key: str) -> int:
        value = self._generation.get(review_key, 0) + 1
        self._generation[review_key] = value
        return value

    def withdraw(self, review_key: str) -> int:
        removed = _drop_memory(self._items, review_key)
        removed += _drop_memory(self._parked, review_key)
        removed += _drop_memory_list(self._dead, review_key)
        self._failures.pop(review_key, None)
        self._journal.pop(review_key, None)
        return removed

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


def _parse_park(item: str | bytes) -> tuple[ParkReason, ReviewRequest] | None:
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
        return cast(ParkReason, reason), ReviewRequest.model_validate(payload)
    except ValueError:
        logger.warning("Job ilegible en el parking de Claude")
        return None


def _parked_request(item: str | bytes) -> ReviewRequest | None:
    parsed = _parse_park(item)
    if parsed is None:
        return None
    return parsed[1]


def parse_parked(items: Iterable[str | bytes]) -> list[tuple[ParkReason, ReviewRequest]]:
    """Entradas del parking con su motivo, saltando las ilegibles.

    Es el parseo del worker y lo comparte el panel: dos lectores del mismo payload
    con reglas distintas acabarían contando aparcados diferentes. Lo que no encaja
    (JSON roto, motivo desconocido, request inválido) se descarta con un aviso en
    `_parse_park`, no se propaga.
    """
    entries: list[tuple[ParkReason, ReviewRequest]] = []
    for item in items:
        parsed = _parse_park(item)
        if parsed is not None:
            entries.append(parsed)
    return entries


def _raw_dead_letter_payload(payload: str, *, reason: str) -> str:
    import json

    return json.dumps({"reason": reason, "discarded_at": time.time(), "raw": payload})


def _failure_key(review_key: str) -> str:
    return f"{FAILURE_LOG_PREFIX}{review_key}"


def _journal_key(review_key: str) -> str:
    return f"{JOURNAL_PREFIX}{review_key}"


def _generation_key(review_key: str) -> str:
    return f"{GENERATION_PREFIX}{review_key}"


def _generation_value(raw: object) -> int:
    if isinstance(raw, bool) or raw is None:
        return 0
    if isinstance(raw, int):
        return raw
    if not isinstance(raw, str):
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _review_key_of(raw: str | bytes) -> str | None:
    import json

    text = raw.decode() if isinstance(raw, bytes) else raw
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    key = data.get("review_key")
    if isinstance(key, str) and key:
        return key
    payload = data.get("request", data)
    if not isinstance(payload, dict):
        return None
    try:
        return ReviewRequest.model_validate(payload).review_key
    except ValueError:
        return None


def _drop_matching(client: redis.Redis, key: str, review_key: str) -> int:
    removed = 0
    for raw in client.lrange(key, 0, -1):
        text = raw.decode() if isinstance(raw, bytes) else str(raw)
        if _review_key_of(text) != review_key:
            continue
        removed += int(client.lrem(key, 1, text))
    return removed


def _drop_memory(items: deque[str], review_key: str) -> int:
    kept: deque[str] = deque()
    removed = 0
    while items:
        raw = items.popleft()
        if _review_key_of(raw) == review_key:
            removed += 1
        else:
            kept.append(raw)
    items.extend(kept)
    return removed


def _drop_memory_list(items: list[str], review_key: str) -> int:
    kept = [raw for raw in items if _review_key_of(raw) != review_key]
    removed = len(items) - len(kept)
    items[:] = kept
    return removed


def _as_text(item: object) -> str:
    if isinstance(item, bytes):
        return item.decode(errors="replace")
    return str(item)


def _clip_log(text: str) -> str:
    if len(text) <= _FAILURE_LOG_LIMIT:
        return text
    return "…\n" + text[-_FAILURE_LOG_LIMIT:]


def _dead_letter_payload(request: ReviewRequest, *, reason: str, error: str = "") -> str:
    import json

    payload: dict[str, object] = {
        "review_key": request.review_key,
        "attempts": request.attempts,
        "reason": reason,
        "discarded_at": time.time(),
        # `mode="json"`: una reanudacion lleva `HitlDecision.decided_at`
        # (`datetime`), que `json.dumps` no serializa.
        "request": request.model_dump(mode="json"),
    }
    clipped = _clip_log(error.strip())
    if clipped:
        payload["error"] = clipped
    return json.dumps(payload)


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

    def ack(self) -> None:
        """Confirma el job que `dequeue` dejo en la lista de procesamiento."""
        ...

    def reclaim_inflight(self) -> int:
        """Devuelve a la cola los jobs cuyo dueno ya no vive. `0` si no hay."""
        ...

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
        worker_id: str | None = None,
    ) -> None:
        self._client = build_redis_client(url, blocking=True)
        self._key = key
        self._dead_key = dead_letter_key
        self._done_key = done_key
        self._done_ttl_s = done_ttl_s
        self._flight = _Inflight(self, source=key, dead_key=dead_letter_key, worker_id=worker_id)

    def available(self) -> bool:
        try:
            return bool(self._client.ping())
        except redis.exceptions.RedisError:
            return False

    def enqueue(self, job: FixJob) -> None:
        self._client.rpush(self._key, job.model_dump_json())

    def dequeue(self, *, timeout_s: float = 5.0) -> FixJob | None:
        payload = self._flight.pop(timeout_s)
        if payload is None:
            return None
        try:
            return FixJob.model_validate_json(payload)
        except ValueError:
            logger.error("Job de fix ilegible en %s; se manda a la DLQ", self._key)
            self._flight.discard(payload, reason="Job de fix ilegible")
            return None

    def ack(self) -> None:
        self._flight.ack()

    def reclaim_inflight(self) -> int:
        return self._flight.reclaim()

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
        payload = self._items.popleft()
        try:
            return FixJob.model_validate_json(payload)
        except ValueError:
            self._dead.append(_raw_dead_letter_payload(payload, reason="Job de fix ilegible"))
            return None

    def ack(self) -> None:
        return None

    def reclaim_inflight(self) -> int:
        return 0

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
