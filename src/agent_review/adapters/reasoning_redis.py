from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import redis

from agent_review.ports.reasoning import ReasoningEvent, reasoning_key
from agent_review.redis_utils import build_redis_client

logger = logging.getLogger(__name__)

# Defaults de la spec (021 §5.4, Q2): 10 dias y 10 MB por review.
DEFAULT_TTL_S = 10 * 24 * 3600.0
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
# Se agrupan los trozos del modelo (llegan de a pocos caracteres) antes de escribir:
# un `XADD` por token seria decenas de miles de viajes por lote.
FLUSH_BYTES = 4096
FLUSH_INTERVAL_S = 0.25


class RedisReasoningSink:
    """`ReasoningSink` sobre un stream de Redis por review (021 R4).

    `reviews:reasoning:<review_key>` lleva una entrada por evento (`start`, trozos
    agrupados de `reasoning`, `end`, `error`) con `kind`, `role`, `batch`, `total`,
    `text`, `meta` (JSON) y `ts`. Lo escribe el worker y lo lee el panel con
    `XRANGE`/`XREAD`. `MAXLEN ~` acota el tamaño (los trozos pesan como mucho
    `FLUSH_BYTES`) y cada escritura renueva el TTL.

    Best-effort, como el progreso de 019: si Redis falla se registra y se sigue. Las
    ramas del fan-out emiten en paralelo, asi que el buffer va bajo un candado.
    """

    def __init__(
        self,
        *,
        url: str,
        ttl_s: float = DEFAULT_TTL_S,
        max_bytes: int = DEFAULT_MAX_BYTES,
        client: redis.Redis | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._redis = client if client is not None else build_redis_client(url)
        self._ttl_s = int(ttl_s)
        self._maxlen = max(1, max_bytes // FLUSH_BYTES)
        self._clock = clock
        self._lock = threading.Lock()
        # Trozos pendientes por (review_key, role, batch): (evento base, textos, desde).
        self._pending: dict[tuple[str, str, int], tuple[ReasoningEvent, list[str], float]] = {}

    def emit(self, review_key: str, event: ReasoningEvent) -> None:
        with self._lock:
            slot = (review_key, event.role, event.batch)
            if event.kind == "reasoning":
                base, pieces, since = self._pending.get(slot, (event, [], self._clock()))
                pieces.append(event.text)
                self._pending[slot] = (base, pieces, since)
                if (
                    sum(map(len, pieces)) >= FLUSH_BYTES
                    or self._clock() - since >= FLUSH_INTERVAL_S
                ):
                    self._flush(review_key, slot)
                return
            # `start`/`end`/`error` cierran lo pendiente de su lote antes de escribirse.
            self._flush(review_key, slot)
            self._write(review_key, event)

    def _flush(self, review_key: str, slot: tuple[str, str, int]) -> None:
        entry = self._pending.pop(slot, None)
        if entry is None:
            return
        base, pieces, _since = entry
        text = "".join(pieces)
        if text:
            self._write(
                review_key,
                ReasoningEvent(
                    kind="reasoning", role=base.role, batch=base.batch, total=base.total, text=text
                ),
            )

    def _write(self, review_key: str, event: ReasoningEvent) -> None:
        key = reasoning_key(review_key)
        fields = {
            "kind": event.kind,
            "role": event.role,
            "batch": str(event.batch),
            "total": str(event.total),
            "text": event.text,
            "meta": json.dumps(event.meta, ensure_ascii=False, default=str),
            "ts": f"{time.time():.3f}",
        }
        try:
            pipe = self._redis.pipeline()
            pipe.xadd(key, fields, maxlen=self._maxlen, approximate=True)  # type: ignore[arg-type]
            pipe.expire(key, self._ttl_s)
            pipe.execute()
        except redis.RedisError as exc:
            logger.debug("No se pudo escribir el razonamiento de %s: %s", review_key, exc)


class RedisReasoningReader:
    """Lee el stream de razonamiento de una review (lo usa el panel, 021 R5).

    `read` devuelve los eventos posteriores a `after` (un id de stream; `"0-0"` es
    desde el principio). Con `block_ms`, espera hasta que llegue alguno: es lo que
    hace el SSE para seguir en vivo. Va con un cliente propio y bloqueante.
    """

    def __init__(self, *, url: str, client: redis.Redis | None = None) -> None:
        self._redis = client if client is not None else build_redis_client(url, blocking=True)

    def exists(self, review_key: str) -> bool:
        try:
            return bool(self._redis.exists(reasoning_key(review_key)))
        except redis.RedisError as exc:
            logger.debug("No se pudo consultar el razonamiento de %s: %s", review_key, exc)
            return False

    def read(
        self, review_key: str, *, after: str = "0-0", block_ms: int | None = None, count: int = 500
    ) -> list[tuple[str, ReasoningEvent]]:
        key = reasoning_key(review_key)
        raw: Any
        try:
            if block_ms is None:
                start = after if after == "0-0" else f"({after}"
                raw = self._redis.xrange(key, min=start, count=count)
            else:
                streams: Any = self._redis.xread({key: after}, count=count, block=block_ms)
                raw = streams[0][1] if streams else []
        except redis.RedisError as exc:
            logger.debug("No se pudo leer el razonamiento de %s: %s", review_key, exc)
            return []
        return [
            (str(entry_id), _event({str(k): str(v) for k, v in (fields or {}).items()}))
            for entry_id, fields in raw or []
        ]


def _event(fields: dict[str, str]) -> ReasoningEvent:
    try:
        meta = json.loads(fields.get("meta") or "{}")
    except json.JSONDecodeError:
        meta = {}
    kind = fields.get("kind", "reasoning")
    return ReasoningEvent(
        kind=kind if kind in ("start", "reasoning", "end", "error") else "reasoning",  # type: ignore[arg-type]
        role=fields.get("role", ""),
        batch=int(fields.get("batch") or 0),
        total=int(fields.get("total") or 0),
        text=fields.get("text", ""),
        meta=meta if isinstance(meta, dict) else {},
    )
