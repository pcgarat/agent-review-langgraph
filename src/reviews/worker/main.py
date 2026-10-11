from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from reviews.adapters.llm_reviewer import LlmReviewer
from reviews.adapters.null_reviewer import NullReviewer
from reviews.adapters.openai_llm import OpenAiCompatibleLlm
from reviews.adapters.teams_webhook import TeamsWebhookAdapter
from reviews.adapters.tooling_reviewer import ToolingReviewer
from reviews.adapters.tooling_source import FileToolingOutputSource
from reviews.adapters.tooling_source import (
    validate_tooling_template as validate_tooling_template_source,
)
from reviews.adapters.twg_vcs import TwgVcsAdapter, check_twg
from reviews.checkpointer import build_checkpointer
from reviews.domain.models import ReviewOutcome, ReviewRequest
from reviews.domain.pending import InMemoryPendingStore, PendingStatus
from reviews.graph.build import ReviewState, build_graph, outcome_from_state
from reviews.pending_store import PendingStore, RedisPendingStore
from reviews.policy.engine import PolicyEngine
from reviews.ports.chat import ChatPort
from reviews.ports.llm import LlmPort
from reviews.ports.reviewer import ReviewerPort
from reviews.ports.tooling import ToolingOutputSource
from reviews.ports.vcs import VcsPort
from reviews.profile import expected_tools, load_profile
from reviews.queue import InMemoryReviewQueue, RedisReviewQueue, ReviewQueue
from reviews.settings import Settings, get_settings, require_production_secrets

logger = logging.getLogger(__name__)

# Grafo de review ya compilado (lo que consume el worker).
ReviewGraph = CompiledStateGraph[ReviewState]

# Fichero de latido que refleja que el worker sigue vivo (healthcheck de compose).
HEARTBEAT_PATH = "/tmp/reviews-worker-heartbeat"


def build_pending(settings: Settings) -> PendingStore:
    """Registro de ingesta: que artefactos han llegado por review."""
    tools = expected_tools(settings)
    store = RedisPendingStore(
        url=settings.redis_url,
        expected_tools=tools,
        ttl_ready_s=settings.ingest_ttl_ready_s,
        ttl_pending_s=settings.ingest_ttl_pending_s,
    )
    if store.available():
        return store
    return InMemoryPendingStore(expected_tools=tools)


def build_queue(settings: Settings) -> ReviewQueue:
    queue = RedisReviewQueue(url=settings.redis_url)
    if queue.available():
        return queue
    logger.warning("Redis no disponible en worker; cola en memoria (solo util en tests)")
    return InMemoryReviewQueue()


def validate_tooling_template(settings: Settings) -> None:
    """Valida la plantilla del artefacto en el arranque, no en la primera review.

    La ruta la publica el CI "por convencion": si la plantilla es irresoluble o
    incompatible con los placeholders, conviene fallar al arrancar (D2) en vez de
    descubrirlo cuando el primer PR necesita leer su salida.
    """
    validate_tooling_template_source(
        template=settings.tooling_output_template, root=settings.tooling_output_root
    )


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
    profile = load_profile(settings)
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
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> Any:
    validate_tooling_template(settings)
    # A2: fallar ruidoso en el arranque si twg no esta, en vez de descubrirlo al
    # procesar la primera review (que pareceria un fallo del PR revisado).
    if vcs is None:
        version = check_twg(settings.twg_bin)
        logger.info("Adaptador VCS: twg %s", version)
    vcs = vcs or TwgVcsAdapter(
        workspace=settings.bitbucket_workspace,
        repo=settings.bitbucket_repo,
        twg_bin=settings.twg_bin,
    )
    # Sin URL de Teams no hay canal: no se construye un adaptador que no publica.
    if chat is None and settings.teams_webhook_url.strip():
        chat = TeamsWebhookAdapter(webhook_url=settings.teams_webhook_url)
    reviewers = reviewers if reviewers is not None else build_reviewers(settings)
    policy = policy or PolicyEngine.from_file(
        settings.autonomy_config, cap_override=settings.max_autonomy_override
    )
    saver = checkpointer or build_checkpointer(settings.database_url)
    graph = build_graph(
        vcs=vcs,
        reviewers=reviewers,
        policy=policy,
        chat=chat,
        scope_ignore=settings.scope_ignore,
    )
    return graph.compile(checkpointer=saver)


