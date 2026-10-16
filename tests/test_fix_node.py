from __future__ import annotations

from agent_review.domain.models import (
    AutofixClass,
    CommandResult,
    Decision,
    DecisionOutcome,
    Finding,
    FindingAction,
    FindingDecision,
    FixResolution,
    FixResolutions,
    FixResult,
    FixStatus,
    FixWaitRequest,
    HitlDecision,
    Owner,
    ProposedPatch,
    ProtectedSubject,
    ReviewRequest,
    Severity,
)
from agent_review.fix.delivery import DiffFormatter, SuggestionFormatter
from agent_review.fix.node import (
    apply_resolutions,
    build_wave,
    dispatch_fixes,
    run_key_for,
)
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
        self.previous_seen: list[FixResult | None] = []

    def propose(
        self, finding: Finding, *, source: str, previous: FixResult | None
    ) -> ProposedPatch:
        self.calls += 1
        self.previous_seen.append(previous)
        if self._patch is not None:
            return self._patch
        replacement = "use App\\Bar;" if previous is None else "use App\\Baz;"
        return build_patch(
            finding, source=source, start_line=2, end_line=2, replacement=replacement
        )


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


def _dispatch(
    *,
    fixer,
    human=None,
    decisions=None,
    guards=None,
    policy: PolicyEngine | None = None,
    finding: Finding | None = None,
    existing=None,
    verification_enabled: bool = True,
) -> dict[str, FixResult]:
    return dispatch_fixes(
        request=_REQUEST,
        findings=[finding or _FINDING],
        decisions=decisions or [_decision("f1")],
        human=human or _human(),
        vcs=_Vcs(),  # type: ignore[arg-type]
        fixer=fixer,
        policy=policy or _policy(guards),
        recipes=["typecheck"],
        targets=[],
        existing=existing,
        verification_enabled=verification_enabled,
    )


def _resolution(
    outcome: str,
    *,
    attempt: int = 1,
    checks: list[CommandResult] | None = None,
    error: str = "",
) -> FixResolutions:
    return FixResolutions(
        items=[
            FixResolution(
                run_key=run_key_for(_REQUEST.review_key, "f1", attempt),
                outcome=outcome,  # type: ignore[arg-type]
                checks=checks or [],
                error=error,
            )
        ]
    )


def _check(ok: bool, recipe: str = "typecheck") -> CommandResult:
    return CommandResult(
        recipe=recipe,
        argv=["make", "stan"],
        exit_code=0 if ok else 1,
        duration_s=1.0,
        stderr_tail="" if ok else "boom",
    )


# --- Dispatch: propone y valida, nunca verifica ------------------------------


def test_dispatch_deja_pending_sin_sandbox() -> None:
    """Un candidato verificable acaba `PENDING`; la verificacion no ocurre aqui."""
    results = _dispatch(fixer=_Fixer())
    assert results["f1"].status is FixStatus.PENDING
    assert results["f1"].patch is not None
    assert results["f1"].attempts == 1


def test_parche_invalido_es_terminal_sin_sandbox() -> None:
    bad = ProposedPatch(
        finding_id="f1",
        file="src/Foo.php",
        diff="diff --git a/src/Foo.php b/src/Foo.php\n",
        start_line=99,
        end_line=99,
        replacement="x",
    )
    results = _dispatch(fixer=_Fixer(patch=bad))
    assert results["f1"].status is FixStatus.INVALID_PATCH


def test_escalate_aprobado_no_genera_fix() -> None:
    results = _dispatch(fixer=_Fixer(), decisions=[_decision("f1", FindingAction.ESCALATE)])
    assert results == {}


def test_sin_fixer_es_no_verificable() -> None:
    results = _dispatch(fixer=None)
    assert results["f1"].status is FixStatus.NOT_VERIFIABLE


def test_sin_sandbox_es_no_verificable_sin_pedir_parche() -> None:
    """Con `SANDBOX_ENABLED=false` nadie resolveria la wave: no se deja `PENDING`."""
    fixer = _Fixer()
    results = _dispatch(fixer=fixer, verification_enabled=False)
    assert results["f1"].status is FixStatus.NOT_VERIFIABLE
    assert "sandbox" in results["f1"].reason
    assert fixer.calls == 0, "no se gasta el fixer en un parche que nadie verificara"


def test_tope_de_fixes_marca_skipped() -> None:
    results = _dispatch(
        fixer=_Fixer(),
        guards={"max_fixes_per_pr": 0, "max_fix_attempts": 2, "max_fix_lines": 30},
    )
    assert results["f1"].status is FixStatus.SKIPPED


def test_ruta_nunca_auto_bloquea_el_fix_aunque_lo_aprueben() -> None:
    fixer = _Fixer()
    policy = _policy(
        {
            "max_fixes_per_pr": 3,
            "max_fix_attempts": 2,
            "max_fix_lines": 30,
            "never_auto_paths": ["src/Foo.php"],
        }
    )
    results = _dispatch(fixer=fixer, policy=policy)
    assert results["f1"].status is FixStatus.BLOCKED
    assert "nunca-auto" in results["f1"].reason
    assert fixer.calls == 0, "una ruta protegida no gasta fixer"


