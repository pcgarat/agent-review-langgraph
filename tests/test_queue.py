from __future__ import annotations

import fakeredis
import pytest
import redis
from redis.connection import DEFAULT_SOCKET_TIMEOUT

from agent_review.domain.models import (
    FixJob,
    FixWaitRequest,
    ReviewRequest,
    VerifyJob,
)
from agent_review.queue import (
    InMemoryFixQueue,
    InMemoryReviewQueue,
    RedisFixQueue,
    RedisReviewQueue,
)
from agent_review.redis_utils import build_redis_client


def test_cola_en_memoria_fifo() -> None:
    queue = InMemoryReviewQueue()
    a = ReviewRequest(repo="r", pr_id=1, head_sha="a")
    b = ReviewRequest(repo="r", pr_id=2, head_sha="b")
    queue.enqueue(a)
    queue.enqueue(b)
    assert len(queue) == 2
    first = queue.dequeue()
    second = queue.dequeue()
    assert first is not None and first.pr_id == 1
    assert second is not None and second.pr_id == 2
    assert queue.dequeue() is None


def test_dead_letter_guarda_el_job_descartado() -> None:
    queue = InMemoryReviewQueue()
    request = ReviewRequest(repo="r", pr_id=1, head_sha="a", pipeline_id="7", attempts=2)
    queue.dead_letter(request, reason="intentos agotados (3)")
    assert queue.dead_letter_count == 1
    entry = queue.dead_letters()[0]
    assert entry["review_key"] == request.review_key
    assert entry["reason"] == "intentos agotados (3)"
    assert entry["request"]["pipeline_id"] == "7"


def test_cliente_bloqueante_sin_deadline_de_socket() -> None:
    """Blpop no debe competir con el `socket_timeout` por defecto de redis-py.

    redis-py 8 fija 5 s de deadline por defecto; con `BLPOP timeout=5` el socket
    vencia antes que el comando y una cola vacia mataba al worker. El cliente que
    bloquea tiene que renunciar a ese deadline.
    """
    client = build_redis_client("redis://localhost:6379/0", blocking=True)
    assert client.connection_pool.connection_kwargs["socket_timeout"] is None


def test_cliente_normal_conserva_deadline() -> None:
    client = build_redis_client("redis://localhost:6379/0")
    conn = client.connection_pool.make_connection()
    assert conn.socket_timeout == DEFAULT_SOCKET_TIMEOUT


def test_dequeue_devuelve_none_si_redis_falla(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un fallo transitorio de la cola no propaga: es un poll sin resultado."""
    queue = RedisReviewQueue(url="redis://localhost:6379/0")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise redis.exceptions.ConnectionError("servidor reiniciando")

    monkeypatch.setattr(queue._client, "blpop", _boom)
    assert queue.dequeue() is None


def test_release_parked_mueve_solo_el_motivo() -> None:
    queue = RedisReviewQueue(url="redis://localhost:6379/0")
    queue._client = fakeredis.FakeRedis(decode_responses=True)  # type: ignore[assignment]
    queue.park(ReviewRequest(repo="r", pr_id=1, head_sha="sesion"), reason="session")
    queue.park(ReviewRequest(repo="r", pr_id=1, head_sha="cupo"), reason="quota")
    queue.park(ReviewRequest(repo="r", pr_id=1, head_sha="espera"), reason="quota-wait")
    sueltos = queue.release_parked(reason="quota-wait")
    assert [item.head_sha for item in sueltos] == ["espera"]
    assert [item.head_sha for item in queue.parked()] == ["sesion", "cupo"]
    job = queue.dequeue()
    assert job is not None and job.head_sha == "espera"


def test_dequeue_propaga_errores_no_transitorios(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = RedisReviewQueue(url="redis://localhost:6379/0")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise ValueError("payload corrupto")

    monkeypatch.setattr(queue._client, "blpop", _boom)
    with pytest.raises(ValueError):
        queue.dequeue()


# ---------------------------------------------------------------------------
# Fase 3-E7: cola de fixes (F18)
# ---------------------------------------------------------------------------


def _fix_job(review_key: str = "r:1:abc") -> FixJob:
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="p1")
    wave = FixWaitRequest(
        review_key=request.review_key,
        repo=request.repo,
        pr_id=request.pr_id,
        head_sha=request.head_sha,
        jobs=[
            VerifyJob(
                run_key=f"{request.review_key}:f1:1",
                finding_id="f1",
                attempt=1,
                repo=request.repo,
                head_sha=request.head_sha,
                patch="--- a\n+++ b\n",
                recipes=["typecheck"],
            )
        ],
    )
    return FixJob(review_key=request.review_key, request=request, wave=wave)


def test_cola_de_fixes_fifo_y_no_se_mezcla_con_reviews() -> None:
    reviews = InMemoryReviewQueue()
    fixes = InMemoryFixQueue()
    job = _fix_job()
    fixes.enqueue(job)

    assert len(fixes) == 1
    assert len(reviews) == 0, "un job de fix no entra en la cola de reviews"
    assert reviews.dequeue() is None
    taken = fixes.dequeue()
    assert taken is not None and taken.review_key == job.review_key
    assert taken.wave.jobs[0].finding_id == "f1"
    assert fixes.dequeue() is None


def test_dlq_de_fixes_guarda_el_job_descartado() -> None:
    queue = InMemoryFixQueue()
    job = _fix_job()
    queue.dead_letter(job, reason="el runner fallo siempre")

    assert queue.dead_letter_count == 1
    entry = queue.dead_letters()[0]
    assert entry["review_key"] == job.review_key
    assert entry["reason"] == "el runner fallo siempre"
    assert entry["job"]["wave"]["jobs"][0]["run_key"].endswith(":f1:1")


def test_done_de_fixes_es_idempotente_por_run_key() -> None:
    queue = InMemoryFixQueue()
    run_key = "r:1:abc:f1:1"
    assert not queue.is_done(run_key)
    queue.mark_done(run_key)
    assert queue.is_done(run_key), "una reentrega no debe relanzar la verificacion"
    assert not queue.is_done("r:1:abc:f1:2"), "otro intento es otra verificacion"


def test_dequeue_de_fixes_devuelve_none_si_redis_falla(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = RedisFixQueue(url="redis://localhost:6379/0")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise redis.exceptions.ConnectionError("servidor reiniciando")

    monkeypatch.setattr(queue._client, "blpop", _boom)
    assert queue.dequeue() is None
