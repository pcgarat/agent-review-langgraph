from __future__ import annotations

from reviews.adapters.teams_webhook import TeamsWebhookAdapter
from reviews.domain.models import (
    AutofixClass,
    Finding,
    Owner,
    ReviewerResult,
    Severity,
    ToolingSourceStatus,
)
from reviews.graph.build import build_graph, outcome_from_state
from reviews.idempotency import InMemoryIdempotencyStore
from reviews.policy.engine import PolicyEngine
from reviews.ports.reviewer import ReviewerPort
from reviews.ports.vcs import InlineAnchor, PullRequestInfo


class FakeVcs:
    def __init__(self, *, anchors: dict[str, InlineAnchor] | None = None) -> None:
        self.comments: list[tuple[str, int, str]] = []
        self.inlines: list[tuple[str, int, str, int, str]] = []
        self._anchors = anchors or {}

    def get_pull_request(self, repo: str, pr_id: int) -> PullRequestInfo:
        return PullRequestInfo(
            repo=repo,
            pr_id=pr_id,
            title="Test PR",
            source_branch="feature/x",
            destination_branch="main",
            author="dev",
            head_sha="abc",
            url=f"https://bitbucket.org/{repo}/pull-requests/{pr_id}",
        )

    def get_diff(self, repo: str, pr_id: int) -> str:
        del repo, pr_id
        return "diff --git a/src/foo.py b/src/foo.py\n+amor\n"

    def get_diffstat(self, repo: str, pr_id: int) -> list[tuple[str, int, int]]:
        del repo, pr_id
        return [("src/foo.py", 1, 0)]

    def resolve_inline_anchor(
        self, repo: str, pr_id: int, *, path: str, text: str
    ) -> InlineAnchor | None:
        del repo, pr_id, text
        return self._anchors.get(path)

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
        del from_line
        self.inlines.append((repo, pr_id, path, line, body))

    def post_comment(self, repo: str, pr_id: int, body: str) -> None:
        self.comments.append((repo, pr_id, body))

    def post_task(self, repo: str, pr_id: int, body: str) -> None:  # pragma: no cover
        del repo, pr_id, body


class StaticReviewer:
    def __init__(
        self,
        name: str,
        findings: list[Finding],
        *,
        status: ToolingSourceStatus | None = None,
    ) -> None:
        self.name = name
        self._findings = findings
        self._status = status

    def review(
        self, *, repo: str, pr_id: int, diff: str, head_sha: str = ""
    ) -> ReviewerResult:
        del repo, pr_id, diff, head_sha
        return ReviewerResult(findings=list(self._findings), tooling_status=self._status)


def make_finding(
    title: str = "Falta test",
    *,
    file: str = "tests/FooTest.php",
    severity: Severity = Severity.P3,
) -> Finding:
    return Finding(
        id="f1",
        title=title,
        severity=severity,
        file=file,
        line=3,
        why_it_matters="No cubre el caso borde",
        autofix_class=AutofixClass.GATED_AUTO,
        owner=Owner.DOWNSTREAM_RESOLVER,
        requires_verification=True,
        confidence=75,
        evidence=[f"{file}:3 falta assert"],
    )


def test_grafo_de_duplica_hallazgos_de_dos_personas() -> None:
    vcs = FakeVcs(
        anchors={"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)}
    )
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
    assert len(vcs.inlines) == 1
    assert vcs.comments, "debe publicar el resumen en el PR"


def test_publish_sin_ancla_cae_al_resumen() -> None:
    vcs = FakeVcs(anchors={})
    reviewers: list[ReviewerPort] = [StaticReviewer("correctness", [make_finding()])]
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=vcs, reviewers=reviewers, policy=policy).compile()

    graph.invoke(
        {
            "request": {"repo": "cqm/emotivoo-v2", "pr_id": 7, "head_sha": "abc"},
            "findings": [],
        }
    )
    assert vcs.inlines == []
    assert vcs.comments
    assert "solo en resumen" in vcs.comments[0][2]


