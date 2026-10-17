from __future__ import annotations

import contextlib

from langgraph.checkpoint.memory import InMemorySaver

from agent_review.domain.models import (
    AutofixClass,
    Finding,
    Owner,
    ReviewerResult,
    ReviewRequest,
    Severity,
    SkipReason,
    ToolingSourceStatus,
)
from agent_review.graph.build import build_graph, outcome_from_state
from agent_review.idempotency import InMemoryIdempotencyStore
from agent_review.policy.engine import PolicyEngine
from agent_review.ports.reviewer import ReviewerPort
from agent_review.ports.vcs import InlineAnchor, PullRequestInfo, ReviewMarker


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
        budget_exhausted: bool = False,
        tokens_used: int = 0,
    ) -> None:
        self.name = name
        self._findings = findings
        self._status = status
        self._budget_exhausted = budget_exhausted
        self._tokens_used = tokens_used

    def review(
        self,
        *,
        repo: str,
        pr_id: int,
        diff: str,
        head_sha: str = "",
        review_key: str = "",
    ) -> ReviewerResult:
        del repo, pr_id, diff, head_sha, review_key
        return ReviewerResult(
            findings=list(self._findings),
            tooling_status=self._status,
            budget_exhausted=self._budget_exhausted,
            tokens_used=self._tokens_used,
        )


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
        failure_scenario="escenario de prueba",
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


def test_el_hecho_del_corte_viaja_al_outcome() -> None:
    # P7/B4: una persona que se corta por presupuesto declara el hecho y su gasto;
    # el `ReviewOutcome` los expone (el render como `SkipReason.BUDGET` es de 012).
    vcs = FakeVcs(anchors={"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)})
    reviewers: list[ReviewerPort] = [
        StaticReviewer("correctness", [make_finding()], budget_exhausted=True, tokens_used=1234),
        StaticReviewer("security", [], tokens_used=10),
    ]
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=vcs, reviewers=reviewers, policy=policy, budget_total=10_000).compile()
    result = graph.invoke(
        {
            "request": {"repo": "cqm/emotivoo-v2", "pr_id": 7, "head_sha": "abc"},
            "findings": [],
        }
    )
    outcome = outcome_from_state(result)
    assert outcome.budget_exhausted == ["correctness"]
    assert outcome.tokens_spent == {"correctness": 1234, "security": 10}
    assert outcome.tokens_total == 1244
    assert outcome.tokens_remaining == 8756, "el saldo se deriva del tope y el gasto"


def test_sin_tope_no_se_puede_derivar_el_saldo() -> None:
    # Sin `budget_total` (tests y replay sin presupuesto) el gasto viaja igual y el
    # saldo queda `None`: no se consulta al puerto para pintarlo (017 B4).
    vcs = FakeVcs(anchors={})
    reviewers: list[ReviewerPort] = [StaticReviewer("correctness", [], tokens_used=42)]
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=vcs, reviewers=reviewers, policy=policy).compile()
    result = graph.invoke(
        {
            "request": {"repo": "cqm/emotivoo-v2", "pr_id": 7, "head_sha": "abc"},
            "findings": [],
        }
    )
    outcome = outcome_from_state(result)
    assert outcome.tokens_spent == {"correctness": 42}
    assert outcome.tokens_remaining is None


def test_sin_corte_no_hay_hecho_de_presupuesto() -> None:
    vcs = FakeVcs(anchors={})
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
    assert outcome.budget_exhausted == []
    assert outcome.tokens_spent == {}
    assert outcome.tokens_total == 0


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

        def review(
            self,
            *,
            repo: str,
            pr_id: int,
            diff: str,
            head_sha: str = "",
            review_key: str = "",
        ):
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


def test_scope_no_recorta_y_deja_la_cobertura_en_el_estado() -> None:
    # 012 C8: sin `max_files_per_pr`; todo lo que no es ruido llega a las personas,
    # ordenado por prioridad (config > code), y la cobertura viaja en el estado.
    class DiffVcs(FakeVcs):
        def get_diff(self, repo: str, pr_id: int) -> str:
            del repo, pr_id
            return (
                "diff --git a/src/a.py b/src/a.py\n"
                "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-a\n+b\n"
                "diff --git a/composer.lock b/composer.lock\n"
                "--- a/composer.lock\n+++ b/composer.lock\n@@ -1 +1 @@\n-a\n+b\n"
                "diff --git a/docker-compose.yml b/docker-compose.yml\n"
                "--- a/docker-compose.yml\n+++ b/docker-compose.yml\n@@ -1 +1 @@\n-a\n+b\n"
            )

    captured: dict[str, str] = {}

    class CaptureReviewer:
        name = "correctness"

        def review(
            self,
            *,
            repo: str,
            pr_id: int,
            diff: str,
            head_sha: str = "",
            review_key: str = "",
        ):
            del repo, pr_id, head_sha, review_key
            captured["diff"] = diff
            return ReviewerResult()

    policy = PolicyEngine(
        {"version": 1, "max_autonomy": "report", "default_action": "report", "rules": []}
    )
    graph = build_graph(vcs=DiffVcs(), reviewers=[CaptureReviewer()], policy=policy).compile()
    state = graph.invoke(
        {
            "request": {"repo": "cqm/emotivoo-v2", "pr_id": 7, "head_sha": "abc"},
            "findings": [],
        }
    )
    assert captured["diff"].index("docker-compose.yml") < captured["diff"].index("src/a.py")
    assert "composer.lock" not in captured["diff"]
    coverage = state["coverage"]
    assert coverage.files_total == 3
    assert coverage.skipped == {"composer.lock": SkipReason.IGNORED}
    assert not coverage.partial


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


