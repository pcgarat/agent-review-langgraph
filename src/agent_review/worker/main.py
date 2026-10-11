from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, RetryPolicy

from agent_review.adapters.github_vcs import GithubVcsAdapter
from agent_review.adapters.http_sandbox import HttpSandboxAdapter
from agent_review.adapters.llm_fixer import LlmFixer
from agent_review.adapters.llm_reviewer import LlmReviewer
from agent_review.adapters.null_reviewer import NullReviewer
from agent_review.adapters.openai_llm import OpenAiCompatibleLlm
from agent_review.adapters.teams_webhook import TeamsWebhookAdapter
from agent_review.adapters.tooling_reviewer import ToolingReviewer
from agent_review.adapters.tooling_source import FileToolingOutputSource
from agent_review.adapters.tooling_source import (
    validate_tooling_template as validate_tooling_template_source,
)
from agent_review.adapters.twg_vcs import TwgVcsAdapter, check_twg
from agent_review.checkpointer import (
    build_checkpointer,
    latest_checkpoints,
    paused_thread_ids,
    prune_completed_threads,
)
from agent_review.domain.models import (
    DecisionOutcome,
    FindingDecision,
    HitlDecision,
    ReviewOutcome,
    ReviewRequest,
)
from agent_review.domain.paused import PausedReview, PauseKind
from agent_review.domain.pending import InMemoryPendingStore, PendingStatus
from agent_review.errors import is_transient
from agent_review.fix.delivery import DeliveryFormatter, DiffFormatter, SuggestionFormatter
from agent_review.graph.build import (
    ReviewState,
    build_approval,
    build_graph,
    outcome_from_state,
    render_pause_notice,
)
from agent_review.paused_store import InMemoryPausedStore, PausedStore, RedisPausedStore
from agent_review.pending_store import PendingStore, RedisPendingStore
from agent_review.policy.engine import PolicyEngine
from agent_review.ports.chat import ChatPort
from agent_review.ports.fixer import FixerPort
from agent_review.ports.llm import LlmPort
from agent_review.ports.reviewer import ReviewerPort
from agent_review.ports.sandbox import SandboxPort
from agent_review.ports.tooling import ToolingOutputSource
from agent_review.ports.vcs import VcsPort
from agent_review.profile import expected_tools, fix_recipes, load_profile, sandbox_timeout_s
from agent_review.queue import InMemoryReviewQueue, RedisReviewQueue, ReviewQueue
from agent_review.settings import Settings, get_settings, is_production, require_production_secrets

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


def build_paused(settings: Settings) -> PausedStore:
    """Inbox de reviews pausadas: lo escribe el worker al pausar, lo lee el canal."""
    store = RedisPausedStore(url=settings.redis_url, ttl_s=settings.decision_ttl_s)
    if store.available():
        return store
    logger.warning("Redis no disponible en worker; inbox de pausas en memoria")
    return InMemoryPausedStore(ttl_s=settings.decision_ttl_s)


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


def build_chat(settings: Settings) -> ChatPort | None:
    """Canal de chat si hay URL. Sin URL no hay canal: no se construye un no-op."""
    if settings.teams_webhook_url.strip():
        return TeamsWebhookAdapter(webhook_url=settings.teams_webhook_url)
    return None


def node_retry_policy(settings: Settings) -> RetryPolicy:
    """Politica de reintento de nodo segun configuracion.

    El default vive en el grafo (`default_retry_policy`), pero el worker la
    parametriza para poder subir/bajar los intentos sin tocar codigo y para
    desactivarla (`node_retry_max_attempts=1`) en escenarios de diagnostico.
    Se desactiva con `max_attempts=1` y no con `None`: `build_graph` lee `None`
    como "usa el default" y volveria a reintentar 3 veces.
    """
    return RetryPolicy(
        max_attempts=max(1, settings.node_retry_max_attempts),
        initial_interval=settings.node_retry_initial_interval_s,
        backoff_factor=settings.node_retry_backoff_factor,
        max_interval=settings.node_retry_max_interval_s,
        retry_on=is_transient,
    )


