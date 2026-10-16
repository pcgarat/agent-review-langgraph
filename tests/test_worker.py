from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent_review.adapters.diff_parser import best_line, file_key, parse_unified_diff
from agent_review.adapters.tooling_reviewer import ToolingReviewer
from agent_review.adapters.tooling_source import FileToolingOutputSource
from agent_review.domain.models import (
    ApprovalRequest,
    AutofixClass,
    DecisionOutcome,
    Finding,
    FindingDecision,
    HitlDecision,
    Owner,
    ReviewerResult,
    ReviewRequest,
    Severity,
)
from agent_review.domain.pending import InMemoryPendingStore
from agent_review.graph.build import build_approval as graph_build_approval
from agent_review.graph.build import build_graph, render_pause_notice
from agent_review.models_config import ModelsConfig
from agent_review.models_registry import Problem
from agent_review.policy.engine import PolicyEngine
from agent_review.ports.vcs import InlineAnchor, PullRequestInfo
from agent_review.profile import expected_tools
from agent_review.queue import InMemoryReviewQueue
from agent_review.settings import Settings
from agent_review.worker.main import (
    _consume,
    build_registry,
    build_reviewers,
    process_request,
    start_heartbeat,
    sweep_expired,
    sweep_pending,
    validate_models,
    verification_enabled,
)

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

    def get_file(self, repo: str, path: str, *, ref: str) -> str:
        del repo, path, ref
        return "$x = 1;\n"

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

    def list_review_markers(self, repo: str, pr_id: int) -> list:
        del repo, pr_id
        return []


