"""Regresiones de los fallos B1–B10 de `docs/revision-main-20261011.md`.

Cada test reproduce el escenario del documento contra el codigo arreglado. Los
fakes se reutilizan de `test_worker` para no duplicarlos.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_worker import FakeVcs, _CountingReviewer, _escalating_finding, _hitl_graph

from reviews.domain.models import (
    DecisionOutcome,
    FindingDecision,
    HitlDecision,
    ReviewerResult,
    ReviewRequest,
    ToolingSourceStatus,
)
from reviews.ports.vcs import ReviewMarker
from reviews.queue import InMemoryReviewQueue
from reviews.worker.main import _consume, process_request

REPO = "cqm/emotivoo-v2"


def _decision(request: ReviewRequest, outcome: DecisionOutcome) -> HitlDecision:
    return HitlDecision(
        review_key=request.review_key,
        actor="alice",
        items=[FindingDecision(finding_id="w1", outcome=outcome)],
    )


class _AlwaysFailingGraph:
    checkpointer = None

    def invoke(self, state, config=None):
        del state, config
        raise RuntimeError("twg caido")


# --- B1 ---------------------------------------------------------------------


def test_b1_dlq_de_un_job_con_decision_no_tumba_el_worker() -> None:
    # `decided_at` es un datetime: antes `json.dumps` lanzaba TypeError dentro del
    # `except` de `_consume` y el worker moria con el job ya fuera de la cola.
    queue = InMemoryReviewQueue()
    request = ReviewRequest(repo=REPO, pr_id=9, head_sha="deadbeef", attempts=2)
    request = request.model_copy(update={"decision": _decision(request, DecisionOutcome.EXPIRED)})

    _consume(queue, _AlwaysFailingGraph(), request, max_attempts=3)

    dead = queue.dead_letters()
    assert len(dead) == 1
    stored = dead[0]["request"]
    assert isinstance(stored, dict)
    assert ReviewRequest.model_validate(stored).decision is not None, "la DLQ es reinyectable"


def test_b1_un_fallo_de_la_propia_dlq_no_escapa_de_consume() -> None:
    class _BrokenQueue(InMemoryReviewQueue):
        def dead_letter(self, request: ReviewRequest, *, reason: str) -> None:
            raise ConnectionError("redis caido")

    request = ReviewRequest(repo=REPO, pr_id=9, head_sha="deadbeef", attempts=2)
    # No debe lanzar: el manejo del error nunca tumba el bucle del worker.
    _consume(_BrokenQueue(), _AlwaysFailingGraph(), request, max_attempts=3)


# --- B2 ---------------------------------------------------------------------


class _MarkersVcs(FakeVcs):
    """Como Bitbucket: lo ya publicado es visible al volver a `publish`."""

    def list_review_markers(self, repo: str, pr_id: int) -> list[ReviewMarker]:
        del repo, pr_id
        return [ReviewMarker(id="", body=body) for body in [*self.inlines, *self.comments]]


class _FlakySummaryVcs(_MarkersVcs):
    """Falla al publicar el resumen las primeras `fail_times` veces."""

    def __init__(self, fail_times: int = 1) -> None:
        super().__init__()
        self._fail = fail_times

    def post_comment(self, repo: str, pr_id: int, body: str) -> None:
        if self._fail > 0:
            self._fail -= 1
            raise RuntimeError("bitbucket 500")
        super().post_comment(repo, pr_id, body)


def _state(graph, request: ReviewRequest) -> dict:
    return graph.get_state({"configurable": {"thread_id": request.thread_id}}).values


def test_b2_reintento_tras_fallo_en_publish_continua_desde_el_checkpoint() -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    from reviews.graph.build import build_graph
    from reviews.policy.engine import PolicyEngine

    reviewer = _CountingReviewer(_escalating_finding())
    vcs = _FlakySummaryVcs()
    graph = build_graph(
        vcs=vcs, reviewers=[reviewer], policy=PolicyEngine.from_file("config/autonomy.yaml")
    ).compile(checkpointer=InMemorySaver())
    request = ReviewRequest(repo=REPO, pr_id=9, head_sha="deadbeef")

    with pytest.raises(RuntimeError):
        process_request(graph, request)
    outcome = process_request(graph, request.model_copy(update={"attempts": 1}))

    assert reviewer.calls == 1, "el reintento no vuelve a pagar las personas"
    assert len(_state(graph, request)["findings"]) == 1, "los hallazgos no se acumulan"
    assert len(outcome.findings) == 1
    assert len(vcs.comments) == 1 and len(vcs.inlines) == 1


def test_b2_reintento_de_un_resume_no_publica_hallazgos_sin_decidir() -> None:
    # Si el resume fallaba, el reintento arrancaba desde START, recalculaba los
    # hallazgos y los publicaba con la decision anterior sin volver a pausar.
    reviewer = _CountingReviewer(_escalating_finding())
    vcs = _FlakySummaryVcs()
    graph = _hitl_graph(vcs, reviewer)
    request = ReviewRequest(repo=REPO, pr_id=9, head_sha="deadbeef")
    assert process_request(graph, request).paused

    resume = request.model_copy(update={"decision": _decision(request, DecisionOutcome.APPROVED)})
    with pytest.raises(RuntimeError):
        process_request(graph, resume)
    outcome = process_request(graph, resume.model_copy(update={"attempts": 1}))

    assert not outcome.paused
    assert reviewer.calls == 1, "las personas no se re-ejecutan tras la decision"
    assert len(_state(graph, request)["findings"]) == 1
    assert len(vcs.comments) == 1


# --- B9 ---------------------------------------------------------------------


class _FlakyChat:
    def __init__(self, exc: Exception, fail_times: int = 1) -> None:
        self._exc = exc
        self._fail = fail_times
        self.notices: list[str] = []

    def notify(self, *, title: str, body: str, link: str | None = None) -> None:
        del body, link
        if self._fail > 0:
            self._fail -= 1
            raise self._exc
        self.notices.append(title)

    def notify_decision_pending(self, *, title: str, body: str, link: str | None = None) -> None:
        del title, body, link


def _graph_with_chat(vcs: FakeVcs, chat: _FlakyChat):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import RetryPolicy
    from test_worker import OneShotReviewer

    from reviews.graph.build import build_graph
    from reviews.policy.engine import PolicyEngine

    return build_graph(
        vcs=vcs,
        reviewers=[OneShotReviewer()],
        policy=PolicyEngine.from_file("config/autonomy.yaml"),
        chat=chat,
        # Sin reintento de nodo: se ejercita el reintento del job.
        retry_policy=RetryPolicy(max_attempts=1),
    ).compile(checkpointer=InMemorySaver())


def test_b9_fallo_transitorio_de_teams_no_repite_publish_y_el_aviso_llega() -> None:
    vcs = FakeVcs()
    chat = _FlakyChat(ConnectionError("teams caido"))
    graph = _graph_with_chat(vcs, chat)
    request = ReviewRequest(repo=REPO, pr_id=9, head_sha="deadbeef")

    with pytest.raises(ConnectionError):
        process_request(graph, request)
    process_request(graph, request.model_copy(update={"attempts": 1}))

    assert len(vcs.comments) == 1, "el resumen se publico una sola vez"
    assert chat.notices == [f"Review de {REPO}#9"], "el reintento retoma el aviso"


def test_b9_fallo_no_transitorio_de_teams_no_tumba_la_review() -> None:
    vcs = FakeVcs()
    chat = _FlakyChat(ValueError("payload de tarjeta invalido"))
    graph = _graph_with_chat(vcs, chat)

    outcome = process_request(graph, ReviewRequest(repo=REPO, pr_id=9, head_sha="deadbeef"))

    assert outcome.complete
    assert len(vcs.comments) == 1


# --- B3 ---------------------------------------------------------------------


def _drain(queue: InMemoryReviewQueue, graph, paused) -> None:
    while (job := queue.dequeue()) is not None:
        _consume(queue, graph, job, max_attempts=3, paused=paused)


def test_b3_pausa_vencida_cuyo_resume_falla_acaba_en_la_dlq_una_vez() -> None:
    from datetime import UTC, datetime, timedelta

    from reviews.paused_store import InMemoryPausedStore
    from reviews.worker.main import sweep_expired

    vcs = _FlakySummaryVcs(fail_times=10**6)
    graph = _hitl_graph(vcs, _CountingReviewer(_escalating_finding()))
    paused = InMemoryPausedStore(ttl_s=60.0)
    queue = InMemoryReviewQueue()
    request = ReviewRequest(repo=REPO, pr_id=9, head_sha="deadbeef")
    assert process_request(graph, request, paused=paused).paused

    later = datetime.now(UTC) + timedelta(seconds=120)
    for _ in range(3):
        sweep_expired(paused, queue, now=later)
        _drain(queue, graph, paused)

    assert len(queue.dead_letters()) == 1, "una sola entrada en la DLQ, no una por barrido"
    assert paused.get(request.review_key) is not None, "sigue visible en el inbox"


def test_b3_rama_ya_completada_limpia_el_inbox() -> None:
    from reviews.paused_store import InMemoryPausedStore

    vcs = FakeVcs()
    graph = _hitl_graph(vcs, _CountingReviewer(_escalating_finding()))
    paused = InMemoryPausedStore()
    request = ReviewRequest(repo=REPO, pr_id=9, head_sha="deadbeef")
    process_request(graph, request, paused=paused)
    resume = request.model_copy(update={"decision": _decision(request, DecisionOutcome.APPROVED)})
    # El worker muere entre `invoke` y la limpieza del inbox: se simula
    # reanudando sin inbox.
    process_request(graph, resume)
    assert paused.get(request.review_key) is not None

    process_request(graph, resume, paused=paused)

    assert paused.get(request.review_key) is None


# --- B4 ---------------------------------------------------------------------


class _ToolingReviewer:
    """Persona estatica cuyo artefacto falta en la primera pasada y llega despues."""

    name = "standards"

    def __init__(self) -> None:
        self.status = ToolingSourceStatus.MISSING

    def review(self, *, repo: str, pr_id: int, diff: str, head_sha: str = "") -> ReviewerResult:
        del repo, pr_id, diff, head_sha
        findings = [] if self.status is ToolingSourceStatus.MISSING else [_escalating_finding()]
        return ReviewerResult(findings=findings, tooling_status=self.status)


def test_b4_artefacto_tardio_en_la_misma_ejecucion_publica_la_review_completa(
    tmp_path: Path,
) -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    from reviews.domain.pending import InMemoryPendingStore
    from reviews.graph.build import build_graph
    from reviews.policy.engine import PolicyEngine
    from reviews.settings import Settings
    from reviews.worker.main import sweep_pending

    vcs = _MarkersVcs()
    reviewer = _ToolingReviewer()
    graph = build_graph(
        vcs=vcs, reviewers=[reviewer], policy=PolicyEngine.from_file("config/autonomy.yaml")
    ).compile(checkpointer=InMemorySaver())
    request = ReviewRequest(repo=REPO, pr_id=9, head_sha="deadbeef", pipeline_id="7")

    # 1. Vence la espera: el barrido encola la pasada degradada.
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"repo: {REPO}\n", encoding="utf-8")
    pending = InMemoryPendingStore(expected_tools=["phpstan", "deptrac"])
    pending.observe(request, "deptrac", "{}", ttl_ready_s=0)
    queue = InMemoryReviewQueue()
    sweep_pending(Settings(repo_profile_config=str(profile)), queue, pending=pending)
    degraded = queue.dequeue()
    assert degraded is not None and degraded.degraded

    partial = process_request(graph, degraded)
    assert not partial.complete
    assert len(vcs.comments) == 1

    # 2. Llega el artefacto: la API encola la misma ejecucion, ya sin degradar.
    reviewer.status = ToolingSourceStatus.PRESENT
    full = process_request(graph, request)

    assert full.complete, "la pasada completa no reutiliza el hilo degradado"
    assert len(vcs.comments) == 2, "se publica el resumen completo tras el parcial"
    assert full.request.review_key == partial.request.review_key


# --- B5 ---------------------------------------------------------------------


class _SameTitleReviewer:
    """Dos errores de PHPStan con el mismo mensaje en lineas distintas."""

    name = "standards"

    def __init__(self, *, with_ids: bool = True) -> None:
        self._with_ids = with_ids

    def review(self, *, repo: str, pr_id: int, diff: str, head_sha: str = "") -> ReviewerResult:
        del repo, pr_id, diff, head_sha
        return ReviewerResult(
            findings=[
                _escalating_finding().model_copy(
                    update={
                        "id": f"s-{line}" if self._with_ids else "",
                        "title": "Call to an undefined method Foo::bar()",
                        "line": line,
                        "evidence": [f"a.php:{line} $foo->bar()"],
                    }
                )
                for line in (1, 40)
            ]
        )


@pytest.mark.parametrize("with_ids", [True, False])
def test_b5_mismo_titulo_en_lineas_distintas_no_se_fusiona(with_ids: bool) -> None:
    from reviews.graph.build import build_graph
    from reviews.policy.engine import PolicyEngine

    vcs = _MarkersVcs()
    graph = build_graph(
        vcs=vcs,
        reviewers=[_SameTitleReviewer(with_ids=with_ids)],
        policy=PolicyEngine.from_file("config/autonomy.yaml"),
    ).compile()

    outcome = process_request(graph, ReviewRequest(repo=REPO, pr_id=9, head_sha="deadbeef"))

    assert len(outcome.findings) == 2
    assert len(vcs.inlines) == 2, "cada linea lleva su inline (y su propia marca)"


# --- B8 ---------------------------------------------------------------------


def test_b8_check_twg_no_se_cuelga(monkeypatch) -> None:
    import subprocess

    from reviews.adapters.twg_vcs import TwgError, check_twg

    captured: dict[str, object] = {}

    def _hang(cmd, **kwargs):
        captured.update(kwargs)
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", _hang)

    with pytest.raises(TwgError, match="excedio"):
        check_twg("twg", timeout_s=2.0)
    assert captured["timeout"] == 2.0
    assert check_twg("twg", required=False, timeout_s=2.0) is None


# --- B10 --------------------------------------------------------------------


def test_b10_aviso_de_pausa_lleva_el_enlace_al_pr() -> None:
    # Antes `_pr_url` buscaba el VCS en el estado del grafo, donde nunca esta:
    # la tarjeta de Teams salia siempre sin el boton «Ver el PR».
    from test_worker import _RecordingChat

    vcs = FakeVcs()
    chat = _RecordingChat()
    graph = _hitl_graph(vcs, _CountingReviewer(_escalating_finding()))
    request = ReviewRequest(repo=REPO, pr_id=9, head_sha="deadbeef")

    assert process_request(graph, request, chat=chat, vcs=vcs).paused

    assert len(chat.pending) == 1
    _title, _body, link = chat.pending[0]
    assert link == "https://example.test/pr"
