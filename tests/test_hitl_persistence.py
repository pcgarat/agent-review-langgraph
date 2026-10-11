from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from test_worker import FakeVcs, _CountingReviewer, _escalating_finding, _hitl_graph, _RecordingChat

from reviews.checkpointer import build_checkpointer, paused_thread_ids
from reviews.decision import DecisionError, build_decision
from reviews.domain.models import (
    DecisionOutcome,
    FindingDecision,
    HitlDecision,
    ReviewRequest,
)
from reviews.paused_store import InMemoryPausedStore
from reviews.worker.main import process_request, reconcile_paused

REQUEST = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef")


def _decision(*items: FindingDecision, actor: str = "alice") -> HitlDecision:
    return HitlDecision(review_key=REQUEST.review_key, actor=actor, items=list(items))


def _approve(finding_id: str = "w1") -> FindingDecision:
    return FindingDecision(finding_id=finding_id, outcome=DecisionOutcome.APPROVED)


def _paused_graph(vcs: FakeVcs | None = None, store: InMemoryPausedStore | None = None):
    reviewer = _CountingReviewer(_escalating_finding())
    graph = _hitl_graph(vcs or FakeVcs(), reviewer)
    assert process_request(graph, REQUEST, paused=store).paused
    return graph, reviewer


# --- La decision debe cubrir todos los pendientes ---------------------------


def test_build_decision_parcial_falla() -> None:
    store = InMemoryPausedStore()
    _paused_graph(store=store)
    paused = store.get(REQUEST.review_key)
    assert paused is not None
    # Se anade un segundo pendiente para que la decision de uno solo sea parcial.
    second = paused.approval.pending[0].model_copy(update={"finding_id": "w2"})
    paused.approval.pending.append(second)

    with pytest.raises(DecisionError, match="Faltan.*w2"):
        build_decision(paused, actor="alice", items=[_approve()], reviewed_key=REQUEST.review_key)


def test_pista_del_aviso_nombra_todos_los_pendientes() -> None:
    store = InMemoryPausedStore()
    _paused_graph(store=store)
    paused = store.get(REQUEST.review_key)
    assert paused is not None
    assert "--approve w1" in paused.approval.command_hint
    assert "<finding_id>" not in paused.approval.command_hint


def test_hitl_vuelve_a_pausar_ante_una_decision_incompleta() -> None:
    vcs = FakeVcs()
    store = InMemoryPausedStore()
    graph, _ = _paused_graph(vcs, store)
    original = store.get(REQUEST.review_key)

    outcome = process_request(
        graph, REQUEST.model_copy(update={"decision": _decision()}), paused=store
    )

    assert outcome.paused
    assert vcs.inlines == [] and vcs.comments == [], "no se publica nada"
    assert store.get(REQUEST.review_key) == original, "el inbox conserva la pausa y su TTL"
    # La poda y la reconciliacion lo siguen viendo pausado.
    assert paused_thread_ids(graph.checkpointer) == [REQUEST.thread_id]
    # Una decision completa responde al nuevo `interrupt` (no al valor viejo).
    outcome = process_request(graph, REQUEST.model_copy(update={"decision": _decision(_approve())}))
    assert not outcome.paused
    assert len(vcs.inlines) == 1


# --- Pausas que el inbox perdio ----------------------------------------------


def test_reentrega_repone_la_pausa_perdida_con_su_fecha_real() -> None:
    graph, reviewer = _paused_graph()  # pauso sin inbox: el registro se "perdio"
    store = InMemoryPausedStore()
    chat = _RecordingChat()

    outcome = process_request(graph, REQUEST, paused=store, chat=chat, vcs=FakeVcs())

    assert outcome.paused
    assert reviewer.calls == 1, "reponer no re-ejecuta las personas"
    restored = store.get(REQUEST.review_key)
    assert restored is not None
    # El TTL cuenta desde el checkpoint de la pausa, no desde que se repone.
    snapshot = graph.get_state({"configurable": {"thread_id": REQUEST.thread_id}})
    assert restored.created_at == datetime.fromisoformat(snapshot.created_at)
    assert len(chat.pending) == 1


