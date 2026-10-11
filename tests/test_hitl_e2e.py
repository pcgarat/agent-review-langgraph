from __future__ import annotations

from reviews import cli
from reviews.domain.models import (
    AutofixClass,
    Finding,
    Owner,
    ReviewerResult,
    ReviewRequest,
    Severity,
)
from reviews.graph.build import build_graph
from reviews.paused_store import InMemoryPausedStore
from reviews.policy.engine import PolicyEngine
from reviews.ports.vcs import InlineAnchor, PullRequestInfo, ReviewMarker
from reviews.queue import InMemoryReviewQueue
from reviews.worker.main import process_request


class _Vcs:
    """VCS que recuerda lo publicado y lo devuelve como marcas (como Bitbucket)."""

    def __init__(self) -> None:
        self.inlines: list[str] = []
        self.comments: list[str] = []

    def get_pull_request(self, repo: str, pr_id: int) -> PullRequestInfo:
        return PullRequestInfo(
            repo=repo,
            pr_id=pr_id,
            title="Transfer",
            source_branch="f",
            destination_branch="main",
            author="a",
            head_sha="h",
            url="https://example.test/pr",
        )

    def get_diff(self, repo: str, pr_id: int) -> str:
        del repo, pr_id
        return "+x\n"

    def get_diffstat(self, repo: str, pr_id: int) -> list[tuple[str, int, int]]:
        del repo, pr_id
        return [("a.php", 1, 0)]

    def resolve_inline_anchor(self, repo: str, pr_id: int, *, path: str, text: str):
        del repo, pr_id, text
        return InlineAnchor(path=path, line=1)

    def post_inline_comment(
        self, repo: str, pr_id: int, *, path: str, line: int, body: str, from_line=None
    ) -> None:
        del repo, pr_id, path, line, from_line
        self.inlines.append(body)

    def post_comment(self, repo: str, pr_id: int, body: str) -> None:
        del repo, pr_id
        self.comments.append(body)

    def post_task(self, repo: str, pr_id: int, body: str) -> None:  # pragma: no cover
        del repo, pr_id, body

    def list_review_markers(self, repo: str, pr_id: int) -> list[ReviewMarker]:
        del repo, pr_id
        return [ReviewMarker(id="", body=body) for body in self.inlines + self.comments]


class _StaticReviewer:
    """Persona estatica con un hallazgo que la politica deja pendiente."""

    name = "correctness"

    def review(self, *, repo: str, pr_id: int, diff: str, head_sha: str = "") -> ReviewerResult:
        del repo, pr_id, diff, head_sha
        return ReviewerResult(
            findings=[
                Finding(
                    id="f1",
                    title="Saldo puede ser nulo",
                    severity=Severity.P2,
                    file="a.php",
                    line=1,
                    why_it_matters="Riesgo de null dereference",
                    autofix_class=AutofixClass.ADVISORY,
                    owner=Owner.HUMAN,
                    requires_verification=False,
                    confidence=75,
                    evidence=["a.php:1 $x"],
                )
            ]
        )


class _RecordingChat:
    def __init__(self) -> None:
        self.pending: list[str] = []

    def notify(self, *, title: str, body: str, link: str | None = None) -> None:
        del title, body, link

    def notify_decision_pending(self, *, title: str, body: str, link: str | None = None) -> None:
        del title, link
        self.pending.append(body)


def _escalating_policy() -> PolicyEngine:
    return PolicyEngine(
        {
            "version": 1,
            "max_autonomy": "fix_with_approval",
            "default_action": "escalate",
            "rules": [],
        }
    )


def _graph(vcs: _Vcs):
    from langgraph.checkpoint.memory import InMemorySaver

    return build_graph(vcs=vcs, reviewers=[_StaticReviewer()], policy=_escalating_policy()).compile(
        checkpointer=InMemorySaver()
    )


def test_e2e_pausa_decision_reanudacion_publica_sin_duplicar(monkeypatch) -> None:
    """Artefacto -> pausa -> decision (CLI) -> resume (worker) -> inline+resumen.

    Ejercita el camino completo sin `twg` ni red: VCS, inbox y cola falsos, pero
    el grafo, la validacion de la decision y el worker son los reales.
    """
    vcs = _Vcs()
    chat = _RecordingChat()
    paused_store = InMemoryPausedStore()
    queue = InMemoryReviewQueue()
    graph = _graph(vcs)
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=1, head_sha="abc", pipeline_id="7")

    # 1) El worker procesa la review y el grafo pausa: no se publica nada.
    first = process_request(graph, request, paused=paused_store, chat=chat)
    assert first.paused
    assert vcs.inlines == [] and vcs.comments == []
    assert len(chat.pending) == 1
    assert paused_store.get(request.review_key) is not None

    # 2) La persona decide por el CLI; el CLI encola la reanudacion.
    monkeypatch.setattr(cli, "build_paused", lambda settings: paused_store)
    monkeypatch.setattr(cli, "build_queue", lambda settings: queue)
    assert cli.main(["decide", request.review_key, "--actor", "alice", "--approve", "f1"]) == 0

    # 3) El worker recoge el job de reanudacion y aplica Command(resume=...).
    job = queue.dequeue()
    assert job is not None and job.decision is not None
    resumed = process_request(graph, job, paused=paused_store, chat=chat)

    assert not resumed.paused
    assert len(vcs.inlines) == 1, "se publica el inline una sola vez"
    assert len(vcs.comments) == 1, "y un unico resumen"
    assert "Decision: aprobado · alice" in vcs.inlines[0]
    assert vcs.comments[0].endswith(f"<!-- {request.dedupe_token}:ok -->")
    assert paused_store.get(request.review_key) is None, "el inbox se limpia al decidir"

    # 4) Una reentrega del job (ack perdido) no republica: hilo completado.
    process_request(graph, job, paused=paused_store, chat=chat)
    assert len(vcs.inlines) == 1 and len(vcs.comments) == 1


def test_e2e_rechazo_descarta_el_hallazgo_del_inline(monkeypatch) -> None:
    vcs = _Vcs()
    chat = _RecordingChat()
    paused_store = InMemoryPausedStore()
    queue = InMemoryReviewQueue()
    graph = _graph(vcs)
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=2, head_sha="def", pipeline_id="8")

    process_request(graph, request, paused=paused_store, chat=chat)
    monkeypatch.setattr(cli, "build_paused", lambda settings: paused_store)
    monkeypatch.setattr(cli, "build_queue", lambda settings: queue)
    assert cli.main(["decide", request.review_key, "--actor", "bob", "--reject", "f1"]) == 0

    job = queue.dequeue()
    assert job is not None
    process_request(graph, job, paused=paused_store, chat=chat)

    assert vcs.inlines == [], "un hallazgo descartado no se anota inline"
    assert vcs.comments, "pero si figura en el resumen"
    assert "Descartado por una persona" in vcs.comments[0]
