from __future__ import annotations

import pytest

from agent_review.domain.models import (
    AutofixClass,
    Finding,
    FindingAction,
    Owner,
    ProtectedSubject,
    PublicationMode,
    Severity,
    Verification,
)
from agent_review.policy.engine import PolicyEngine, PolicyError


@pytest.fixture
def policy() -> PolicyEngine:
    return PolicyEngine.from_file("config/autonomy.yaml")


def make_finding(**overrides: object) -> Finding:
    base: dict[str, object] = {
        "id": "f1",
        "title": "Titulo",
        "severity": Severity.P3,
        "file": "src/foo.py",
        "line": 10,
        "why_it_matters": "Rompe algo",
        "autofix_class": AutofixClass.GATED_AUTO,
        "owner": Owner.DOWNSTREAM_RESOLVER,
        "failure_scenario": "escenario de prueba",
        "confidence": 75,
        "evidence": ["src/foo.py:10 amor"],
    }
    base.update(overrides)
    return Finding(**base)  # type: ignore[arg-type]


def _engine(*, max_autonomy: str, rules: list[dict] | None = None) -> PolicyEngine:
    return PolicyEngine(
        {
            "version": 7,
            "max_autonomy": max_autonomy,
            "default_action": "report",
            "rules": rules
            or [
                {
                    "when": {"severity": "P0"},
                    "action": "escalate",
                    "reason": "critico",
                },
                {
                    "when": {"autofix_class": "gated_auto", "severity": "P3"},
                    "action": "fix_auto",
                    "reason": "seguro",
                },
            ],
        }
    )


def test_fase1_p0_unverified_degrada_y_se_reporta(policy: PolicyEngine) -> None:
    # 011: P0 sin verificar no llega a la regla de escalate; el gate lo degrada.
    result = policy.decide(make_finding(severity=Severity.P0))
    assert result.action is FindingAction.REPORT
    assert result.severity is Severity.P3
    assert result.matched_rule == "gate:degrade"
    assert "degradado" in result.severity_reason


def test_fase1_p0_verificado_conserva_regla(policy: PolicyEngine) -> None:
    result = policy.decide(
        make_finding(
            severity=Severity.P0,
            verification=Verification.VERIFIED_GROUNDED,
            verification_evidence=["src/foo.py:10 prueba"],
        )
    )
    assert result.action is FindingAction.REPORT  # max_autonomy: report
    assert result.severity is Severity.P0
    assert result.matched_rule.startswith("rule:")


def test_fase1_protected_subject_se_reporta(policy: PolicyEngine) -> None:
    finding = make_finding(protected_subject=ProtectedSubject.CRYPTOGRAPHY, severity=Severity.P3)
    result = policy.decide(finding)
    assert result.action is FindingAction.REPORT
    assert result.policy_version == "1"
    assert "protegido" in result.reason.lower() or result.reason


def test_fase1_manual_se_reporta(policy: PolicyEngine) -> None:
    assert (
        policy.decide(make_finding(autofix_class=AutofixClass.MANUAL)).action
        is FindingAction.REPORT
    )


def test_fase1_gated_auto_p3_se_reporta(policy: PolicyEngine) -> None:
    result = policy.decide(make_finding(severity=Severity.P3, confidence=100))
    assert result.action is FindingAction.REPORT


def test_baja_confianza_se_reporta(policy: PolicyEngine) -> None:
    assert policy.decide(make_finding(confidence=50)).action is FindingAction.REPORT


def test_con_techo_alto_p0_sigue_escalando() -> None:
    engine = _engine(max_autonomy="fix_with_approval")
    assert engine.decide(make_finding(severity=Severity.P0)).action is FindingAction.ESCALATE


def test_p3_en_ruta_segura_es_autofix_si_techo_lo_permite() -> None:
    top = _engine(max_autonomy="fix_auto")
    finding = make_finding(
        severity=Severity.P3, confidence=100, file="tests/Shared/Model/FooTest.php"
    )
    result = top.decide(finding)
    assert result.action is FindingAction.FIX_AUTO
    assert result.matched_rule == "rule:1"
    assert result.policy_version == "7"


def test_ruta_nunca_auto_se_frena_y_en_fase1_es_report(policy: PolicyEngine) -> None:
    guarded = make_finding(severity=Severity.P3, confidence=100, file="infra/main.tf")
    result = policy.decide(guarded)
    assert result.action is FindingAction.REPORT
    assert result.matched_rule == "guard:never_auto_paths"
    assert "protegida" in result.reason
    assert policy.is_never_auto("docker/php/Dockerfile")
    assert policy.is_never_auto("bitbucket-pipelines.yml")
    assert policy.is_never_auto(".cursor/rules/foo.mdc")


