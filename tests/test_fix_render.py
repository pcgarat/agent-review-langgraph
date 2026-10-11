from __future__ import annotations

from reviews.domain.models import (
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
)
from reviews.fix.delivery import DiffFormatter
from reviews.graph.build import _render_inline, _render_summary


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
        requires_verification=True,
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
