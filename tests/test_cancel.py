"""OP26-OP28: cancelación cooperativa (019, bloque 5).

Tres frentes:

1. **Clasificación** (OP26, D6): `Cancelled` es `PermanentError`, así que la
   `RetryPolicy` del grafo (`retry_on=is_transient`) **no** lo reintenta.
2. **Frontera de nodo** (OP27, W4): la envoltura comprueba la marca **antes** de
   entrar al nodo; con marca, aborta sin ejecutar el cuerpo. Sin marca, el grafo
   se comporta exactamente como hoy.
3. **Worker** (OP28, D5): una `Cancelled` borra el hilo del checkpointer, limpia el
   inbox de pausas y **no** reencola. La marca queda como *tombstone* (con TTL) y
   aborta los jobs rezagados de la misma review en vez de re-ejecutarla desde cero.
"""

from __future__ import annotations

import fakeredis
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from test_checkpointer import _approve, _fix_graph, _FixVcs
from test_worker import FakeVcs, _CountingReviewer, _escalating_finding, _hitl_graph

from agent_review.domain.models import (
    ApprovalRequest,
    FixResolution,
    FixResolutions,
    ReviewRequest,
)
from agent_review.domain.paused import PauseKind
from agent_review.errors import Cancelled, PermanentError, cancel_key, is_transient
from agent_review.graph.wrapper import node_wrapper
from agent_review.paused_store import InMemoryPausedStore
from agent_review.queue import InMemoryReviewQueue
from agent_review.web.commands import CommandEffect, build_command_service
from agent_review.worker.cancel import RedisCancellation
from agent_review.worker.main import _consume, process_request


def _redis_cancel() -> RedisCancellation:
    """Adaptador de cancelación sobre `fakeredis` (sin servidor real)."""
    return RedisCancellation(url="redis://localhost:6379/0", client=fakeredis.FakeRedis())


# --- OP26: clasificación ---------------------------------------------------------


def test_cancelled_es_permanent_error_y_no_se_reintenta() -> None:
    """D6: una cancelación no se reintenta; heredar de `PermanentError` lo garantiza."""
    assert issubclass(Cancelled, PermanentError)
    assert is_transient(Cancelled("cancelada")) is False


def test_cancel_key_construye_la_clave_de_la_marca() -> None:
    assert cancel_key("repo:1:sha") == "reviews:cancel:repo:1:sha"


def test_redis_cancellation_es_cancellation_port() -> None:
    from agent_review.worker.cancel import CancellationPort

    assert isinstance(_redis_cancel(), CancellationPort)


# --- Adaptador de la marca -------------------------------------------------------


def test_redis_cancellation_lee_y_limpia_la_marca() -> None:
    cancel = _redis_cancel()
    assert cancel.is_cancelled("k") is False
    cancel._client.set(cancel_key("k"), "1")
    assert cancel.is_cancelled("k") is True
    cancel.clear("k")
    assert cancel.is_cancelled("k") is False


def test_redis_caido_no_aborta_la_review() -> None:
    """Best-effort: si Redis no responde, la review sigue (criterio 8)."""
    cancel = _redis_cancel()
    cancel._client.close()
    assert cancel.is_cancelled("k") is False


# --- OP27: frontera de nodo ------------------------------------------------------


def test_sin_marca_la_envoltura_deja_pasar_el_nodo() -> None:
    calls: list[str] = []

    def node(state: dict[str, object]) -> dict[str, object]:
        calls.append("run")
        return state

    check = _redis_cancel()
    wrapped = node_wrapper("scope", node, cancel=check.is_cancelled)
    builder: StateGraph[dict[str, object]] = StateGraph(dict[str, object])
    builder.add_node("scope", wrapped)
    builder.add_edge(START, "scope")
    builder.add_edge("scope", END)
    builder.compile().invoke({"request": {"review_key": "k"}})
    assert calls == ["run"]


