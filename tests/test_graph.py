from __future__ import annotations

from reviews.adapters.teams_webhook import TeamsWebhookAdapter
from reviews.domain.models import AutofixClass, Finding, Owner, Severity
from reviews.graph.build import build_graph, outcome_from_state
from reviews.idempotency import InMemoryIdempotencyStore
from reviews.policy.engine import PolicyEngine
from reviews.ports.reviewer import ReviewerPort
from reviews.ports.vcs import PullRequestInfo


class FakeVcs:
    def __init__(self) -> None:
        self.comments: list[tuple[str, int, str]] = []

    def get_pull_request(self, repo: str, pr_id: int) -> PullRequestInfo:
        return PullRequestInfo(
            repo=repo,
            pr_id=pr_id,
            title="Test PR",
            source_branch="feature/x",
            destination_branch="main",
            author="dev",
            head_sha="abc",
        )

    def get_diff(self, repo: str, pr_id: int) -> str:
        return "diff --git a/src/foo.py b/src/foo.py\n+amor\n"

    def get_diffstat(self, repo: str, pr_id: int) -> list[tuple[str, int, int]]:
        return [("src/foo.py", 1, 0)]

    def post_comment(self, repo: str, pr_id: int, body: str) -> None:
        self.comments.append((repo, pr_id, body))

    def post_task(self, repo: str, pr_id: int, body: str) -> None:  # pragma: no cover
        pass


class StaticReviewer:
    def __init__(self, name: str, findings: list[Finding]) -> None:
        self.name = name
        self._findings = findings

    def review(self, *, repo: str, pr_id: int, diff: str) -> list[Finding]:
        return list(self._findings)


def make_finding(title: str = "Falta test") -> Finding:
    return Finding(
        id="f1",
        title=title,
        severity=Severity.P3,
        file="tests/FooTest.php",
        line=3,
        why_it_matters="No cubre el caso borde",
        autofix_class=AutofixClass.GATED_AUTO,
        owner=Owner.DOWNSTREAM_RESOLVER,
        requires_verification=True,
        confidence=75,
        evidence=["tests/FooTest.php:3 falta assert"],
    )


def test_grafo_de_duplica_hallazgos_de_dos_personas() -> None:
    vcs = FakeVcs()
    reviewers: list[ReviewerPort] = [
        StaticReviewer("correctness", [make_finding()]),
        StaticReviewer("tests", [make_finding()]),
    ]
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=vcs, reviewers=reviewers, policy=policy).compile()

    result = graph.invoke(
        {
            "request": {"repo": "cqm/emotivoo-v2", "pr_id": 7, "head_sha": "abc"},
            "findings": [],
        }
    )

    outcome = outcome_from_state(result)
    assert len(outcome.findings) == 1, "el merge debe deduplicar"
    assert len(outcome.decisions) == 1
    assert vcs.comments, "debe publicar el resumen en el PR"


def test_pending_approvals_deriva_de_decisions() -> None:
    """pending_approvals no es un campo: es una vista de decisions."""
    vcs = FakeVcs()
    reviewers: list[ReviewerPort] = [StaticReviewer("correctness", [make_finding()])]
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=vcs, reviewers=reviewers, policy=policy).compile()

    result = graph.invoke(
        {
            "request": {"repo": "cqm/emotivoo-v2", "pr_id": 7, "head_sha": "abc"},
            "findings": [],
        }
    )
    outcome = outcome_from_state(result)
    # Con el techo por defecto (fix_with_approval), un P3 espera aprobacion.
    assert outcome.pending_approvals == ["f1"]
    assert "pending_approvals" not in result


def test_decision_tiene_regla_y_version() -> None:
    vcs = FakeVcs()
    reviewers: list[ReviewerPort] = [StaticReviewer("correctness", [make_finding()])]
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=vcs, reviewers=reviewers, policy=policy).compile()

    result = graph.invoke(
        {
            "request": {"repo": "cqm/emotivoo-v2", "pr_id": 7, "head_sha": "abc"},
            "findings": [],
        }
    )
    decision = outcome_from_state(result).decisions[0]
    assert decision.matched_rule
    assert decision.policy_version == policy.version


def test_grafo_sin_hallazgos_no_publica() -> None:
    vcs = FakeVcs()
    reviewers: list[ReviewerPort] = [StaticReviewer("correctness", [])]
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=vcs, reviewers=reviewers, policy=policy).compile()

    graph.invoke(
        {
            "request": {"repo": "cqm/emotivoo-v2", "pr_id": 7, "head_sha": "abc"},
            "findings": [],
        }
    )
    assert vcs.comments == []


def test_teams_sin_webhook_no_falla() -> None:
    adapter = TeamsWebhookAdapter(webhook_url="")
    adapter.notify(title="t", body="b")
    adapter.notify_decision_pending(title="t", body="b")


def test_idempotencia_con_store_en_memoria() -> None:
    store = InMemoryIdempotencyStore()
    assert store.claim("k") is True
    assert store.claim("k") is False
