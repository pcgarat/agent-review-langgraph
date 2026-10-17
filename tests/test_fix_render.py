from __future__ import annotations

from agent_review.domain.models import (
    AutofixClass,
    CommandResult,
    Decision,
    DecisionOutcome,
    Finding,
    FindingAction,
    FindingDecision,
    FixResult,
    FixStatus,
    HitlDecision,
    Owner,
    ProposedPatch,
    Severity,
    Verification,
)
from agent_review.fix.delivery import DiffFormatter
from agent_review.graph.build import (
    _fix_token,
    _render_fix,
    _render_fix_followup,
    _render_inline,
    _render_summary,
)


def _finding() -> Finding:
    return Finding(
        id="f1",
        title="Import sin usar",
        severity=Severity.P2,
        file="src/Foo.php",
        line=2,
        why_it_matters="Ruido",
        autofix_class=AutofixClass.GATED_AUTO,
        owner=Owner.HUMAN,
        failure_scenario="escenario de prueba",
        confidence=75,
        evidence=["src/Foo.php:2 +use"],
        suggested_fix="Quita el import",
    )


def _decision() -> Decision:
    return Decision(
        finding_id="f1",
        action=FindingAction.FIX_WITH_APPROVAL,
        reason="r",
        matched_rule="rule:0",
        policy_version="1",
    )


def _human() -> HitlDecision:
    return HitlDecision(
        review_key="k",
        actor="alice",
        items=[FindingDecision(finding_id="f1", outcome=DecisionOutcome.APPROVED)],
    )


def _verified_fix() -> FixResult:
    patch = ProposedPatch(
        finding_id="f1",
        file="src/Foo.php",
        diff="diff --git a/src/Foo.php b/src/Foo.php\n@@ -1,2 +1,2 @@\n-a\n+b\n",
        start_line=2,
        end_line=2,
        replacement="use App\\Bar;",
    )
    return FixResult(
        finding_id="f1",
        status=FixStatus.VERIFIED,
        attempts=1,
        patch=patch,
        checks=[CommandResult(recipe="typecheck", argv=["make"], exit_code=0, duration_s=1.0)],
        image_digest="sha256:deadbeefcafebabe",
    )


def test_inline_verificado_lleva_el_bloque_del_formatter() -> None:
    body = _render_inline(
        _finding(),
        _decision(),
        human=_human(),
        fix=_verified_fix(),
        delivery=DiffFormatter(),
    )
    assert "```diff" in body
    assert "+b" in body
    assert "Verificado: typecheck" in body


def test_inline_fallido_lleva_el_log_del_check() -> None:
    fix = FixResult(
        finding_id="f1",
        status=FixStatus.FAILED,
        attempts=2,
        checks=[
            CommandResult(
                recipe="typecheck",
                argv=["make", "stan"],
                exit_code=1,
                duration_s=1.0,
                stderr_tail="Fatal error: no such class",
            )
        ],
        reason="typecheck fallo (exit 1)",
    )
    body = _render_inline(
        _finding(), _decision(), human=_human(), fix=fix, delivery=DiffFormatter()
    )
    assert "Fix no verificado" in body
    assert "no such class" in body


def test_inline_saltado_no_publica_sugerencia() -> None:
    fix = FixResult(finding_id="f1", status=FixStatus.SKIPPED, reason="supera max_fixes_per_pr=3")
    body = _render_inline(
        _finding(), _decision(), human=_human(), fix=fix, delivery=DiffFormatter()
    )
    assert "sin arreglo automatico" in body
    assert "```diff" not in body
    assert "```suggestion" not in body


def test_resumen_incluye_la_seccion_fixes() -> None:
    summary = _render_summary(
        [_finding()],
        [_decision()],
        inlined=[_finding()],
        fixes={"f1": _verified_fix()},
        human=_human(),
    )
    assert "### Fixes" in summary
    assert "verified: 1" in summary


def test_inline_muestra_severidad_efectiva_y_verificacion_degradada() -> None:
    # 011 G13: P1 degradado a P3 muestra severidad efectiva y linea de verificacion.
    finding = _finding().model_copy(
        update={"severity": Severity.P1, "verification": Verification.UNVERIFIED}
    )
    decision = Decision(
        finding_id="f1",
        action=FindingAction.REPORT,
        reason="sin verificacion: P1 degradado a P3",
        matched_rule="gate:degrade",
        policy_version="1",
        severity=Severity.P3,
        severity_reason="sin verificacion: P1 degradado a P3",
    )
    body = _render_inline(finding, decision)
    assert "**[P3]" in body
    assert "Verificacion:" in body
    assert "degradado a P3" in body


