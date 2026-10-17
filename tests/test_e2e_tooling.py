from __future__ import annotations

from pathlib import Path

from agent_review.adapters.diff_parser import best_line, file_key, parse_unified_diff
from agent_review.adapters.llm_reviewer import LlmReviewer
from agent_review.adapters.tooling_reviewer import ToolingReviewer
from agent_review.domain.models import ReviewRequest
from agent_review.graph.build import build_graph
from agent_review.policy.engine import PolicyEngine
from agent_review.ports.llm import LlmReply
from agent_review.ports.vcs import InlineAnchor, PullRequestInfo

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

    def list_review_markers(self, repo: str, pr_id: int) -> list:
        del repo, pr_id
        return []


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


class _FakeLlm:
    """Devuelve un hallazgo `correctness` que duplica al de `standards`.

    La dedupe del grafo es por `(fichero, titulo)`: el fake reutiliza el titulo
    real que produce la persona estatica para que el test verifique el merge de
    verdad (no una coincidencia accidental de texto).
    """

    def __init__(self, title: str) -> None:
        self._title = title

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        json_mode=None,
        response_schema=None,
    ) -> LlmReply:
        del system, prompt, max_tokens, json_mode, response_schema
        import json

        return LlmReply(
            text=json.dumps(
                {
                    "findings": [
                        {
                            "title": self._title,
                            "severity": "P2",
                            "file": "src/BackOffice/Application/Wallet/Transfer.php",
                            "line": 13,
                            "why_it_matters": "El saldo puede ser nulo antes de retirar",
                            "autofix_class": "gated_auto",
                            "owner": "downstream-resolver",
                            "failure_scenario": "escenario de prueba",
                            "confidence": 75,
                            "evidence": [
                                "src/BackOffice/Application/Wallet/Transfer.php:13 $balance->frozen"
                            ],
                        }
                    ]
                }
            )
        )


def test_e2e_estatica_y_llm_conviven_y_el_merge_deduplica() -> None:
    # La persona LLM repite un hallazgo de la estatica (mismo fichero y titulo):
    # el merge debe quedarse con uno, y el publish no duplicar el inline.
    tooling = ToolingReviewer(phpstan_json=PHPSTAN)
    tooling_finding = next(
        f
        for f in tooling.review(repo="cqm/emotivoo-v2", pr_id=1, diff=DIFF).findings
        if f.file.endswith("Transfer.php")
    )
    vcs = DiffLineVcs(DIFF)
    reviewers = [tooling, LlmReviewer(llm=_FakeLlm(tooling_finding.title))]
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=vcs, reviewers=reviewers, policy=policy).compile()

    result = graph.invoke(
        {
            "request": ReviewRequest(repo="cqm/emotivoo-v2", pr_id=1, head_sha="abc"),
            "findings": [],
        }
    )

    merged = result["merged"]
    duplicates = [f for f in merged if f.file.endswith("Transfer.php")]
    assert len(duplicates) == 1, "el merge deduplica entre personas"
    # Fase 1 sigue sin pedir arreglos.
    assert all("fix_auto" not in body for _path, _line, body in vcs.inlines)
