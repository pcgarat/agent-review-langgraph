from __future__ import annotations

import contextlib

from reviews.adapters.teams_webhook import TeamsWebhookAdapter
from reviews.domain.models import (
    AutofixClass,
    Finding,
    Owner,
    ReviewerResult,
    ReviewRequest,
    Severity,
    ToolingSourceStatus,
)
from reviews.graph.build import build_graph, outcome_from_state
from reviews.idempotency import InMemoryIdempotencyStore
from reviews.policy.engine import PolicyEngine
from reviews.ports.reviewer import ReviewerPort
from reviews.ports.vcs import InlineAnchor, PullRequestInfo, ReviewMarker


class FakeVcs:
    def __init__(self, *, anchors: dict[str, InlineAnchor] | None = None) -> None:
        self.comments: list[tuple[str, int, str]] = []
        self.inlines: list[tuple[str, int, str, int, str]] = []
        self._anchors = anchors or {}
        # El PR recuerda lo publicado, como haria Bitbucket: el publish lo lee
        # para no duplicar el resultado de una review ya hecha.
        self.markers: list[ReviewMarker] = []

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

    def list_review_markers(self, repo: str, pr_id: int) -> list[ReviewMarker]:
        del repo, pr_id
        return list(self.markers)

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
        # Bitbucket conserva el comentario: el siguiente publish lo vera.
        self.markers.append(ReviewMarker(id=f"i{len(self.markers)}", body=body))

    def post_comment(self, repo: str, pr_id: int, body: str) -> None:
        self.comments.append((repo, pr_id, body))
        self.markers.append(ReviewMarker(id=f"c{len(self.markers)}", body=body))

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

    def review(self, *, repo: str, pr_id: int, diff: str, head_sha: str = "") -> ReviewerResult:
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
    vcs = FakeVcs(anchors={"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)})
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
        make_finding(title=f"H{i}", file=f"src/f{i}.py", severity=Severity.P3) for i in range(3)
    ]
    # ids unicos
    for i, finding in enumerate(findings):
        findings[i] = finding.model_copy(update={"id": f"f{i}"})

    anchors = {f"src/f{i}.py": InlineAnchor(path=f"src/f{i}.py", line=3) for i in range(3)}
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
    vcs = FakeVcs(anchors={"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)})
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
    vcs = FakeVcs(anchors={"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)})
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
    vcs = FakeVcs(anchors={"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)})
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


def test_scope_filtra_ruido_antes_de_las_personas() -> None:
    # B5: un lockfile en el diff no debe llegar a las personas ni al publish.
    class DiffVcs(FakeVcs):
        def get_diff(self, repo: str, pr_id: int) -> str:
            del repo, pr_id
            return (
                "diff --git a/composer.lock b/composer.lock\n"
                "--- a/composer.lock\n+++ b/composer.lock\n@@ -1 +1 @@\n-old\n+new\n"
                "diff --git a/src/foo.py b/src/foo.py\n"
                "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -1 +1 @@\n-a\n+b\n"
            )

    captured: dict[str, str] = {}

    class CaptureReviewer:
        name = "correctness"

        def review(self, *, repo: str, pr_id: int, diff: str, head_sha: str = ""):
            del repo, pr_id, head_sha
            captured["diff"] = diff
            return ReviewerResult()

    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=DiffVcs(), reviewers=[CaptureReviewer()], policy=policy).compile()
    graph.invoke(
        {
            "request": {"repo": "cqm/emotivoo-v2", "pr_id": 7, "head_sha": "abc"},
            "findings": [],
        }
    )
    assert "composer.lock" not in captured["diff"]
    assert "src/foo.py" in captured["diff"]


def test_scope_aplica_max_files_per_pr() -> None:
    class DiffVcs(FakeVcs):
        def get_diff(self, repo: str, pr_id: int) -> str:
            del repo, pr_id
            return (
                "diff --git a/src/a.py b/src/a.py\n"
                "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-a\n+b\n"
                "diff --git a/src/b.py b/src/b.py\n"
                "--- a/src/b.py\n+++ b/src/b.py\n@@ -1 +1 @@\n-a\n+b\n"
            )

        def get_diffstat(self, repo: str, pr_id: int) -> list[tuple[str, int, int]]:
            del repo, pr_id
            return [("src/a.py", 1, 0), ("src/b.py", 100, 0)]

    captured: dict[str, str] = {}

    class CaptureReviewer:
        name = "correctness"

        def review(self, *, repo: str, pr_id: int, diff: str, head_sha: str = ""):
            del repo, pr_id, head_sha
            captured["diff"] = diff
            return ReviewerResult()

    policy = PolicyEngine(
        {
            "version": 1,
            "max_autonomy": "report",
            "default_action": "report",
            "rules": [],
            "guards": {"max_files_per_pr": 1},
        }
    )
    graph = build_graph(vcs=DiffVcs(), reviewers=[CaptureReviewer()], policy=policy).compile()
    graph.invoke(
        {
            "request": {"repo": "cqm/emotivoo-v2", "pr_id": 7, "head_sha": "abc"},
            "findings": [],
        }
    )
    # Se conserva el fichero con mas churn (src/b.py) y se descarta el otro.
    assert "src/b.py" in captured["diff"]
    assert "src/a.py" not in captured["diff"]


def test_grafo_avisa_cuando_la_fuente_estatica_no_esta() -> None:
    # 0 hallazgos con fuente declarada ausente NO es "sin hallazgos": el PR
    # recibe un aviso de review incompleta y el outcome no esta completo.
    vcs = FakeVcs()
    reviewers: list[ReviewerPort] = [
        StaticReviewer("standards", [], status=ToolingSourceStatus.MISSING)
    ]
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=vcs, reviewers=reviewers, policy=policy).compile()

    result = graph.invoke(
        {
            "request": {"repo": "cqm/emotivoo-v2", "pr_id": 7, "head_sha": "abc"},
            "findings": [],
        }
    )
    assert vcs.inlines == []
    assert len(vcs.comments) == 1
    assert "Review incompleta" in vcs.comments[0][2]
    assert "NO cubre" in vcs.comments[0][2]

    outcome = outcome_from_state(result)
    assert outcome.complete is False
    assert outcome.unavailable_sources == {"standards": ToolingSourceStatus.MISSING}


def test_grafo_con_hallazgos_anota_fuente_ausente_en_el_resumen() -> None:
    vcs = FakeVcs(anchors={"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)})
    reviewers: list[ReviewerPort] = [
        StaticReviewer("correctness", [make_finding()]),
        StaticReviewer("standards", [], status=ToolingSourceStatus.UNREADABLE),
    ]
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=vcs, reviewers=reviewers, policy=policy).compile()

    result = graph.invoke(
        {
            "request": {"repo": "cqm/emotivoo-v2", "pr_id": 7, "head_sha": "abc"},
            "findings": [],
        }
    )
    assert vcs.inlines
    assert "Review incompleta" in vcs.comments[0][2]
    assert outcome_from_state(result).complete is False


def test_grafo_completo_cuando_la_fuente_esta_presente() -> None:
    vcs = FakeVcs()
    reviewers: list[ReviewerPort] = [
        StaticReviewer("standards", [], status=ToolingSourceStatus.PRESENT)
    ]
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=vcs, reviewers=reviewers, policy=policy).compile()

    result = graph.invoke(
        {
            "request": {"repo": "cqm/emotivoo-v2", "pr_id": 7, "head_sha": "abc"},
            "findings": [],
        }
    )
    # Fuente presente y sin hallazgos: review completa y en silencio.
    assert vcs.comments == []
    assert outcome_from_state(result).complete is True


def test_teams_sin_webhook_no_falla() -> None:
    adapter = TeamsWebhookAdapter(webhook_url="")
    adapter.notify(title="t", body="b")
    adapter.notify_decision_pending(title="t", body="b")


def test_idempotencia_con_store_en_memoria() -> None:
    store = InMemoryIdempotencyStore()
    assert store.claim("k") is True
    assert store.claim("k") is False


def _run(vcs: FakeVcs, *, pipeline_id: str = "") -> dict:
    reviewers: list[ReviewerPort] = [StaticReviewer("correctness", [make_finding()])]
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=vcs, reviewers=reviewers, policy=policy).compile()
    return graph.invoke(
        {
            "request": {
                "repo": "cqm/emotivoo-v2",
                "pr_id": 7,
                "head_sha": "abc",
                "pipeline_id": pipeline_id,
            },
            "findings": [],
        }
    )


def test_publish_no_duplica_si_ya_publico_para_el_commit() -> None:
    # Un `pipeline_id` nuevo sobre el mismo commit (hilo distinto) no debe
    # re-publicar: la marca de publicacion embebida lo delata.
    vcs = FakeVcs(anchors={"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)})
    _run(vcs, pipeline_id="7")
    inlines_primera = len(vcs.inlines)
    comments_primera = len(vcs.comments)
    assert inlines_primera == 1

    _run(vcs, pipeline_id="8")
    assert len(vcs.inlines) == inlines_primera, "no debe repetir el inline"
    assert len(vcs.comments) == comments_primera, "no debe repetir el resumen"


def test_marca_de_publicacion_lleva_el_token_del_commit() -> None:
    vcs = FakeVcs(anchors={"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)})
    result = _run(vcs)
    token = ReviewRequest.model_validate(result["request"]).dedupe_token
    assert token.startswith("reviews:")
    # El resumen identifica la review y su completitud.
    for _repo, _pr, body in vcs.comments:
        assert f"<!-- {token}:ok -->" in body
    # Cada inline lleva su marca, corta y por hallazgo (no solo el cuerpo).
    for _repo, _pr, _path, _line, body in vcs.inlines:
        assert f"<!-- {token}:i:" in body


def test_inlines_con_mismo_titulo_en_ficheros_distintos_no_se_confunden() -> None:
    title = "Falta test"
    findings = [
        make_finding(title=title, file="src/a.py").model_copy(update={"id": "a"}),
        make_finding(title=title, file="src/b.py").model_copy(update={"id": "b"}),
    ]
    anchors = {
        "src/a.py": InlineAnchor(path="src/a.py", line=3),
        "src/b.py": InlineAnchor(path="src/b.py", line=3),
    }
    vcs = FakeVcs(anchors=anchors)
    reviewers: list[ReviewerPort] = [StaticReviewer("correctness", findings)]
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=vcs, reviewers=reviewers, policy=policy).compile()
    graph.invoke(
        {
            "request": ReviewRequest(repo="r", pr_id=1, head_sha="abc"),
            "findings": [],
        }
    )
    # Mismo titulo y mismo why => mismos cuerpos; sin la marca por id, el segundo
    # inline se perderia. Deben publicarse los dos.
    assert len(vcs.inlines) == 2
    tokens = {body.rsplit("<!-- ", 1)[1] for _r, _p, _path, _line, body in vcs.inlines}
    assert len(tokens) == 2, "cada inline lleva una marca distinta"


def test_review_degradada_no_bloquea_una_completa_posterior() -> None:
    # Primera pasada sin artefacto (degradada): se publica con aviso y marca :partial.
    vcs = FakeVcs()
    degraded_reviewers: list[ReviewerPort] = [
        StaticReviewer("standards", [], status=ToolingSourceStatus.MISSING)
    ]
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=vcs, reviewers=degraded_reviewers, policy=policy).compile()
    request = {
        "repo": "cqm/emotivoo-v2",
        "pr_id": 7,
        "head_sha": "abc",
        "pipeline_id": "7",
    }
    graph.invoke({"request": request, "findings": []})
    assert len(vcs.comments) == 1
    assert ":partial -->" in vcs.comments[0][2]

    # Segunda pasada con datos completos (pipeline nuevo): la marca :partial no
    # debe bloquear; ahora la review sustituye a la degradada.
    clean_reviewers: list[ReviewerPort] = [
        StaticReviewer("correctness", [make_finding()], status=ToolingSourceStatus.PRESENT)
    ]
    graph2 = build_graph(vcs=vcs, reviewers=clean_reviewers, policy=policy).compile()
    graph2.invoke({"request": {**request, "pipeline_id": "8"}, "findings": []})
    assert len(vcs.comments) == 2, "la completa no debe quedar bloqueada por la degradada"
    assert ":ok -->" in vcs.comments[-1][2]


def test_dedupe_token_depende_del_commit_no_del_pipeline() -> None:
    base = dict(repo="r", pr_id=1, head_sha="abc")
    a = ReviewRequest(**base, pipeline_id="7")
    b = ReviewRequest(**base, pipeline_id="8")
    c = ReviewRequest(repo="r", pr_id=1, head_sha="def", pipeline_id="7")
    assert a.dedupe_token == b.dedupe_token
    assert a.dedupe_token != c.dedupe_token
    assert a.thread_id != b.thread_id, "un pipeline nuevo es un hilo nuevo"


class _FailingSummaryVcs(FakeVcs):
    """Falla al publicar el resumen la primera vez, como Teams/twg caidos."""

    def __init__(self) -> None:
        anchors = {"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)}
        super().__init__(anchors=anchors)
        self.fail_next_summary = True

    def post_comment(self, repo: str, pr_id: int, body: str) -> None:
        if self.fail_next_summary:
            self.fail_next_summary = False
            raise RuntimeError("resumen caido")
        super().post_comment(repo, pr_id, body)


def test_reintento_tras_fallo_del_resumen_no_duplica_el_inline() -> None:
    # Si el inline salio pero el resumen fallo, el reintento (gracia del
    # checkpointer) no debe repetir el inline ya publicado, y si escribir el
    # resumen que faltaba.
    vcs = _FailingSummaryVcs()
    reviewers: list[ReviewerPort] = [StaticReviewer("correctness", [make_finding()])]
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=vcs, reviewers=reviewers, policy=policy).compile()

    request = {"repo": "cqm/emotivoo-v2", "pr_id": 7, "head_sha": "abc", "pipeline_id": "7"}
    with contextlib.suppress(RuntimeError):
        # El fallo del resumen propaga; el worker lo reencola.
        graph.invoke({"request": request, "findings": []})

    assert len(vcs.inlines) == 1
    assert vcs.comments == []

    graph.invoke({"request": request, "findings": []})
    assert len(vcs.inlines) == 1, "no debe duplicar el inline ya publicado"
    assert len(vcs.comments) == 1, "debe completar el resumen que faltaba"


def _inline_body(vcs: FakeVcs) -> str:
    return vcs.inlines[0][4]


def test_inline_no_repite_el_titulo_en_el_cuerpo() -> None:
    # La persona estatica propaga el mensaje crudo de la herramienta como titulo
    # y como `why_it_matters`: no debe imprimirse dos veces.
    texto = "Cannot call method withdraw() on Balance|null."
    finding = make_finding(title=texto).model_copy(update={"why_it_matters": texto})
    vcs = FakeVcs(anchors={"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)})
    graph = build_graph(
        vcs=vcs,
        reviewers=[StaticReviewer("correctness", [finding])],
        policy=PolicyEngine.from_file("config/autonomy.yaml"),
    ).compile()
    graph.invoke({"request": {"repo": "r", "pr_id": 1, "head_sha": "abc"}, "findings": []})
    body = _inline_body(vcs)
    assert body.count(texto) == 1, "el cuerpo no debe repetir el titulo"
    assert "**Sugerencia:**" not in body


def test_inline_omite_la_politica_cuando_la_razon_es_generica() -> None:
    vcs = FakeVcs(anchors={"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)})
    graph = build_graph(
        vcs=vcs,
        reviewers=[StaticReviewer("correctness", [make_finding()])],
        policy=PolicyEngine.from_file("config/autonomy.yaml"),
    ).compile()
    graph.invoke({"request": {"repo": "r", "pr_id": 1, "head_sha": "abc"}, "findings": []})
    body = _inline_body(vcs)
    assert "_Politica:" not in body
    assert "_Accion: report_" in body


def test_inline_conserva_la_politica_cuando_aporta() -> None:
    # Un guard como `never_auto_paths` si da una razon que el revisor debe ver.
    path = ".github/workflows/ci.yml"
    finding = make_finding(file=path)
    vcs = FakeVcs(anchors={path: InlineAnchor(path=path, line=3)})
    graph = build_graph(
        vcs=vcs,
        reviewers=[StaticReviewer("correctness", [finding])],
        policy=PolicyEngine.from_file("config/autonomy.yaml"),
    ).compile()
    graph.invoke({"request": {"repo": "r", "pr_id": 1, "head_sha": "abc"}, "findings": []})
    body = _inline_body(vcs)
    assert "Ruta protegida" in body


def test_la_marca_del_inline_es_corta_y_por_hallazgo() -> None:
    vcs = FakeVcs(anchors={"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)})
    graph = build_graph(
        vcs=vcs,
        reviewers=[StaticReviewer("correctness", [make_finding()])],
        policy=PolicyEngine.from_file("config/autonomy.yaml"),
    ).compile()
    graph.invoke({"request": {"repo": "r", "pr_id": 1, "head_sha": "abc"}, "findings": []})
    body = _inline_body(vcs)
    marca = body.rsplit("<!-- ", 1)[1].removesuffix(" -->")
    # `reviews:<12 hex>:i:<8 base32>`: el id largo del hallazgo no llega al PR.
    assert marca.startswith("reviews:")
    assert ":i:" in marca
    assert len(marca) <= 40, f"marca demasiado larga: {marca}"
    assert "standards-" not in marca


def test_resumen_no_repite_el_detalle_de_los_inline() -> None:
    anclado = make_finding(title="Anclado", file="tests/FooTest.php")
    solo_resumen = make_finding(title="Sin ancla", file="tests/OtherTest.php")
    vcs = FakeVcs(anchors={"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)})
    graph = build_graph(
        vcs=vcs,
        reviewers=[StaticReviewer("correctness", [anclado, solo_resumen])],
        policy=PolicyEngine.from_file("config/autonomy.yaml"),
    ).compile()
    graph.invoke({"request": {"repo": "r", "pr_id": 1, "head_sha": "abc"}, "findings": []})
    resumen = vcs.comments[0][2]
    lineas = resumen.splitlines()
    idx_anclado = next(i for i, ln in enumerate(lineas) if "`tests/FooTest.php:3`" in ln)
    idx_resumen = next(i for i, ln in enumerate(lineas) if "`tests/OtherTest.php:3`" in ln)
    # El anclado no arrastra su detalle; el que solo va al resumen si.
    assert "No cubre el caso borde" not in lineas[idx_anclado + 1]
    assert "No cubre el caso borde" in lineas[idx_resumen + 1]
