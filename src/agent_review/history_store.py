"""Histórico de reviews terminadas (021 R9).

Dos claves en Redis: `reviews:history` (*sorted set*: `review_key` con el instante
de cierre como *score*) y `reviews:history:data` (hash: `review_key` -> resumen en
JSON). Una reentrega del mismo job actualiza la entrada en vez de duplicarla. Se
poda por fecha con la retencion de los checkpoints, que es cuanto vive el detalle.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

import redis

from agent_review.domain.history import ReviewSummary
from agent_review.redis_utils import build_redis_client

logger = logging.getLogger(__name__)

INDEX_KEY = "reviews:history"
DATA_KEY = "reviews:history:data"


@runtime_checkable
class ReviewHistory(Protocol):
    def record(self, summary: ReviewSummary) -> None: ...

    def recent(
        self, *, limit: int = 100, repo: str | None = None, pr_id: int | None = None
    ) -> list[ReviewSummary]: ...

    def prune(self, *, older_than_days: float, now: datetime | None = None) -> int: ...


def _matches(summary: ReviewSummary, repo: str | None, pr_id: int | None) -> bool:
    return (repo is None or summary.repo == repo) and (pr_id is None or summary.pr_id == pr_id)


class InMemoryReviewHistory:
    def __init__(self) -> None:
        self._items: dict[str, ReviewSummary] = {}

    def record(self, summary: ReviewSummary) -> None:
        self._items[summary.review_key] = summary

    def recent(
        self, *, limit: int = 100, repo: str | None = None, pr_id: int | None = None
    ) -> list[ReviewSummary]:
        ordered = sorted(self._items.values(), key=lambda s: s.finished_at, reverse=True)
        return [s for s in ordered if _matches(s, repo, pr_id)][:limit]

    def prune(self, *, older_than_days: float, now: datetime | None = None) -> int:
        limit = (now or datetime.now(UTC)) - timedelta(days=older_than_days)
        old = [key for key, s in self._items.items() if s.finished_at < limit]
        for key in old:
            del self._items[key]
        return len(old)


class RedisReviewHistory:
    """Histórico en Redis. *Best-effort*: un fallo se registra y no toca la review."""

    def __init__(
        self,
        *,
        url: str,
        retention_days: float = 30.0,
        client: redis.Redis | None = None,
    ) -> None:
        self._redis = client if client is not None else build_redis_client(url)
        self._retention_days = retention_days

    def record(self, summary: ReviewSummary) -> None:
        try:
            pipe = self._redis.pipeline()
            pipe.zadd(INDEX_KEY, {summary.review_key: summary.finished_at.timestamp()})
            pipe.hset(DATA_KEY, summary.review_key, summary.model_dump_json())
            pipe.execute()
            # Recorte por fecha en cada escritura: el histórico no crece aunque el
            # barrido de la poda no corra.
            self.prune(older_than_days=self._retention_days, now=summary.finished_at)
        except redis.RedisError as exc:
            logger.warning("No se pudo anotar %s en el histórico: %s", summary.review_key, exc)

    def recent(
        self, *, limit: int = 100, repo: str | None = None, pr_id: int | None = None
    ) -> list[ReviewSummary]:
        try:
            # Con filtro se lee mas para no quedarse corto tras filtrar.
            window = limit if repo is None and pr_id is None else max(limit * 10, 1000)
            keys = [str(k) for k in self._redis.zrevrange(INDEX_KEY, 0, window - 1)]
            raw = self._redis.hmget(DATA_KEY, keys) if keys else []
        except redis.RedisError as exc:
            logger.warning("No se pudo leer el histórico: %s", exc)
            return []
        out: list[ReviewSummary] = []
        for item in raw:
            if not item:
                continue
            try:
                summary = ReviewSummary.model_validate_json(item)
            except ValueError:
                continue
            if _matches(summary, repo, pr_id):
                out.append(summary)
            if len(out) >= limit:
                break
        return out

    def prune(self, *, older_than_days: float, now: datetime | None = None) -> int:
        limit = ((now or datetime.now(UTC)) - timedelta(days=older_than_days)).timestamp()
        try:
            old = [str(k) for k in self._redis.zrangebyscore(INDEX_KEY, "-inf", f"({limit}")]
            if not old:
                return 0
            pipe = self._redis.pipeline()
            pipe.zrem(INDEX_KEY, *old)
            pipe.hdel(DATA_KEY, *old)
            pipe.execute()
            return len(old)
        except redis.RedisError as exc:
            logger.warning("No se pudo podar el histórico: %s", exc)
            return 0
