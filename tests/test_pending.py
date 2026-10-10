from __future__ import annotations

from datetime import UTC, datetime, timedelta

from reviews.domain.models import ReviewRequest
from reviews.domain.pending import InMemoryPendingStore, PendingReview, PendingStatus


def _request(sha: str = "abc") -> ReviewRequest:
    return ReviewRequest(repo="cqm/emotivoo-v2", pr_id=42, head_sha=sha, pipeline_id="7")


def test_barrera_pasa_a_ready_cuando_llegan_todos() -> None:
    review = PendingReview(request=_request(), expected_tools=["phpstan", "php-cs-fixer"])
    assert review.observe("phpstan", "{}") is PendingStatus.PENDING
    assert review.missing == ["php-cs-fixer"]
    assert review.observe("php-cs-fixer", "{}") is PendingStatus.READY
    assert review.missing == []


def test_observar_un_artefacto_no_retrocede_el_estado() -> None:
    review = PendingReview(request=_request(), expected_tools=["phpstan"])
    assert review.observe("phpstan", "{}") is PendingStatus.READY
    # Un reintento del CI no debe reabrir una review ya completa.
    assert review.observe("otro", "{}") is PendingStatus.READY
    assert review.status is PendingStatus.READY


def test_ttl_ready_degrada_si_falta_algun_artefacto() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    review = PendingReview(
        request=_request(),
        expected_tools=["phpstan", "php-cs-fixer"],
        created_at=start,
        ttl_ready_s=60,
        ttl_pending_s=3600,
    )
    review.observe("phpstan", "{}", now=start)
    assert review.resolve_timeout(now=start + timedelta(seconds=59)) is None
    assert review.resolve_timeout(now=start + timedelta(seconds=61)) is PendingStatus.DEGRADED
    assert review.missing == ["php-cs-fixer"]


def test_ttl_pending_degrada_si_no_llego_nada() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    review = PendingReview(
        request=_request(), expected_tools=["phpstan"], created_at=start, ttl_pending_s=600
    )
    assert review.resolve_timeout(now=start + timedelta(seconds=599)) is None
    assert review.resolve_timeout(now=start + timedelta(seconds=601)) is PendingStatus.DEGRADED


def test_un_solo_artefacto_no_usa_el_ttl_largo() -> None:
    # Con un artefacto recibido, el reloj que manda es el de "ready", no el de "pending".
    start = datetime(2026, 1, 1, tzinfo=UTC)
    review = PendingReview(
        request=_request(),
        expected_tools=["phpstan", "php-cs-fixer"],
        created_at=start,
        ttl_ready_s=60,
        ttl_pending_s=100000,
    )
    review.observe("phpstan", "{}", now=start)
    assert review.resolve_timeout(now=start + timedelta(seconds=61)) is PendingStatus.DEGRADED


def test_store_en_memoria_agrupa_por_review_y_barre() -> None:
    store = InMemoryPendingStore(expected_tools=["phpstan", "php-cs-fixer"])
    request = _request()
    assert store.observe(request, "phpstan", "{}") is PendingStatus.PENDING
    assert store.ready() == []

    assert store.observe(request, "php-cs-fixer", "{}") is PendingStatus.READY
    assert [r.key for r in store.ready()] == [request.idempotency_key]


def test_store_en_memoria_degrada_por_ttl() -> None:
    store = InMemoryPendingStore(expected_tools=["phpstan", "php-cs-fixer"])
    request = _request()
    store.observe(request, "phpstan", "{}", ttl_ready_s=0)
    resolved = store.sweep()
    assert [r.status for r in resolved] == [PendingStatus.DEGRADED]


def test_los_tools_esperados_son_por_review_no_globales() -> None:
    # Contrato de la barrera: el conjunto esperado lo declara el perfil del repo.
    review = PendingReview(request=_request(), expected_tools=["phpstan"])
    assert review.observe("phpstan", "{}") is PendingStatus.READY
