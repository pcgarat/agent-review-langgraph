from __future__ import annotations

from agent_review.domain.models import Finding, ReviewRequest
from agent_review.idempotency import InMemoryIdempotencyStore


def test_clave_de_idempotencia() -> None:
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=42, head_sha="abc123")
    assert request.idempotency_key == "cqm/emotivoo-v2:42:abc123"


def test_clave_de_review_por_pipeline() -> None:
    # Sin pipeline, la review es idempotente por commit; con pipeline, un pipeline
    # nuevo sobre el mismo commit es una review nueva.
    sin_pipeline = ReviewRequest(repo="r", pr_id=1, head_sha="abc")
    assert sin_pipeline.review_key == sin_pipeline.idempotency_key

    con_pipeline = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="7")
    assert con_pipeline.review_key == "r:1:abc:7"
    otro_pipeline = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="8")
    assert otro_pipeline.review_key != con_pipeline.review_key


def test_clave_de_ingesta_por_herramienta() -> None:
    # El evento de ingesta es distinto de la ejecucion de la review: no pueden
    # colisionar o el segundo evento se descartaria como duplicado.
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="7")
    assert request.ingest_key("phpstan") == "r:1:abc:phpstan"
    assert request.ingest_key("phpstan") != request.review_key


def test_claim_solo_una_vez() -> None:
    store = InMemoryIdempotencyStore()
    assert store.claim("cqm/emotivoo-v2:1:aaa") is True
    assert store.claim("cqm/emotivoo-v2:1:aaa") is False
    assert store.claim("cqm/emotivoo-v2:1:bbb") is True


def test_finding_exige_evidencia() -> None:
    import pytest

    from agent_review.domain.models import AutofixClass, Owner, Severity

    with pytest.raises(ValueError):
        Finding(
            id="x",
            title="t",
            severity=Severity.P3,
            file="a.py",
            line=1,
            why_it_matters="w",
            autofix_class=AutofixClass.ADVISORY,
            owner=Owner.HUMAN,
            failure_scenario="escenario de prueba",
            confidence=75,
            evidence=[],
        )


def _p(severity: str, **overrides) -> dict:
    base = {
        "id": "x",
        "title": "t",
        "severity": severity,
        "file": "a.py",
        "line": 1,
        "why_it_matters": "w",
        "autofix_class": "advisory",
        "owner": "human",
        "confidence": 75,
        "evidence": ["a.py:1 x"],
    }
    return base | overrides


def test_i1_un_p0_p2_sin_verificar_exige_escenario() -> None:
    import pytest
    from pydantic import ValidationError

    for severity in ("P0", "P1", "P2"):
        with pytest.raises(ValidationError, match="failure_scenario"):
            Finding.model_validate(_p(severity))
        with pytest.raises(ValidationError, match="failure_scenario"):
            Finding.model_validate(_p(severity, failure_scenario="   "))
    # Un P3 no lo necesita.
    assert Finding.model_validate(_p("P3")).failure_scenario is None


def test_i1_un_p2_verificado_no_necesita_escenario() -> None:
    # 007 Q5: el escenario se sustituye por la evidencia de verificacion.
    finding = Finding.model_validate(
        _p("P2", verification="verified_static", verification_evidence=["a.py:1 x"])
    )
    assert finding.failure_scenario is None


def test_i2_un_veredicto_sin_evidencia_se_rechaza() -> None:
    import pytest
    from pydantic import ValidationError

    for verdict in ("verified_static", "verified_grounded", "verified_executed", "refuted"):
        with pytest.raises(ValidationError, match="verification_evidence"):
            Finding.model_validate(_p("P3", verification=verdict))


def test_i3_context_scope_cerrado_y_opcional() -> None:
    import pytest
    from pydantic import ValidationError

    from agent_review.domain.models import ContextScope, Verification

    finding = Finding.model_validate(_p("P3"))
    assert finding.context_scope is None
    assert finding.verification is Verification.UNVERIFIED
    assert finding.verification_evidence == []
    assert Finding.model_validate(_p("P3", context_scope="outside_diff")).context_scope is (
        ContextScope.OUTSIDE_DIFF
    )
    with pytest.raises(ValidationError):
        Finding.model_validate(_p("P3", context_scope="por_ahi"))


def test_requires_verification_ya_no_existe_y_se_ignora() -> None:
    # 007 D12: un hallazgo antiguo con el campo sigue validando; el campo se pierde.
    finding = Finding.model_validate(_p("P3", requires_verification=True))
    assert "requires_verification" not in finding.model_dump()