def test_inline_p3_nativo_no_lleva_verificacion() -> None:
    # 011 G13: fuera del umbral, sin degradar, no hay linea de verificacion.
    body = _render_inline(_finding(), _decision())
    assert "Verificacion:" not in body


def test_resumen_distingue_degradado_de_p3_nativo() -> None:
    degraded = _finding().model_copy(
        update={"id": "d1", "title": "Hipótesis", "severity": Severity.P1}
    )
    native = _finding().model_copy(update={"id": "n1", "title": "Ruido menor"})
    decisions = [
        Decision(
            finding_id="d1",
            action=FindingAction.REPORT,
            reason="sin verificacion: P1 degradado a P3",
            matched_rule="gate:degrade",
            policy_version="1",
            severity=Severity.P3,
            severity_reason="sin verificacion: P1 degradado a P3",
        ),
        Decision(
            finding_id="n1",
            action=FindingAction.REPORT,
            reason="r",
            matched_rule="rule:0",
            policy_version="1",
            severity=Severity.P2,
        ),
    ]
    summary = _render_summary([degraded, native], decisions)
    assert "degradado a P3" in summary
    assert "**[P3] Hipótesis**" in summary
    assert "degradado a P2" not in summary


# ---------------------------------------------------------------------------
# Fase 3-E7 (F22): render del `PENDING`, dedupe del seguimiento y log acotado.
# ---------------------------------------------------------------------------


def _pending_fix() -> FixResult:
    patch = ProposedPatch(
        finding_id="f1",
        file="src/Foo.php",
        diff="diff --git a/src/Foo.php b/src/Foo.php\n@@ -1,2 +1,2 @@\n-a\n+b\n",
        start_line=2,
        end_line=2,
        replacement="use App\\Bar;",
    )
    return FixResult(finding_id="f1", status=FixStatus.PENDING, attempts=1, patch=patch)


def test_inline_pendiente_se_rotula_en_verificacion() -> None:
    # E7: hay parche valido y la wave esta despachada; no es un fix resuelto ni un
    # fallo, asi que el inline lo rotula y no cae en "no verificado".
    body = _render_inline(
        _finding(), _decision(), human=_human(), fix=_pending_fix(), delivery=DiffFormatter()
    )
    assert "en verificacion" in body
    assert "no verificado" not in body
    assert "```diff" not in body, "aun no hay resultado verificado que entregar"


def test_render_fix_pendiente_no_cae_en_la_rama_de_fallo() -> None:
    text = _render_fix(_pending_fix(), delivery=DiffFormatter())
    assert "en verificacion" in text
    assert "no verificado" not in text


def test_resumen_refleja_el_pendiente_como_en_verificacion() -> None:
    summary = _render_summary(
        [_finding()],
        [_decision()],
        inlined=[_finding()],
        fixes={"f1": _pending_fix()},
        human=_human(),
    )
    assert "en verificacion" in summary


def test_seguimiento_lleva_marca_propia_y_no_duplica() -> None:
    from agent_review.domain.models import ReviewRequest

    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="7")
    body = _render_fix_followup(
        request, {"f1": _verified_fix()}, [_finding()], delivery=DiffFormatter()
    )
    assert _fix_token(request.dedupe_token) in body
    assert body.count(_fix_token(request.dedupe_token)) == 1
    assert "## Fix verificado" in body
    assert "```diff" in body


def test_fallo_lleva_el_log_acotado_al_limite() -> None:
    fix = FixResult(
        finding_id="f1",
        status=FixStatus.FAILED,
        attempts=2,
        checks=[
            CommandResult(
                recipe="typecheck",
                argv=["make", "stan"],
                exit_code=1,
                duration_s=1.0,
                stderr_tail="linea\n" * 5000,
            )
        ],
    )
    text = _render_fix(fix, delivery=DiffFormatter())
    # El log se acota a `_MAX_TAIL_CHARS` (1500): nunca se vuelca un stderr entero.
    assert len(text) < 2000
    assert text.count("linea") <= 260


def test_seguimiento_de_fallo_no_dice_verificado() -> None:
    from agent_review.domain.models import ReviewRequest

    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="7")
    fix = FixResult(
        finding_id="f1",
        status=FixStatus.NOT_VERIFIABLE,
        attempts=1,
        reason="runner inalcanzable",
    )
    body = _render_fix_followup(request, {"f1": fix}, [_finding()], delivery=DiffFormatter())
    assert "## Resultado del fix" in body
    assert "Fix verificado" not in body
    assert "no verificable" in body