class OneShotReviewer:
    name = "standards"

    def review(self, *, repo: str, pr_id: int, diff: str, head_sha: str = "") -> ReviewerResult:
        del repo, pr_id, diff, head_sha
        return ReviewerResult(
            findings=[
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
        )


def test_worker_procesa_cola_y_publica_inline() -> None:
    queue = InMemoryReviewQueue()
    vcs = FakeVcs()
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=vcs, reviewers=[OneShotReviewer()], policy=policy).compile()

    queue.enqueue(ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef"))
    request = queue.dequeue()
    assert request is not None
    process_request(graph, request)

    assert len(vcs.inlines) == 1
    assert vcs.comments


def test_build_vcs_eligio_github_segun_el_proveedor() -> None:
    from agent_review.adapters.github_vcs import GithubVcsAdapter
    from agent_review.worker.main import build_vcs

    settings = Settings(
        vcs_provider="github",
        github_owner="cqm",
        github_repo="emotivoo-v2",
        github_token="ghp_x",
    )
    assert isinstance(build_vcs(settings), GithubVcsAdapter)


def test_build_vcs_github_sin_token_falla_ruidoso() -> None:
    from agent_review.worker.main import build_vcs

    settings = Settings(vcs_provider="github", github_token="")
    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        build_vcs(settings)


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

    def list_review_markers(self, repo: str, pr_id: int) -> list:
        del repo, pr_id
        return []


def _settings_for(tmp_path: Path) -> Settings:
    profile = tmp_path / "profile.yaml"
    profile.write_text("repo: cqm/emotivoo-v2\nchecks:\n  typecheck: make stan\n", encoding="utf-8")
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
    assert [r.name for r in build_reviewers(Settings(repo_profile_config=str(lista)))] == ["null"]


_PROFILE_WITH_CHECKS = "repo: cqm/emotivoo-v2\nchecks:\n  typecheck: make stan\n"


def test_build_reviewers_llm_desactivado_por_defecto(tmp_path: Path) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text(_PROFILE_WITH_CHECKS, encoding="utf-8")
    settings = Settings(repo_profile_config=str(profile), llm_model="gpt-x", models_config="")
    names = [r.name for r in build_reviewers(settings)]
    assert names == ["standards"]


def test_build_reviewers_anade_correctness_si_se_habilita(tmp_path: Path) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text(_PROFILE_WITH_CHECKS, encoding="utf-8")
    settings = Settings(
        repo_profile_config=str(profile),
        llm_reviewer_enabled=True,
        llm_model="gpt-x",
        llm_api_key="k",
        models_config="",
    )
    names = [r.name for r in build_reviewers(settings)]
    assert names == ["standards", "correctness"]


def test_build_reviewers_llm_habilitado_sin_modelo_no_se_crea(tmp_path: Path) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text(_PROFILE_WITH_CHECKS, encoding="utf-8")
    settings = Settings(
        repo_profile_config=str(profile), llm_reviewer_enabled=True, models_config=""
    )
    assert [r.name for r in build_reviewers(settings)] == ["standards"]


def test_build_reviewers_bindea_cada_rol_a_su_modelo(tmp_path: Path) -> None:
    # Con registro declarativo, cada rol usa SU modelo: `correctness` y `security`
    # pueden correr en modelos distintos. El fan-out los construye a ambos.
    profile = tmp_path / "profile.yaml"
    profile.write_text(_PROFILE_WITH_CHECKS, encoding="utf-8")
    config = tmp_path / "models.yaml"
    config.write_text(
        "version: 1\n"
        "providers:\n"
        "  p:\n"
        "    base_url: https://example.test/v1\n"
        "models:\n"
        "  modelo-a: {provider: p, kind: chat}\n"
        "  modelo-b: {provider: p, kind: chat, json_mode: json_schema}\n"
        "bindings:\n"
        "  correctness: modelo-a\n"
        "  security: modelo-b\n",
        encoding="utf-8",
    )
    settings = Settings(
        repo_profile_config=str(profile),
        llm_reviewer_enabled=True,
        models_config=str(config),
    )
    names = [r.name for r in build_reviewers(settings)]
    assert names == ["standards", "correctness", "security"]


def test_build_registry_sin_habilitar_no_bindea_roles() -> None:
    registry = build_registry(Settings())
    assert registry.port_for("correctness") is None
    assert registry.config.models == {}


def test_sin_models_config_se_usa_el_registro_implicito_de_llm() -> None:
    """D7 de la iniciativa 005: un `.env` con solo `LLM_*` no cambia de proveedor.

    Sin declarar `MODELS_CONFIG`, el registro se sintetiza desde `LLM_*`; nunca se
    lee el `config/models.yaml` versionado (otro proveedor, otra clave).
    """
    settings = Settings(
        llm_reviewer_enabled=True,
        llm_model="gpt-x",
        llm_base_url="https://llm.example.test/v1",
        llm_api_key="k",
    )
    registry = build_registry(settings)
    assert registry.model_for("correctness") == "gpt-x"
    assert registry.model_for("fixer") == "gpt-x"


class _StubRegistry:
    def __init__(self, problems: list[Problem], *, bindings: dict[str, str] | None = None):
        self.config = ModelsConfig.model_construct(
            providers={}, models={}, bindings=bindings if bindings is not None else {"r": "m"}
        )
        self._problems = problems
        self.probed = False

    def probe(self) -> list[Problem]:
        self.probed = True
        return self._problems


def test_validate_models_falla_ante_un_problema_fatal() -> None:
    registry = _StubRegistry([Problem("p", "el modelo 'm' no aparece")])
    with pytest.raises(RuntimeError, match="no aparece"):
        validate_models(registry)  # type: ignore[arg-type]


def test_validate_models_solo_avisa_si_el_proveedor_no_responde(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = _StubRegistry([Problem("p", "otro texto cualquiera", fatal=False)])
    validate_models(registry)  # type: ignore[arg-type]
    assert "otro texto cualquiera" in caplog.text


def test_validate_models_sin_bindings_no_consulta() -> None:
    registry = _StubRegistry([], bindings={})
    validate_models(registry)  # type: ignore[arg-type]
    assert not registry.probed


@pytest.mark.parametrize(
    ("enabled", "url", "expected"),
    [
        (True, "http://sandbox-runner:8099", True),
        (False, "http://sandbox-runner:8099", False),
        (True, "", False),
    ],
)
def test_verification_enabled_exige_sandbox_y_runner(
    enabled: bool, url: str, expected: bool
) -> None:
    settings = Settings(sandbox_enabled=enabled, sandbox_runner_url=url)
    assert verification_enabled(settings) is expected


def test_worker_hasta_formato_publica_salida_del_perfil(tmp_path: Path) -> None:
    # Se usa la plantilla de produccion (Settings) y el artefacto se deposita en
    # la ruta que resuelve, para ejercitar la ruta por defecto de extremo a extremo.
    profile = tmp_path / "profile.yaml"
    profile.write_text("repo: cqm/emotivoo-v2\nchecks:\n  typecheck: make stan\n", encoding="utf-8")
    settings = Settings(repo_profile_config=str(profile))
    source = FileToolingOutputSource(template=settings.tooling_output_template, root=tmp_path)
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
    # la persona falla ruidoso, process_request propaga la excepcion y no publica.
    profile = tmp_path / "profile.yaml"
    profile.write_text("repo: cqm/emotivoo-v2\nchecks:\n  typecheck: make stan\n", encoding="utf-8")
    settings = Settings(repo_profile_config=str(profile))
    source = FileToolingOutputSource(template=settings.tooling_output_template, root=tmp_path)
    vcs = _DiffVcs(DIFF)
    reviewers = build_reviewers(settings, tooling_source=source)
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(vcs=vcs, reviewers=reviewers, policy=policy).compile()

    with pytest.raises(ValueError, match="head_sha"):
        process_request(graph, ReviewRequest(repo="cqm/emotivoo-v2", pr_id=5, head_sha=""))

    assert vcs.inlines == []
    assert vcs.comments == [], "ante sha vacio no se debe reportar 'sin hallazgos'"


def test_sweep_pending_encola_solo_las_degradadas(tmp_path: Path) -> None:
    # Una ingesta que nunca completa debe terminar ejecutandose (degradada), no
    # quedarse en PENDING para siempre.
    profile = tmp_path / "profile.yaml"
    profile.write_text("repo: cqm/emotivoo-v2\nchecks:\n  typecheck: make stan\n", encoding="utf-8")
    settings = Settings(repo_profile_config=str(profile))
    pending = InMemoryPendingStore(expected_tools=["phpstan", "php-cs-fixer"])
    queue = InMemoryReviewQueue()
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=5, head_sha="abc", pipeline_id="7")
    pending.observe(request, "phpstan", "{}", ttl_ready_s=0)

    enqueued = sweep_pending(settings, queue, pending=pending)

    assert [r.pipeline_id for r in enqueued] == ["7"]
    assert len(queue) == 1


class _FailingGraph:
    """Grafo que falla siempre, para ejercitar el reintento sin side effects."""

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, state):
        del state
        self.calls += 1
        raise RuntimeError("twg caido")


