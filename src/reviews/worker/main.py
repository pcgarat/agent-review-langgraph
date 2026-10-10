from __future__ import annotations

import logging

from reviews.adapters.null_reviewer import NullReviewer
from reviews.adapters.teams_webhook import TeamsWebhookAdapter
from reviews.adapters.twg_vcs import TwgVcsAdapter
from reviews.domain.models import ReviewRequest
from reviews.graph.build import build_graph
from reviews.policy.engine import PolicyEngine
from reviews.ports.chat import ChatPort
from reviews.ports.reviewer import ReviewerPort
from reviews.ports.vcs import VcsPort
from reviews.queue import InMemoryReviewQueue, RedisReviewQueue, ReviewQueue
from reviews.settings import Settings, get_settings

logger = logging.getLogger(__name__)


def build_queue(settings: Settings) -> ReviewQueue:
    queue = RedisReviewQueue(url=settings.redis_url)
    if queue.available():
        return queue
    logger.warning("Redis no disponible en worker; cola en memoria (solo util en tests)")
    return InMemoryReviewQueue()


def build_runtime(
    settings: Settings,
    *,
    vcs: VcsPort | None = None,
    chat: ChatPort | None = None,
    reviewers: list[ReviewerPort] | None = None,
    policy: PolicyEngine | None = None,
):
    vcs = vcs or TwgVcsAdapter(
        workspace=settings.bitbucket_workspace,
        repo=settings.bitbucket_repo,
        twg_bin=settings.twg_bin,
    )
    chat = chat if chat is not None else TeamsWebhookAdapter(webhook_url=settings.teams_webhook_url)
    reviewers = reviewers or [NullReviewer("null")]
    policy = policy or PolicyEngine.from_file(settings.autonomy_config)
    graph = build_graph(vcs=vcs, reviewers=reviewers, policy=policy, chat=chat).compile()
    return graph


def process_request(graph, request: ReviewRequest) -> None:
    logger.info("Procesando review %s", request.idempotency_key)
    try:
        graph.invoke({"request": request, "findings": []})
        logger.info("Review completada %s", request.idempotency_key)
    except Exception:
        logger.exception("Fallo procesando %s", request.idempotency_key)


def run_forever(
    *,
    settings: Settings | None = None,
    queue: ReviewQueue | None = None,
    graph=None,
    poll_s: float = 5.0,
) -> None:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level)
    queue = queue or build_queue(settings)
    graph = graph or build_runtime(settings)
    logger.info("Worker de reviews arrancado (Fase 1)")
    logger.info("Politica: %s", settings.autonomy_config)
    while True:
        request = queue.dequeue(timeout_s=poll_s)
        if request is None:
            continue
        process_request(graph, request)


def main() -> None:
    run_forever()


if __name__ == "__main__":
    main()
