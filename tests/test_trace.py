"""Recorrido de una review desde el checkpointer (iniciativa 019, OP1/OP2/OP4).

El test fuerte es `test_recorrido_coincide_con_get_state_history`: contrasta la
reconstruccion que hace `read_trace` (solo con el `saver`) contra el calculo de
`next` que hace LangGraph en `get_state_history`. Si una version futura de LangGraph
cambia el canal de las aristas (`branch:to:`) o los `Send` del fan-out, es este test
el que lo delata, no el panel en produccion.
"""

from __future__ import annotations

import logging

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from test_checkpointer import _approve, _fix_graph, _FixVcs, _OneFinding
from test_worker import FakeVcs, _CountingReviewer, _escalating_finding, _hitl_graph

from agent_review.checkpointer import build_reader_checkpointer, build_serde
from agent_review.domain.models import (
    DecisionOutcome,
    FindingDecision,
    HitlDecision,
    ReviewRequest,
)
from agent_review.domain.trace import ReviewTrace
from agent_review.graph.build import ReviewState, build_graph
from agent_review.policy.engine import PolicyEngine
from agent_review.trace import read_trace
from agent_review.worker.main import process_request

# Orden real del grafo (docs/arquitectura.md §3) hasta que pausa.
_PREFIX = ["scope", "persona", "merge", "verify", "policy", "hitl"]


def _memory_saver() -> InMemorySaver:
    return InMemorySaver(serde=build_serde())


def _read_graph(saver: BaseCheckpointSaver[object]) -> CompiledStateGraph[ReviewState]:
    """Grafo minimo sobre un saver ya poblado, solo para `get_state_history`.

    `read_trace` no necesita el grafo; el test contrasta contra el, y
    `get_state_history` es un metodo del grafo compilado. Las personas y el VCS no
    intervienen en la lectura de un hilo ya escrito.
    """
    return build_graph(
        vcs=FakeVcs(),
        reviewers=[_OneFinding()],
        policy=PolicyEngine.from_file("config/autonomy.yaml"),
    ).compile(checkpointer=saver)


def _paused_human() -> tuple[BaseCheckpointSaver[object], ReviewRequest]:
    """Hilo pausado esperando una decision humana."""
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="paused")
    graph = _hitl_graph(FakeVcs(), _CountingReviewer(_escalating_finding()))
    process_request(graph, request)
    return _saver_of(graph), request


def _saver_of(graph: object) -> BaseCheckpointSaver[object]:
    saver = graph.checkpointer  # type: ignore[attr-defined]
    assert saver is not None
    return saver  # type: ignore[no-any-return]


def _completed() -> tuple[BaseCheckpointSaver[object], ReviewRequest]:
    """Hilo completado: la pausa humana se resuelve, publica y termina."""
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="done")
    graph = _hitl_graph(FakeVcs(), _CountingReviewer(_escalating_finding()))
    process_request(graph, request)
    decision = HitlDecision(
        review_key=request.review_key,
        actor="alice",
        items=[FindingDecision(finding_id="w1", outcome=DecisionOutcome.APPROVED)],
    )
    process_request(graph, request.model_copy(update={"decision": decision}))
    return _saver_of(graph), request


def _in_progress() -> tuple[BaseCheckpointSaver[object], ReviewRequest]:
    """Hilo a medias: `publish` falla y el hilo queda con ese nodo pendiente."""

    class _FailingPublishVcs(FakeVcs):
        def post_comment(self, repo: str, pr_id: int, body: str) -> None:
            del repo, pr_id, body
            raise RuntimeError("resumen caido (transitorio)")

    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="mid")
    graph = build_graph(
        vcs=_FailingPublishVcs(),
        reviewers=[_OneFinding()],
        policy=PolicyEngine.from_file("config/autonomy.yaml"),
    ).compile(checkpointer=_memory_saver())
    with pytest.raises(RuntimeError):
        process_request(graph, request)
    return _saver_of(graph), request


