from __future__ import annotations

import logging
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
