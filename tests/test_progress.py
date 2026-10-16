"""OP9/OP10: puerto de progreso (019) y envoltura de nodo.

Dos frentes:

1. **Semántica del puerto**: `RedisProgress` y `NullProgress` sobre `fakeredis`. El
   fan-out cuenta ramas (el sello del nodo es el de la primera rama, no el de la
   última) y la marca desaparece cuando termina la última.
2. **Envoltura de nodo** (R1): `node_wrapper` no altera ni la firma del nodo (lo que
   LangGraph lee para inyectar el `Runtime` de `persona`), ni su `RetryPolicy`, ni
   el comportamiento del grafo. Sin puerto, el grafo produce el mismo estado.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from typing import Any

import fakeredis
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, RetryPolicy, interrupt
from test_graph import FakeVcs, StaticReviewer, make_finding

from agent_review.adapters.progress_null import NullProgress
from agent_review.adapters.progress_redis import RedisProgress
from agent_review.domain.models import ReviewRequest
from agent_review.errors import TransientError, is_transient
from agent_review.graph.build import build_graph
from agent_review.graph.wrapper import _review_key_of, node_wrapper
from agent_review.policy.engine import PolicyEngine
from agent_review.ports.progress import ProgressPort, clear, finish_node, start_node


def _redis_progress(*, ttl_s: float = 3600.0) -> RedisProgress:
    """Adaptador Redis sobre `fakeredis`: ejecuta los scripts Lua de verdad."""
    return RedisProgress(
        url="redis://localhost:6379/0",
        ttl_s=ttl_s,
        client=fakeredis.FakeRedis(decode_responses=True),
    )


def _stateful_graph(progress: ProgressPort | None = None) -> Any:
    """Grafo real con una persona y un hallazgo, para comparar estados."""
    return build_graph(
        vcs=FakeVcs(),
        reviewers=[StaticReviewer("correctness", [make_finding()])],
        policy=PolicyEngine.from_file("config/autonomy.yaml"),
        progress=progress,
    ).compile()


# --- Semántica del puerto --------------------------------------------------------


def test_los_dos_adaptadores_son_progress_port() -> None:
    assert isinstance(NullProgress(), ProgressPort)
    assert isinstance(_redis_progress(), ProgressPort)


def test_redis_anota_el_nodo_en_curso_y_su_sello() -> None:
    progress = _redis_progress()
    at = datetime(2026, 10, 16, 15, 0, 0, tzinfo=UTC)
    progress.start_node("repo:1:sha", "scope", at=at)
    stored = progress._client.hgetall("reviews:active:repo:1:sha")
    assert json.loads(stored["scope"]) == {
        "node": "scope",
        "started_at": "2026-10-16T15:00:00+00:00",
        "branches": 1,
    }


def test_fan_out_cuenta_ramas_sin_mover_el_sello() -> None:
    """El sello del nodo es el de la **primera** rama; el conteo sube por rama."""
    progress = _redis_progress()
    first = datetime(2026, 10, 16, 15, 0, 0, tzinfo=UTC)
    second = datetime(2026, 10, 16, 15, 0, 9, tzinfo=UTC)
    progress.start_node("k", "persona", at=first)
    progress.start_node("k", "persona", at=second)
    raw = progress._client.hget("reviews:active:k", "persona")
    assert raw is not None
    assert '"started_at":"2026-10-16T15:00:00+00:00"' in raw
    assert '"branches":2' in raw


def test_finish_borra_el_campo_solo_al_cerrar_la_ultima_rama() -> None:
    progress = _redis_progress()
    progress.start_node("k", "persona")
    progress.start_node("k", "persona")
    progress.finish_node("k", "persona")
    # Una rama sigue en curso: el nodo permanece, con una rama menos.
    raw = progress._client.hget("reviews:active:k", "persona")
    assert raw is not None and '"branches":1' in raw
    progress.finish_node("k", "persona")
    # Última rama: no queda un contador zombi a 0.
    assert progress._client.hget("reviews:active:k", "persona") is None
    assert progress._client.exists("reviews:active:k") == 0


def test_clear_borra_toda_la_marca_del_hilo() -> None:
    progress = _redis_progress()
    progress.start_node("k", "scope")
    progress.start_node("k", "merge")
    progress.clear("k")
    assert progress._client.exists("reviews:active:k") == 0


def test_la_marca_tiene_ttl() -> None:
    progress = _redis_progress(ttl_s=42)
    progress.start_node("k", "scope")
    assert 0 < progress._client.ttl("reviews:active:k") <= 42


def test_redis_caido_no_propaga_el_fallo() -> None:
    """El progreso es best-effort: un Redis caído no tumba la review (criterio 8)."""
    progress = _redis_progress()
    progress._client.close()
    # No debe lanzar: el adaptador registra y sigue.
    progress.start_node("k", "scope")
    progress.finish_node("k", "scope")
    progress.clear("k")


def test_helpers_nunca_propagan_un_fallo_del_puerto() -> None:
    class Explosive:
        def start_node(self, review_key: str, node: str, *, at: datetime | None = None) -> None:
            raise RuntimeError("boom")

        def finish_node(self, review_key: str, node: str) -> None:
            raise RuntimeError("boom")

        def clear(self, review_key: str) -> None:
            raise RuntimeError("boom")

    start_node(Explosive(), "k", "scope")
    finish_node(Explosive(), "k", "scope")
    clear(Explosive(), "k")


def test_helpers_saltan_sin_puerto_o_sin_clave() -> None:
    start_node(None, "k", "scope")
    start_node(NullProgress(), "", "scope")
    finish_node(None, "k", "scope")
    clear(None, "k")


def test_build_progress_elige_por_configuracion() -> None:
    """OP5: elección por configuración, como `build_chat` (W2)."""
    from agent_review.settings import Settings
    from agent_review.worker.main import build_progress

    assert isinstance(build_progress(Settings(progress_enabled=False)), NullProgress)
    disabled = build_progress(Settings(progress_enabled=False, redis_url=""))
    assert isinstance(disabled, NullProgress)
    enabled = build_progress(Settings(progress_enabled=True, redis_url="redis://localhost:6379/0"))
    assert isinstance(enabled, RedisProgress)


# --- Envoltura de nodo (R1) ------------------------------------------------------


def test_la_envoltura_preserva_la_firma_para_inyectar_runtime() -> None:
    """`functools.wraps` copia la firma: LangGraph sigue inyectando el `Runtime`."""

    def persona(state: dict[str, Any], runtime: Runtime[None]) -> dict[str, Any]:
        return state

    original = inspect.signature(persona)
    wrapped = inspect.signature(node_wrapper("persona", persona, progress=NullProgress()))
    assert list(wrapped.parameters) == list(original.parameters) == ["state", "runtime"]


def test_el_runtime_llega_al_nodo_envuelto() -> None:
    seen: dict[str, Any] = {}

    def persona(state: dict[str, Any], runtime: Runtime[None]) -> dict[str, Any]:
        info = runtime.execution_info
        seen["attempt"] = info.node_attempt if info is not None else None
        return state

    builder: StateGraph[dict[str, Any]] = StateGraph(dict[str, Any])
    builder.add_node("persona", node_wrapper("persona", persona, progress=NullProgress()))
    builder.add_edge(START, "persona")
    builder.add_edge("persona", END)
    builder.compile().invoke({})
    assert seen["attempt"] == 1


def test_la_retry_policy_sigue_aplicandose_al_nodo_envuelto() -> None:
    """El reintento sigue al nodo, no a la envoltura (riesgo R1 del plan)."""
    calls = {"n": 0}

    def flaky(state: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientError("boom")
        return {"ok": True}

    builder: StateGraph[dict[str, Any]] = StateGraph(dict[str, Any])
    builder.add_node(
        "flaky",
        node_wrapper("flaky", flaky, progress=NullProgress()),
        retry_policy=RetryPolicy(max_attempts=3, retry_on=is_transient),
    )
    builder.add_edge(START, "flaky")
    builder.add_edge("flaky", END)
    assert builder.compile().invoke({}) == {"ok": True}
    assert calls["n"] == 3


def test_sin_puerto_el_grafo_produce_lo_mismo() -> None:
    """OP9: el sistema sin panel se comporta igual que con `NullProgress` (W2)."""
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="7")
    payload = {"request": request, "findings": []}
    without = _stateful_graph(None).invoke(payload)
    with_null = _stateful_graph(NullProgress()).invoke(payload)
    assert without["merged"] == with_null["merged"]
    assert without["published"] == with_null["published"]


def test_la_envoltura_anota_y_limpia_cada_nodo_en_el_grafo_real() -> None:
    """Con un puerto real, el grafo deja el nodo en curso en el hash de progreso."""
    progress = _redis_progress()
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="7")
    _stateful_graph(progress).invoke({"request": request, "findings": []})
    # El hilo terminó: la envoltura cierra cada campo al salir de su nodo.
    assert progress._client.exists(f"reviews:active:{request.review_key}") == 0


def test_el_fan_out_anota_una_rama_por_persona() -> None:
    """Cada rama del fan-out es una marca en curso; se cierran todas."""
    recorded: list[tuple[str, str, str]] = []

    class Recording:
        def start_node(self, review_key: str, node: str, *, at: datetime | None = None) -> None:
            del review_key, at
            recorded.append(("start", node, ""))

        def finish_node(self, review_key: str, node: str) -> None:
            del review_key
            recorded.append(("finish", node, ""))

        def clear(self, review_key: str) -> None:
            del review_key

    graph = build_graph(
        vcs=FakeVcs(),
        reviewers=[StaticReviewer("correctness", []), StaticReviewer("security", [])],
        policy=PolicyEngine.from_file("config/autonomy.yaml"),
        progress=Recording(),
    ).compile()
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="7")
    graph.invoke({"request": request, "findings": []})
    persona_starts = [entry for entry in recorded if entry == ("start", "persona", "")]
    assert len(persona_starts) == 2, "una marca por rama del fan-out"
    # Toda marca abierta se cierra.
    for node in {entry[1] for entry in recorded}:
        assert recorded.count(("start", node, "")) == recorded.count(("finish", node, ""))


def test_review_key_se_lee_del_estado() -> None:
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc")
    assert _review_key_of({"request": request}) == request.review_key
    assert _review_key_of({"request": {"review_key": "k"}}) == "k"
    assert _review_key_of({}) == ""
    assert _review_key_of("no-es-un-estado") == ""


@pytest.mark.parametrize("node", ["scope", "persona", "merge", "hitl"])
def test_la_envoltura_no_traga_la_excepcion_del_nodo(node: str) -> None:
    """El `finally` cierra la marca pero deja pasar la excepción."""

    def boom(state: dict[str, Any]) -> dict[str, Any]:
        raise TransientError("boom")

    builder: StateGraph[dict[str, Any]] = StateGraph(dict[str, Any])
    builder.add_node(node, node_wrapper(node, boom, progress=NullProgress()))
    builder.add_edge(START, node)
    builder.add_edge(node, END)
    with pytest.raises(TransientError):
        builder.compile().invoke({})


def test_checkpoint_con_interrupt_atraviesa_la_envoltura() -> None:
    """Un `interrupt` (pausa) sale por el `finally` sin corromperse."""

    def hitl(state: dict[str, Any]) -> dict[str, Any]:
        value = interrupt({"q": "decide"})
        return {"answered": value}

    builder: StateGraph[dict[str, Any]] = StateGraph(dict[str, Any])
    builder.add_node("hitl", node_wrapper("hitl", hitl, progress=NullProgress()))
    builder.add_edge(START, "hitl")
    builder.add_edge("hitl", END)
    app = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t"}}
    paused = app.invoke({}, config=config)
    assert paused["__interrupt__"]
    resumed = app.invoke(Command(resume="si"), config=config)
    assert resumed["answered"] == "si"


# --- Limpieza al cerrar la pasada (OP8) ------------------------------------------


def test_consume_limpia_la_marca_al_terminar_la_pasada() -> None:
    """Completada o con fallo, la marca de la review no queda colgando (OP8)."""
    from agent_review.queue import InMemoryReviewQueue
    from agent_review.worker.main import _consume

    class FailingGraph:
        def invoke(self, state: Any) -> Any:
            del state
            raise RuntimeError("twg caido")

    progress = _redis_progress()
    request = ReviewRequest(repo="r", pr_id=1, head_sha="a", pipeline_id="7")
    progress.start_node(request.review_key, "scope")
    queue = InMemoryReviewQueue()
    _consume(queue, FailingGraph(), request, max_attempts=3, progress=progress)
    assert progress._client.exists(f"reviews:active:{request.review_key}") == 0
