"""Regresiones de los hallazgos de la code review de `fix/revision-main`.

Un test (o grupo) por hallazgo, en el orden del informe. Los fakes se reutilizan
de `test_worker` para no duplicarlos.
"""

from __future__ import annotations

import shlex
from datetime import UTC, datetime, timedelta
from itertools import permutations
from typing import Any

import httpx
import pytest
from test_worker import FakeVcs, _CountingReviewer, _escalating_finding, _hitl_graph

from agent_review.adapters.openai_llm import OpenAiCompatibleLlm
from agent_review.api.main import _release_quietly
from agent_review.checkpointer import (
    build_checkpointer,
    latest_checkpoints,
    paused_thread_ids,
    prune_completed_threads,
)
from agent_review.domain.models import Finding, ReviewRequest
from agent_review.errors import is_transient
from agent_review.graph.build import _merge_findings
from agent_review.idempotency import InMemoryIdempotencyStore
from agent_review.paused_store import (
    EXPIRED_REQUEUE_AFTER_S,
    MAX_EXPIRED_ENQUEUES,
    InMemoryPausedStore,
)
from agent_review.ports.llm import LlmError, LlmTransientError
from agent_review.queue import InMemoryReviewQueue
from agent_review.settings import Settings
from agent_review.worker.main import (
    node_retry_policy,
    process_request,
    reconcile_paused,
    sweep_expired,
)

REQUEST = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef")


# --- 1. node_retry_max_attempts=1 desactiva el reintento --------------------


def test_un_intento_desactiva_el_reintento_de_nodo() -> None:
    policy = node_retry_policy(Settings(node_retry_max_attempts=1))
    # Una politica explicita: `None` haria que `build_graph` usara el default (3).
    assert policy is not None
    assert policy.max_attempts == 1


# --- 2. Pasada degradada y completa pausadas a la vez -----------------------


def test_pausa_completa_desplaza_a_la_degradada_y_su_hilo_se_borra() -> None:
    graph = _hitl_graph(FakeVcs(), _CountingReviewer(_escalating_finding()))
    store = InMemoryPausedStore()
    partial = REQUEST.model_copy(update={"degraded": True})

    assert process_request(graph, partial, paused=store).paused
    assert process_request(graph, REQUEST, paused=store).paused
    entry = store.get(REQUEST.review_key)
    assert entry is not None and entry.request.thread_id == REQUEST.thread_id

    assert reconcile_paused(graph, store) == []
    assert paused_thread_ids(graph.checkpointer) == [REQUEST.thread_id], "el :partial no se fuga"


def test_pausa_degradada_tardia_no_pisa_la_completa() -> None:
    graph = _hitl_graph(FakeVcs(), _CountingReviewer(_escalating_finding()))
    store = InMemoryPausedStore()

    assert process_request(graph, REQUEST, paused=store).paused
    assert process_request(graph, REQUEST.model_copy(update={"degraded": True}), paused=store)
    entry = store.get(REQUEST.review_key)
    assert entry is not None and entry.request.thread_id == REQUEST.thread_id


# --- 3. Reanudacion `expired` perdida ----------------------------------------


def test_expired_perdido_se_reencola_tras_el_plazo_con_tope() -> None:
    graph = _hitl_graph(FakeVcs(), _CountingReviewer(_escalating_finding()))
    store = InMemoryPausedStore(ttl_s=60.0)
    queue = InMemoryReviewQueue()
    assert process_request(graph, REQUEST, paused=store).paused

    now = datetime.now(UTC) + timedelta(seconds=120)
    assert len(sweep_expired(store, queue, now=now)) == 1
    assert queue.dequeue() is not None  # el job se "pierde": nadie lo procesa
    assert sweep_expired(store, queue, now=now) == [], "en vuelo: no se duplica"

    enqueued = 1
    for _ in range(MAX_EXPIRED_ENQUEUES + 2):
        now += timedelta(seconds=EXPIRED_REQUEUE_AFTER_S)
        enqueued += len(sweep_expired(store, queue, now=now))
    assert enqueued == MAX_EXPIRED_ENQUEUES, "se reencola, pero acotado"


# --- 4. La compensacion de la API no enmascara el error ----------------------


def test_release_quietly_libera_todas_aunque_una_falle() -> None:
    class _Flaky(InMemoryIdempotencyStore):
        released: list[str] = []

        def release(self, key: str) -> None:
            if key == "review":
                raise ConnectionError("redis caido")
            self.released.append(key)

    store = _Flaky()
    _release_quietly(store, "review", "ingest")
    assert store.released == ["ingest"]


# --- 5. Clasificacion de los errores del LLM ---------------------------------