def test_consume_reencola_con_intento_incremental_ante_excepcion() -> None:
    # Un fallo transitorio (twg/Teams) no puede perder el job: se reencola con el
    # contador incrementado para no reintentar infinitamente.
    queue = InMemoryReviewQueue()
    request = ReviewRequest(repo="r", pr_id=1, head_sha="a", pipeline_id="7")
    _consume(queue, _FailingGraph(), request, max_attempts=3)
    retried = queue.dequeue()
    assert retried is not None
    assert retried.attempts == 1
    assert retried.review_key == request.review_key


def test_consume_descarta_el_job_al_agotar_intentos() -> None:
    queue = InMemoryReviewQueue()
    request = ReviewRequest(repo="r", pr_id=1, head_sha="a", pipeline_id="7", attempts=2)
    _consume(queue, _FailingGraph(), request, max_attempts=3)
    assert queue.dequeue() is None, "agotado el tope, el job se descarta"
    dead = queue.dead_letters()
    assert len(dead) == 1, "el job descartado queda en la DLQ, no solo en el log"
    assert dead[0]["review_key"] == request.review_key


def test_expected_tools_perfil_distinto_del_env_es_intencional(tmp_path: Path) -> None:
    # B4: el perfil manda. Con el `.env` en su valor por defecto, un perfil que
    # declara `expected_tools` no debe quedar tapado por el env.
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "repo: cqm/emotivoo-v2\nexpected_tools:\n  - phpstan\n  - rector\n", encoding="utf-8"
    )
    settings = Settings(repo_profile_config=str(profile))
    assert expected_tools(settings) == ["phpstan", "rector"]


def test_expected_tools_env_explicito_manda_sobre_el_perfil(tmp_path: Path) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text("repo: r\nexpected_tools:\n  - phpstan\n", encoding="utf-8")
    settings = Settings(repo_profile_config=str(profile), expected_tools="psalm")
    assert expected_tools(settings) == ["psalm"]


def test_expected_tools_api_y_worker_coinciden(tmp_path: Path) -> None:
    # La barrera de la API y la del worker deben ver el mismo conjunto (B4).
    profile = tmp_path / "profile.yaml"
    profile.write_text("repo: r\nexpected_tools:\n  - phpstan\n  - rector\n", encoding="utf-8")
    settings = Settings(repo_profile_config=str(profile))
    assert expected_tools(settings) == ["phpstan", "rector"]


def test_heartbeat_refresca_el_fichero_desde_un_hilo(tmp_path: Path) -> None:
    # B7: el latido debe seguir refrescandose mientras el worker esta ocupado en
    # una review larga (B3 es secuencial), no solo entre polls.
    path = tmp_path / "heartbeat"
    start_heartbeat(str(path), interval_s=0.01)
    deadline = time.monotonic() + 2.0
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert path.exists(), "el hilo de latido debe crear el fichero sin intervencion del bucle"
    first = path.read_text(encoding="utf-8")
    deadline = time.monotonic() + 2.0
    while path.read_text(encoding="utf-8") == first and time.monotonic() < deadline:
        time.sleep(0.005)
    assert path.read_text(encoding="utf-8") != first, "el latido debe refrescarse periodicamente"


class _CountingReviewer:
    """Persona que cuenta cuantas veces se le llama (para medir re-ejecuciones)."""

    name = "correctness"

    def __init__(self, finding: Finding) -> None:
        self._finding = finding
        self.calls = 0

    def review(self, *, repo: str, pr_id: int, diff: str, head_sha: str = "") -> ReviewerResult:
        del repo, pr_id, diff, head_sha
        self.calls += 1
        return ReviewerResult(findings=[self._finding])


