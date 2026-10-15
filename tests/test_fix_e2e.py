from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent_review.domain.models import (
    AutofixClass,
    CommandResult,
    DecisionOutcome,
    Finding,
    FindingDecision,
    FixResolution,
    FixResolutions,
    FixResult,
    FixStatus,
    HitlDecision,
    Owner,
    ReviewerResult,
    ReviewRequest,
    Severity,
)
from agent_review.fix.delivery import DiffFormatter
from agent_review.fix.patch import build_patch
from agent_review.graph.build import build_graph
from agent_review.policy.engine import PolicyEngine
from agent_review.ports.vcs import InlineAnchor, PullRequestInfo, ReviewMarker
from agent_review.worker.main import process_request


class _Vcs:
    """VCS que recuerda lo publicado y simula el fichero al `head_sha`."""

    def __init__(self) -> None:
        self.inlines: list[str] = []
        self.comments: list[str] = []
        self.source = "<?php\nuse App\\Baz;\nfunction foo() {\n"

    def get_pull_request(self, repo: str, pr_id: int) -> PullRequestInfo:
        return PullRequestInfo(
            repo=repo,
            pr_id=pr_id,
            title="t",
            source_branch="f",
            destination_branch="main",
            author="a",
            head_sha="h",
            url="https://example.test/pr",
        )

    def get_diff(self, repo: str, pr_id: int) -> str:
        del repo, pr_id
        return (
            "diff --git a/src/Foo.php b/src/Foo.php\n"
            "--- a/src/Foo.php\n"
            "+++ b/src/Foo.php\n"
            "@@ -1,3 +1,4 @@\n"
            " <?php\n"
            "+use App\\Bar;\n"
            " function foo() {\n"
        )

    def get_diffstat(self, repo: str, pr_id: int) -> list[tuple[str, int, int]]:
        del repo, pr_id
        return [("src/Foo.php", 1, 0)]

    def get_file(self, repo: str, path: str, *, ref: str) -> str:
        del repo, path, ref
        return self.source

    def resolve_inline_anchor(self, repo: str, pr_id: int, *, path: str, text: str):
        del repo, pr_id, text
        return InlineAnchor(path=path, line=2)

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


class _FindingReviewer:
    name = "correctness"

    def review(self, *, repo: str, pr_id: int, diff: str, head_sha: str = "") -> ReviewerResult:
        del repo, pr_id, diff, head_sha
        return ReviewerResult(
            findings=[
                Finding(
                    id="f1",
                    title="Import sin usar",
                    severity=Severity.P2,
                    file="src/Foo.php",
                    line=2,
                    why_it_matters="Ruido",
                    autofix_class=AutofixClass.GATED_AUTO,
                    owner=Owner.HUMAN,
                    requires_verification=True,
                    confidence=75,
                    evidence=["src/Foo.php:2 +use App\\Bar;"],
                )
            ]
        )


class _Fixer:
    def propose(self, finding: Finding, *, source: str, previous: FixResult | None):
        return build_patch(
            finding, source=source, start_line=2, end_line=2, replacement="use App\\Bar;"
        )


def _policy() -> PolicyEngine:
    return PolicyEngine(
        {
            "version": 1,
            "max_autonomy": "fix_with_approval",
            "default_action": "report",
            "guards": {"max_fixes_per_pr": 3, "max_fix_attempts": 2, "max_fix_lines": 30},
            "rules": [
                {
                    "when": {"autofix_class": "gated_auto", "severity": ["P1", "P2"]},
                    "action": "fix_with_approval",
                    "reason": "fix seguro",
                }
            ],
        }
    )


def _resolutions(request: ReviewRequest, *, outcome: str = "passed") -> FixResolutions:
    """Wave resuelta por el worker de fixes (lo que hoy llega por la cola de fixes)."""
    run_key = f"{request.review_key}:f1:1"
    checks = [CommandResult(recipe="typecheck", argv=["make", "stan"], exit_code=0, duration_s=1.0)]
    return FixResolutions(
        items=[
            FixResolution(
                run_key=run_key,
                outcome=outcome,
                checks=checks,
                image_digest="sha256:abc",
            )
        ]
    )