def test_reentrega_no_duplica_la_pausa_si_ya_esta_en_el_inbox() -> None:
    store = InMemoryPausedStore()
    graph, _ = _paused_graph(store=store)
    original = store.get(REQUEST.review_key)
    chat = _RecordingChat()

    process_request(graph, REQUEST, paused=store, chat=chat)

    assert store.get(REQUEST.review_key) == original
    assert chat.pending == [], "no se re-avisa"


def test_reconcile_repone_los_hilos_pausados_fuera_del_inbox() -> None:
    graph, _ = _paused_graph()
    store = InMemoryPausedStore()

    assert paused_thread_ids(graph.checkpointer) == [REQUEST.thread_id]
    assert reconcile_paused(graph, store) == [REQUEST.thread_id]
    assert store.get(REQUEST.review_key) is not None
    assert reconcile_paused(graph, store) == [], "idempotente"


def test_reconcile_ignora_los_hilos_completados() -> None:
    graph, _ = _paused_graph()
    process_request(graph, REQUEST.model_copy(update={"decision": _decision(_approve())}))
    store = InMemoryPausedStore()

    assert paused_thread_ids(graph.checkpointer) == []
    assert reconcile_paused(graph, store) == []


def test_pausa_repuesta_vencida_la_barre_el_ttl() -> None:
    graph, _ = _paused_graph()
    store = InMemoryPausedStore(ttl_s=60.0)
    reconcile_paused(graph, store)

    later = datetime.now(UTC) + timedelta(minutes=5)
    assert [r.review_key for r in store.sweep(now=later)] == [REQUEST.review_key]


# --- Aviso de pausa con enlace al PR ------------------------------------------


def test_aviso_de_pausa_lleva_el_enlace_del_pr() -> None:
    vcs = FakeVcs()
    chat = _RecordingChat()
    graph = _hitl_graph(vcs, _CountingReviewer(_escalating_finding()))

    process_request(graph, REQUEST, paused=InMemoryPausedStore(), chat=chat, vcs=vcs)

    assert chat.pending[0][2] == "https://example.test/pr"


# --- Lectura del hilo y checkpointer de produccion ----------------------------


def test_fallo_al_leer_el_hilo_no_lo_trata_como_nuevo(monkeypatch) -> None:
    graph, reviewer = _paused_graph()

    def boom(config):
        raise ConnectionError("postgres caido")

    monkeypatch.setattr(graph, "get_state", boom)
    with pytest.raises(ConnectionError):
        process_request(graph, REQUEST)
    assert reviewer.calls == 1, "no se re-ejecuta desde START un hilo quiza pausado"


def test_checkpointer_obligatorio_sin_url_falla() -> None:
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        build_checkpointer("", required=True)


def test_checkpointer_obligatorio_no_cae_a_memoria() -> None:
    with pytest.raises(Exception):  # noqa: B017 - el tipo depende del driver
        build_checkpointer(
            "postgresql://x:y@127.0.0.1:1/nada?connect_timeout=1",
            required=True,
            connect_timeout_s=1.0,
        )


# --- Decisiones que llegan tarde ----------------------------------------------


def test_decision_tardia_se_registra_al_descartarse(caplog) -> None:
    graph, _ = _paused_graph()
    expired = FindingDecision(finding_id="w1", outcome=DecisionOutcome.EXPIRED)
    process_request(
        graph, REQUEST.model_copy(update={"decision": _decision(expired, actor="system:ttl")})
    )

    with caplog.at_level(logging.WARNING):
        process_request(graph, REQUEST.model_copy(update={"decision": _decision(_approve())}))

    assert "Decision de alice" in caplog.text and "descartada" in caplog.text