def process_request(graph: ReviewGraph, request: ReviewRequest) -> ReviewOutcome:
    """Ejecuta la review una vez. No reencola: eso es de `run_forever`.

    Publicar y reencolar no pueden convivir en la misma pasada: `graph.invoke`
    ya corre el nodo `publish`, asi que un reencolado tras una excepcion de
    Teams duplicaria los comentarios. El reintento se decide con la excepcion.

    El grafo se invoca con `thread_id` (= `review_key`): si el mensaje se
    reentrega, el checkpointer devuelve el estado ya terminado y no se vuelve a
    publicar. Tres estados del hilo (ver docs/fase2-hitl-20261011.md §4.3):

    - **Pausado** (`interrupt` pendiente): con decision -> `Command(resume=...)`;
      sin decision -> no-op (no se re-ejecuta `scope` ni las personas).
    - **Completado** (`next` vacio): no-op.
    - **A medias** (`next` no vacio, sin `interrupt`): se re-invoca (dedupe por
      marcas).
    """
    logger.info("Procesando review %s (intento %d)", request.review_key, request.attempts + 1)
    config: RunnableConfig = {"configurable": {"thread_id": request.thread_id}}
    snapshot = _thread_snapshot(graph, config)
    if snapshot is not None and snapshot.interrupts:
        return _resume_paused(graph, request, config=config, paused_state=snapshot)
    if snapshot is not None and not snapshot.next:
        logger.info(
            "Review %s ya completada en su hilo; no se re-ejecuta ni re-publica",
            request.review_key,
        )
        return outcome_from_state(cast("ReviewState", snapshot.values))
    result = cast("ReviewState", graph.invoke({"request": request, "findings": []}, config=config))
    return _log_outcome(result)


def _resume_paused(
    graph: ReviewGraph,
    request: ReviewRequest,
    *,
    config: RunnableConfig,
    paused_state: Any,
) -> ReviewOutcome:
    """Reanuda un hilo pausado. Sin decision es no-op: no repite el trabajo."""
    if request.decision is None:
        logger.info(
            "Review %s esta pausada esperando decision; la reentrega no la re-ejecuta",
            request.review_key,
        )
        return outcome_from_state(cast("ReviewState", paused_state.values), paused=True)
    result = cast("ReviewState", graph.invoke(Command(resume=request.decision), config=config))
    logger.info(
        "Review %s reanudada con decision de %s", request.review_key, request.decision.actor
    )
    return _log_outcome(result)


def _log_outcome(result: ReviewState) -> ReviewOutcome:
    outcome = outcome_from_state(result)
    if outcome.paused:
        logger.info("Review PAUSADA %s; esperando decision humana", outcome.request.review_key)
    elif outcome.complete:
        logger.info("Review completada %s", outcome.request.review_key)
    else:
        # No se hizo el analisis declarado: no es una review completada. No se
        # reencola: la ingesta diferida ya espero al artefacto (barrera/TTL), asi
        # que reintentar aqui solo duplicaria la publicacion.
        logger.warning(
            "Review INCOMPLETA %s; sin cobertura de: %s",
            outcome.request.review_key,
            ", ".join(
                f"{name}={status.value}" for name, status in outcome.unavailable_sources.items()
            ),
        )
    return outcome


def _thread_snapshot(graph: ReviewGraph, config: RunnableConfig) -> Any | None:
    """Snapshot del hilo (pausado o completado), o `None` si no hay.

    `invoke(input)` sobre un hilo existente re-ejecuta el grafo desde `START`
    (duplicando la publicacion). Antes de invocar se consulta el checkpointer y
    se decide: pausado (hay `interrupts`), completado (`next` vacio) o a medias.
    Sin checkpointer no hay hilo que consultar y se procesa siempre.
    """
    if getattr(graph, "checkpointer", None) is None:
        return None
    try:
        snapshot = graph.get_state(config)
    except Exception:  # pragma: no cover - base caida
        logger.exception("No se pudo leer el estado del hilo %s", config)
        return None
    values = getattr(snapshot, "values", None)
    if not values:
        return None
    return snapshot


