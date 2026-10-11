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
            requires_verification=False,
            confidence=75,
            evidence=[],
        )