def test_e2e_fix_verificado_se_publica_como_sugerencia() -> None:
    """Aprobado -> fix_dispatch (propone) -> publish (inline) -> fix_wait (pausa) ->
    wave del worker de fixes -> publish_fix (sugerencia verificada)."""
    vcs = _Vcs()
    graph = build_graph(
        vcs=vcs,
        reviewers=[_FindingReviewer()],
        policy=_policy(),
        fixer=_Fixer(),
        delivery=DiffFormatter(),
        fix_recipes=["typecheck"],
    ).compile(checkpointer=InMemorySaver())

    request = ReviewRequest(repo="cqm/emotivox", pr_id=9, head_sha="abc", pipeline_id="1")
    # Sin decision humana, el hallazgo `fix_with_approval` pausa: no se publica.
    paused = process_request(graph, request)
    assert paused.paused
    assert vcs.inlines == []

    # Se aprueba y se reanuda: la review publica el inline y pausa esperando la wave.
    decision = HitlDecision(
        review_key=request.review_key,
        actor="alice",
        items=[FindingDecision(finding_id="f1", outcome=DecisionOutcome.APPROVED)],
    )
    config = {"configurable": {"thread_id": request.thread_id}}
    waiting = graph.invoke(Command(resume=decision), config=config)

    assert waiting["__interrupt__"][0].value.kind == "fix"
    assert waiting["fixes"]["f1"].status is FixStatus.PENDING
    assert len(vcs.inlines) == 1
    assert "### Fixes" in vcs.comments[0]
    assert "```diff" not in vcs.inlines[0], "el PENDING no publica sugerencia"

    # El worker de fixes resuelve la wave y se reanuda: se aplica y se publica el fix.
    resumed = graph.invoke(Command(resume=_resolutions(request)), config=config)

    assert resumed["fixes"]["f1"].status is FixStatus.VERIFIED
    followups = [c for c in vcs.comments if ":fix -->" in c]
    assert len(followups) == 1
    assert "## Fix verificado" in followups[0]
    assert "```diff" in followups[0]


def test_e2e_runner_caido_deja_el_fix_no_verificable() -> None:
    """Outcome `error` (runner caido) -> NOT_VERIFIABLE publicado, nunca FAILED."""
    vcs = _Vcs()
    graph = build_graph(
        vcs=vcs,
        reviewers=[_FindingReviewer()],
        policy=_policy(),
        fixer=_Fixer(),
        delivery=DiffFormatter(),
        fix_recipes=["typecheck"],
    ).compile(checkpointer=InMemorySaver())

    request = ReviewRequest(repo="cqm/emotivox", pr_id=9, head_sha="abc", pipeline_id="1")
    process_request(graph, request)
    decision = HitlDecision(
        review_key=request.review_key,
        actor="alice",
        items=[FindingDecision(finding_id="f1", outcome=DecisionOutcome.APPROVED)],
    )
    config = {"configurable": {"thread_id": request.thread_id}}
    graph.invoke(Command(resume=decision), config=config)

    wave = _resolutions(request, outcome="error")
    wave.items[0].error = "runner inalcanzable"
    resumed = graph.invoke(Command(resume=wave), config=config)

    assert resumed["fixes"]["f1"].status is FixStatus.NOT_VERIFIABLE
    followups = [c for c in vcs.comments if ":fix -->" in c]
    assert len(followups) == 1
    assert "no verificable" in followups[0]


def test_e2e_fix_fallido_agota_intentos_y_publica_el_fallo() -> None:
    """`failed` -> re-pausa -> re-propuesta (otra wave) -> al agotar, FAILED publicado."""
    vcs = _Vcs()
    graph = build_graph(
        vcs=vcs,
        reviewers=[_FindingReviewer()],
        policy=_policy(),
        fixer=_Fixer(),
        delivery=DiffFormatter(),
        fix_recipes=["typecheck"],
    ).compile(checkpointer=InMemorySaver())

    request = ReviewRequest(repo="cqm/emotivox", pr_id=9, head_sha="abc", pipeline_id="1")
    process_request(graph, request)
    decision = HitlDecision(
        review_key=request.review_key,
        actor="alice",
        items=[FindingDecision(finding_id="f1", outcome=DecisionOutcome.APPROVED)],
    )
    config = {"configurable": {"thread_id": request.thread_id}}
    graph.invoke(Command(resume=decision), config=config)

    # Primera wave fallida: quedan intentos, el grafo vuelve a `fix_dispatch` y re-pausa.
    failed_checks = [
        CommandResult(
            recipe="typecheck",
            argv=["make", "stan"],
            exit_code=1,
            duration_s=1.0,
            stderr_tail="Undefined method",
        )
    ]
    first = FixResolutions(
        items=[
            FixResolution(
                run_key=f"{request.review_key}:f1:1", outcome="failed", checks=failed_checks
            )
        ]
    )
    again = graph.invoke(Command(resume=first), config=config)

    assert again["__interrupt__"][0].value.kind == "fix"
    assert again["fixes"]["f1"].status is FixStatus.PENDING
    assert again["fixes"]["f1"].attempts == 2

    # Segunda wave fallida: agota intentos, publica el fallo con el log acotado.
    second = FixResolutions(
        items=[
            FixResolution(
                run_key=f"{request.review_key}:f1:2", outcome="failed", checks=failed_checks
            )
        ]
    )
    done = graph.invoke(Command(resume=second), config=config)

    assert done["fixes"]["f1"].status is FixStatus.FAILED
    followups = [c for c in vcs.comments if ":fix -->" in c]
    assert len(followups) == 1
    assert "Undefined method" in followups[0]