def _escalating_finding() -> Finding:
    return Finding(
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


def _hitl_graph(vcs: FakeVcs, reviewer: _CountingReviewer):
    from langgraph.checkpoint.memory import InMemorySaver

    return build_graph(
        vcs=vcs,
        reviewers=[reviewer],
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


def test_worker_reentrega_pausada_no_reejecuta_scope_ni_personas() -> None:
    # §4.3: re-invocar con input mientras esta pausada re-ejecuta scope+personas
    # (fuga de tokens). La reentrega sin decision debe ser no-op.
    reviewer = _CountingReviewer(_escalating_finding())
    graph = _hitl_graph(FakeVcs(), reviewer)
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef")

    first = process_request(graph, request)
    assert first.paused, "las decisiones pendientes deben pausar la review"
    assert reviewer.calls == 1

    second = process_request(graph, request)
    assert second.paused
    assert reviewer.calls == 1, "la reentrega pausada no debe re-ejecutar las personas"


def test_worker_resume_aplica_command_y_publica() -> None:
    reviewer = _CountingReviewer(_escalating_finding())
    vcs = FakeVcs()
    graph = _hitl_graph(vcs, reviewer)
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef")
    process_request(graph, request)
    assert vcs.inlines == [] and vcs.comments == []

    decision = HitlDecision(
        review_key=request.review_key,
        actor="alice",
        items=[FindingDecision(finding_id="w1", outcome=DecisionOutcome.APPROVED)],
    )
    outcome = process_request(graph, request.model_copy(update={"decision": decision}))

    assert not outcome.paused
    assert outcome.human_decisions is not None
    assert len(vcs.inlines) == 1, "tras decidir se publica el inline"
    assert vcs.comments, "y el resumen"
    assert reviewer.calls == 1, "reanudar no vuelve a ejecutar las personas"


def test_worker_doble_decision_es_noop() -> None:
    reviewer = _CountingReviewer(_escalating_finding())
    vcs = FakeVcs()
    graph = _hitl_graph(vcs, reviewer)
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef")
    process_request(graph, request)

    decision = HitlDecision(
        review_key=request.review_key,
        actor="alice",
        items=[FindingDecision(finding_id="w1", outcome=DecisionOutcome.APPROVED)],
    )
    process_request(graph, request.model_copy(update={"decision": decision}))
    published = (len(vcs.inlines), len(vcs.comments))

    repeated = process_request(graph, request.model_copy(update={"decision": decision}))

    assert not repeated.paused
    assert (len(vcs.inlines), len(vcs.comments)) == published, "no se republica"
    assert reviewer.calls == 1


class _RecordingChat:
    def __init__(self) -> None:
        self.pending: list[ApprovalRequest] = []
        self.pending_links: list[str | None] = []
        self.notices: list[str] = []

    def notify(
        self,
        *,
        repo: str,
        pr_id: int,
        review_key: str,
        title: str,
        body: str,
        link: str | None = None,
    ) -> None:
        del repo, pr_id, review_key, body, link
        self.notices.append(title)

    def notify_decision_pending(
        self,
        *,
        repo: str,
        pr_id: int,
        review_key: str,
        approval: ApprovalRequest,
        link: str | None = None,
    ) -> None:
        del repo, pr_id, review_key
        self.pending.append(approval)
        self.pending_links.append(link)


def test_pausa_escribe_el_inbox_y_avisa_sin_publicar() -> None:
    from agent_review.paused_store import InMemoryPausedStore

    reviewer = _CountingReviewer(_escalating_finding())
    vcs = FakeVcs()
    chat = _RecordingChat()
    paused_store = InMemoryPausedStore()
    graph = _hitl_graph(vcs, reviewer)
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef")

    outcome = process_request(graph, request, paused=paused_store, chat=chat)

    assert outcome.paused
    assert vcs.inlines == [] and vcs.comments == [], "la pausa no publica los hallazgos"
    recorded = paused_store.get(request.review_key)
    assert recorded is not None and recorded.approval.pending[0].finding_id == "w1"
    # El aviso lleva la `ApprovalRequest` estructurada (C12), no un texto.
    assert len(chat.pending) == 1
    approval = chat.pending[0]
    assert approval.review_key == request.review_key
    assert [item.finding_id for item in approval.pending] == ["w1"]
    assert approval.command_hint.strip()


def test_la_pausa_deja_el_aviso_en_la_outbox_sin_red() -> None:
    """T1: el egress es una outbox, no una llamada HTTP.

    El worker deja un `ChatNotice` de pausa observable en la cola (sin HTTP ni
    SDK); el bot lo consumira despues. Es el aviso de pausa con la carga
    **estructurada** (C12) el que viaja.
    """
    from agent_review.chat_outbox import InMemoryChatOutbox, OutboxChatAdapter
    from agent_review.paused_store import InMemoryPausedStore

    outbox = InMemoryChatOutbox()
    review_key = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef").review_key
    graph = _hitl_graph(FakeVcs(), _CountingReviewer(_escalating_finding()))

    outcome = process_request(
        graph,
        ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef"),
        paused=InMemoryPausedStore(),
        chat=OutboxChatAdapter(outbox=outbox),
    )

    assert outcome.paused
    notice = outbox.dequeue()
    assert notice is not None
    assert notice.kind == "pause"
    assert notice.review_key == review_key
    assert notice.repo == "cqm/emotivoo-v2" and notice.pr_id == 9
    assert notice.approval is not None
    assert [item.finding_id for item in notice.approval.pending] == ["w1"]
    assert len(outbox) == 0


def test_el_aviso_de_pausa_lleva_enlace_al_codigo_del_hallazgo() -> None:
    """T2: el canal pinta el enlace; quien conoce el permalink es el worker."""
    from agent_review.chat_outbox import InMemoryChatOutbox, OutboxChatAdapter
    from agent_review.paused_store import InMemoryPausedStore

    class VcsConPermalink(FakeVcs):
        def file_url(self, repo: str, path: str, *, ref: str, line: int) -> str | None:
            return f"https://bb/{repo}/src/{ref}/{path}#lines-{line}"

    outbox = InMemoryChatOutbox()
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef")
    graph = _hitl_graph(VcsConPermalink(), _CountingReviewer(_escalating_finding()))

    process_request(
        graph,
        request,
        paused=InMemoryPausedStore(),
        chat=OutboxChatAdapter(outbox=outbox),
        vcs=VcsConPermalink(),
    )

    notice = outbox.dequeue()
    assert notice is not None
    item = notice.approval.pending[0]
    assert item.code_url == f"https://bb/{request.repo}/src/deadbeef/{item.file}#lines-{item.line}"
    # El contexto del hallazgo viaja tambien para poder explicar el problema.
    assert item.why_it_matters


def test_un_vcs_sin_permalink_no_rompe_el_aviso() -> None:
    """Un doble de test (o un host sin URL web) deja el pendiente sin enlace."""
    from agent_review.chat_outbox import InMemoryChatOutbox, OutboxChatAdapter
    from agent_review.paused_store import InMemoryPausedStore

    outbox = InMemoryChatOutbox()
    graph = _hitl_graph(FakeVcs(), _CountingReviewer(_escalating_finding()))

    process_request(
        graph,
        ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef"),
        paused=InMemoryPausedStore(),
        chat=OutboxChatAdapter(outbox=outbox),
    )

    notice = outbox.dequeue()
    assert notice is not None and notice.approval is not None
    assert notice.approval.pending[0].code_url is None


def test_una_outbox_caida_no_tumba_la_pausa() -> None:
    """El respaldo real es el CLI (C9): un fallo del canal no rompe el worker."""
    import redis

    from agent_review.chat_outbox import OutboxChatAdapter, RedisChatOutbox
    from agent_review.paused_store import InMemoryPausedStore

    outbox = RedisChatOutbox(url="redis://localhost:6379/0")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise redis.exceptions.ConnectionError("redis caido")

    outbox._client.rpush = _boom  # type: ignore[method-assign]
    paused_store = InMemoryPausedStore()
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef")
    graph = _hitl_graph(FakeVcs(), _CountingReviewer(_escalating_finding()))

    outcome = process_request(
        graph, request, paused=paused_store, chat=OutboxChatAdapter(outbox=outbox)
    )

    assert outcome.paused
    assert paused_store.get(request.review_key) is not None, "la pausa queda registrada igual"


class _RaisingChat:
    """`ChatPort` cuyo aviso de pausa falla con la excepcion dada."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def notify(self, **_kwargs: object) -> None:
        return None

    def notify_decision_pending(self, **_kwargs: object) -> None:
        raise self._exc


def test_un_fallo_permanente_del_aviso_de_pausa_no_tumba_la_pausa() -> None:
    from agent_review.paused_store import InMemoryPausedStore

    paused_store = InMemoryPausedStore()
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef")
    graph = _hitl_graph(FakeVcs(), _CountingReviewer(_escalating_finding()))

    outcome = process_request(
        graph, request, paused=paused_store, chat=_RaisingChat(ValueError("payload"))
    )

    assert outcome.paused
    assert paused_store.get(request.review_key) is not None


def test_un_fallo_transitorio_del_aviso_de_pausa_reintenta_el_job() -> None:
    from agent_review.errors import TransientError
    from agent_review.paused_store import InMemoryPausedStore

    paused_store = InMemoryPausedStore()
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef")
    graph = _hitl_graph(FakeVcs(), _CountingReviewer(_escalating_finding()))

    with pytest.raises(TransientError):
        process_request(
            graph, request, paused=paused_store, chat=_RaisingChat(TransientError("canal"))
        )

    assert paused_store.get(request.review_key) is not None, "la pausa se registro antes"


def test_sin_teams_no_hay_canal() -> None:
    from agent_review.worker.main import build_chat

    assert build_chat(Settings(teams_enabled=False)) is None


@pytest.mark.parametrize("redis_up", [True, False])
def test_con_teams_la_outbox_es_la_de_redis_aunque_redis_este_caido(
    monkeypatch: pytest.MonkeyPatch, redis_up: bool
) -> None:
    """Una outbox en memoria perderia todos los avisos hasta reiniciar el worker."""
    from agent_review.chat_outbox import OutboxChatAdapter, RedisChatOutbox
    from agent_review.worker.main import build_chat

    monkeypatch.setattr(RedisChatOutbox, "available", lambda self: redis_up)

    chat = build_chat(Settings(teams_enabled=True, redis_url="redis://localhost:6399/0"))

    assert isinstance(chat, OutboxChatAdapter)
    assert isinstance(chat._outbox, RedisChatOutbox)


def test_resume_borra_la_pausa_del_inbox() -> None:
    from agent_review.paused_store import InMemoryPausedStore

    reviewer = _CountingReviewer(_escalating_finding())
    paused_store = InMemoryPausedStore()
    graph = _hitl_graph(FakeVcs(), reviewer)
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef")
    process_request(graph, request, paused=paused_store)
    assert paused_store.get(request.review_key) is not None

    decision = HitlDecision(
        review_key=request.review_key,
        actor="alice",
        items=[FindingDecision(finding_id="w1", outcome=DecisionOutcome.APPROVED)],
    )
    process_request(graph, request.model_copy(update={"decision": decision}), paused=paused_store)

    assert paused_store.get(request.review_key) is None, "al decidir, el inbox se limpia"


def test_aviso_de_pausa_sin_marca_de_dedupe() -> None:
    # Si el aviso llevara el token de dedupe, el `publish` tras decidir lo leeria
    # como review ya publicada y no publicaria nada (spec §4.5).
    reviewer = _CountingReviewer(_escalating_finding())
    vcs = FakeVcs()
    graph = _hitl_graph(vcs, reviewer)
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef")
    process_request(graph, request)

    config = {"configurable": {"thread_id": request.thread_id}}
    approval = graph_build_approval(graph.get_state(config).values)
    assert approval is not None
    notice = render_pause_notice(approval)
    assert request.dedupe_token not in notice
    assert ":ok" not in notice and ":partial" not in notice


def test_sweep_expired_reencola_sin_decision() -> None:
    from datetime import UTC, datetime, timedelta

    from agent_review.domain.models import ApprovalRequest, FindingAction, PendingDecision, Severity
    from agent_review.paused_store import InMemoryPausedStore

    paused = InMemoryPausedStore(ttl_s=60.0)
    queue = InMemoryReviewQueue()
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef")
    approval = ApprovalRequest(
        review_key=request.review_key,
        repo=request.repo,
        pr_id=request.pr_id,
        head_sha=request.head_sha,
        pending=[
            PendingDecision(
                finding_id="w1",
                proposed_action=FindingAction.ESCALATE,
                reason="Tema protegido",
                matched_rule="default",
                policy_version="1",
                title="T",
                file="a.php",
                line=1,
                severity=Severity.P1,
            )
        ],
    )
    paused.pause(request, approval)

    # Recien creada: nada que expirar.
    assert sweep_expired(paused, queue) == []
    assert len(queue) == 0

    future = datetime.now(UTC) + timedelta(seconds=120)
    enqueued = sweep_expired(paused, queue, now=future)

    assert [r.review_key for r in enqueued] == [request.review_key]
    job = queue.dequeue()
    assert job is not None and job.decision is not None
    assert job.decision.actor == "system:ttl"
    assert job.decision.items[0].outcome is DecisionOutcome.EXPIRED
    assert paused.get(request.review_key) is not None, "el sweep no borra; lo limpia el resume"


# ---------------------------------------------------------------------------
# Fase 3-E7 (F17): worker kind-aware. La pausa de fix encola la wave y reanuda
# con el `FixResolutions`; nunca pasa por el HITL.
# ---------------------------------------------------------------------------


def _approve(finding_id: str) -> HitlDecision:
    return HitlDecision(
        review_key=ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef").review_key,
        actor="alice",
        items=[FindingDecision(finding_id=finding_id, outcome=DecisionOutcome.APPROVED)],
    )


class _FixVcs(FakeVcs):
    """VCS con `a.php:1` en el diff y su contenido, para que el parche valide."""

    def get_diff(self, repo: str, pr_id: int) -> str:
        del repo, pr_id
        return (
            "diff --git a/a.php b/a.php\n"
            "--- a/a.php\n"
            "+++ b/a.php\n"
            "@@ -1,1 +1,1 @@\n"
            "-$x = 0;\n"
            "+$x = 1;\n"
        )

    def get_file(self, repo: str, path: str, *, ref: str) -> str:
        del repo, path, ref
        return "$x = 0;\n"


def _fix_graph(vcs: FakeVcs):
    """Grafo con un hallazgo aprobable que pide fix y un fixer que propone parche."""
    from langgraph.checkpoint.memory import InMemorySaver

    from agent_review.fix.delivery import DiffFormatter
    from agent_review.fix.patch import build_patch

    class _Fixer:
        def propose(self, finding: Finding, *, source: str, previous) -> object:
            return build_patch(
                finding, source=source, start_line=1, end_line=1, replacement="+use X;"
            )

    finding = _escalating_finding().model_copy(
        update={"autofix_class": AutofixClass.GATED_AUTO, "severity": Severity.P2}
    )
    reviewer = _CountingReviewer(finding)
    return build_graph(
        vcs=vcs,
        reviewers=[reviewer],
        policy=PolicyEngine(
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
        ),
        fixer=_Fixer(),
        delivery=DiffFormatter(),
        fix_recipes=["typecheck"],
    ).compile(checkpointer=InMemorySaver())


def test_pausa_de_fix_registra_inbox_y_encola_la_wave() -> None:
    from agent_review.domain.paused import PauseKind
    from agent_review.paused_store import InMemoryPausedStore
    from agent_review.queue import InMemoryFixQueue

    vcs = _FixVcs()
    graph = _fix_graph(vcs)
    store = InMemoryPausedStore()
    fix_queue = InMemoryFixQueue()
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef")

    # 1) Sin decision: pausa humana (aun no hay wave).
    assert process_request(graph, request, paused=store, fix_queue=fix_queue).paused
    assert len(fix_queue) == 0
    assert store.get(request.review_key).kind is PauseKind.HUMAN

    # 2) Con aprobacion: el fix se despacha, se publica el resultado y pausa de fix.
    outcome = process_request(
        graph,
        request.model_copy(update={"decision": _approve("w1")}),
        paused=store,
        fix_queue=fix_queue,
    )

    assert outcome.paused, "ahora la review espera la wave (pausa de fix)"
    recorded = store.get(request.review_key)
    assert recorded is not None
    assert recorded.kind is PauseKind.FIX
    assert recorded.fix_request.jobs[0].finding_id == "w1"
    wave = fix_queue.dequeue()
    assert wave is not None and wave.review_key == request.review_key
    assert wave.wave.jobs[0].run_key.endswith(":w1:1")


def test_reentrega_pausada_de_fix_no_reejecuta_scope_ni_personas() -> None:
    from agent_review.paused_store import InMemoryPausedStore
    from agent_review.queue import InMemoryFixQueue

    graph = _fix_graph(_FixVcs())
    store = InMemoryPausedStore()
    fix_queue = InMemoryFixQueue()
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef")
    process_request(graph, request, paused=store, fix_queue=fix_queue)
    process_request(
        graph,
        request.model_copy(update={"decision": _approve("w1")}),
        paused=store,
        fix_queue=fix_queue,
    )
    assert len(fix_queue) == 1

    # Reentrega sin `fix_resolution`: no-op, no re-ejecuta ni re-encola.
    again = process_request(graph, request, paused=store, fix_queue=fix_queue)
    assert again.paused
    assert len(fix_queue) == 1, "la wave no se duplica"


def test_resume_de_fix_aplica_la_wave_y_publica_el_seguimiento() -> None:
    from agent_review.domain.models import CommandResult, FixResolution, FixResolutions
    from agent_review.paused_store import InMemoryPausedStore
    from agent_review.queue import InMemoryFixQueue

    vcs = _FixVcs()
    graph = _fix_graph(vcs)
    store = InMemoryPausedStore()
    fix_queue = InMemoryFixQueue()
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef")
    process_request(graph, request, paused=store, fix_queue=fix_queue)
    process_request(
        graph,
        request.model_copy(update={"decision": _approve("w1")}),
        paused=store,
        fix_queue=fix_queue,
    )
    followups_before = [c for c in vcs.comments if ":fix -->" in c]
    assert followups_before == []

    resolution = FixResolutions(
        items=[
            FixResolution(
                run_key=f"{request.review_key}:w1:1",
                outcome="passed",
                checks=[
                    CommandResult(recipe="typecheck", argv=["make"], exit_code=0, duration_s=1.0)
                ],
                image_digest="sha256:abc",
            )
        ]
    )
    outcome = process_request(
        graph,
        request.model_copy(update={"fix_resolution": resolution}),
        paused=store,
        fix_queue=fix_queue,
    )

    assert not outcome.paused
    assert outcome.fixes["w1"].status.value == "verified"
    followups = [c for c in vcs.comments if ":fix -->" in c]
    assert len(followups) == 1, "el seguimiento se publica una sola vez"
    assert store.get(request.review_key) is None, "el inbox se limpia al resolver"


def test_reconcile_repone_pausa_de_fix_y_reencola_la_wave() -> None:
    from agent_review.paused_store import InMemoryPausedStore
    from agent_review.queue import InMemoryFixQueue
    from agent_review.worker.main import reconcile_paused

    graph = _fix_graph(_FixVcs())
    # Pausa de fix sin inbox: el registro se "perdio" (worker caido tras el checkpoint).
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef")
    process_request(graph, request)
    process_request(graph, request.model_copy(update={"decision": _approve("w1")}))
    store = InMemoryPausedStore()
    fix_queue = InMemoryFixQueue()

    restored = reconcile_paused(graph, store, fix_queue=fix_queue)

    assert restored == [request.thread_id]
    assert store.get(request.review_key) is not None
    assert len(fix_queue) == 1, "reponer la pausa de fix re-encola la wave"
    assert reconcile_paused(graph, store, fix_queue=fix_queue) == [], "idempotente"
    assert len(fix_queue) == 1, "no se re-encola dos veces"
    assert store.get(request.review_key).kind.value == "fix"


def test_sweep_expired_no_toca_las_pausas_de_fix() -> None:
    from datetime import UTC, datetime, timedelta

    from agent_review.domain.models import FixWaitRequest
    from agent_review.paused_store import InMemoryPausedStore

    paused = InMemoryPausedStore(ttl_s=60.0)
    queue = InMemoryReviewQueue()
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef")
    from agent_review.domain.paused import PauseKind

    paused.pause(
        request,
        FixWaitRequest(review_key=request.review_key, repo=request.repo, pr_id=9, head_sha="x"),
        kind=PauseKind.FIX,
    )

    future = datetime.now(UTC) + timedelta(seconds=120)
    assert sweep_expired(paused, queue, now=future) == []
    assert len(queue) == 0, "una pausa de fix no se reanuda como 'sin decision'"


# ---------------------------------------------------------------------------
# Fase 3-E7 (F20): idempotencia y durabilidad del handoff.
# ---------------------------------------------------------------------------


def _paused_fix_setup():
    from agent_review.paused_store import InMemoryPausedStore
    from agent_review.queue import InMemoryFixQueue

    vcs = _FixVcs()
    graph = _fix_graph(vcs)
    store = InMemoryPausedStore()
    fix_queue = InMemoryFixQueue()
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=9, head_sha="deadbeef")
    process_request(graph, request, paused=store, fix_queue=fix_queue)
    process_request(
        graph,
        request.model_copy(update={"decision": _approve("w1")}),
        paused=store,
        fix_queue=fix_queue,
    )
    return graph, vcs, store, fix_queue, request


def test_resume_de_fix_con_run_key_ajeno_es_noop() -> None:
    """R2: una resolucion que el hilo no espera no se aplica."""
    from agent_review.domain.models import CommandResult, FixResolution, FixResolutions

    graph, vcs, store, fix_queue, request = _paused_fix_setup()
    stale = FixResolutions(
        items=[
            FixResolution(
                run_key=f"{request.review_key}:w9:1",  # de otra wave
                outcome="passed",
                checks=[CommandResult(recipe="t", argv=["x"], exit_code=0, duration_s=1.0)],
            )
        ]
    )

    outcome = process_request(
        graph,
        request.model_copy(update={"fix_resolution": stale}),
        paused=store,
        fix_queue=fix_queue,
    )

    assert outcome.paused, "no-op: el hilo sigue esperando su wave"
    assert store.get(request.review_key) is not None
    assert [c for c in vcs.comments if ":fix -->" in c] == []


def test_sweep_fix_expired_reanuda_como_no_verificable() -> None:
    from datetime import UTC, datetime, timedelta

    from agent_review.worker.main import sweep_fix_expired

    _, _, store, _, request = _paused_fix_setup()
    queue = InMemoryReviewQueue()
    # La pausa de fix usa `FIX_VERIFY_TTL_S`: se acorta para el test.
    current = store.get(request.review_key)
    assert current is not None
    current.ttl_s = 60.0

    assert sweep_fix_expired(store, queue) == []

    future = datetime.now(UTC) + timedelta(seconds=120)
    enqueued = sweep_fix_expired(store, queue, now=future)

    assert [r.review_key for r in enqueued] == [request.review_key]
    job = queue.dequeue()
    assert job is not None and job.fix_resolution is not None
    assert job.fix_resolution.items[0].outcome == "error"
    assert job.fix_resolution.items[0].run_key.endswith(":w1:1")
    assert store.get(request.review_key) is not None, "el sweep no borra; lo limpia el resume"


def test_sweep_fix_expired_es_idempotente() -> None:
    from datetime import UTC, datetime, timedelta

    from agent_review.worker.main import sweep_fix_expired

    _, _, store, _, request = _paused_fix_setup()
    queue = InMemoryReviewQueue()
    current = store.get(request.review_key)
    assert current is not None
    current.ttl_s = 60.0
    future = datetime.now(UTC) + timedelta(seconds=120)

    first = sweep_fix_expired(store, queue, now=future)
    second = sweep_fix_expired(store, queue, now=future)

    assert len(first) == 1
    assert second == [], "ya encolada; no se duplica el barrido"