def test_publish_no_repite_la_misma_ejecucion() -> None:
    # El reintento de la MISMA ejecucion (mismo `pipeline_id`) no re-publica: la
    # marca de publicacion embebida lo delata.
    vcs = FakeVcs(anchors={"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)})
    _run(vcs, pipeline_id="7")
    inlines_primera = len(vcs.inlines)
    comments_primera = len(vcs.comments)
    assert inlines_primera == 1

    _run(vcs, pipeline_id="7")
    assert len(vcs.inlines) == inlines_primera, "no debe repetir el inline"
    assert len(vcs.comments) == comments_primera, "no debe repetir el resumen"


def test_pipeline_nuevo_sobre_el_mismo_commit_publica_su_review() -> None:
    # Un `pipeline_id` nuevo sobre el mismo `sha` es una review nueva: se desea
    # que publique (corrige el "gana la primera marca por commit"). El commit
    # anterior no bloquea a la nueva ejecucion.
    vcs = FakeVcs(anchors={"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)})
    _run(vcs, pipeline_id="7")
    assert len(vcs.inlines) == 1 and len(vcs.comments) == 1

    _run(vcs, pipeline_id="8")
    assert len(vcs.inlines) == 2, "el pipeline nuevo publica su propio inline"
    assert len(vcs.comments) == 2, "y su propio resumen"


def test_marca_de_publicacion_lleva_el_token_de_la_ejecucion() -> None:
    vcs = FakeVcs(anchors={"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)})
    result = _run(vcs, pipeline_id="7")
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


def test_reintento_de_la_misma_ejecucion_degradada_no_repite_el_aviso() -> None:
    # Misma ejecucion, dos veces degradada: el segundo intento no vuelve a
    # escribir el resumen con aviso (marca :partial identica).
    vcs = FakeVcs()
    reviewers: list[ReviewerPort] = [
        StaticReviewer("standards", [], status=ToolingSourceStatus.MISSING)
    ]
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=vcs, reviewers=reviewers, policy=policy).compile()
    request = {
        "repo": "cqm/emotivoo-v2",
        "pr_id": 7,
        "head_sha": "abc",
        "pipeline_id": "7",
    }
    graph.invoke({"request": request, "findings": []})
    graph.invoke({"request": request, "findings": []})
    assert len(vcs.comments) == 1, "el reintento degradado no duplica el aviso"


def test_una_ejecucion_degradada_no_bloquea_su_completa_con_mismo_pipeline() -> None:
    # Carrera real: el pipeline publica el aviso (:partial) y, al llegar el
    # artefacto, la MISMA ejecucion produce la review completa. Como :ok no esta
    # en el PR, la completa si se publica y sustituye al aviso.
    vcs = FakeVcs(anchors={"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)})
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    request = {
        "repo": "cqm/emotivoo-v2",
        "pr_id": 7,
        "head_sha": "abc",
        "pipeline_id": "7",
    }
    degraded = build_graph(
        vcs=vcs,
        reviewers=[StaticReviewer("standards", [], status=ToolingSourceStatus.MISSING)],
        policy=policy,
    ).compile()
    degraded.invoke({"request": request, "findings": []})
    assert ":partial -->" in vcs.comments[0][2]

    complete = build_graph(
        vcs=vcs,
        reviewers=[StaticReviewer("correctness", [make_finding()])],
        policy=policy,
    ).compile()
    complete.invoke({"request": request, "findings": []})
    assert len(vcs.comments) == 2, "la completa sustituye al aviso degradado"
    assert ":ok -->" in vcs.comments[-1][2]

    # Y un tercer reintento de la ejecucion ya completa no re-publica.
    complete.invoke({"request": request, "findings": []})
    assert len(vcs.comments) == 2, "la ejecucion completa ya publicada no se repite"


def test_dedupe_token_se_ancla_a_la_ejecucion_no_al_commit() -> None:
    base = dict(repo="r", pr_id=1, head_sha="abc")
    a = ReviewRequest(**base, pipeline_id="7")
    b = ReviewRequest(**base, pipeline_id="8")
    c = ReviewRequest(repo="r", pr_id=1, head_sha="def", pipeline_id="7")
    # Mismo pipeline sobre el mismo sha => misma marca (reintento no re-publica).
    assert a.dedupe_token == ReviewRequest(**base, pipeline_id="7").dedupe_token
    # Pipeline nuevo sobre el mismo sha => marca nueva (review nueva, si publica).
    assert a.dedupe_token != b.dedupe_token
    # Commit distinto => marca distinta (el sha sigue dentro de la clave).
    assert a.dedupe_token != c.dedupe_token
    # El token coincide con el hilo: una ejecucion, una marca.
    assert a.dedupe_token != a.thread_id
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


# ---------------------------------------------------------------------------
# HITL (Fase 2): pausa, reanudacion y decision humana.
# ---------------------------------------------------------------------------


def _hitl_graph(vcs: FakeVcs, findings: list[Finding]):
    return build_graph(
        vcs=vcs,
        reviewers=[StaticReviewer("correctness", findings)],
        policy=PolicyEngine(
            {
                "version": 1,
                # `escalate` y `fix_with_approval` dejan pendiente; `fix_auto`
                # se degrada a aprobacion, asi que el pendiente siempre llega.
                "max_autonomy": "fix_with_approval",
                "default_action": "escalate",
                "rules": [],
            }
        ),
    ).compile(checkpointer=InMemorySaver())


def test_hitl_pausa_antes_de_publicar() -> None:

    vcs = FakeVcs(anchors={})
    graph = _hitl_graph(vcs, [make_finding()])
    config = {"configurable": {"thread_id": "r:1:abc"}}
    result = graph.invoke(
        {"request": {"repo": "r", "pr_id": 1, "head_sha": "abc"}, "findings": []}, config=config
    )
    # Pausada: hay interrupt pendiente y NO se publico nada.
    snapshot = graph.get_state(config)
    assert snapshot.next == ("hitl",)
    assert snapshot.interrupts, "debe haber un interrupt pendiente"
    assert vcs.comments == [] and vcs.inlines == []
    assert "__interrupt__" in result


def test_hitl_sin_pendientes_no_pausa() -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    vcs = FakeVcs(anchors={"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)})
    graph = build_graph(
        vcs=vcs,
        reviewers=[StaticReviewer("correctness", [make_finding()])],
        policy=PolicyEngine(
            {"version": 1, "max_autonomy": "report", "default_action": "report", "rules": []}
        ),
    ).compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "r:1:abc"}}
    graph.invoke(
        {"request": {"repo": "r", "pr_id": 1, "head_sha": "abc"}, "findings": []}, config=config
    )
    snapshot = graph.get_state(config)
    assert snapshot.next == ()
    assert not snapshot.interrupts
    assert vcs.comments, "sin pendientes publica como en Fase 1"


def test_hitl_reanuda_y_publica_con_la_decision() -> None:
    from langgraph.types import Command

    from agent_review.domain.models import DecisionOutcome, FindingDecision, HitlDecision

    vcs = FakeVcs(anchors={"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)})
    graph = _hitl_graph(vcs, [make_finding()])
    config = {"configurable": {"thread_id": "r:1:abc"}}
    graph.invoke(
        {"request": {"repo": "r", "pr_id": 1, "head_sha": "abc"}, "findings": []}, config=config
    )
    assert graph.get_state(config).interrupts

    decision = HitlDecision(
        review_key="r:1:abc",
        actor="alice",
        items=[FindingDecision(finding_id="f1", outcome=DecisionOutcome.APPROVED)],
    )
    graph.invoke(Command(resume=decision), config=config)

    snapshot = graph.get_state(config)
    assert snapshot.next == ()
    assert vcs.comments, "tras decidir se publica"
    assert len(vcs.inlines) == 1
    assert "Decision: aprobado · alice" in vcs.inlines[0][4]


def test_hitl_rechazo_va_al_resumen_sin_inline() -> None:
    from langgraph.types import Command

    from agent_review.domain.models import DecisionOutcome, FindingDecision, HitlDecision

    vcs = FakeVcs(anchors={"tests/FooTest.php": InlineAnchor(path="tests/FooTest.php", line=3)})
    graph = _hitl_graph(vcs, [make_finding()])
    config = {"configurable": {"thread_id": "r:1:abc"}}
    graph.invoke(
        {"request": {"repo": "r", "pr_id": 1, "head_sha": "abc"}, "findings": []}, config=config
    )
    decision = HitlDecision(
        review_key="r:1:abc",
        actor="alice",
        items=[FindingDecision(finding_id="f1", outcome=DecisionOutcome.REJECTED)],
    )
    graph.invoke(Command(resume=decision), config=config)

    assert vcs.inlines == [], "un hallazgo descartado no se anota inline"
    assert vcs.comments
    assert "Descartado por una persona" in vcs.comments[0][2]
    assert "Decision: descartado · alice" in vcs.comments[0][2]


def test_persona_con_cobertura_que_cae_entera_declara_todo_failed() -> None:
    # 012: si la persona que revisa por lotes agota los reintentos, no leyo nada;
    # eso se declara, o "sin hallazgos" se leeria como "revisado y limpio".
    from agent_review.errors import TransientError

    class DiffVcs(FakeVcs):
        def get_diff(self, repo: str, pr_id: int) -> str:
            del repo, pr_id
            return (
                "diff --git a/src/a.py b/src/a.py\n"
                "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-a\n+b\n"
            )

    class FallingReviewer:
        name = "correctness"
        declares_coverage = True

        def review(self, **kwargs) -> ReviewerResult:
            raise TransientError("modelo caido")

    from langgraph.types import RetryPolicy

    policy = PolicyEngine(
        {"version": 1, "max_autonomy": "report", "default_action": "report", "rules": []}
    )
    graph = build_graph(
        vcs=DiffVcs(),
        reviewers=[FallingReviewer()],
        policy=policy,
        retry_policy=RetryPolicy(max_attempts=1),
    ).compile()
    state = graph.invoke({"request": {"repo": "r", "pr_id": 7, "head_sha": "abc"}, "findings": []})
    assert state["coverage"].skipped == {"src/a.py": SkipReason.FAILED}
    assert state["coverage"].partial


class _CoverageVcs(FakeVcs):
    def get_diff(self, repo: str, pr_id: int) -> str:
        del repo, pr_id
        return "".join(
            f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-a\n+b\n"
            for p in ("docker-compose.yml", "src/a.py", "docs/guia.md", "composer.lock")
        )


class _CoverageReviewer:
    name = "correctness"
    declares_coverage = True

    def __init__(self, skipped: dict[str, SkipReason]) -> None:
        self._skipped = skipped

    def review(self, **kwargs) -> ReviewerResult:
        from agent_review.domain.models import ReviewCoverage

        all_files = ["docker-compose.yml", "src/a.py", "docs/guia.md"]
        reviewed = [p for p in all_files if p not in self._skipped]
        return ReviewerResult(coverage=ReviewCoverage(reviewed=reviewed, skipped=self._skipped))


def _coverage_run(skipped: dict[str, SkipReason]):
    vcs = _CoverageVcs()
    policy = PolicyEngine(
        {"version": 1, "max_autonomy": "report", "default_action": "report", "rules": []}
    )
    graph = build_graph(vcs=vcs, reviewers=[_CoverageReviewer(skipped)], policy=policy).compile()
    state = graph.invoke({"request": {"repo": "r", "pr_id": 7, "head_sha": "abc"}, "findings": []})
    return vcs, outcome_from_state(state)


def test_configuracion_sin_revisar_publica_review_parcial_aunque_no_haya_hallazgos() -> None:
    # 012 C7 (criterio 3): "sin hallazgos" con configuracion sin leer no es limpio.
    vcs, outcome = _coverage_run({"docker-compose.yml": SkipReason.BUDGET})

    assert not outcome.complete
    assert outcome.coverage is not None and outcome.coverage.partial
    [(_, _, body)] = vcs.comments
    assert "**Review parcial:**" in body
    assert "`docker-compose.yml` (presupuesto agotado)" in body
    assert "1 de configuración" in body
    # 4 ficheros en el diff, uno ignorado (composer.lock): 3 relevantes.
    assert "sin revisar 1 de 3 ficheros relevantes" in body
    assert "composer.lock" not in body
    assert ":partial" in body


def test_solo_docs_sin_revisar_no_hace_parcial_la_review() -> None:
    vcs, outcome = _coverage_run({"docs/guia.md": SkipReason.TOO_LARGE})

    assert outcome.complete
    # Sin hallazgos y completa: no se publica nada (como siempre).
    assert vcs.comments == []


def test_la_lista_plegable_de_docs_acompana_a_una_review_parcial() -> None:
    vcs, _ = _coverage_run({"src/a.py": SkipReason.FAILED, "docs/guia.md": SkipReason.TOO_LARGE})
    [(_, _, body)] = vcs.comments
    assert "1 de código: `src/a.py` (fallo del modelo)" in body
    assert "<details><summary>Sin revisar (no cambia el resultado)</summary>" in body
    assert "de documentación: `docs/guia.md` (demasiado grande para un lote)" in body
