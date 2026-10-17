from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent_review.checkpointer import build_checkpointer, build_serde
from agent_review.domain.models import (
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
from agent_review.graph.build import build_graph
from agent_review.policy.engine import PolicyEngine
from agent_review.ports.vcs import InlineAnchor, PullRequestInfo, ReviewMarker
from agent_review.worker.main import process_request


class _MemoryVcs:
    """VCS falso que recuerda lo publicado, como haria Bitbucket."""

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
            url="u",
        )

    def get_diff(self, repo: str, pr_id: int) -> str:
        del repo, pr_id
        from diff_helpers import added_lines_diff

        return added_lines_diff(("a.php", 1))

    def get_file(self, repo: str, path: str, *, ref: str) -> str:
        del repo, path, ref
        return "$x = 0;\n"

    def resolve_inline_anchor(self, repo: str, pr_id: int, *, path: str, text: str):
        del repo, pr_id, text
        return InlineAnchor(path=path, line=1)

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
        # Devuelve lo publicado por este VCS, como haria Bitbucket: es lo que
        # evita duplicar un inline ya escrito cuando el resumen fallo despues.
        return [ReviewMarker(id="", body=body) for body in self.inlines + self.comments]


class _OneFinding:
    name = "standards"

    def review(
        self,
        *,
        repo: str,
        pr_id: int,
        diff: str,
        head_sha: str = "",
        review_key: str = "",
        context=None,
    ) -> ReviewerResult:
        del repo, pr_id, diff, head_sha
        return ReviewerResult(
            findings=[
                Finding(
                    id="f1",
                    title="T",
                    severity=Severity.P3,
                    file="a.php",
                    line=1,
                    why_it_matters="w",
                    autofix_class=AutofixClass.ADVISORY,
                    owner=Owner.HUMAN,
                    failure_scenario="escenario de prueba",
                    confidence=75,
                    evidence=["a.php:1 x"],
                )
            ]
        )


def _graph(vcs: _MemoryVcs):
    saver = build_checkpointer("")  # memoria, sin Postgres
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    return build_graph(vcs=vcs, reviewers=[_OneFinding()], policy=policy).compile(
        checkpointer=saver
    )


def test_serde_no_depende_del_msgpack_permisivo() -> None:
    # Si el serializer no tuviera allowlist, la deserializacion avisaria. Con la
    # allowlist explicita el roundtrip del estado de dominio es silencioso.
    serde = build_serde()
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="7")
    payload = serde.dumps_typed(request)
    assert serde.loads_typed(payload) == request


def test_thread_id_estable_por_ejecucion() -> None:
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="7")
    # Mismo request y un reintento (attempts distinto) comparten hilo.
    retry = request.model_copy(update={"attempts": 1})
    assert request.thread_id == retry.thread_id == "r:1:abc:7"


def _hitl_graph(vcs: _MemoryVcs):
    saver = build_checkpointer("")
    policy = PolicyEngine(
        {
            "version": 1,
            "max_autonomy": "fix_with_approval",
            "default_action": "escalate",
            "rules": [],
        }
    )
    return build_graph(vcs=vcs, reviewers=[_OneFinding()], policy=policy).compile(
        checkpointer=saver
    )


def test_hilo_pausado_serializa_sin_avisos(caplog) -> None:
    # El estado pausado lleva `ApprovalRequest`/`HitlDecision`: deben estar en la
    # allowlist del serde, o el roundtrip (lectura del hilo pausado, reanudacion)
    # avisa hoy y bloqueara en versiones futuras de LangGraph.
    import logging

    from agent_review.domain.models import DecisionOutcome, FindingDecision, HitlDecision

    vcs = _MemoryVcs()
    graph = _hitl_graph(vcs)
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="7")
    decision = HitlDecision(
        review_key=request.review_key,
        actor="alice",
        items=[FindingDecision(finding_id="f1", outcome=DecisionOutcome.APPROVED)],
    )

    with caplog.at_level(logging.WARNING, logger="langgraph.checkpoint.serde.jsonplus"):
        assert process_request(graph, request).paused
        resumed = process_request(graph, request.model_copy(update={"decision": decision}))

    assert not resumed.paused
    avisos = [r.message for r in caplog.records if "unregistered" in r.message]
    assert avisos == [], f"tipos HITL sin registrar en el serde: {avisos}"


