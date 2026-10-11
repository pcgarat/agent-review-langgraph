from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import yaml

from reviews.adapters.llm_reviewer import LlmReviewer
from reviews.adapters.null_reviewer import NullReviewer
from reviews.adapters.openai_llm import OpenAiCompatibleLlm
from reviews.adapters.teams_webhook import TeamsWebhookAdapter
from reviews.adapters.tooling_reviewer import ToolingReviewer
from reviews.adapters.tooling_source import FileToolingOutputSource
from reviews.adapters.twg_vcs import TwgVcsAdapter
from reviews.domain.models import ReviewRequest
from reviews.domain.pending import InMemoryPendingStore, PendingStatus
from reviews.graph.build import build_graph, outcome_from_state
from reviews.pending_store import PendingStore, RedisPendingStore
from reviews.policy.engine import PolicyEngine
from reviews.ports.chat import ChatPort
from reviews.ports.llm import LlmPort
from reviews.ports.reviewer import ReviewerPort
from reviews.ports.tooling import ToolingOutputSource
from reviews.ports.vcs import VcsPort
from reviews.queue import InMemoryReviewQueue, RedisReviewQueue, ReviewQueue
from reviews.settings import Settings, get_settings

logger = logging.getLogger(__name__)


def _expected_tools(settings: Settings) -> list[str]:
    return [tool.strip() for tool in settings.expected_tools.split(",") if tool.strip()]


def build_pending(settings: Settings):
    """Registro de ingesta: que artefactos han llegado por review."""
    store = RedisPendingStore(
        url=settings.redis_url,
        expected_tools=_expected_tools(settings),
        ttl_ready_s=settings.ingest_ttl_ready_s,
        ttl_pending_s=settings.ingest_ttl_pending_s,
    )
    if store.available():
        return store
    return InMemoryPendingStore(expected_tools=_expected_tools(settings))


def build_queue(settings: Settings) -> ReviewQueue:
    queue = RedisReviewQueue(url=settings.redis_url)
    if queue.available():
        return queue
    logger.warning("Redis no disponible en worker; cola en memoria (solo util en tests)")
    return InMemoryReviewQueue()


def _load_profile(settings: Settings) -> dict[str, Any]:
    path = Path(settings.repo_profile_config)
    try:
        profile = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        logger.warning("Sin perfil de repo en %s; se omite la persona estatica", path)
        return {}
    except yaml.YAMLError as exc:
        logger.error("Perfil de repo invalido en %s (%s); se omite la persona estatica", path, exc)
        return {}
    if not isinstance(profile, dict):
        logger.warning("Perfil de repo en %s no es un mapa; se omite la persona estatica", path)
        return {}
    return profile


def build_llm(settings: Settings) -> LlmPort | None:
    """Proveedor LLM si esta habilitado y configurado. Si no, None (sin ruido)."""
    if not settings.llm_reviewer_enabled:
        return None
    if not settings.llm_model:
        logger.warning("Persona LLM habilitada sin LLM_MODEL; se omite")
        return None
    return OpenAiCompatibleLlm(
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        base_url=settings.llm_base_url,
    )


def build_reviewers(
    settings: Settings, *, tooling_source: ToolingOutputSource | None = None
) -> list[ReviewerPort]:
    """Personas a partir del perfil del repo y de la configuracion.

    Fase 1: la persona estatica arranca por defecto; la persona LLM `correctness`
    es opcional (opt-in) para no gastar tokens hasta validar el publish E2E. El
    artefacto de la herramienta estatica lo deposita el CI; el worker no ejecuta
    PHPStan (eso es el sandbox de Fase 3).
    """
    reviewers: list[ReviewerPort] = []
    profile = _load_profile(settings)
    checks = profile.get("checks", {}) or {}
    if isinstance(checks, dict) and checks.get("typecheck"):
        source = tooling_source or FileToolingOutputSource(
            template=settings.tooling_output_template
        )
        reviewers.append(ToolingReviewer(source=source))

    llm = build_llm(settings)
    if llm is not None:
        reviewers.append(
            LlmReviewer(
                llm=llm,
                max_findings=settings.llm_max_findings,
                budget_tokens_per_pr=settings.llm_budget_tokens_per_pr,
            )
        )

    if not reviewers:
        return [NullReviewer("null")]
    return reviewers


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
    reviewers = reviewers if reviewers is not None else build_reviewers(settings)
    policy = policy or PolicyEngine.from_file(settings.autonomy_config)
    graph = build_graph(vcs=vcs, reviewers=reviewers, policy=policy, chat=chat).compile()
    return graph


def process_request(graph, request: ReviewRequest) -> None:
    logger.info("Procesando review %s", request.idempotency_key)
    try:
        result = graph.invoke({"request": request, "findings": []})
    except Exception:
        logger.exception("Fallo procesando %s", request.idempotency_key)
        return
    outcome = outcome_from_state(result)
    if outcome.complete:
        logger.info("Review completada %s", request.idempotency_key)
    else:
        # No se hizo el analisis declarado: no es una review completada.
        logger.warning(
            "Review INCOMPLETA %s; sin cobertura de: %s",
            request.idempotency_key,
            ", ".join(
                f"{name}={status.value}"
                for name, status in outcome.unavailable_sources.items()
            ),
        )


def sweep_pending(
    settings: Settings, queue: ReviewQueue, *, pending: PendingStore | None = None
) -> list[ReviewRequest]:
    """Encola las reviews cuya espera vencio (review degradada).

    Sin esto, una review cuyo CI nunca publica el artefacto quedaria en `PENDING`
    para siempre. El barrido es la unica via por la que una ingesta incompleta
    llega a ejecutarse, y por eso la review sale marcada como incompleta.
    """
    pending = pending if pending is not None else build_pending(settings)
    enqueued: list[ReviewRequest] = []
    for review in pending.sweep():
        if review.status is not PendingStatus.DEGRADED:
            continue
        queue.enqueue(review.request)
        enqueued.append(review.request)
        logger.warning(
            "Ingesta vencida para %s; review degradada encolada (faltan: %s)",
            review.key,
            ", ".join(review.missing) or "nada",
        )
    return enqueued


def run_forever(
    *,
    settings: Settings | None = None,
    queue: ReviewQueue | None = None,
    graph=None,
    pending: PendingStore | None = None,
    poll_s: float = 5.0,
    sweep_s: float = 30.0,
) -> None:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level)
    queue = queue or build_queue(settings)
    graph = graph or build_runtime(settings)
    pending_store = pending if pending is not None else build_pending(settings)
    logger.info("Worker de reviews arrancado (Fase 1)")
    logger.info("Politica: %s", settings.autonomy_config)
    last_sweep = time.monotonic()
    while True:
        request = queue.dequeue(timeout_s=poll_s)
        if request is not None:
            process_request(graph, request)
            continue
        # La cola esta vacia: es el momento de aplicar los TTL de ingesta.
        if time.monotonic() - last_sweep >= sweep_s:
            sweep_pending(settings, queue, pending=pending_store)
            last_sweep = time.monotonic()


def main() -> None:
    run_forever()


if __name__ == "__main__":
    main()