def test_recorrido_de_un_hilo_completado() -> None:
    saver, request = _completed()
    trace = read_trace(saver, request.thread_id)

    assert trace is not None
    assert trace.review_key == trace.thread_id == request.thread_id
    assert trace.state == "completed"
    assert trace.next == []
    assert trace.pause_kind is None
    # Todo el grafo, en orden; `notify` es el ultimo nodo antes de `END`.
    assert [n.name for n in trace.nodes] == [
        *_PREFIX,
        "fix_dispatch",
        "publish",
        "fix_wait",
        "publish_fix",
        "notify_fix",
        "notify",
    ]
    assert [n.index for n in trace.nodes] == list(range(len(trace.nodes)))
    assert trace.updated_at is not None


def test_recorrido_de_un_hilo_pausado_humano() -> None:
    saver, request = _paused_human()
    trace = read_trace(saver, request.thread_id)

    assert trace is not None
    assert trace.state == "paused"
    assert trace.pause_kind == "human"
    # El recorrido llega hasta `hitl`, el nodo que pauso y quedo pendiente.
    assert [n.name for n in trace.nodes] == _PREFIX
    assert trace.next == ["hitl"]


def test_recorrido_de_una_pausa_de_fix() -> None:
    request = ReviewRequest(repo="r", pr_id=2, head_sha="h", pipeline_id="fix")
    graph = _fix_graph(_FixVcs())
    process_request(graph, request)  # pausa humana
    process_request(
        graph, request.model_copy(update={"decision": _approve(request.review_key, "f1")})
    )  # aprueba -> despacha el fix -> pausa de fix

    trace = read_trace(graph.checkpointer, request.thread_id)
    assert trace is not None
    assert trace.state == "paused"
    assert trace.pause_kind == "fix"
    assert trace.next == ["fix_wait"]
    assert trace.nodes[-1].name == "fix_wait"


def test_recorrido_de_un_hilo_a_medias() -> None:
    saver, request = _in_progress()
    trace = read_trace(saver, request.thread_id)

    assert trace is not None
    assert trace.state == "in_progress"
    assert trace.next == ["publish"], "el nodo que fallo queda pendiente"
    assert trace.nodes[-1].name == "publish"
    assert trace.pause_kind is None


def test_recorrido_de_un_nodo_corriendo_ahora_es_en_curso() -> None:
    """Un nodo vivo (o uno cuyo worker murio a mitad) no deja `__error__`: no es completado.

    Lo estable es que el hilo **no** se lea como `completed` mientras un nodo corre
    y que quede algo pendiente. Cual es el nodo en curso no se fija: el checkpointer
    es historia y el volcado del checkpoint de la tarea viva es una carrera del
    saver (lo dice `TraceNode`: el nodo en curso lo sabe la marca de progreso del
    worker, no esta lectura). Fijar el nombre mediria el azar del instante.
    """
    from langgraph.graph import END, START, StateGraph

    saver = _memory_saver()
    thread_id = "r:1:abc:vivo"
    seen: list[ReviewTrace | None] = []

    def first(state: dict[str, object]) -> dict[str, object]:
        return {"x": 1}

    def running(state: dict[str, object]) -> dict[str, object]:
        seen.append(read_trace(saver, thread_id))
        return {"x": 2}

    builder: StateGraph[dict[str, object]] = StateGraph(dict[str, object])
    builder.add_node("scope", first)
    builder.add_node("persona", running)
    builder.add_edge(START, "scope")
    builder.add_edge("scope", "persona")
    builder.add_edge("persona", END)
    config = {"configurable": {"thread_id": thread_id}}
    builder.compile(checkpointer=saver).invoke({"x": 0}, config=config)

    trace = seen[0]
    assert trace is not None
    # Un nodo corriendo ahora no es un hilo completado.
    assert trace.state == "in_progress"
    assert trace.next, "el nodo vivo queda pendiente"
    assert set(trace.next) <= {"scope", "persona"}
    after = read_trace(saver, thread_id)
    assert after is not None and after.state == "completed" and after.next == []


def test_recorrido_de_un_hilo_inexistente_es_none() -> None:
    assert read_trace(_memory_saver(), "no:existe:1") is None


def test_recorrido_sin_saver_es_none() -> None:
    # Un proceso sin Postgres no inventa un recorrido vacio: no tiene datos.
    assert read_trace(None, "r:1:abc:1") is None