def test_verifications_sobrevive_pausa_hitl_y_reanudacion(caplog) -> None:
    # 009 VF9: Verdict/VerificationResult en _STATE_TYPES; el veredicto viaja
    # por la pausa HITL y sigue ahí tras el resume.
    import logging

    from agent_review.ports.verifier import Verdict, VerificationResult

    class _P1Finding:
        name = "standards"

        def review(
            self,
            *,
            repo: str,
            pr_id: int,
            diff: str,
            head_sha: str = "",
            review_key: str = "",
            context=None,
        ) -> ReviewerResult:
            del repo, pr_id, diff, head_sha, review_key, context
            return ReviewerResult(
                findings=[
                    Finding(
                        id="f1",
                        title="T",
                        severity=Severity.P1,
                        file="a.php",
                        line=1,
                        why_it_matters="w",
                        autofix_class=AutofixClass.ADVISORY,
                        owner=Owner.HUMAN,
                        failure_scenario="escenario de prueba",
                        confidence=75,
                        evidence=["a.php:1 x"],
                    )
                ]
            )

    class _Confirming:
        name = "verifier"

        def verify(
            self, finding, *, repo: str, head_sha: str, diff: str = "", review_key: str = ""
        ):
            del repo, head_sha, diff, review_key
            return VerificationResult(
                finding_id=finding.id,
                verdict=Verdict.CONFIRMED,
                failure_scenario="estado X -> a.php:1 -> resultado incorrecto",
                evidence=["a.php:1 x"],
                files_read=["a.php"],
                reason="confirmado",
            )

    saver = build_checkpointer("")
    policy = PolicyEngine(
        {
            "version": 1,
            "max_autonomy": "fix_with_approval",
            "default_action": "escalate",
            "rules": [],
        }
    )
    vcs = _MemoryVcs()
    graph = build_graph(
        vcs=vcs,
        reviewers=[_P1Finding()],
        policy=policy,
        verifier=_Confirming(),
    ).compile(checkpointer=saver)
    request = ReviewRequest(repo="r", pr_id=9, head_sha="abc", pipeline_id="vf9")
    config = {"configurable": {"thread_id": request.thread_id}}
    decision = HitlDecision(
        review_key=request.review_key,
        actor="alice",
        items=[FindingDecision(finding_id="f1", outcome=DecisionOutcome.APPROVED)],
    )

    with caplog.at_level(logging.WARNING, logger="langgraph.checkpoint.serde.jsonplus"):
        assert process_request(graph, request).paused
        paused_state = graph.get_state(config)
        assert paused_state.values["verifications"]["f1"].verdict is Verdict.CONFIRMED
        resumed = process_request(graph, request.model_copy(update={"decision": decision}))

    assert not resumed.paused
    final_state = graph.get_state(config)
    assert final_state.values["verifications"]["f1"].verdict is Verdict.CONFIRMED
    avisos = [r.message for r in caplog.records if "unregistered" in r.message]
    assert avisos == [], f"tipos del verificador sin registrar en el serde: {avisos}"


def test_prune_poda_completados_y_respeta_pausados() -> None:
    from agent_review.checkpointer import prune_completed_threads

    saver = build_checkpointer("")  # memoria
    vcs = _MemoryVcs()
    graph = build_graph(
        vcs=vcs,
        reviewers=[_OneFinding()],
        policy=PolicyEngine(
            {
                "version": 1,
                "max_autonomy": "fix_with_approval",
                "default_action": "escalate",
                "rules": [],
            }
        ),
    ).compile(checkpointer=saver)

    completed = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="done")
    paused_req = ReviewRequest(repo="r", pr_id=2, head_sha="def", pipeline_id="paused")
    assert process_request(graph, completed).paused
    process_request(
        graph,
        completed.model_copy(
            update={
                "decision": HitlDecision(
                    review_key=completed.review_key,
                    actor="alice",
                    items=[FindingDecision(finding_id="f1", outcome=DecisionOutcome.APPROVED)],
                )
            }
        ),
    )
    assert process_request(graph, paused_req).paused

    # Retencion 0 dias no poda nada (evita borrar todo en un arranque mal configurado).
    assert prune_completed_threads(saver, older_than_days=0, paused_threads=[]) == []

    # Un reloj futuro hace "antiguos" a los completados; el pausado se respeta por
    # estar en `paused_threads`, aunque supere la retencion.
    future = datetime.now(UTC) + timedelta(days=60)
    pruned = prune_completed_threads(
        saver, older_than_days=30, paused_threads=[paused_req.review_key], now=future
    )
    assert completed.review_key in pruned
    assert paused_req.review_key not in pruned