def test_escalado_en_ruta_nunca_auto_sigue_siendo_escalado() -> None:
    engine = _engine(max_autonomy="fix_with_approval")
    critical = make_finding(severity=Severity.P0, file=".cursor/rules/x.mdc")
    result = engine.decide(critical)
    assert result.action is FindingAction.ESCALATE
    assert result.matched_rule.startswith("rule:")


def test_no_escalado_en_ruta_nunca_auto_pide_aprobacion() -> None:
    engine = _engine(max_autonomy="fix_with_approval")
    minor = make_finding(severity=Severity.P3, confidence=100, file=".cursor/rules/x.mdc")
    result = engine.decide(minor)
    assert result.action is FindingAction.FIX_WITH_APPROVAL
    assert result.matched_rule == "guard:never_auto_paths"


def test_techo_global_limita_autofix() -> None:
    capped = _engine(max_autonomy="report")
    result = capped.decide(make_finding(severity=Severity.P3, confidence=75))
    assert result.action is FindingAction.REPORT


def test_decision_lleva_trazabilidad(policy: PolicyEngine) -> None:
    """Una decision debe poder auditarse: que regla y con que version."""
    result = policy.decide(
        make_finding(
            severity=Severity.P0,
            verification=Verification.VERIFIED_STATIC,
            verification_evidence=["tool:semgrep"],
        )
    )
    assert result.policy_version
    assert result.matched_rule == "rule:1"
    assert result.reason


def test_max_inline_comments_en_guards(policy: PolicyEngine) -> None:
    assert policy.guards.get("max_inline_comments") == 20


def test_cap_override_solo_puede_bajar_el_techo() -> None:
    # D1: el YAML permite fix_auto, pero el deploy lo baja a report.
    lowered = PolicyEngine(
        {"version": 1, "max_autonomy": "fix_auto", "default_action": "report", "rules": []},
        cap_override="report",
    )
    assert lowered.raw()["max_autonomy"] == "report"


def test_cap_override_no_puede_subir_el_techo() -> None:
    # Un override que pide mas autonomia que el YAML se ignora (el YAML manda).
    engine = PolicyEngine(
        {"version": 1, "max_autonomy": "report", "default_action": "report", "rules": []},
        cap_override="fix_auto",
    )
    assert engine.raw()["max_autonomy"] == "report"


def test_cap_override_vacio_deja_el_yaml() -> None:
    engine = PolicyEngine(
        {
            "version": 1,
            "max_autonomy": "fix_with_approval",
            "default_action": "report",
            "rules": [],
        },
        cap_override="",
    )
    assert engine.raw()["max_autonomy"] == "fix_with_approval"


def test_cap_override_escalate_no_baja_el_techo() -> None:
    # `escalate` no es un nivel de autonomia: pedirlo como override no endurece
    # nada (el YAML manda) y no debe lanzar KeyError al comparar la escalera.
    engine = PolicyEngine(
        {"version": 1, "max_autonomy": "fix_auto", "default_action": "report", "rules": []},
        cap_override="escalate",
    )
    assert engine.raw()["max_autonomy"] == "fix_auto"


def test_max_autonomy_escalate_no_rompe_el_arranque() -> None:
    # Un techo `escalate` es absurdo como politica, pero no debe tumbar el
    # arranque con KeyError: el clamp lo deja pasar (freno de seguridad).
    engine = _engine(max_autonomy="escalate")
    assert engine.raw()["max_autonomy"] == "escalate"
    assert engine.decide(make_finding(severity=Severity.P0)).action is FindingAction.ESCALATE


def test_techo_fix_with_approval_conserva_escalate_y_degrada_fix_auto() -> None:
    # Techo de Fase 2: `escalate` pasa (freno), `fix_auto` se degrada a
    # `fix_with_approval` (nunca se autoarregla sin aprobacion).
    engine = _engine(max_autonomy="fix_with_approval")
    assert engine.decide(make_finding(severity=Severity.P0)).action is FindingAction.ESCALATE
    degradado = engine.decide(make_finding(severity=Severity.P3, confidence=100))
    assert degradado.action is FindingAction.FIX_WITH_APPROVAL


