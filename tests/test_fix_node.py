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
    ProtectedSubject,
    ReviewRequest,
    Severity,
)
from agent_review.fix.delivery import DiffFormatter, SuggestionFormatter
from agent_review.fix.node import run_fixes
from agent_review.fix.patch import build_patch
from agent_review.policy.engine import PolicyEngine


class _Vcs:
    def __init__(self, *, source: str = "<?php\nuse App\\Baz;\nfunction foo() {\n") -> None:
        self.source = source
        self.diff = (
            "diff --git a/src/Foo.php b/src/Foo.php\n"
            "--- a/src/Foo.php\n"
            "+++ b/src/Foo.php\n"
            "@@ -1,3 +1,4 @@\n"
            " <?php\n"
            "+use App\\Bar;\n"
            " function foo() {\n"
        )

    def get_diff(self, repo: str, pr_id: int) -> str:
        return self.diff

    def get_file(self, repo: str, path: str, *, ref: str) -> str:
        return self.source


class _Fixer:
    def __init__(self, *, patch: ProposedPatch | None = None) -> None:
        self.calls = 0
        self._patch = patch

    def propose(
        self, finding: Finding, *, source: str, previous: FixResult | None
    ) -> ProposedPatch:
        self.calls += 1
        if self._patch is not None:
            return self._patch
        return build_patch(
            finding, source=source, start_line=2, end_line=2, replacement="use App\\Bar;"
        )


class _Sandbox:
    def __init__(self, *, passed: bool = True) -> None:
        self.passed = passed
        self.requests: list[str] = []

    def verify(self, request):  # noqa: ANN001 - fake
        self.requests.append(request.run_key)
        result = CommandResult(
            recipe="typecheck",
            argv=["make", "stan"],
            exit_code=0 if self.passed else 1,
            duration_s=1.0,
            stderr_tail="" if self.passed else "boom",
        )
        return type(
            "Report",
            (),
            {
                "run_key": request.run_key,
                "image_digest": "sha256:abc",
                "results": [result],
                "passed": self.passed,
            },
        )()


_FINDING = Finding(
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
    evidence=["src/Foo.php:2 +use App\\Bar;"],
)
_REQUEST = ReviewRequest(repo="cqm/x", pr_id=1, head_sha="abc", pipeline_id="7")


def _decision(fid: str, action: FindingAction = FindingAction.FIX_WITH_APPROVAL) -> Decision:
    return Decision(
        finding_id=fid, action=action, reason="r", matched_rule="rule:0", policy_version="1"
    )


def _human(fid: str = "f1", outcome: DecisionOutcome = DecisionOutcome.APPROVED) -> HitlDecision:
    return HitlDecision(
        review_key="k", actor="alice", items=[FindingDecision(finding_id=fid, outcome=outcome)]
    )


def _policy(guards: dict | None = None) -> PolicyEngine:
    return PolicyEngine(
        {
            "version": 1,
            "max_autonomy": "fix_with_approval",
            "default_action": "report",
            "guards": guards or {"max_fixes_per_pr": 3, "max_fix_attempts": 2, "max_fix_lines": 30},
        }
    )


def _run(
    *,
    fixer,
    sandbox,
    human=None,
    decisions=None,
    guards=None,
    policy: PolicyEngine | None = None,
    finding: Finding | None = None,
) -> dict[str, FixResult]:
    return run_fixes(
        request=_REQUEST,
        findings=[finding or _FINDING],
        decisions=decisions or [_decision("f1")],
        human=human or _human(),
        vcs=_Vcs(),  # type: ignore[arg-type]
        fixer=fixer,
        sandbox=sandbox,
        policy=policy or _policy(guards),
        recipes=["typecheck"],
        targets=[],
    )


def test_fix_verificado_cuando_el_sandbox_pasa() -> None:
    results = _run(fixer=_Fixer(), sandbox=_Sandbox(passed=True))
    assert results["f1"].status is FixStatus.VERIFIED
    assert results["f1"].patch is not None
    assert results["f1"].image_digest == "sha256:abc"