def build_vcs(settings: Settings) -> VcsPort:
    """Adaptador del proveedor de VCS activo (`VCS_PROVIDER`).

    El grafo solo conoce el `VcsPort`; este es el unico punto que sabe si detras
    hay Bitbucket (CLI `twg`) o GitHub (API REST).
    """
    if settings.vcs_provider == "github":
        if not settings.github_token.strip():
            # Falla ruidoso en el arranque: descubrirlo en la primera review
            # pareceria un fallo del PR revisado.
            raise RuntimeError(
                "VCS_PROVIDER=github requiere GITHUB_TOKEN; sin el no hay diff ni comentarios."
            )
        logger.info("Adaptador VCS: GitHub (%s/%s)", settings.github_owner, settings.github_repo)
        return GithubVcsAdapter(
            owner=settings.github_owner,
            repo=settings.github_repo,
            token=settings.github_token,
            api_url=settings.github_api_url,
            timeout_s=settings.twg_timeout_s,
        )
    # A2: fallar ruidoso en el arranque si twg no esta, en vez de descubrirlo al
    # procesar la primera review (que pareceria un fallo del PR revisado).
    version = check_twg(settings.twg_bin, timeout_s=settings.twg_timeout_s)
    logger.info("Adaptador VCS: twg %s", version)
    return TwgVcsAdapter(
        workspace=settings.bitbucket_workspace,
        repo=settings.bitbucket_repo,
        twg_bin=settings.twg_bin,
        timeout_s=settings.twg_timeout_s,
    )


def build_fixer(settings: Settings) -> FixerPort | None:
    """Fixer LLM si el modelo esta configurado. Sin el, el nodo `fix` marca
    `NOT_VERIFIABLE` (no se puede proponer parche) en vez de fingir un fix."""
    llm = build_llm(settings)
    if llm is None:
        return None
    return LlmFixer(
        llm=llm,
        budget_tokens_per_pr=settings.llm_budget_tokens_per_pr,
        max_fix_lines=settings.max_fix_lines,
    )


def build_delivery(settings: Settings) -> DeliveryFormatter:
    """Formato de entrega del parche verificado (E6). `diff` hasta que R1 confirme
    que Bitbucket aplica `suggestion`."""
    if settings.fix_delivery.strip().lower() == "suggestion":
        return SuggestionFormatter()
    return DiffFormatter()


def build_sandbox(settings: Settings) -> SandboxPort | None:
    """Adaptador HTTP del `sandbox-runner` (Fase 3). `None` si no esta habilitado:
    en ese caso el nodo `fix` marca `NOT_VERIFIABLE` y no se ejecuta nada (F7)."""
    if not settings.sandbox_enabled or not settings.sandbox_runner_url.strip():
        return None
    return HttpSandboxAdapter(
        base_url=settings.sandbox_runner_url,
        token=settings.sandbox_runner_token,
        timeout_s=sandbox_timeout_s(settings),
    )


def build_runtime(
    settings: Settings,
    *,
    vcs: VcsPort | None = None,
    chat: ChatPort | None = None,
    reviewers: list[ReviewerPort] | None = None,
    policy: PolicyEngine | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    fixer: FixerPort | None = None,
    sandbox: SandboxPort | None = None,
) -> Any:
    validate_tooling_template(settings)
    vcs = vcs or build_vcs(settings)
    # Sin URL de Teams no hay canal: no se construye un adaptador que no publica.
    if chat is None:
        chat = build_chat(settings)
    reviewers = reviewers if reviewers is not None else build_reviewers(settings)
    policy = policy or PolicyEngine.from_file(
        settings.autonomy_config, cap_override=settings.max_autonomy_override
    )
    saver = checkpointer or build_checkpointer(
        settings.database_url, required=is_production(settings)
    )
    graph = build_graph(
        vcs=vcs,
        reviewers=reviewers,
        policy=policy,
        chat=chat,
        sandbox=sandbox if sandbox is not None else build_sandbox(settings),
        fixer=fixer if fixer is not None else build_fixer(settings),
        delivery=build_delivery(settings),
        fix_recipes=fix_recipes(settings),
        scope_ignore=settings.scope_ignore,
        retry_policy=node_retry_policy(settings),
    )
    return graph.compile(checkpointer=saver)