def test_prune_nunca_poda_un_pausado_aunque_no_se_le_pase() -> None:
    from agent_review.checkpointer import prune_completed_threads

    saver = build_checkpointer("")
    vcs = _MemoryVcs()
    graph = build_graph(
        vcs=vcs,
        reviewers=[_OneFinding()],
        policy=PolicyEngine(
            {
                "version": 1,
                "max_autonomy": "fix_with_approval",
                "default_action": "escalate",
                "rules": [],
            }
        ),
    ).compile(checkpointer=saver)
    request = ReviewRequest(repo="r", pr_id=3, head_sha="xyz", pipeline_id="p")
    assert process_request(graph, request).paused

    # Sin `paused_threads`, la deteccion de `__interrupt__` en `pending_writes`
    # ya lo protege: un hilo con interrupt pendiente nunca es "completado".
    future = datetime.now(UTC) + timedelta(days=60)
    pruned = prune_completed_threads(saver, older_than_days=30, now=future)
    assert request.review_key not in pruned


def test_prune_threads_aplica_la_politica_inyectada() -> None:
    # T10: el motor de poda acepta cualquier `RetentionPolicy`. Una politica falsa
    # decide que borrar sin mirar la antiguedad, y el motor la obedece: es el
    # desacople que permite cambiar de criterio (o de backend) sin tocar el scan.
    from agent_review.checkpointer import prune_threads

    saver = build_checkpointer("")
    vcs = _MemoryVcs()
    graph = build_graph(
        vcs=vcs,
        reviewers=[_OneFinding()],
        policy=PolicyEngine(
            {
                "version": 1,
                "max_autonomy": "fix_with_approval",
                "default_action": "escalate",
                "rules": [],
            }
        ),
    ).compile(checkpointer=saver)
    request = ReviewRequest(repo="r", pr_id=9, head_sha="aaa", pipeline_id="keep")
    assert process_request(graph, request).paused

    class _Never:
        def can_prune(self, thread_id: str, latest, now) -> bool:
            del thread_id, latest, now
            return False

    # Aunque el hilo exista, una politica que nunca aprueba no borra nada.
    assert prune_threads(saver, _Never()) == []


def test_reentrega_del_mismo_thread_no_republica() -> None:
    vcs = _MemoryVcs()
    graph = _graph(vcs)
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="7")

    process_request(graph, request)
    assert len(vcs.inlines) == 1
    assert len(vcs.comments) == 1

    # Reentrega identica (p.ej. ack perdido): no debe duplicar.
    process_request(graph, request.model_copy(update={"attempts": 1}))
    assert len(vcs.inlines) == 1
    assert len(vcs.comments) == 1


