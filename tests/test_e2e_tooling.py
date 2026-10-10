from __future__ import annotations

from pathlib import Path

from reviews.adapters.diff_parser import best_line, file_key, parse_unified_diff
from reviews.adapters.tooling_reviewer import ToolingReviewer
from reviews.domain.models import ReviewRequest
from reviews.graph.build import build_graph
from reviews.policy.engine import PolicyEngine
from reviews.ports.vcs import InlineAnchor, PullRequestInfo

FIXTURES = Path(__file__).parent / "fixtures"
DIFF = (FIXTURES / "diff_emotivoo.diff").read_text(encoding="utf-8")
PHPSTAN = (FIXTURES / "phpstan" / "analysis.json").read_text(encoding="utf-8")


class DiffLineVcs:
    """VCS falso que resuelve anclas como lo haria `twg prs diff-line`."""

    def __init__(self, diff: str) -> None:
        self._diff = diff
        self._files = parse_unified_diff(diff)
        self.inlines: list[tuple[str, int, str]] = []
        self.comments: list[str] = []

    def get_pull_request(self, repo: str, pr_id: int) -> PullRequestInfo:
        return PullRequestInfo(
            repo=repo,
            pr_id=pr_id,
            title="Transfer budget check",
            source_branch="feature/wallet",
            destination_branch="main",
            author="dev",
            head_sha="abc",
            url="https://bitbucket.org/cqm/emotivoo-v2/pull-requests/1",
        )

    def get_diff(self, repo: str, pr_id: int) -> str:
        del repo, pr_id
        return self._diff

    def get_diffstat(self, repo: str, pr_id: int) -> list[tuple[str, int, int]]:
        del repo, pr_id
        return [(path, len(fd.added), 0) for path, fd in self._files.items()]

    def resolve_inline_anchor(
        self, repo: str, pr_id: int, *, path: str, text: str
    ) -> InlineAnchor | None:
        del repo, pr_id
        key = file_key(self._files, path)
        if key is None:
            return None
        line = best_line(self._files[key].added, hint=None, needle=text)
        return InlineAnchor(path=key, line=line) if line else None

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
        del repo, pr_id, from_line
        self.inlines.append((path, line, body))

    def post_comment(self, repo: str, pr_id: int, body: str) -> None:
        del repo, pr_id
        self.comments.append(body)

    def post_task(self, repo: str, pr_id: int, body: str) -> None:  # pragma: no cover
        del repo, pr_id, body


def test_flujo_completo_estatico_ancla_en_lineas_reales() -> None:
    vcs = DiffLineVcs(DIFF)
    reviewer = ToolingReviewer(phpstan_json=PHPSTAN)
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=vcs, reviewers=[reviewer], policy=policy).compile()

    graph.invoke(
        {
            "request": ReviewRequest(repo="cqm/emotivoo-v2", pr_id=1, head_sha="abc"),
            "findings": [],
        }
    )

    assert len(vcs.inlines) == 2, "los dos hallazgos del diff deben anclarse"
    files = parse_unified_diff(DIFF)
    for path, line, _body in vcs.inlines:
        assert line in files[path].added, "el ancla debe ser una linea del diff"

    summary = vcs.comments[-1]
    assert "2 hallazgos" in summary
    assert "inline" in summary
    # Fase 1 es report-only: nunca se pide arreglo en el resumen.
    assert "fix_auto" not in summary


def test_hallazgo_fuera_del_diff_solo_va_al_resumen() -> None:
    # El tercer aviso de PHPStan (Legacy.php) no esta en el diff.
    vcs = DiffLineVcs(DIFF)
    reviewer = ToolingReviewer(phpstan_json=PHPSTAN)
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=vcs, reviewers=[reviewer], policy=policy).compile()
    graph.invoke(
        {
            "request": ReviewRequest(repo="cqm/emotivoo-v2", pr_id=1, head_sha="abc"),
            "findings": [],
        }
    )
    assert all("Legacy" not in path for path, _line, _body in vcs.inlines)
