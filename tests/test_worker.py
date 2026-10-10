from __future__ import annotations

from pathlib import Path

from reviews.adapters.diff_parser import best_line, file_key, parse_unified_diff
from reviews.adapters.tooling_reviewer import ToolingReviewer
from reviews.adapters.tooling_source import FileToolingOutputSource
from reviews.domain.models import AutofixClass, Finding, Owner, ReviewRequest, Severity
from reviews.graph.build import build_graph
from reviews.policy.engine import PolicyEngine
from reviews.ports.vcs import InlineAnchor, PullRequestInfo
from reviews.queue import InMemoryReviewQueue
from reviews.settings import Settings
from reviews.worker.main import build_reviewers, process_request

FIXTURES = Path(__file__).parent / "fixtures"
DIFF = (FIXTURES / "diff_emotivoo.diff").read_text(encoding="utf-8")
PHPSTAN = (FIXTURES / "phpstan" / "analysis.json").read_text(encoding="utf-8")


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

    def review(
        self, *, repo: str, pr_id: int, diff: str, head_sha: str = ""
    ) -> list[Finding]:
        del repo, pr_id, diff, head_sha
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


class _DiffVcs:
    """VCS minimo que ancla como `twg prs diff-line` sobre el diff dado."""

    def __init__(self, diff: str) -> None:
        self._files = parse_unified_diff(diff)
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
        return DIFF

    def get_diffstat(self, repo: str, pr_id: int) -> list[tuple[str, int, int]]:
        del repo, pr_id
        return [(path, len(fd.added), 0) for path, fd in self._files.items()]

    def resolve_inline_anchor(self, repo: str, pr_id: int, *, path: str, text: str):
        del repo, pr_id
        key = file_key(self._files, path)
        if key is None:
            return None
        line = best_line(self._files[key].added, hint=None, needle=text)
        return InlineAnchor(path=key, line=line) if line else None

    def post_inline_comment(
        self, repo: str, pr_id: int, *, path: str, line: int, body: str, from_line=None
    ) -> None:
        del repo, pr_id, from_line
        self.inlines.append(f"{path}:{line}")

    def post_comment(self, repo: str, pr_id: int, body: str) -> None:
        del repo, pr_id
        self.comments.append(body)

    def post_task(self, repo: str, pr_id: int, body: str) -> None:
        del repo, pr_id, body


def _settings_for(tmp_path: Path) -> Settings:
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "repo: cqm/emotivoo-v2\nchecks:\n  typecheck: make stan\n", encoding="utf-8"
    )
    return Settings(repo_profile_config=str(profile))


def test_build_reviewers_activa_tooling_desde_el_perfil(tmp_path: Path) -> None:
    reviewers = build_reviewers(_settings_for(tmp_path))
    assert len(reviewers) == 1
    assert isinstance(reviewers[0], ToolingReviewer)


def test_build_reviewers_cae_a_null_sin_perfil(tmp_path: Path) -> None:
    settings = Settings(repo_profile_config=str(tmp_path / "no-existe.yaml"))
    reviewers = build_reviewers(settings)
    assert [r.name for r in reviewers] == ["null"]


def test_build_reviewers_cae_a_null_con_perfil_invalido(tmp_path: Path) -> None:
    malformed = tmp_path / "malformado.yaml"
    malformed.write_text("repo: [sin cerrar\n", encoding="utf-8")
    assert [r.name for r in build_reviewers(Settings(repo_profile_config=str(malformed)))] == [
        "null"
    ]

    lista = tmp_path / "lista.yaml"
    lista.write_text("- a\n- b\n", encoding="utf-8")
    assert [r.name for r in build_reviewers(Settings(repo_profile_config=str(lista)))] == [
        "null"
    ]


def test_worker_hasta_formato_publica_salida_del_perfil(tmp_path: Path) -> None:
    # Se usa la plantilla de produccion (Settings) y el artefacto se deposita en
    # la ruta que resuelve, para ejercitar la ruta por defecto de extremo a extremo.
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "repo: cqm/emotivoo-v2\nchecks:\n  typecheck: make stan\n", encoding="utf-8"
    )
    settings = Settings(repo_profile_config=str(profile))
    source = FileToolingOutputSource(
        template=settings.tooling_output_template, root=tmp_path
    )
    artifact = source.path_for("phpstan", repo="cqm/emotivoo-v2", pr_id=5, head_sha="abc")
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(PHPSTAN, encoding="utf-8")

    vcs = _DiffVcs(DIFF)
    reviewers = build_reviewers(settings, tooling_source=source)
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=vcs, reviewers=reviewers, policy=policy).compile()

    graph.invoke(
        {
            "request": ReviewRequest(repo="cqm/emotivoo-v2", pr_id=5, head_sha="abc"),
            "findings": [],
        }
    )

    assert len(vcs.inlines) == 2, "los hallazgos del artefacto deben anclarse"


def test_worker_head_sha_vacio_no_publica_en_silencio(tmp_path: Path) -> None:
    # Un head_sha vacio no puede resolverse a un segmento `unknown` y salir verde:
    # la persona falla ruidoso y process_request lo registra sin publicar nada.
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "repo: cqm/emotivoo-v2\nchecks:\n  typecheck: make stan\n", encoding="utf-8"
    )
    settings = Settings(repo_profile_config=str(profile))
    source = FileToolingOutputSource(
        template=settings.tooling_output_template, root=tmp_path
    )
    vcs = _DiffVcs(DIFF)
    reviewers = build_reviewers(settings, tooling_source=source)
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=vcs, reviewers=reviewers, policy=policy).compile()

    process_request(graph, ReviewRequest(repo="cqm/emotivoo-v2", pr_id=5, head_sha=""))

    assert vcs.inlines == []
    assert vcs.comments == [], "ante sha vacio no se debe reportar 'sin hallazgos'"