def test_fix_fallido_tras_agotar_intentos() -> None:
    results = _run(fixer=_Fixer(), sandbox=_Sandbox(passed=False))
    result = results["f1"]
    assert result.status is FixStatus.FAILED
    assert result.attempts == 2, "reintenta con el stderr del primero"
    assert "typecheck" in result.reason


def test_parche_invalido_no_llama_al_sandbox() -> None:
    bad = ProposedPatch(
        finding_id="f1",
        file="src/Foo.php",
        diff="diff --git a/src/Foo.php b/src/Foo.php\n",
        start_line=99,
        end_line=99,
        replacement="x",
    )
    sandbox = _Sandbox()
    results = _run(fixer=_Fixer(patch=bad), sandbox=sandbox)
    assert results["f1"].status is FixStatus.INVALID_PATCH
    assert sandbox.requests == [], "un parche invalido no gasta sandbox"


def test_escalate_aprobado_no_genera_fix() -> None:
    results = _run(
        fixer=_Fixer(),
        sandbox=_Sandbox(),
        decisions=[_decision("f1", FindingAction.ESCALATE)],
    )
    assert results == {}


def test_sin_fixer_es_no_verificable() -> None:
    results = _run(fixer=None, sandbox=_Sandbox())
    assert results["f1"].status is FixStatus.NOT_VERIFIABLE


def test_tope_de_fixes_marca_skipped() -> None:
    results = _run(
        fixer=_Fixer(),
        sandbox=_Sandbox(),
        guards={"max_fixes_per_pr": 0, "max_fix_attempts": 2, "max_fix_lines": 30},
    )
    assert results["f1"].status is FixStatus.SKIPPED


def test_ruta_nunca_auto_bloquea_el_fix_aunque_lo_aprueben() -> None:
    sandbox = _Sandbox()
    fixer = _Fixer()
    policy = _policy(
        {
            "max_fixes_per_pr": 3,
            "max_fix_attempts": 2,
            "max_fix_lines": 30,
            "never_auto_paths": ["src/Foo.php"],
        }
    )
    results = _run(
        fixer=fixer,
        sandbox=sandbox,
        policy=policy,
        human=_human(outcome=DecisionOutcome.APPROVED),
    )
    assert results["f1"].status is FixStatus.BLOCKED
    assert "nunca-auto" in results["f1"].reason
    assert fixer.calls == 0, "una ruta protegida no gasta fixer"
    assert sandbox.requests == [], "una ruta protegida no gasta sandbox"


def test_tema_protegido_bloquea_el_fix_aunque_lo_aprueben() -> None:
    finding = _FINDING.model_copy(
        update={"protected_subject": ProtectedSubject.AUTHORIZATION_AUTHENTICATION}
    )
    sandbox = _Sandbox()
    fixer = _Fixer()
    results = _run(fixer=fixer, sandbox=sandbox, finding=finding)
    assert results["f1"].status is FixStatus.BLOCKED
    assert "tema protegido" in results["f1"].reason
    assert fixer.calls == 0
    assert sandbox.requests == []


def test_delivery_diff_usa_el_bloque_diff() -> None:
    patch = ProposedPatch(
        finding_id="f1",
        file="src/Foo.php",
        diff="diff --git a/src/Foo.php b/src/Foo.php\n@@ -1 +1 @@\n-a\n+b\n",
        start_line=1,
        end_line=1,
        replacement="b",
    )
    checks = [CommandResult(recipe="typecheck", argv=["make"], exit_code=0, duration_s=1.0)]
    body = DiffFormatter().render(patch, checks=checks, image_digest="sha256:deadbeef")
    assert "```diff" in body
    assert "Verificado: typecheck" in body


def test_delivery_suggestion_usa_el_bloque_suggestion() -> None:
    patch = ProposedPatch(
        finding_id="f1",
        file="src/Foo.php",
        diff="x",
        start_line=1,
        end_line=1,
        replacement="b",
    )
    body = SuggestionFormatter().render(patch, checks=[], image_digest="")
    assert "```suggestion" in body