def test_con_marca_aborta_en_la_frontera_y_no_ejecuta_el_nodo() -> None:
    """W4/R5: el `Cancelled` cae entre nodos; el cuerpo del nodo nunca corre."""
    calls: list[str] = []
    check = _redis_cancel()
    check._client.set(cancel_key("k"), "1")

    def node(state: dict[str, object]) -> dict[str, object]:
        calls.append("run")
        return state

    wrapped = node_wrapper("scope", node, cancel=check.is_cancelled)
    builder: StateGraph[dict[str, object]] = StateGraph(dict[str, object])
    builder.add_node("scope", wrapped)
    builder.add_edge(START, "scope")
    builder.add_edge("scope", END)
    app = builder.compile()
    try:
        app.invoke({"request": {"review_key": "k"}})
    except Cancelled:
        pass
    else:  # pragma: no cover - el aborto siempre lanza
        raise AssertionError("se esperaba Cancelled")
    assert calls == [], "el nodo no se ejecuta: se aborta en la frontera"


def test_sin_chequeo_el_comportamiento_actual_no_cambia() -> None:
    """W2: sin marca ni chequeo, la envoltura solo deja pasar los argumentos."""
    calls: list[str] = []

    def node(state: dict[str, object]) -> dict[str, object]:
        calls.append("run")
        return state

    wrapped = node_wrapper("scope", node)
    builder: StateGraph[dict[str, object]] = StateGraph(dict[str, object])
    builder.add_node("scope", wrapped)
    builder.add_edge(START, "scope")
    builder.add_edge("scope", END)
    builder.compile().invoke({"request": {"review_key": "k"}})
    assert calls == ["run"]


def test_el_chequeo_llega_por_parametro_a_toda_la_frontera_del_grafo() -> None:
    """El `cancel` viaja a `build_graph` (no por estado global): frótalo con el progreso.

    Sin estado mutable de módulo, dos grafos en el mismo proceso no comparten chequeo
    y un test no necesita restaurar nada.
    """
    check = _redis_cancel()
    check._client.set(cancel_key("k"), "1")
    builder: StateGraph[dict[str, object]] = StateGraph(dict[str, object])
    builder.add_node("scope", node_wrapper("scope", lambda state: state, cancel=check.is_cancelled))
    builder.add_edge(START, "scope")
    builder.add_edge("scope", END)
    try:
        builder.compile().invoke({"request": {"review_key": "k"}})
    except Cancelled:
        pass
    else:  # pragma: no cover - el aborto siempre lanza
        raise AssertionError("se esperaba Cancelled")


def test_un_fallo_del_chequeo_no_tumba_el_nodo() -> None:
    """El panel no puede ser una causa de fallo de lo que observa (criterio 8)."""

    def boom(_review_key: str) -> bool:
        raise RuntimeError("redis caido")

    wrapped = node_wrapper("scope", lambda state: {"ok": True}, cancel=boom)
    assert wrapped({"request": {"review_key": "k"}}) == {"ok": True}


def test_la_retry_policy_no_reintenta_una_cancelacion() -> None:
    """D6 en el grafo: con `retry_on=is_transient`, un `Cancelled` no se reintenta."""
    from langgraph.types import RetryPolicy

    calls = {"n": 0}
    check = _redis_cancel()
    check._client.set(cancel_key("k"), "1")

    def node(state: dict[str, object]) -> dict[str, object]:
        calls["n"] += 1
        return state

    builder: StateGraph[dict[str, object]] = StateGraph(dict[str, object])
    builder.add_node(
        "scope",
        node_wrapper("scope", node, cancel=check.is_cancelled),
        retry_policy=RetryPolicy(max_attempts=3, retry_on=is_transient),
    )
    builder.add_edge(START, "scope")
    builder.add_edge("scope", END)
    try:
        builder.compile().invoke({"request": {"review_key": "k"}})
    except Cancelled:
        pass
    else:  # pragma: no cover - el aborto siempre lanza
        raise AssertionError("se esperaba Cancelled")
    assert calls["n"] == 0, "ni el cuerpo ni reintentos: se aborta antes del nodo"


