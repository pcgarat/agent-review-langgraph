from __future__ import annotations

import pytest

from reviews.domain.models import (
    AutofixClass,
    Finding,
    Owner,
    ProtectedSubject,
    Severity,
)
from reviews.policy.engine import PolicyEngine


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
        "requires_verification": False,
        "confidence": 75,
        "evidence": ["src/foo.py:10 amor"],
    }
    base.update(overrides)
    return Finding(**base)  # type: ignore[arg-type]


def test_protected_subject_escala(policy: PolicyEngine) -> None:
    finding = make_finding(protected_subject=ProtectedSubject.CRYPTOGRAPHY, severity=Severity.P3)
    result = policy.decide(finding)
    assert result.action.value == "escalate"
    assert result.policy_version == "1"
    assert result.matched_rule.startswith("rule:")


def test_p0_escala(policy: PolicyEngine) -> None:
    assert policy.decide(make_finding(severity=Severity.P0)).action.value == "escalate"


def test_manual_escala(policy: PolicyEngine) -> None:
    assert (
        policy.decide(make_finding(autofix_class=AutofixClass.MANUAL)).action.value == "escalate"
    )


def test_baja_confianza_se_reporta(policy: PolicyEngine) -> None:
    assert policy.decide(make_finding(confidence=50)).action.value == "report"


def test_p2_con_aprobacion(policy: PolicyEngine) -> None:
    result = policy.decide(make_finding(severity=Severity.P2, confidence=75))
    assert result.action.value == "fix_with_approval"


def test_p3_en_ruta_segura_es_autofix() -> None:
    # Con el techo al maximo, un P3 con confianza alta si se arregla solo.
    top = PolicyEngine(
        {
            "version": 7,
            "max_autonomy": "fix_auto",
            "default_action": "report",
            "rules": [
                {
                    "when": {"autofix_class": "gated_auto", "severity": "P3"},
                    "action": "fix_auto",
                    "reason": "test",
                }
            ],
        }
    )
    finding = make_finding(
        severity=Severity.P3, confidence=100, file="tests/Shared/Model/FooTest.php"
    )
    result = top.decide(finding)
    assert result.action.value == "fix_auto"
    assert result.matched_rule == "rule:0"
    assert result.policy_version == "7"


def test_ruta_nunca_auto_se_frena(policy: PolicyEngine) -> None:
    # infra/** esta en never_auto_paths: nunca auto, aunque la regla diga lo contrario.
    guarded = make_finding(severity=Severity.P3, confidence=100, file="infra/main.tf")
    result = policy.decide(guarded)
    assert result.action.value == "fix_with_approval"
    assert result.matched_rule == "guard:never_auto_paths"
    assert "protegida" in result.reason
    assert policy.is_never_auto("docker/php/Dockerfile")
    assert policy.is_never_auto("bitbucket-pipelines.yml")
    assert policy.is_never_auto(".cursor/rules/foo.mdc")


def test_techo_global_limita_autofix(policy: PolicyEngine) -> None:
    capped = PolicyEngine(
        {
            "max_autonomy": "report",
            "default_action": "report",
            "rules": [
                {
                    "when": {"autofix_class": "gated_auto", "severity": "P3"},
                    "action": "fix_auto",
                    "reason": "test",
                }
            ],
        }
    )
    assert capped.decide(make_finding(severity=Severity.P3, confidence=75)).action.value == "report"


def test_decision_lleva_trazabilidad(policy: PolicyEngine) -> None:
    """Una decision debe poder auditarse: que regla y con que version."""
    result = policy.decide(make_finding(severity=Severity.P0))
    assert result.policy_version
    assert result.matched_rule == "rule:1"
    assert result.reason