def test_tema_protegido_bloquea_el_fix_aunque_lo_aprueben() -> None:
    finding = _FINDING.model_copy(
        update={"protected_subject": ProtectedSubject.AUTHORIZATION_AUTHENTICATION}
    )
    fixer = _Fixer()
    results = _dispatch(fixer=fixer, finding=finding)
    assert results["f1"].status is FixStatus.BLOCKED
    assert "tema protegido" in results["f1"].reason
    assert fixer.calls == 0


# --- Aplicacion de la wave ---------------------------------------------------


def test_resolucion_passed_es_verified() -> None:
    pending = _dispatch(fixer=_Fixer())
    applied = apply_resolutions(
        request=_REQUEST, fixes=pending, resolutions=_resolution("passed", checks=[_check(True)])
    )
    assert applied["f1"].status is FixStatus.VERIFIED
    assert applied["f1"].checks[0].ok


def test_resolucion_failed_guarda_el_check_y_la_razon() -> None:
    pending = _dispatch(fixer=_Fixer())
    applied = apply_resolutions(
        request=_REQUEST, fixes=pending, resolutions=_resolution("failed", checks=[_check(False)])
    )
    assert applied["f1"].status is FixStatus.FAILED
    assert "typecheck" in applied["f1"].reason


def test_resolucion_error_es_no_verificable() -> None:
    """Un fallo del runner no es `FAILED`: la verificacion no llego a ejecutarse."""
    pending = _dispatch(fixer=_Fixer())
    applied = apply_resolutions(
        request=_REQUEST,
        fixes=pending,
        resolutions=_resolution("error", error="runner caido"),
    )
    assert applied["f1"].status is FixStatus.NOT_VERIFIABLE
    assert "runner caido" in applied["f1"].reason


def test_resolucion_ausente_no_deja_el_fix_colgado() -> None:
    pending = _dispatch(fixer=_Fixer())
    applied = apply_resolutions(
        request=_REQUEST, fixes=pending, resolutions=FixResolutions(items=[])
    )
    assert applied["f1"].status is FixStatus.NOT_VERIFIABLE


def test_apply_resolutions_es_reejecutable() -> None:
    """Al reanudar el nodo se re-ejecuta: aplicar la misma wave dos veces no cambia."""
    pending = _dispatch(fixer=_Fixer())
    resolutions = _resolution("failed", checks=[_check(False)])
    once = apply_resolutions(request=_REQUEST, fixes=pending, resolutions=resolutions)
    twice = apply_resolutions(request=_REQUEST, fixes=once, resolutions=resolutions)
    assert twice == {}, "un fix ya terminal no se re-aplica"


# --- Reintento: el grafo vuelve a `fix_dispatch` tras un `failed` ------------


def test_failed_con_intentos_re_propone_con_el_log_previo() -> None:
    fixer = _Fixer()
    pending = _dispatch(fixer=fixer)
    failed = apply_resolutions(
        request=_REQUEST, fixes=pending, resolutions=_resolution("failed", checks=[_check(False)])
    )
    # Visita de reintento: el grafo vuelve a `fix_dispatch` con el `FAILED` en el estado.
    retried = _dispatch(fixer=fixer, existing=failed)
    assert retried["f1"].status is FixStatus.PENDING
    assert retried["f1"].attempts == 2
    assert fixer.previous_seen[-1] is not None, "el 2º intento ve el fallo anterior"


def test_failed_sin_intentos_no_re_propone() -> None:
    fixer = _Fixer()
    pending = _dispatch(
        fixer=_Fixer(),
        guards={"max_fixes_per_pr": 3, "max_fix_attempts": 1, "max_fix_lines": 30},
    )
    exhausted = apply_resolutions(
        request=_REQUEST, fixes=pending, resolutions=_resolution("failed", checks=[_check(False)])
    )
    retried = _dispatch(
        fixer=fixer,
        existing=exhausted,
        guards={"max_fixes_per_pr": 3, "max_fix_attempts": 1, "max_fix_lines": 30},
    )
    assert retried == {}, "sin intentos no hay otra wave"
    assert fixer.calls == 0


def test_build_wave_lista_los_pending() -> None:
    pending = _dispatch(fixer=_Fixer())
    wave = build_wave(_REQUEST, pending, recipes=["typecheck"], targets=[])
    assert isinstance(wave, FixWaitRequest)
    assert wave.jobs[0].run_key == run_key_for(_REQUEST.review_key, "f1", 1)
    assert wave.jobs[0].recipes == ["typecheck"]


def test_build_wave_sin_pendientes_no_es_una_pausa() -> None:
    from agent_review.graph.build import build_fix_wait

    assert build_fix_wait({"request": _REQUEST, "fixes": {}}) is None


# --- Delivery (E6) -----------------------------------------------------------


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
