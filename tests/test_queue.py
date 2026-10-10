from __future__ import annotations

from reviews.domain.models import ReviewRequest
from reviews.queue import InMemoryReviewQueue


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