# --- OP28: el worker maneja `Cancelled` ------------------------------------------


class _CancellingGraph:
    """Grafo con checkpointer que aborta como lo haría la envoltura real."""

    def __init__(self, saver: InMemorySaver) -> None:
        self.checkpointer = saver

    def get_state(self, config: object) -> object:
        del config
        return type("Snapshot", (), {"values": None, "next": (), "interrupts": ()})()

    def invoke(self, state: object, config: object = None) -> object:
        del state, config
        raise Cancelled("cancelada por una persona")


def _seed_thread(saver: InMemorySaver, thread_id: str) -> None:
    """Escribe un hilo real para comprobar que la cancelación lo borra."""

    def node(state: dict[str, object]) -> dict[str, object]:
        return state

    builder: StateGraph[dict[str, object]] = StateGraph(dict[str, object])
    builder.add_node("scope", node)
    builder.add_edge(START, "scope")
    builder.add_edge("scope", END)
    builder.compile(checkpointer=saver).invoke(
        {}, config={"configurable": {"thread_id": thread_id}}
    )


def test_cancelled_borra_el_hilo_no_reencola_y_limpia_el_inbox() -> None:
    """D5: borrar el hilo es la guarda durable; el job no vuelve ni acaba en la DLQ."""
    request = ReviewRequest(repo="r", pr_id=1, head_sha="a", pipeline_id="7")
    saver = InMemorySaver()
    _seed_thread(saver, request.thread_id)
    assert saver.get_tuple({"configurable": {"thread_id": request.thread_id}}) is not None

    paused = InMemoryPausedStore()
    paused.pause(request, _approval(request), kind=PauseKind.HUMAN)

    queue = InMemoryReviewQueue()
    _consume(queue, _CancellingGraph(saver), request, max_attempts=3, paused=paused)

    assert saver.get_tuple({"configurable": {"thread_id": request.thread_id}}) is None, (
        "el hilo cancelado se borra para que una reentrega no lo reanude"
    )
    assert queue.dequeue() is None, "una cancelación no se reencola"
    assert queue.dead_letters() == [], "una cancelación no va a la DLQ"
    assert paused.get(request.review_key) is None, "el inbox queda limpio"


def test_un_comando_cancelled_no_reanuda_la_pausa_y_borra_el_hilo() -> None:
    """OP29/OP30: la marca sola no basta en una pausa; el comando la aborta al desencolar."""
    request = ReviewRequest(repo="r", pr_id=1, head_sha="a", pipeline_id="7")
    saver = InMemorySaver()
    _seed_thread(saver, request.thread_id)

    paused = InMemoryPausedStore()
    paused.pause(request, _approval(request), kind=PauseKind.HUMAN)

    def node(state: dict[str, object]) -> dict[str, object]:
        raise AssertionError("una review cancelada no debe reanudar el grafo")

    builder: StateGraph[dict[str, object]] = StateGraph(dict[str, object])
    builder.add_node("scope", node)
    builder.add_edge(START, "scope")
    builder.add_edge("scope", END)
    graph = builder.compile(checkpointer=saver)

    queue = InMemoryReviewQueue()
    _consume(
        queue, graph, request.model_copy(update={"cancelled": True}), max_attempts=3, paused=paused
    )

    assert saver.get_tuple({"configurable": {"thread_id": request.thread_id}}) is None
    assert paused.get(request.review_key) is None
    assert queue.dequeue() is None


def test_la_marca_queda_como_tombstone_al_abortar() -> None:
    """Sin hilo, un rezagado re-ejecutaria la review: la marca se queda hasta su TTL."""
    request = ReviewRequest(repo="r", pr_id=1, head_sha="a", pipeline_id="7")
    cancel = _redis_cancel()
    cancel.mark(request.review_key)

    _consume(
        InMemoryReviewQueue(),
        _CancellingGraph(InMemorySaver()),
        request,
        max_attempts=3,
        cancel=cancel,
    )

    assert cancel.is_cancelled(request.review_key) is True


