from __future__ import annotations

from reviews.domain.models import AutofixClass, Finding, Owner, ReviewRequest, Severity
from reviews.graph.build import build_graph
from reviews.policy.engine import PolicyEngine
from reviews.ports.vcs import InlineAnchor, PullRequestInfo
from reviews.queue import InMemoryReviewQueue
from reviews.worker.main import process_request


class FakeVcs:
    def __init__(self) -> None:
        self.inlines: list[str] = []
        self.comments: list[str] = []

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
        return "+x\n"

    def get_diffstat(self, repo: str, pr_id: int) -> list[tuple[str, int, int]]:
        del repo, pr_id
        return [("a.php", 1, 0)]

    def resolve_inline_anchor(
        self, repo: str, pr_id: int, *, path: str, text: str
    ) -> InlineAnchor | None:
        del repo, pr_id, text
        return InlineAnchor(path=path, line=1)

    def post_inline_comment(
        self,
        repo: str,
        pr_id: int,
        *,
        path: str,
        line: int,
        body: str,
        from_line: int | None = None,
    ) -> None:
        del repo, pr_id, line, from_line
        self.inlines.append(f"{path}:{body}")

    def post_comment(self, repo: str, pr_id: int, body: str) -> None:
        del repo, pr_id
        self.comments.append(body)

    def post_task(self, repo: str, pr_id: int, body: str) -> None:
        del repo, pr_id, body


class OneShotReviewer:
    name = "standards"

    def review(self, *, repo: str, pr_id: int, diff: str) -> list[Finding]:
        del repo, pr_id, diff
        return [
            Finding(
                id="w1",
                title="Naming",
                severity=Severity.P3,
                file="a.php",
                line=1,
                why_it_matters="Claridad",
                autofix_class=AutofixClass.ADVISORY,
                owner=Owner.HUMAN,
                requires_verification=False,
                confidence=75,
                evidence=["a.php:1 $x"],
            )
        ]


def test_worker_procesa_cola_y_publica_inline() -> None:
    queue = InMemoryReviewQueue()
    vcs = FakeVcs()
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(
        vcs=vcs, reviewers=[OneShotReviewer()], policy=policy
    ).compile()

    queue.enqueue(ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef"))
    request = queue.dequeue()
    assert request is not None
    process_request(graph, request)

    assert len(vcs.inlines) == 1
    assert vcs.comments
