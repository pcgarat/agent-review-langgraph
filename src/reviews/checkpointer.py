from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from reviews.domain.models import (
    ApprovalRequest,
    AutofixClass,
    Decision,
    DecisionOutcome,
    Finding,
    FindingAction,
    FindingDecision,
    HitlDecision,
    Owner,
    PendingDecision,
    ReviewRequest,
    Severity,
    ToolingSourceStatus,
)

logger = logging.getLogger(__name__)

# Tipos del dominio que viajan por el estado del grafo. Allowlist explicita para
# que el serializer no dependa del msgpack permisivo (que avisa hoy y bloqueara
# los tipos no registrados en versiones futuras). Incluye los HITL (Fase 2): el
# `ApprovalRequest` del `interrupt` y la `HitlDecision` de la reanudacion.
_STATE_TYPES: tuple[type, ...] = (
    ReviewRequest,
    Finding,
    Decision,
    Severity,
    AutofixClass,
    Owner,
    FindingAction,
    ToolingSourceStatus,
    ApprovalRequest,
    PendingDecision,
    FindingDecision,
    HitlDecision,
    DecisionOutcome,
)


def build_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=_STATE_TYPES)


def build_checkpointer(database_url: str) -> BaseCheckpointSaver[Any]:
    """Checkpointer durable si hay Postgres; en memoria si no (tests/local/CI).

    Nunca lanza: sin base de datos el worker sigue funcionando sin dedupe por
    hilo (la marca de publicacion cubre el resto de casos). El checkpointer
    durable permite reanudar el grafo en Fase 2 (HITL) sin cambios adicionales.
    """
    if not database_url:
        logger.info("Sin DATABASE_URL; checkpointer en memoria (no durable)")
        return InMemorySaver(serde=build_serde())
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg import Connection
        from psycopg.rows import dict_row

        # Conexion dedicada y de larga vida: el worker es un proceso unico.
        conn = Connection.connect(
            database_url, autocommit=True, prepare_threshold=0, row_factory=dict_row
        )
        saver = PostgresSaver(conn, serde=build_serde())
        saver.setup()
        logger.info("Checkpointer Postgres listo")
        return saver
    except Exception:
        logger.exception("No se pudo abrir el checkpointer Postgres; uso memoria")
        return InMemorySaver(serde=build_serde())


def prune_completed_threads(
    saver: BaseCheckpointSaver[Any],
    *,
    older_than_days: float,
    paused_threads: Iterable[str] = (),
    now: datetime | None = None,
) -> list[str]:
    """Borra hilos **completados** mas antiguos que la retencion. Nunca poda pausados.

    Un hilo pausado espera una decision humana que puede llegar tarde: podarlo
    dejaria la review indecidible (`Command(resume=...)` sin estado). Por eso los
    pausados (`paused_threads`, del inbox) se excluyen siempre, aunque superen la
    retencion; su ciclo lo cierra el TTL de decision (`sweep_expired`).

    La dedupe de publicacion vive en las marcas del PR, no en el checkpoint, asi
    que podar un hilo completado no puede duplicar comentarios (buena separacion
    que no hay que romper, spec §4.8). Nunca lanza: la poda es mantenimiento.
    """
    if older_than_days <= 0:
        return []
    cutoff = (now or datetime.now(UTC)) - timedelta(days=older_than_days)
    protected = set(paused_threads)
    # Solo la ultima tupla de cada hilo decide si esta completado; se recorre de
    # la mas nueva a la mas vieja para no dejar que una tupla antigua marque el
    # hilo como pendiente.
    latest: dict[str, Any] = {}
    try:
        for tup in saver.list(None):
            thread_id = tup.config["configurable"]["thread_id"]
            latest.setdefault(thread_id, tup)
    except Exception:  # pragma: no cover - base caida
        logger.exception("No se pudieron listar los hilos para podar")
        return []

    pruned: list[str] = []
    for thread_id, tup in latest.items():
        if thread_id in protected:
            continue
        if not _is_completed(tup):
            continue
        ts = _checkpoint_ts(tup)
        if ts is None or ts >= cutoff:
            continue
        try:
            saver.delete_thread(thread_id)
        except Exception:  # pragma: no cover - base caida
            logger.exception("No se pudo podar el hilo %s", thread_id)
            continue
        pruned.append(thread_id)
    if pruned:
        logger.info("Podados %d hilo(s) completados por retencion", len(pruned))
    return pruned


def _is_completed(tup: Any) -> bool:
    """Un hilo terminado no tiene un `interrupt` pendiente ni un `__resume__` vivo.

    El `__interrupt__` de un hilo pausado aparece en `pending_writes`; el de uno ya
    reanudado tambien, pero acompanado de un `__resume__` en la misma tupla. Es la
    senal mas fiable entre versiones, sin depender del formato del mensaje.
    """
    keys = [write[1] for write in (tup.pending_writes or [])]
    if "__resume__" in keys:
        return True
    return "__interrupt__" not in keys


def _checkpoint_ts(tup: Any) -> datetime | None:
    raw = (tup.checkpoint or {}).get("ts")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
