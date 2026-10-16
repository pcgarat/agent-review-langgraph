"""Recorrido de una review, reconstruido del checkpointer (iniciativa 019, OP1).

Es el «Q7» que la [018](../../../docs/iniciativas/018-trazas-de-la-review/spec.md)
planteo y que la operacion necesita: **que nodos corrieron, en que orden y donde
esta parada** la review, sin infraestructura nueva. El sustrato ya estaba: el
checkpointer de Postgres guarda, por paso, que nodo se ejecuto y que dejo escrito.

Deliberadamente **no** depende del grafo compilado (D1 del plan): la API declaro que
no habla con Postgres, asi que esto lo consume el proceso `web` con un checkpointer
de solo lectura. Se apoya solo en la API publica del `BaseCheckpointSaver`
(`list`/`get_tuple`) y reproduce el calculo de `next` que LangGraph hace en
`get_state_history` (`pregel/main.py`), en vez de construir el grafo para leerlo.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple, PendingWrite

from agent_review.checkpointer import pause_kind_of
from agent_review.domain.trace import PauseKindName, ReviewTrace, TraceNode, TraceState

logger = logging.getLogger(__name__)

# Arista de salida de un paso: `branch:to:<nodo>`. El fan-out de `persona` no usa
# una arista sino la lista de `Send` en `__pregel_tasks` (ver `_edge_targets`).
_BRANCH_RE = re.compile(r"^branch:to:(?P<node>.+)$")
# `__start__`/`__end__` no son nodos del grafo: son los extremos del recorrido.
_NODE_END = "__end__"
# Canales con los que LangGraph marca el cierre anormal de un paso. El resto son
# datos del estado.
_INTERRUPT_CHANNEL = "__interrupt__"
_ERROR_CHANNEL = "__error__"


def read_trace(saver: BaseCheckpointSaver[Any] | None, thread_id: str) -> ReviewTrace | None:
    """Recorrido del hilo `thread_id`, o `None` si no existe (OP1).

    Sin saver (proceso sin Postgres) devuelve `None`: una lectura imposible no es
    un hilo vacio, y quien llama debe distinguir «no hay panel de datos» de «esta
    review no tiene recorrido».
    """
    if saver is None or not thread_id:
        return None
    config: Any = {"configurable": {"thread_id": thread_id}}
    try:
        tuples = list(saver.list(config))
    except Exception:  # pragma: no cover - base caida
        logger.exception("No se pudo leer el historial del hilo %s", thread_id)
        return None
    if not tuples:
        return None
    return _build_trace(thread_id, tuples)


def _build_trace(thread_id: str, tuples: list[CheckpointTuple]) -> ReviewTrace:
    """Arma el recorrido a partir del historial, de mas nuevo a mas viejo.

    Invariante que sostiene la derivacion: un checkpoint guarda las **aristas de
    salida** del nodo que lo escribio (canal `branch:to:<nodo>`, o los `Send` del
    fan-out en `__pregel_tasks`). Por tanto el nodo que se ejecuto a continuacion
    del checkpoint `j` es el destino que `j` dejo anotado. El checkpoint de entrada
    (el mas viejo, `j` = 0) no lo escribio ningun nodo y se salta: su destino (el
    primer nodo) ya lo aporta el checkpoint mas nuevo.

    La marca de tiempo de cada nodo es la del checkpoint que lo **cerro**, o sea el
    inmediatamente mas nuevo; coincide con el `created_at` que `get_state_history`
    da para ese paso. El nodo mas joven, que aun no tiene sucesor, usa su propio
    checkpoint.
    """
    nodes: list[TraceNode] = []
    # La lista `tuples` viene de mas nuevo a mas viejo: se recorre al reves para
    # emitir los nodos en su orden real de ejecucion, saltando el checkpoint de
    # entrada (el ultimo de la lista), que no lo escribio ningun nodo.
    for index in range(len(tuples) - 1, 0, -1):
        for name in _edge_targets(tuples[index].pending_writes):
            nodes.append(TraceNode(name=name, index=len(nodes), started_at=_node_ts(tuples, index)))

    state, pending = _state_of(tuples, nodes)
    return ReviewTrace(
        review_key=thread_id,
        thread_id=thread_id,
        state=state,
        nodes=nodes,
        next=pending,
        pause_kind=_pause_kind(tuples[0]) if state == "paused" else None,
        updated_at=_tuple_ts(tuples[0]),
    )


def _pause_kind(newest: CheckpointTuple) -> PauseKindName | None:
    """Normaliza a `human`/`fix` el `kind` crudo que saca `pause_kind_of`.

    El checkpointer devuelve el `kind` como `str` (una pausa antigua sin el se
    asume humana); aqui se acota al `Literal` del dominio, y cualquier valor
    inesperado cae a `human`, el default tolerante de la Fase 2.
    """
    kind = pause_kind_of(newest)
    return "fix" if kind == "fix" else "human"


def _state_of(
    tuples: list[CheckpointTuple], nodes: list[TraceNode]
) -> tuple[TraceState, list[str]]:
    """Estado del hilo y nodos pendientes, leidos del checkpoint mas nuevo.

    Los canales que LangGraph escribe al cerrar un paso delatan el estado:
    `__error__` (un nodo fallo), `__interrupt__` (pausa) o ninguno (completado).
    Es la misma convencion que `checkpointer._is_completed`, sin grafo.
    """
    newest = tuples[0]
    channels = {write[1] for write in newest.pending_writes or []}
    # El nodo que quedo pendiente es el ultimo despachado: el destino que dejo el
    # checkpoint inmediatamente anterior, que no llego a cerrar un paso propio.
    pending = _edge_targets(tuples[1].pending_writes) if len(tuples) > 1 else []
    if _ERROR_CHANNEL in channels:
        return "in_progress", pending
    if _INTERRUPT_CHANNEL in channels:
        return "paused", pending
    if not nodes:
        # Ni un solo nodo cerrado: el hilo solo tiene el checkpoint de entrada.
        # Llamarlo completado seria mentir; lo honesto es «aun no ha empezado».
        return "in_progress", _edge_targets(newest.pending_writes)
    return "completed", []


def _edge_targets(pending: list[PendingWrite] | None) -> list[str]:
    """Nodos a los que apunta un checkpoint: aristas de salida + `Send` del fan-out.

    Un paso normal deja una arista `branch:to:<nodo>`; el fan-out (`persona`) deja
    la lista de `Send` en `__pregel_tasks`. `__end__` no es un nodo del grafo.
    """
    targets: list[str] = []
    for _task_id, channel, value in pending or []:
        if channel == "__pregel_tasks":
            sends = value if isinstance(value, (list, tuple)) else [value]
            for send in sends:
                node = getattr(send, "node", None)
                if isinstance(node, str) and node and node != _NODE_END:
                    targets.append(node)
            continue
        match = _BRANCH_RE.match(channel)
        if match and match.group("node") != _NODE_END:
            targets.append(match.group("node"))
    return targets


def _node_ts(tuples: list[CheckpointTuple], from_index: int) -> datetime | None:
    """Sello del nodo despachado por el checkpoint `from_index`.

    Es el del checkpoint que lo cerro, o el suyo propio si aun no tiene sucesor
    (nodo en curso o fallido).
    """
    successor = from_index - 1 if from_index > 0 else 0
    return _tuple_ts(tuples[successor])


def _tuple_ts(tup: CheckpointTuple) -> datetime | None:
    raw = tup.checkpoint.get("ts")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
