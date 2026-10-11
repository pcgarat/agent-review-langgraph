from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from reviews.domain.models import (
    AutofixClass,
    CommandResult,
    DecisionOutcome,
    Finding,
    FindingDecision,
    FixResult,
    FixStatus,
    HitlDecision,
    Owner,
    ReviewerResult,
    ReviewRequest,
    Severity,
)
from reviews.fix.delivery import DiffFormatter
from reviews.fix.patch import build_patch
from reviews.graph.build import build_graph
from reviews.policy.engine import PolicyEngine
from reviews.ports.vcs import InlineAnchor, PullRequestInfo, ReviewMarker
from reviews.worker.main import process_request


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


class _Sandbox:
    def verify(self, request):  # noqa: ANN001
        result = CommandResult(
            recipe="typecheck", argv=["make", "stan"], exit_code=0, duration_s=1.0
        )
        return type(
            "Report",
            (),
            {
                "run_key": request.run_key,
                "image_digest": "sha256:abc",
                "results": [result],
                "passed": True,
            },
        )()


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


def test_e2e_fix_verificado_se_publica_como_sugerencia() -> None:
    """Aprobado con `fix_with_approval` -> fixer -> sandbox -> inline verificado."""
    vcs = _Vcs()
    graph = build_graph(
        vcs=vcs,
        reviewers=[_FindingReviewer()],
        policy=_policy(),
        sandbox=_Sandbox(),
        fixer=_Fixer(),
        delivery=DiffFormatter(),
        fix_recipes=["typecheck"],
    ).compile(checkpointer=InMemorySaver())

    request = ReviewRequest(repo="cqm/emotivox", pr_id=9, head_sha="abc", pipeline_id="1")
    # Sin decision humana, el hallazgo `fix_with_approval` pausa: no se publica.
    paused = process_request(graph, request)
    assert paused.paused
    assert vcs.inlines == []

    # Se aprueba y se reanuda: el nodo fix verifica y publish emite la sugerencia.
    decision = HitlDecision(
        review_key=request.review_key,
        actor="alice",
        items=[FindingDecision(finding_id="f1", outcome=DecisionOutcome.APPROVED)],
    )
    config = {"configurable": {"thread_id": request.thread_id}}
    resumed = graph.invoke(Command(resume=decision), config=config)

    assert resumed["fixes"]["f1"].status is FixStatus.VERIFIED
    assert len(vcs.inlines) == 1
    assert "```diff" in vcs.inlines[0]
    assert "### Fixes" in vcs.comments[0]