def process_request(
    graph: ReviewGraph,
    request: ReviewRequest,
    *,
    paused: PausedStore | None = None,
    chat: ChatPort | None = None,
    vcs: VcsPort | None = None,
) -> ReviewOutcome:
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
    - **A medias** (`next` no vacio, sin `interrupt`): se continua desde el ultimo
      checkpoint con `invoke(None)`. Re-invocar con input arrancaria otra pasada
      desde `START`: pagaria el LLM otra vez, acumularia `findings` (reducer
      `operator.add`) y, tras una decision humana, publicaria hallazgos nuevos que
      nadie decidio (`human_decisions` ya esta en el estado y `hitl` no pausa).

    `paused`/`chat`/`vcs` son opcionales: en los tests se ejecuta sin inbox ni
    aviso, y en produccion se pasan para registrar la pausa y avisar con el enlace
    del PR (spec §4.4).
    """
    logger.info("Procesando review %s (intento %d)", request.review_key, request.attempts + 1)
    config: RunnableConfig = {"configurable": {"thread_id": request.thread_id}}
    snapshot = _thread_snapshot(graph, config)
    if snapshot is not None and snapshot.interrupts:
        return _resume_paused(
            graph, request, config=config, paused_state=snapshot, paused=paused, chat=chat, vcs=vcs
        )
    if snapshot is not None and not snapshot.next:
        logger.info(
            "Review %s ya completada en su hilo; no se re-ejecuta ni re-publica",
            request.review_key,
        )
        if request.decision is not None:
            # Carrera habitual: el `expired` del TTL y la decision humana estaban
            # ambos en cola y gano el primero. No se pierde en silencio.
            logger.warning(
                "Decision de %s sobre %s descartada: el hilo ya estaba completado",
                request.decision.actor,
                request.review_key,
            )
        # Si el worker murio entre el resume y la limpieza, la entrada seguiria en
        # el inbox y el barrido la reencolaria en bucle.
        _clear_paused(paused, request)
        return outcome_from_state(cast("ReviewState", snapshot.values))
    if snapshot is not None:
        logger.info(
            "Review %s a medias en su hilo (siguiente: %s); se continua desde el checkpoint",
            request.review_key,
            ", ".join(snapshot.next),
        )
        result = cast("ReviewState", graph.invoke(None, config=config))
    else:
        if request.decision is not None:
            # Sin hilo (podado o checkpointer en memoria reiniciado) no hay pausa que
            # reanudar: se revisa desde cero y la decision no aplica.
            logger.warning(
                "Decision de %s sobre %s descartada: no hay hilo; se revisa desde cero",
                request.decision.actor,
                request.review_key,
            )
        result = cast(
            "ReviewState", graph.invoke({"request": request, "findings": []}, config=config)
        )
    outcome = _log_outcome(result)
    if outcome.paused:
        _on_paused(result, outcome.request, paused=paused, chat=chat, vcs=vcs)
    else:
        # Un resume que fallo y se completa en el reintento (rama a medias) tambien
        # debe salir del inbox.
        _clear_paused(paused, request)
    return outcome


def _resume_paused(
    graph: ReviewGraph,
    request: ReviewRequest,
    *,
    config: RunnableConfig,
    paused_state: Any,
    paused: PausedStore | None = None,
    chat: ChatPort | None = None,
    vcs: VcsPort | None = None,
) -> ReviewOutcome:
    """Reanuda un hilo pausado. Sin decision no repite el trabajo.

    Sin decision, solo comprueba que la pausa siga en el inbox: si el registro se
    perdio (Redis caido al pausar, el job se reencolo), lo repone. Sin esa entrada
    nadie la ve ni el TTL la vence nunca.
    """
    state = cast("ReviewState", paused_state.values)
    if request.decision is None:
        logger.info(
            "Review %s esta pausada esperando decision; la reentrega no la re-ejecuta",
            request.review_key,
        )
        _ensure_registered(paused_state, paused=paused, chat=chat, vcs=vcs)
        return outcome_from_state(state, paused=True)
    result = cast("ReviewState", graph.invoke(Command(resume=request.decision), config=config))
    logger.info(
        "Review %s reanudada con decision de %s", request.review_key, request.decision.actor
    )
    outcome = _log_outcome(result)
    if not outcome.paused:
        # Aplicada la decision, el inbox deja de mostrarla. El estado durable del
        # hilo sigue en el checkpointer.
        _clear_paused(paused, request)
    return outcome


def _clear_paused(paused: PausedStore | None, request: ReviewRequest) -> None:
    """Saca la review del inbox si la entrada es de *este* hilo.

    La comprobacion del hilo evita que una pasada degradada ya terminada (hilo
    `:partial`, ver `ReviewRequest.thread_id`) borre la pausa de la pasada completa
    de la misma ejecucion, que comparte `review_key`.
    """
    if paused is None:
        return
    entry = paused.get(request.review_key)
    if entry is not None and entry.request.thread_id == request.thread_id:
        paused.resume(request.review_key)


def _on_paused(
    result: ReviewState,
    request: ReviewRequest,
    *,
    paused: PausedStore | None,
    chat: ChatPort | None,
    vcs: VcsPort | None = None,
    paused_at: datetime | None = None,
) -> None:
    """Registra la pausa en el inbox y avisa, sin publicar los hallazgos (spec §4.4)."""
    approval = build_approval(result)
    if approval is None:
        # No deberia ocurrir: se pauso con pendientes. Se registra para no quedar mudo.
        logger.warning("Review %s pausada sin ApprovalRequest en el estado", request.review_key)
        return
    if paused is not None:
        if _superseded(paused.get(request.review_key), request):
            # La pasada completa ya esta pausada: manda ella. La degradada queda
            # superada y `reconcile_paused` borra su hilo.
            logger.info(
                "Pausa degradada de %s superada por la pasada completa; no se registra",
                request.review_key,
            )
            return
        paused.pause(request, approval, created_at=paused_at)
    if chat is not None:
        chat.notify_decision_pending(
            title=f"{request.repo}#{request.pr_id}",
            body=render_pause_notice(approval),
            link=_pr_url(vcs, request),
        )
    if paused is None:
        logger.info(
            "Review %s pausada con %d pendiente(s) (sin inbox configurado)",
            request.review_key,
            len(approval.pending),
        )


def _ensure_registered(
    snapshot: Any,
    *,
    paused: PausedStore | None,
    chat: ChatPort | None,
    vcs: VcsPort | None,
) -> bool:
    """Repone en el inbox un hilo pausado que no esta en el; `True` si lo repuso.

    El hilo es la verdad durable y el inbox solo su indice. `paused_at` sale del
    checkpoint para que el TTL cuente desde la pausa real, no desde que se repone.
    """
    if paused is None:
        return False
    state = cast("ReviewState", snapshot.values)
    request = ReviewRequest.model_validate(state["request"])
    if not _needs_registration(paused.get(request.review_key), request):
        return False
    logger.warning("Pausa de %s ausente del inbox; se repone", request.review_key)
    _on_paused(
        state,
        request,
        paused=paused,
        chat=chat,
        vcs=vcs,
        paused_at=_snapshot_ts(snapshot),
    )
    return True


def _superseded(current: PausedReview | None, request: ReviewRequest) -> bool:
    """¿La entrada del inbox es de otra pasada que manda sobre este hilo?

    La pasada degradada (`:partial`) y la completa comparten `review_key`, la
    unica clave del inbox. Si ambas pausan, manda la completa: la degradada queda
    superada (no se registra) y `reconcile_paused` borra su hilo.
    """
    return (
        current is not None
        and current.request.thread_id != request.thread_id
        and request.degraded
        and not current.request.degraded
    )


def _needs_registration(current: PausedReview | None, request: ReviewRequest) -> bool:
    """Falta en el inbox, o lo ocupa una pasada degradada que esta supera."""
    if current is None:
        return True
    return current.request.thread_id != request.thread_id and not _superseded(current, request)


def reconcile_paused(
    graph: ReviewGraph,
    paused: PausedStore,
    *,
    chat: ChatPort | None = None,
    vcs: VcsPort | None = None,
    thread_ids: list[str] | None = None,
) -> list[str]:
    """Repone en el inbox los hilos pausados que el checkpointer conoce y el inbox no.

    Cubre la caida del worker entre el checkpoint del `interrupt` y `_on_paused`:
    el job ya salio de la cola (`BLPOP`), asi que ninguna reentrega lo arreglaria.

    Tambien borra los hilos degradados (`:partial`) pausados cuya entrada del
    inbox ocupa la pasada completa: nadie puede decidirlos ni el TTL los vence, y
    la poda los protege por tener `interrupt`, asi que vivirian para siempre.

    `thread_ids` permite pasar los hilos pausados ya calculados (el bucle del
    worker comparte un unico listado del saver con la poda).
    """
    saver = getattr(graph, "checkpointer", None)
    if saver is None:
        return []
    restored: list[str] = []
    for thread_id in thread_ids if thread_ids is not None else paused_thread_ids(saver):
        try:
            snapshot = graph.get_state({"configurable": {"thread_id": thread_id}})
            if not snapshot.interrupts:
                continue
            request = ReviewRequest.model_validate(snapshot.values["request"])
            if _superseded(paused.get(request.review_key), request):
                logger.warning(
                    "Hilo degradado %s superado por la pausa completa; se borra", thread_id
                )
                saver.delete_thread(thread_id)
                continue
            if _ensure_registered(snapshot, paused=paused, chat=chat, vcs=vcs):
                restored.append(thread_id)
        except Exception:
            logger.exception("No se pudo reponer la pausa del hilo %s", thread_id)
    return restored


def _snapshot_ts(snapshot: Any) -> datetime | None:
    raw = getattr(snapshot, "created_at", None)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _pr_url(vcs: VcsPort | None, request: ReviewRequest) -> str | None:
    """URL del PR para el aviso, si el VCS la tiene; nunca aborta la pausa."""
    if vcs is None:
        return None
    try:
        return vcs.get_pull_request(request.repo, request.pr_id).url or None
    except Exception:
        logger.exception(
            "No se pudo resolver la URL del PR %s#%s para el aviso", request.repo, request.pr_id
        )
        return None


def sweep_expired(
    paused: PausedStore,
    queue: ReviewQueue,
    *,
    now: datetime | None = None,
) -> list[ReviewRequest]:
    """Reencola una reanudacion `expired` para cada pausa vencida (spec §4.7).

    Un hilo pausado no puede quedarse eterno: al superar `DECISION_TTL_S` se
    reanuda con `expired` para todos los pendientes (la review se publica "sin
    decision"), nunca se deja colgada. No borra el inbox: lo limpia el resume.
    Cada pausa se encola una vez y solo se reencola si el job se perdio (sigue en
    el inbox pasado un plazo), con tope: si su reanudacion falla de forma
    persistente, acaba en la DLQ en vez de volver con `attempts=0` cada barrido.
    """
    enqueued: list[ReviewRequest] = []
    for review in paused.sweep(now=now):
        # El barrido `expired` es del HITL: una pausa de fix vence por su propio
        # TTL (`FIX_VERIFY_TTL_S`, F20), no se reanuda como "sin decision".
        if review.kind is not PauseKind.HUMAN:
            continue
        expired = HitlDecision(
            review_key=review.review_key,
            actor="system:ttl",
            items=[
                FindingDecision(finding_id=item.finding_id, outcome=DecisionOutcome.EXPIRED)
                for item in review.human_request.pending
            ],
        )
        request = review.request.model_copy(update={"decision": expired})
        queue.enqueue(request)
        paused.mark_expired_enqueued(review.review_key, now=now)
        enqueued.append(request)
        logger.warning(
            "Pausa de %s vencida; se reanuda como sin decision (%d hallazgo(s))",
            review.review_key,
            len(expired.items),
        )
    return enqueued


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

    Si la lectura falla, la excepcion sube (el job se reencola): tratarlo como
    "no hay hilo" re-ejecutaria desde `START` un hilo quiza pausado, que volveria
    a pausar y a avisar.
    """
    if getattr(graph, "checkpointer", None) is None:
        return None
    snapshot = graph.get_state(config)
    values = getattr(snapshot, "values", None)
    if not values:
        return None
    return snapshot


def _consume(
    queue: ReviewQueue,
    graph: ReviewGraph,
    request: ReviewRequest,
    *,
    max_attempts: int,
    paused: PausedStore | None = None,
    chat: ChatPort | None = None,
    vcs: VcsPort | None = None,
) -> None:
    """Procesa un job; ante un fallo transitorio reencola, y al agotar intentos descarta."""
    try:
        process_request(graph, request, paused=paused, chat=chat, vcs=vcs)
    except Exception as exc:
        if is_transient(exc):
            logger.warning(
                "Fallo transitorio procesando %s (%s); se reencola",
                request.review_key,
                type(exc).__name__,
            )
        else:
            # Un fallo permanente (bug, payload invalido) no se arregla reintentando
            # el job entero, pero la clasificacion de job es mas laxa que la de
            # nodo: se registra para poder distinguirlo en la DLQ.
            logger.exception("Fallo no clasificado como transitorio en %s", request.review_key)
        try:
            _requeue_or_drop(queue, request, max_attempts=max_attempts)
        except Exception:
            # El manejo del error no puede tumbar el worker: el job ya salio de la
            # cola (`BLPOP`) y una excepcion aqui mataria el bucle entero.
            logger.exception(
                "No se pudo reencolar ni mandar a la DLQ %s; job perdido: %s",
                request.review_key,
                request.model_dump_json(),
            )


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
        request = review.request.model_copy(update={"degraded": True})
        queue.enqueue(request)
        enqueued.append(request)
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
    paused: PausedStore | None = None,
    poll_s: float = 5.0,
    sweep_s: float = 30.0,
    prune_s: float = 3600.0,
) -> None:
    settings = settings or get_settings()
    require_production_secrets(settings)
    logging.basicConfig(level=settings.log_level)
    queue = queue or build_queue(settings)
    chat = build_chat(settings)
    # El worker guarda el VCS para el enlace del aviso de pausa. Con un grafo
    # inyectado (tests) no lo hay y el aviso sale sin enlace.
    vcs: VcsPort | None = None
    if graph is None:
        vcs = build_vcs(settings)
        graph = build_runtime(settings, chat=chat, vcs=vcs)
    pending_store = pending if pending is not None else build_pending(settings)
    paused_store = paused if paused is not None else build_paused(settings)
    saver = getattr(graph, "checkpointer", None)
    logger.info("Worker de reviews arrancado (Fase 2)")
    logger.info("Politica: %s", settings.autonomy_config)
    last_sweep = time.monotonic()
    last_prune = time.monotonic()
    start_heartbeat()
    while True:
        request = queue.dequeue(timeout_s=poll_s)
        if request is not None:
            _consume(
                queue,
                graph,
                request,
                max_attempts=settings.max_review_attempts,
                paused=paused_store,
                chat=chat,
                vcs=vcs,
            )
            continue
        # La cola esta vacia: es el momento de aplicar los TTL (ingesta y decision).
        now = time.monotonic()
        if now - last_sweep >= sweep_s:
            sweep_pending(settings, queue, pending=pending_store)
            sweep_expired(paused_store, queue)
            last_sweep = now
        # La poda lista todos los hilos (escaneo caro en Postgres): va con su propio
        # intervalo, mucho mas largo que los TTL.
        if saver is not None and now - last_prune >= prune_s:
            # Un solo listado del saver alimenta la reconciliacion y la poda.
            latest = latest_checkpoints(saver)
            if latest is not None:
                # Antes de podar: repone las pausas que el inbox perdio, para que
                # la poda (y el barrido del TTL) las vean.
                reconcile_paused(
                    graph,
                    paused_store,
                    chat=chat,
                    vcs=vcs,
                    thread_ids=paused_thread_ids(saver, latest=latest),
                )
                prune_completed_threads(
                    saver,
                    older_than_days=settings.checkpoint_retention_days,
                    paused_threads=[review.review_key for review in paused_store.all()],
                    latest=latest,
                )
            last_prune = now


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
