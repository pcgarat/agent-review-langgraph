"""Histórico de reviews terminadas (021 RZ10): resumen, almacén y escritura del worker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import fakeredis
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from test_graph import FakeVcs, StaticReviewer, make_finding
from test_worker import _CountingReviewer, _escalating_finding, _hitl_graph

from agent_review.domain.history import ReviewEnd, ReviewSummary
from agent_review.domain.models import (
    ReviewCoverage,
    ReviewOutcome,
    ReviewRequest,
    SkipReason,
    ToolingSourceStatus,
)
from agent_review.graph.build import build_graph
from agent_review.history_store import InMemoryReviewHistory, RedisReviewHistory
from agent_review.policy.engine import PolicyEngine
from agent_review.queue import InMemoryReviewQueue
from agent_review.worker.main import _consume

_NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def _summary(key: str, *, days_ago: float = 0, repo: str = "r", pr_id: int = 1) -> ReviewSummary:
    return ReviewSummary(
        review_key=key,
        repo=repo,
        pr_id=pr_id,
        head_sha="abc",
        finished_at=_NOW - timedelta(days=days_ago),
        end=ReviewEnd.COMPLETE,
    )


# --- Resumen ---


def test_el_resumen_distingue_completa_parcial_e_incompleta() -> None:
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="7")
    complete = ReviewOutcome(request=request, findings=[make_finding()], tokens_spent={"c": 900})
    partial = ReviewOutcome(
        request=request,
        coverage=ReviewCoverage(
            reviewed=["a.py"],
            skipped={"b.py": SkipReason.BUDGET, "composer.lock": SkipReason.IGNORED},
        ),
    )
    incomplete = ReviewOutcome(
        request=request, unavailable_sources={"standards": ToolingSourceStatus.MISSING}
    )

    s = ReviewSummary.from_outcome(complete, finished_at=_NOW, reasoning=True)
    assert (s.end, s.findings, s.tokens, s.reasoning) == (ReviewEnd.COMPLETE, 1, 900, True)
    assert s.by_severity == {make_finding().severity.value: 1}

    s = ReviewSummary.from_outcome(partial, finished_at=_NOW, reasoning=True)
    # Lo ignorado no cuenta como «sin revisar»; sin tokens no hubo razonamiento.
    assert (s.end, s.files_reviewed, s.files_skipped, s.reasoning) == (
        ReviewEnd.PARTIAL,
        1,
        1,
        False,
    )

    assert (
        ReviewSummary.from_outcome(incomplete, finished_at=_NOW, reasoning=False).end
        is ReviewEnd.INCOMPLETE
    )


# --- Almacén ---


@pytest.fixture(params=["memoria", "redis"])
def history(request):
    if request.param == "memoria":
        return InMemoryReviewHistory()
    return RedisReviewHistory(url="", client=fakeredis.FakeRedis(decode_responses=True))


def test_lo_mas_reciente_primero_y_sin_duplicados(history) -> None:
    history.record(_summary("a", days_ago=2))
    history.record(_summary("b", days_ago=1))
    history.record(_summary("a", days_ago=0))  # reentrega: actualiza, no duplica
    assert [s.review_key for s in history.recent()] == ["a", "b"]


def test_filtra_por_repo_y_pr(history) -> None:
    history.record(_summary("a", repo="x", pr_id=1, days_ago=2))
    history.record(_summary("b", repo="x", pr_id=2, days_ago=1))
    history.record(_summary("c", repo="y", pr_id=1))
    assert [s.review_key for s in history.recent(repo="x")] == ["b", "a"]
    assert [s.review_key for s in history.recent(repo="x", pr_id=2)] == ["b"]
    assert [s.review_key for s in history.recent(limit=1)] == ["c"]


def test_la_poda_quita_lo_anterior_a_la_retencion(history) -> None:
    history.record(_summary("vieja", days_ago=40))
    history.record(_summary("nueva", days_ago=1))
    history.prune(older_than_days=30, now=_NOW)
    assert [s.review_key for s in history.recent()] == ["nueva"]


def test_redis_recorta_al_escribir_y_aguanta_una_caida() -> None:
    server = fakeredis.FakeServer()
    history = RedisReviewHistory(
        url="", retention_days=30, client=fakeredis.FakeRedis(server=server, decode_responses=True)
    )
    history.record(_summary("vieja", days_ago=40))
    history.record(_summary("nueva"))
    assert [s.review_key for s in history.recent()] == ["nueva"]
    server.connected = False
    history.record(_summary("otra"))  # no lanza
    assert history.recent() == []


# --- El worker anota al cerrar ---


def test_el_worker_anota_la_review_terminada_y_no_la_pausada() -> None:
    history = InMemoryReviewHistory()
    queue = InMemoryReviewQueue()

    report = PolicyEngine(
        {"version": 1, "max_autonomy": "report", "default_action": "report", "rules": []}
    )
    done = build_graph(
        vcs=FakeVcs(), reviewers=[StaticReviewer("standards", [make_finding()])], policy=report
    ).compile(checkpointer=InMemorySaver())
    finished = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="1")
    _consume(queue, done, finished, max_attempts=3, history=history, reasoning=False)

    paused_graph = _hitl_graph(FakeVcs(), _CountingReviewer(_escalating_finding()))
    waiting = ReviewRequest(repo="r", pr_id=2, head_sha="def", pipeline_id="1")
    _consume(queue, paused_graph, waiting, max_attempts=3, history=history)

    [summary] = history.recent()
    assert summary.review_key == finished.review_key
    assert summary.findings == 1
