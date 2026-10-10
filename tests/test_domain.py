from __future__ import annotations

from reviews.domain.models import Finding, ReviewRequest
from reviews.idempotency import InMemoryIdempotencyStore


def test_clave_de_idempotencia() -> None:
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=42, head_sha="abc123")
    assert request.idempotency_key == "cqm/emotivoo-v2:42:abc123"


def test_claim_solo_una_vez() -> None:
    store = InMemoryIdempotencyStore()
    assert store.claim("cqm/emotivoo-v2:1:aaa") is True
    assert store.claim("cqm/emotivoo-v2:1:aaa") is False
    assert store.claim("cqm/emotivoo-v2:1:bbb") is True


def test_finding_exige_evidencia() -> None:
    import pytest

    from reviews.domain.models import AutofixClass, Owner, Severity

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
