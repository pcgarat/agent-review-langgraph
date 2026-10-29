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
from agent_review.domain.models import DiscardedFinding
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


def read_review_notes(saver: BaseCheckpointSaver[Any] | None, thread_id: str) -> list[str]:
    """Hechos del estado final que el recorrido de nodos no cuenta.

    Una review incompleta deja el motivo en `source_status`, no en el nombre del
    nodo. Sin esto la ficha dice «incompleta» y no dice por qué.
    """
    if saver is None or not thread_id:
        return []
    try:
        tup = saver.get_tuple({"configurable": {"thread_id": thread_id}})
    except Exception:  # pragma: no cover - base caida
        logger.exception("No se pudo leer el estado del hilo %s", thread_id)
        return []
    if tup is None:
        return []
    checkpoint = getattr(tup, "checkpoint", None)
    values = checkpoint.get("channel_values") if isinstance(checkpoint, dict) else None
    if not isinstance(values, dict):
        return []
    return notes_from_values(values)


def notes_from_values(values: dict[str, Any]) -> list[str]:
    """Líneas del log a partir de los canales del último checkpoint."""
    lines: list[str] = []
    sources = values.get("source_status") or {}
    if isinstance(sources, dict):
        for name, status in sorted(sources.items(), key=lambda item: str(item[0])):
            label = _label(status)
            if label == "present":
                continue
            note = _SOURCE_NOTES.get(label, "la salida no está disponible")
            lines.append(f"Fuente estática {name}: {label}. {note}")
    coverage = values.get("coverage")
    skipped = getattr(coverage, "skipped", None)
    if isinstance(coverage, dict):
        skipped = coverage.get("skipped")
    if isinstance(skipped, dict):
        for path, reason in sorted(skipped.items(), key=lambda item: str(item[0])):
            label = _label(reason)
            if label == "ignored":
                continue
            lines.append(f"Sin revisar {path}: {label}.")
    exhausted = values.get("budget_exhausted") or []
    if exhausted:
        names = ", ".join(sorted(str(item) for item in exhausted))
        lines.append(f"Presupuesto agotado en: {names}.")
    if values.get("published") and lines:
        lines.append("Se escribió el aviso en el pull request.")
    if values.get("silence_without_choice") and not values.get("published"):
        merged = values.get("merged")
        if not isinstance(merged, list):
            merged = values.get("findings") if isinstance(values.get("findings"), list) else []
        if not merged:
            lines.append(
                "Terminó sin hallazgos. El motor claude no escribe un comentario vacío "
                "en el pull request."
            )
    return lines


def _label(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw)


_SOURCE_NOTES = {
    "missing": (
        "No se encontró la salida de la herramienta. "
        "La review no cubre ese análisis y cero hallazgos no significa un PR limpio."
    ),
    "unreadable": (
        "La salida de la herramienta no se pudo leer. "
        "La review no cubre ese análisis y cero hallazgos no significa un PR limpio."
    ),
}


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
        targets = _edge_targets(tuples[index].pending_writes)
        # La tarea corre desde el checkpoint siguiente y sus escrituras quedan en el.
        ran_from = tuples[index - 1]
        for name in targets:
            summary, discarded = (
                _summarize(name, ran_from, reviewer=_send_reviewer(tuples[index].pending_writes))
                if len(targets) == 1
                else (None, [])
            )
            nodes.append(
                TraceNode(
                    name=name,
                    index=len(nodes),
                    started_at=_node_ts(tuples, index),
                    summary=summary,
                    discarded=discarded,
                )
            )

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
    `__error__` (un nodo fallo) o `__interrupt__` (pausa). Sin ninguno de los dos, el
    hilo esta completado **solo si** su ultimo checkpoint no despacha nada: un nodo
    corriendo ahora (o uno cuyo worker murio a mitad) deja ese checkpoint con su
    arista `branch:to:<nodo>` (o los `Send` del fan-out) en `channel_values` y sin
    escrituras propias. Es lo que LangGraph usa para calcular `next`.
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
    running = _dispatched_by(newest)
    if running:
        return "in_progress", running
    return "completed", []


def _dispatched_by(tup: CheckpointTuple) -> list[str]:
    """Nodos que el checkpoint `tup` deja despachados y aun sin cerrar.

    Se leen de sus `channel_values` con la misma convencion que las escrituras
    (`branch:to:<nodo>` y `__pregel_tasks`), asi que se reutiliza `_edge_targets`.
    """
    values = tup.checkpoint.get("channel_values") or {}
    return _edge_targets([("", channel, value) for channel, value in values.items()])


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


# Persona cuyas cifras se cuentan (034): las de `llm` corren en paralelo y sus
# escrituras no dicen qué rama fue cada una.
_COUNTED_PERSONA = "claude"


def _send_reviewer(pending: list[PendingWrite] | None) -> str | None:
    """Revisor del único `Send` del fan-out, o `None` si no hay uno solo."""
    for _task_id, channel, value in pending or []:
        if channel != "__pregel_tasks":
            continue
        sends = value if isinstance(value, (list, tuple)) else [value]
        if len(sends) != 1:
            return None
        arg = getattr(sends[0], "arg", None)
        reviewer = arg.get("reviewer") if isinstance(arg, dict) else None
        return reviewer if isinstance(reviewer, str) else None
    return None


def _summarize(
    name: str, ran_from: CheckpointTuple, *, reviewer: str | None
) -> tuple[dict[str, int] | None, list[DiscardedFinding]]:
    """Cifras de lo que escribió la tarea del nodo `name` (034 §4.1).

    Sin escrituras el nodo no cerró: no hay cifras que dar.
    """
    pending = ran_from.pending_writes or []
    if not pending:
        return None, []
    writes = {channel: value for _task_id, channel, value in pending}
    if name == "scope":
        coverage = writes.get("coverage")
        skipped = _field(coverage, "skipped")
        ignored = sum(1 for reason in (skipped or {}).values() if _label(reason) == "ignored")
        total = _field(coverage, "files_total")
        if not isinstance(total, int):
            return None, []
        return {"files": total, "ignored": ignored}, []
    if name == "persona" and reviewer == _COUNTED_PERSONA:
        found = writes.get("findings")
        discarded = [
            item if isinstance(item, DiscardedFinding) else DiscardedFinding.model_validate(item)
            for item in writes.get("discarded") or []
        ]
        kept = len(found) if isinstance(found, list) else 0
        contract = sum(1 for item in discarded if item.stage == "contract")
        l0 = sum(1 for item in discarded if item.stage == "l0")
        summary = {"returned": kept + contract + l0, "contract": contract, "l0": l0, "kept": kept}
        return summary, discarded
    if name == "merge":
        merged = writes.get("merged")
        before = (ran_from.checkpoint.get("channel_values") or {}).get("findings")
        if not isinstance(merged, list):
            return None, []
        count = len(before) if isinstance(before, list) else len(merged)
        return {"before": count, "after": len(merged)}, []
    if name == "stamp":
        decisions = writes.get("decisions")
        if not isinstance(decisions, list):
            return None, []
        publish = sum(1 for item in decisions if _label(_field(item, "action")) == "publish")
        return {"publish": publish, "other": len(decisions) - publish}, []
    if name == "publish":
        inline = writes.get("inlined_count")
        return {
            "published": 1 if writes.get("published") else 0,
            "inline": inline if isinstance(inline, int) else 0,
        }, []
    return None, []


def _field(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


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
