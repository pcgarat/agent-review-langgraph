from __future__ import annotations

import pytest
import redis
from redis.connection import DEFAULT_SOCKET_TIMEOUT

from agent_review.domain.models import ReviewRequest
from agent_review.queue import InMemoryReviewQueue, RedisReviewQueue
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


def test_dequeue_propaga_errores_no_transitorios(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = RedisReviewQueue(url="redis://localhost:6379/0")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise ValueError("payload corrupto")

    monkeypatch.setattr(queue._client, "blpop", _boom)
    with pytest.raises(ValueError):
        queue.dequeue()