def test_gate_p1_unverified_degrada_a_p3(policy: PolicyEngine) -> None:
    result = policy.decide(make_finding(severity=Severity.P1))
    assert result.severity is Severity.P3
    assert result.action is FindingAction.REPORT
    assert result.matched_rule == "gate:degrade"
    assert result.publication is PublicationMode.FULL


def test_gate_p1_verified_static_no_degrada(policy: PolicyEngine) -> None:
    result = policy.decide(
        make_finding(
            severity=Severity.P1,
            verification=Verification.VERIFIED_STATIC,
            verification_evidence=["tool:phpstan"],
        )
    )
    assert result.severity is Severity.P1
    assert result.matched_rule != "gate:degrade"
    assert result.publication is PublicationMode.FULL


def test_gate_refuted_drop_no_publica(policy: PolicyEngine) -> None:
    result = policy.decide(
        make_finding(
            severity=Severity.P1,
            verification=Verification.REFUTED,
            verification_evidence=["src/foo.py:10 contraejemplo"],
        )
    )
    assert result.publication is PublicationMode.DROP
    assert result.action is FindingAction.REPORT
    assert result.matched_rule == "gate:refuted"


def test_gate_refuted_summary_solo_resumen() -> None:
    engine = PolicyEngine(
        {
            "version": 1,
            "max_autonomy": "report",
            "default_action": "report",
            "severity_gate": {
                "require_verified_for": ["P0", "P1"],
                "verified": ["verified_static", "verified_grounded", "verified_executed"],
                "on_unverified": "degrade",
                "degrade_to": "P3",
                "on_refuted": "summary",
            },
            "rules": [],
        }
    )
    result = engine.decide(
        make_finding(
            severity=Severity.P0,
            verification=Verification.REFUTED,
            verification_evidence=["contra"],
        )
    )
    assert result.publication is PublicationMode.SUMMARY
    assert result.action is FindingAction.REPORT


def test_gate_p3_unverified_indiferente(policy: PolicyEngine) -> None:
    result = policy.decide(make_finding(severity=Severity.P3))
    assert result.severity is Severity.P3
    assert result.matched_rule != "gate:degrade"


def test_gate_degradado_no_entra_en_fix_auto() -> None:
    # G7: aunque las reglas darian fix_auto a un P3, un P1 degradado queda en report.
    engine = PolicyEngine(
        {
            "version": 1,
            "max_autonomy": "fix_auto",
            "default_action": "report",
            "severity_gate": {
                "require_verified_for": ["P0", "P1"],
                "verified": ["verified_static", "verified_grounded", "verified_executed"],
                "on_unverified": "degrade",
                "degrade_to": "P3",
                "on_refuted": "drop",
            },
            "rules": [
                {
                    "when": {"severity": "P3", "autofix_class": "gated_auto"},
                    "action": "fix_auto",
                    "reason": "seguro",
                }
            ],
        }
    )
    result = engine.decide(
        make_finding(severity=Severity.P1, confidence=100, autofix_class=AutofixClass.GATED_AUTO)
    )
    assert result.severity is Severity.P3
    assert result.action is FindingAction.REPORT
    assert result.matched_rule == "gate:degrade"


def test_gate_mal_formado_falla_al_cargar() -> None:
    with pytest.raises(PolicyError, match="require_verified_for"):
        PolicyEngine(
            {
                "version": 1,
                "max_autonomy": "report",
                "severity_gate": {
                    "require_verified_for": [],
                    "verified": ["verified_static"],
                    "on_unverified": "degrade",
                    "degrade_to": "P3",
                    "on_refuted": "drop",
                },
                "rules": [],
            }
        )
    with pytest.raises(PolicyError, match="degrade_to"):
        PolicyEngine(
            {
                "version": 1,
                "max_autonomy": "report",
                "severity_gate": {
                    "require_verified_for": ["P0"],
                    "verified": ["verified_static"],
                    "on_unverified": "degrade",
                    "degrade_to": "P9",
                    "on_refuted": "drop",
                },
                "rules": [],
            }
        )
    with pytest.raises(PolicyError, match="possibles"):
        PolicyEngine(
            {
                "version": 1,
                "max_autonomy": "report",
                "severity_gate": {
                    "require_verified_for": ["P0"],
                    "verified": ["verified_static"],
                    "on_unverified": "possibles",
                    "degrade_to": "P3",
                    "on_refuted": "drop",
                },
                "rules": [],
            }
        )