@pytest.mark.parametrize(
    ("status", "transient"), [(500, True), (503, True), (429, True), (401, False), (400, False)]
)
def test_error_http_del_llm_se_clasifica(status: int, transient: bool) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status)))
    llm = OpenAiCompatibleLlm(api_key="k", model="m", client=client)
    with pytest.raises(LlmError) as excinfo:
        llm.complete(system="s", prompt="p", max_tokens=1)
    assert isinstance(excinfo.value, LlmTransientError) is transient
    assert is_transient(excinfo.value) is transient


def test_respuesta_malformada_del_llm_no_es_transitoria() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})))
    llm = OpenAiCompatibleLlm(api_key="k", model="m", client=client)
    with pytest.raises(LlmError) as excinfo:
        llm.complete(system="s", prompt="p", max_tokens=1)
    assert not is_transient(excinfo.value)


# --- 6. El pool se cierra al caer a memoria ----------------------------------


def test_fallback_a_memoria_cierra_el_pool(monkeypatch) -> None:
    import psycopg_pool

    closed: list[bool] = []

    class _DeadPool(psycopg_pool.ConnectionPool):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def wait(self, timeout: float) -> None:
            raise psycopg_pool.PoolTimeout("postgres inalcanzable")

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(psycopg_pool, "ConnectionPool", _DeadPool)
    saver = build_checkpointer("postgresql://nadie@127.0.0.1:1/x", required=False)
    assert type(saver).__name__ == "InMemorySaver"
    assert closed == [True]


# --- 7. Un resume ilegible no atasca el hilo ---------------------------------


def test_resume_ilegible_vuelve_a_pausar() -> None:
    from langgraph.types import Command

    from agent_review.domain.models import DecisionOutcome, FindingDecision, HitlDecision

    vcs = FakeVcs()
    graph = _hitl_graph(vcs, _CountingReviewer(_escalating_finding()))
    assert process_request(graph, REQUEST).paused
    config = {"configurable": {"thread_id": REQUEST.thread_id}}

    graph.invoke(Command(resume={"basura": True}), config=config)
    assert graph.get_state(config).interrupts, "sigue pausado, no lanza"

    decision = HitlDecision(
        review_key=REQUEST.review_key,
        actor="alice",
        items=[FindingDecision(finding_id="w1", outcome=DecisionOutcome.APPROVED)],
    )
    outcome = process_request(graph, REQUEST.model_copy(update={"decision": decision}))
    assert not outcome.paused
    assert len(vcs.inlines) == 1


# --- 8. Un solo listado del saver por ciclo ----------------------------------


def test_reconcile_y_poda_comparten_un_listado() -> None:
    graph = _hitl_graph(FakeVcs(), _CountingReviewer(_escalating_finding()))
    assert process_request(graph, REQUEST).paused
    saver = graph.checkpointer
    calls = 0
    original = saver.list

    def counting_list(*args: Any, **kwargs: Any):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    saver.list = counting_list  # type: ignore[method-assign]
    store = InMemoryPausedStore()
    latest = latest_checkpoints(saver)
    assert latest is not None
    reconcile_paused(graph, store, thread_ids=paused_thread_ids(saver, latest=latest))
    prune_completed_threads(saver, older_than_days=1, latest=latest)
    assert calls == 1


# --- 9. La pista del comando se puede pegar en la shell ----------------------


def test_pista_cita_los_ids_con_espacios() -> None:
    finding = _escalating_finding().model_copy(update={"id": "F1 (null deref)"})
    graph = _hitl_graph(FakeVcs(), _CountingReviewer(finding))
    store = InMemoryPausedStore()
    assert process_request(graph, REQUEST, paused=store).paused
    entry = store.get(REQUEST.review_key)
    assert entry is not None

    argv = shlex.split(entry.approval.command_hint)
    assert argv[argv.index("--approve") + 1] == "F1 (null deref)"


# --- 10. El merge no depende del orden del fan-out ---------------------------


def _finding(fid: str, line: int, evidence: int = 1) -> Finding:
    return _escalating_finding().model_copy(
        update={
            "id": fid,
            "file": "a.php",
            "title": "Null deref",
            "line": line,
            "evidence": [f"a.php:{line} x"] * evidence,
        }
    )


def test_merge_es_determinista_y_no_fusiona_la_misma_persona() -> None:
    a10, a14, b12 = _finding("a10", 10), _finding("a14", 14), _finding("b12", 12, evidence=3)
    sources = {"a10": "A", "a14": "A", "b12": "B"}

    results = {
        tuple(sorted(f.id for f in _merge_findings(list(order), sources)))
        for order in permutations([a10, a14, b12])
    }

    assert len(results) == 1, "el mismo resultado en cualquier orden"
    (merged,) = results
    # B absorbe uno de los de A, pero los dos de A nunca acaban juntos.
    assert len(merged) == 2
    assert "b12" in merged