@pytest.mark.parametrize(
    "builder",
    [_completed, _paused_human, _in_progress],
    ids=["completado", "pausado", "a_medias"],
)
def test_recorrido_coincide_con_get_state_history(builder) -> None:
    """La reconstruccion del saver coincide con lo que calcula LangGraph.

    `get_state_history` da, por checkpoint de paso (`step >= 0`), el nodo que se
    ejecuto (`next`) y el estado final. El recorrido derivado debe clavar los mismos
    nodos en el mismo orden y el mismo punto de parada.
    """
    saver, request = builder()
    graph = _read_graph(saver)
    config = {"configurable": {"thread_id": request.thread_id}}
    snaps = list(graph.get_state_history(config))
    newest = snaps[0]
    channels = {write[1] for write in (list(saver.list(config))[0].pending_writes or [])}

    executed = [
        s.next[0]
        for s in reversed(snaps)
        if s.metadata.get("step", -1) >= 0 and s.next and s.next[0] != "__start__"
    ]
    if "__error__" in channels:
        expected_state = "in_progress"
    elif newest.interrupts:
        expected_state = "paused"
    else:
        expected_state = "completed"

    trace = read_trace(saver, request.thread_id)
    assert trace is not None
    assert [n.name for n in trace.nodes] == executed
    assert trace.state == expected_state
    assert trace.next == ([] if expected_state == "completed" else list(newest.next))


class _BrokenSaver:
    def list(self, config: object) -> list:
        del config
        raise RuntimeError("postgres caido")


def test_lectura_de_un_hilo_corrupto_no_lanza(caplog) -> None:
    """Una base caida se registra y devuelve `None`, nunca tumba al panel."""
    with caplog.at_level(logging.ERROR, logger="agent_review.trace"):
        assert read_trace(_BrokenSaver(), "r:1:abc:1") is None  # type: ignore[arg-type]
    assert any("historial" in r.message for r in caplog.records)


def test_checkpointer_de_lectura_sin_url_es_none() -> None:
    # Sin Postgres no hay saver de lectura; el panel debe distinguirlo de un
    # recorrido vacio (`read_trace` devuelve `None`, no un `ReviewTrace` vacio).
    assert build_reader_checkpointer("") is None


def test_checkpointer_de_lectura_no_crea_tablas() -> None:
    """El lector no hace DDL ni cierra el pool: `setup()` es del worker, no del panel."""
    calls: list[str] = []

    class _FakePool:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        @staticmethod
        def check_connection(conn: object) -> None:
            del conn

        def wait(self, *, timeout: float) -> None:
            del timeout

        def close(self) -> None:
            calls.append("close")

    class _FakeSaver:
        def __init__(self, pool: object, *, serde: object = None) -> None:
            del pool, serde

        def setup(self) -> None:  # pragma: no cover - no debe llamarse
            calls.append("setup")

    # Se importan los modulos ANTES de parchear: `_internal` evalua
    # `ConnectionPool[...]` al importarse y un doble sin `__class_getitem__` lo rompe.
    import langgraph.checkpoint.postgres as pg_module
    import psycopg_pool

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(psycopg_pool, "ConnectionPool", _FakePool)
        mp.setattr(pg_module, "PostgresSaver", _FakeSaver)
        saver = build_reader_checkpointer("postgresql://x")

    assert saver is not None
    assert calls == [], f"el lector no debe llamar setup ni cerrar el pool: {calls}"


def test_el_recorrido_no_viaja_por_el_estado() -> None:
    """Es un read model derivado: no entra en la allowlist del serde.

    Si `ReviewTrace` viajara por el estado del grafo, tendria que estar en
    `_STATE_TYPES` o el roundtrip avisaria hoy. Este test fija esa frontera: leer
    el recorrido no toca el estado de la review.
    """
    allowed = {name for _module, name in build_serde()._allowed_msgpack_modules or ()}
    assert "ReviewTrace" not in allowed
    assert "TraceNode" not in allowed
    assert set(ReviewTrace.model_fields) == {
        "review_key",
        "thread_id",
        "state",
        "nodes",
        "next",
        "pause_kind",
        "updated_at",
    }