def test_publish_respeta_max_inline_comments() -> None:
    findings = [
        make_finding(title=f"H{i}", file=f"src/f{i}.py", severity=Severity.P3)
        for i in range(3)
    ]
    # ids unicos
    for i, finding in enumerate(findings):
        findings[i] = finding.model_copy(update={"id": f"f{i}"})

    anchors = {
        f"src/f{i}.py": InlineAnchor(path=f"src/f{i}.py", line=3) for i in range(3)
    }
    vcs = FakeVcs(anchors=anchors)
    reviewers: list[ReviewerPort] = [StaticReviewer("correctness", findings)]
    policy = PolicyEngine(
        {
            "version": 1,
            "max_autonomy": "report",
            "default_action": "report",
            "rules": [],
            "guards": {"max_inline_comments": 2},
        }
    )
    graph = build_graph(vcs=vcs, reviewers=reviewers, policy=policy).compile()
    graph.invoke(
        {
            "request": {"repo": "cqm/emotivoo-v2", "pr_id": 7, "head_sha": "abc"},
            "findings": [],
        }
    )
    assert len(vcs.inlines) == 2
    assert "1 solo en resumen" in vcs.comments[0][2] or "solo en resumen" in vcs.comments[0][2]


def test_fase1_report_only_no_deja_pending() -> None:
    vcs = FakeVcs(
        anchors={"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)}
    )
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
    assert outcome.pending_approvals == []
    assert outcome.decisions[0].action.value == "report"
    assert "pending_approvals" not in result


def test_pending_approvals_deriva_de_decisions() -> None:
    vcs = FakeVcs(
        anchors={"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)}
    )
    reviewers: list[ReviewerPort] = [StaticReviewer("correctness", [make_finding()])]
    policy = PolicyEngine(
        {
            "version": 1,
            "max_autonomy": "fix_with_approval",
            "default_action": "report",
            "rules": [
                {
                    "when": {"autofix_class": "gated_auto", "severity": "P3"},
                    "action": "fix_with_approval",
                    "reason": "pide OK",
                }
            ],
        }
    )
    graph = build_graph(vcs=vcs, reviewers=reviewers, policy=policy).compile()

    result = graph.invoke(
        {
            "request": {"repo": "cqm/emotivoo-v2", "pr_id": 7, "head_sha": "abc"},
            "findings": [],
        }
    )
    outcome = outcome_from_state(result)
    assert outcome.pending_approvals == ["f1"]
    assert "pending_approvals" not in result


def test_decision_tiene_regla_y_version() -> None:
    vcs = FakeVcs(
        anchors={"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)}
    )
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
    assert vcs.inlines == []


def test_grafo_avisa_cuando_la_fuente_estatica_no_esta() -> None:
    # 0 hallazgos con fuente ausente NO es "sin hallazgos": se publica un aviso.
    vcs = FakeVcs()
    reviewers: list[ReviewerPort] = [
        StaticReviewer("standards", [], status=ToolingSourceStatus.MISSING)
    ]
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=vcs, reviewers=reviewers, policy=policy).compile()

    graph.invoke(
        {
            "request": {"repo": "cqm/emotivoo-v2", "pr_id": 7, "head_sha": "abc"},
            "findings": [],
        }
    )
    assert vcs.inlines == []
    assert len(vcs.comments) == 1
    assert "NO cubre" in vcs.comments[0][2]


def test_grafo_con_hallazgos_anota_fuente_ausente_en_el_resumen() -> None:
    vcs = FakeVcs(
        anchors={"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)}
    )
    reviewers: list[ReviewerPort] = [
        StaticReviewer("correctness", [make_finding()]),
        StaticReviewer("standards", [], status=ToolingSourceStatus.UNREADABLE),
    ]
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=vcs, reviewers=reviewers, policy=policy).compile()

    graph.invoke(
        {
            "request": {"repo": "cqm/emotivoo-v2", "pr_id": 7, "head_sha": "abc"},
            "findings": [],
        }
    )
    assert vcs.inlines
    assert "NO cubre" in vcs.comments[0][2]


def test_teams_sin_webhook_no_falla() -> None:
    adapter = TeamsWebhookAdapter(webhook_url="")
    adapter.notify(title="t", body="b")
    adapter.notify_decision_pending(title="t", body="b")


def test_idempotencia_con_store_en_memoria() -> None:
    store = InMemoryIdempotencyStore()
    assert store.claim("k") is True
    assert store.claim("k") is False