class _MidRunFailVcs(_MemoryVcs):
    """Falla el resumen la primera vez, tras haber publicado el inline."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next_summary = True

    def post_comment(self, repo: str, pr_id: int, body: str) -> None:
        if self.fail_next_summary:
            self.fail_next_summary = False
            raise RuntimeError("resumen caido (transitorio)")
        super().post_comment(repo, pr_id, body)


def test_fallo_a_media_ejecucion_deja_el_hilo_incompleto() -> None:
    # La premisa del reintento: un nodo que falla a media ejecucion NO deja el
    # hilo "completado" (queda `next=('publish',)`), asi que el checkpointer no
    # puede saltarse la re-ejecucion. El job ya salio de la cola con BLPOP: solo
    # `_consume` impide perderlo.
    vcs = _MidRunFailVcs()
    graph = _graph(vcs)
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="7")

    with pytest.raises(RuntimeError):
        process_request(graph, request)

    config = {"configurable": {"thread_id": request.thread_id}}
    snapshot = graph.get_state(config)
    assert snapshot.next, "el hilo queda con el nodo pendiente, no completado"
    assert len(vcs.inlines) == 1
    assert vcs.comments == []


def test_reintento_con_checkpointer_completa_sin_duplicar_el_inline() -> None:
    # End-to-end del fallo parcial: el reintento re-ejecuta el nodo pendiente,
    # completa el resumen que faltaba y no repite el inline ya publicado (lo
    # detecta por la marca del commit leida del VCS).
    vcs = _MidRunFailVcs()
    graph = _graph(vcs)
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="7")

    with pytest.raises(RuntimeError):
        process_request(graph, request)

    process_request(graph, request.model_copy(update={"attempts": 1}))

    assert len(vcs.inlines) == 1, "no debe duplicar el inline ya publicado"
    assert len(vcs.comments) == 1, "debe escribir el resumen que faltaba"
    snapshot = graph.get_state({"configurable": {"thread_id": request.thread_id}})
    assert not snapshot.next, "el hilo queda completado"


# ---------------------------------------------------------------------------
# Fase 3-E7 (F21): poda kind-aware. Un hilo pausado en `fix_wait` no espera a
# nadie (su wave se resolvio o vencio y la review quedo publicada), asi que se
# poda; una pausa `human` sigue intocable.
# ---------------------------------------------------------------------------


class _FixVcs(_MemoryVcs):
    """VCS con `a.php` modificable en el diff, para que el parche valide."""

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


def _fix_graph(vcs: _MemoryVcs, saver=None):
    """Grafo con un hallazgo aprobable que pide fix y un fixer que propone parche."""
    from agent_review.fix.delivery import DiffFormatter
    from agent_review.fix.patch import build_patch

    class _Fixer:
        def propose(
            self,
            finding: Finding,
            *,
            source: str,
            previous,
            review_key: str = "",
        ) -> object:
            del previous
            return build_patch(
                finding, source=source, start_line=1, end_line=1, replacement="+use X;"
            )

    finding = Finding(
        id="f1",
        title="T",
        severity=Severity.P2,
        file="a.php",
        line=1,
        why_it_matters="w",
        autofix_class=AutofixClass.GATED_AUTO,
        owner=Owner.HUMAN,
        failure_scenario="escenario de prueba",
        confidence=75,
        evidence=["a.php:1 x"],
    )
    return build_graph(
        vcs=vcs,
        reviewers=[_OneFinder(finding)],
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
    ).compile(checkpointer=saver if saver is not None else build_checkpointer(""))


class _OneFinder:
    name = "standards"

    def __init__(self, finding: Finding) -> None:
        self._finding = finding

    def review(
        self,
        *,
        repo: str,
        pr_id: int,
        diff: str,
        head_sha: str = "",
        review_key: str = "",
        context=None,
    ) -> ReviewerResult:
        del repo, pr_id, diff, head_sha
        return ReviewerResult(findings=[self._finding])


def _approve(review_key: str, finding_id: str) -> HitlDecision:
    return HitlDecision(
        review_key=review_key,
        actor="alice",
        items=[FindingDecision(finding_id=finding_id, outcome=DecisionOutcome.APPROVED)],
    )


def test_pause_kind_distingue_fix_de_human() -> None:
    from agent_review.checkpointer import pause_kind_of, paused_thread_kinds

    saver = build_checkpointer("")
    graph = _fix_graph(_FixVcs(), saver=saver)

    human = ReviewRequest(repo="r", pr_id=1, head_sha="h", pipeline_id="human")
    fixing = ReviewRequest(repo="r", pr_id=2, head_sha="h", pipeline_id="fix")
    assert process_request(graph, human).paused  # sin decision: pausa humana
    # El fix pausa en human y, al aprobar, despacha y pausa en fix.
    assert process_request(graph, fixing).paused
    process_request(
        graph, fixing.model_copy(update={"decision": _approve(fixing.review_key, "f1")})
    )

    kinds = paused_thread_kinds(saver)
    assert kinds[human.thread_id] == "human"
    assert kinds[fixing.thread_id] == "fix"

    from agent_review.checkpointer import latest_checkpoints

    latest = latest_checkpoints(saver)
    assert latest is not None
    assert pause_kind_of(latest[human.thread_id]) == "human"
    assert pause_kind_of(latest[fixing.thread_id]) == "fix"


def test_prune_fix_poda_un_fix_vencido_pero_nunca_un_human() -> None:
    from agent_review.checkpointer import paused_thread_kinds, prune_fix_threads

    saver = build_checkpointer("")
    graph = _fix_graph(_FixVcs(), saver=saver)

    human = ReviewRequest(repo="r", pr_id=1, head_sha="h", pipeline_id="human")
    fixing = ReviewRequest(repo="r", pr_id=2, head_sha="h", pipeline_id="fix")
    assert process_request(graph, human).paused
    assert process_request(graph, fixing).paused
    process_request(
        graph, fixing.model_copy(update={"decision": _approve(fixing.review_key, "f1")})
    )

    kinds = paused_thread_kinds(saver)
    assert kinds[fixing.thread_id] == "fix"

    # Con un reloj futuro, el fix supera `max_age_s` y se poda; el human no.
    future = datetime.now(UTC) + timedelta(days=7)
    pruned = prune_fix_threads(saver, max_age_s=3600.0, paused_kinds=kinds, now=future)
    assert fixing.thread_id in pruned
    assert human.thread_id not in pruned
    assert "human" in paused_thread_kinds(saver).values(), "la pausa humana sigue viva"


def test_prune_fix_no_toca_un_fix_reciente() -> None:
    from agent_review.checkpointer import paused_thread_kinds, prune_fix_threads

    saver = build_checkpointer("")
    graph = _fix_graph(_FixVcs(), saver=saver)
    fixing = ReviewRequest(repo="r", pr_id=2, head_sha="h", pipeline_id="fix")
    assert process_request(graph, fixing).paused
    process_request(
        graph, fixing.model_copy(update={"decision": _approve(fixing.review_key, "f1")})
    )

    # Retencion `0` (o sin vencer) desactiva la poda: no se borra una wave viva.
    kinds = paused_thread_kinds(saver)
    assert prune_fix_threads(saver, max_age_s=0, paused_kinds=kinds) == []
    assert prune_fix_threads(saver, max_age_s=3600.0, paused_kinds=kinds) == []


def test_un_finding_de_antes_de_007_se_reanuda() -> None:
    """Un checkpoint guardado antes de 007 lleva `requires_verification` y un `P1`
    sin `failure_scenario`. Reanudar ese hilo no puede romper (007 D12, plan)."""
    import ormsgpack
    from langgraph.checkpoint.serde.jsonplus import EXT_PYDANTIC_V2, _msgpack_enc

    legacy = {
        "id": "f-legacy",
        "title": "T",
        "severity": "P1",
        "file": "a.php",
        "line": 1,
        "why_it_matters": "w",
        "autofix_class": "manual",
        "owner": "human",
        "requires_verification": True,
        "confidence": 75,
        "evidence": ["a.php:1 x"],
        "pre_existing": False,
        "suggested_fix": None,
        "protected_subject": None,
    }
    raw = ormsgpack.packb(
        ormsgpack.Ext(
            EXT_PYDANTIC_V2,
            _msgpack_enc(("agent_review.domain.models", "Finding", legacy, "model_validate_json")),
        )
    )
    finding = build_serde().loads_typed(("msgpack", raw))

    assert isinstance(finding, Finding)
    assert finding.id == "f-legacy"
    assert finding.failure_scenario is None
    assert "requires_verification" not in finding.model_dump()


def test_serde_roundtrip_de_los_campos_de_verificacion(caplog) -> None:
    from agent_review.domain.models import ContextScope, Verification

    finding = Finding(
        id="f",
        title="T",
        severity=Severity.P1,
        file="a.php",
        line=1,
        why_it_matters="w",
        autofix_class=AutofixClass.MANUAL,
        owner=Owner.HUMAN,
        confidence=100,
        evidence=["a.php:1 x"],
        verification=Verification.VERIFIED_STATIC,
        verification_evidence=["a.php:1 x"],
        context_scope=ContextScope.OUTSIDE_DIFF,
    )
    serde = build_serde()
    with caplog.at_level("WARNING"):
        assert serde.loads_typed(serde.dumps_typed(finding)) == finding
    assert not [r for r in caplog.records if "msgpack" in r.getMessage().lower()]


class _CoverageFinding(_OneFinding):
    """Persona que declara cobertura, como hara el `LlmReviewer` por lotes (012)."""

    def review(self, **kwargs) -> ReviewerResult:
        from agent_review.domain.models import ReviewCoverage, SkipReason

        result = super().review(**kwargs)
        return result.model_copy(
            update={
                "coverage": ReviewCoverage(
                    reviewed=["a.php"], skipped={"docker-compose.yml": SkipReason.BUDGET}
                )
            }
        )


def test_la_cobertura_sobrevive_a_pausa_y_reanudacion(caplog) -> None:
    import logging

    from agent_review.domain.models import DecisionOutcome, FindingDecision, SkipReason

    saver = build_checkpointer("")
    policy = PolicyEngine(
        {
            "version": 1,
            "max_autonomy": "fix_with_approval",
            "default_action": "escalate",
            "rules": [],
        }
    )
    graph = build_graph(vcs=_MemoryVcs(), reviewers=[_CoverageFinding()], policy=policy).compile(
        checkpointer=saver
    )
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="7")
    config = {"configurable": {"thread_id": request.thread_id}}

    with caplog.at_level(logging.WARNING, logger="langgraph.checkpoint.serde.jsonplus"):
        assert process_request(graph, request).paused
        paused = graph.get_state(config).values["coverage"]
        decision = HitlDecision(
            review_key=request.review_key,
            actor="alice",
            items=[FindingDecision(finding_id="f1", outcome=DecisionOutcome.APPROVED)],
        )
        process_request(graph, request.model_copy(update={"decision": decision}))

    assert paused.reviewed == ["a.php"]
    assert paused.skipped["docker-compose.yml"] is SkipReason.BUDGET
    assert graph.get_state(config).values["coverage"] == paused
    avisos = [r.message for r in caplog.records if "unregistered" in r.message]
    assert avisos == [], f"tipos de cobertura sin registrar en el serde: {avisos}"