def test_la_marca_caduca_con_su_ttl() -> None:
    cancel = RedisCancellation(
        url="redis://localhost:6379/0", client=fakeredis.FakeRedis(), ttl_s=600
    )
    cancel.mark("k")

    assert 0 < cancel._client.ttl(cancel_key("k")) <= 600


def test_una_decision_rezagada_tras_cancelar_no_re_ejecuta_la_review() -> None:
    """Pausa humana cancelada: la decision que llega despues no arranca desde START."""
    vcs = FakeVcs()
    reviewer = _CountingReviewer(_escalating_finding())
    graph = _hitl_graph(vcs, reviewer)
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="7")
    paused = InMemoryPausedStore()
    queue = InMemoryReviewQueue()
    cancel = _redis_cancel()
    process_request(graph, request, paused=paused)
    calls = reviewer.calls
    service = build_command_service(queue=queue, paused=paused, cancellation=cancel)

    assert service.cancel(request.review_key, actor="ana") is CommandEffect.CANCELLED
    job = queue.dequeue()
    assert job is not None
    _consume(queue, graph, job, max_attempts=3, paused=paused, cancel=cancel)
    late = request.model_copy(update={"decision": _approve(request.review_key, "w1")})
    _consume(queue, graph, late, max_attempts=3, paused=paused, cancel=cancel)

    assert reviewer.calls == calls, "el rezagado no vuelve a revisar"
    assert graph.get_state({"configurable": {"thread_id": request.thread_id}}).values == {}


def test_cancelar_una_pausa_de_fix_resuelve_la_wave_y_el_rezagado_es_no_op() -> None:
    """La cancelacion de fix no se marca: resuelve la wave; la reanudacion real no re-publica."""
    vcs = _FixVcs()
    graph = _fix_graph(vcs)
    request = ReviewRequest(repo="r", pr_id=2, head_sha="h", pipeline_id="fix")
    paused = InMemoryPausedStore()
    queue = InMemoryReviewQueue()
    cancel = _redis_cancel()
    process_request(graph, request, paused=paused)
    process_request(
        graph,
        request.model_copy(update={"decision": _approve(request.review_key, "f1")}),
        paused=paused,
    )
    entry = paused.get(request.review_key)
    assert entry is not None and entry.kind is PauseKind.FIX
    run_key = entry.fix_request.jobs[0].run_key
    service = build_command_service(queue=queue, paused=paused, cancellation=cancel)

    effect = service.cancel(request.review_key, actor="ana")
    job = queue.dequeue()
    assert job is not None
    _consume(queue, graph, job, max_attempts=3, paused=paused, cancel=cancel)

    config = {"configurable": {"thread_id": request.thread_id}}
    assert effect is CommandEffect.CANCELLED_PENDING
    assert cancel.is_cancelled(request.review_key) is False
    snapshot = graph.get_state(config)
    assert snapshot.values and not snapshot.next, "la wave se resuelve y el hilo completa"
    published = (len(vcs.comments), len(vcs.inlines))

    late = request.model_copy(
        update={
            "fix_resolution": FixResolutions(
                items=[FixResolution(run_key=run_key, outcome="error", error="tarde")]
            )
        }
    )
    _consume(queue, graph, late, max_attempts=3, paused=paused, cancel=cancel)

    assert (len(vcs.comments), len(vcs.inlines)) == published, "el rezagado no re-publica"
    assert paused.get(request.review_key) is None


def _approval(request: ReviewRequest) -> ApprovalRequest:
    return ApprovalRequest(
        review_key=request.review_key,
        repo=request.repo,
        pr_id=request.pr_id,
        head_sha=request.head_sha,
    )