def _consume(
    queue: ReviewQueue, graph: ReviewGraph, request: ReviewRequest, *, max_attempts: int
) -> None:
    """Procesa un job; ante un fallo transitorio reencola, y al agotar intentos descarta."""
    try:
        process_request(graph, request)
    except Exception:
        logger.exception("Fallo procesando %s", request.review_key)
        _requeue_or_drop(queue, request, max_attempts=max_attempts)


def _requeue_or_drop(queue: ReviewQueue, request: ReviewRequest, *, max_attempts: int) -> None:
    if request.attempts + 1 >= max_attempts:
        # Job descartado a proposito tras fallos persistentes: no desaparece en el
        # log, va a la DLQ para poder reinyectarlo o inspeccionarlo (B2).
        logger.error(
            "Review %s agoto los %d intentos; se manda a la DLQ",
            request.review_key,
            max_attempts,
        )
        queue.dead_letter(request, reason=f"intentos agotados ({max_attempts})")
        return
    retry = request.model_copy(update={"attempts": request.attempts + 1})
    queue.enqueue(retry)
    logger.warning(
        "Review %s reencolada para el intento %d/%d",
        retry.review_key,
        retry.attempts + 1,
        max_attempts,
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
    graph: ReviewGraph | None = None,
    pending: PendingStore | None = None,
    poll_s: float = 5.0,
    sweep_s: float = 30.0,
) -> None:
    settings = settings or get_settings()
    require_production_secrets(settings)
    logging.basicConfig(level=settings.log_level)
    queue = queue or build_queue(settings)
    graph = graph or build_runtime(settings)
    pending_store = pending if pending is not None else build_pending(settings)
    logger.info("Worker de reviews arrancado (Fase 1)")
    logger.info("Politica: %s", settings.autonomy_config)
    last_sweep = time.monotonic()
    start_heartbeat()
    while True:
        request = queue.dequeue(timeout_s=poll_s)
        if request is not None:
            _consume(queue, graph, request, max_attempts=settings.max_review_attempts)
            continue
        # La cola esta vacia: es el momento de aplicar los TTL de ingesta.
        if time.monotonic() - last_sweep >= sweep_s:
            sweep_pending(settings, queue, pending=pending_store)
            last_sweep = time.monotonic()


def start_heartbeat(path: str = HEARTBEAT_PATH, interval_s: float = 10.0) -> None:
    """Lanza el latido en un hilo aparte y devuelve el control.

    Refrescar el latido solo entre polls lo ataria a la duracion de la review: una
    review larga (B3 es secuencial) dejaria el fichero viejo y el healthcheck
    reiniciaria un worker que esta trabajando. Con un hilo independiente el latido
    mide "el proceso sigue vivo", no "el bucle esta en reposo".
    """

    def _loop() -> None:
        while True:
            _touch_heartbeat(path)
            time.sleep(interval_s)

    threading.Thread(target=_loop, name="reviews-heartbeat", daemon=True).start()


def _touch_heartbeat(path: str = HEARTBEAT_PATH) -> None:
    """Refresca el latido que usa el healthcheck para saber que el worker vive.

    El proceso puede morir sin que nadie lo note (hoy no hay watchdog): el latido
    convierte "el contenedor sigue arriba" en "el bucle sigue corriendo". Si el
    fichero se queda viejo, `restart: unless-stopped` lo re-arranca.
    """
    try:
        Path(path).write_text(str(time.time()), encoding="utf-8")
    except OSError:
        logger.debug("No se pudo escribir el latido del worker en %s", path)


def main() -> None:
    run_forever()


if __name__ == "__main__":
    main()
