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
from agent_review.adapters.llm_fixer import LlmFixer
from agent_review.adapters.llm_reviewer import LlmReviewer
from agent_review.adapters.null_reviewer import NullReviewer
from agent_review.adapters.tooling_reviewer import ToolingReviewer
from agent_review.adapters.tooling_source import FileToolingOutputSource
from agent_review.adapters.tooling_source import (
    validate_tooling_template as validate_tooling_template_source,
)
from agent_review.adapters.twg_vcs import TwgVcsAdapter, check_twg
from agent_review.chat_outbox import OutboxChatAdapter, RedisChatOutbox
from agent_review.checkpointer import (
    build_checkpointer,
    latest_checkpoints,
    paused_thread_ids,
    paused_thread_kinds,
    prune_completed_threads,
    prune_fix_threads,
)
from agent_review.domain.models import (
    ApprovalRequest,
    DecisionOutcome,
    FindingDecision,
    FixJob,
    FixResolution,
    FixResolutions,
    FixWaitRequest,
    HitlDecision,
    PendingDecision,
    ReviewOutcome,
    ReviewRequest,
)
from agent_review.domain.paused import PausedReview, PauseKind
from agent_review.domain.pending import InMemoryPendingStore, PendingStatus
from agent_review.errors import Cancelled, is_transient
from agent_review.fix.delivery import DeliveryFormatter, DiffFormatter, SuggestionFormatter
from agent_review.graph.build import (
    ReviewState,
    build_graph,
    build_pause,
    outcome_from_state,
)
from agent_review.models_registry import ModelRegistry
from agent_review.paused_store import InMemoryPausedStore, PausedStore, RedisPausedStore
from agent_review.pending_store import PendingStore, RedisPendingStore
from agent_review.policy.engine import PolicyEngine
from agent_review.ports.budget import BudgetPort
from agent_review.ports.chat import ChatPort
from agent_review.ports.fixer import FixerPort
from agent_review.ports.progress import ProgressPort
from agent_review.ports.progress import clear as clear_progress
from agent_review.ports.reviewer import ReviewerPort
from agent_review.ports.tooling import ToolingOutputSource
from agent_review.ports.vcs import VcsPort
from agent_review.profile import expected_tools, fix_recipes, load_profile
from agent_review.queue import FixQueue, InMemoryReviewQueue, RedisReviewQueue, ReviewQueue
from agent_review.settings import Settings, get_settings, is_production, require_production_secrets
from agent_review.worker.cancel import CancellationPort, build_cancellation

logger = logging.getLogger(__name__)

# Grafo de review ya compilado (lo que consume el worker).
ReviewGraph = CompiledStateGraph[ReviewState]

# Roles que el worker construye como personas de revision. `fixer` no esta aqui:
# no es una persona del fan-out, es el FixerPort de `fix_dispatch`.
LLM_REVIEW_ROLES = ("correctness", "security", "arch")

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


def build_fix_queue(settings: Settings) -> FixQueue:
    """Cola de waves de fix (E7): la consume el worker de fixes, no este."""
    from agent_review.queue import InMemoryFixQueue, RedisFixQueue

    queue = RedisFixQueue(url=settings.redis_url)
    if queue.available():
        return queue
    logger.warning("Redis no disponible en worker; cola de fixes en memoria")
    return InMemoryFixQueue()


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


def validate_models(registry: ModelRegistry) -> None:
    """Contrasta los modelos bindeados contra el catalogo del proveedor.

    Un `LLM_MODEL` con typo o retirado hoy se descubre en la primera review, con la
    persona degradando a 0 hallazgos. Un binding que no resuelve, o una credencial
    que el proveedor rechaza (401/403), falla el arranque aqui.
    Un proveedor inalcanzable **no** bloquea el arranque: un modelo es un binding
    declarado, no una dependencia dura (el worker debe poder arrancar en un
    despliegue sin salida a internet).
    """
    if not registry.config.bindings:
        return
    problems = registry.probe()
    if not problems:
        return
    # Solo es fatal lo que no depende de la red (un modelo que el proveedor no
    # lista, una credencial rechazada); lo decide `probe` en `Problem.fatal`. Un
    # proveedor que no responde se registra como aviso.
    unresolved = [p for p in problems if p.fatal]
    if unresolved:
        raise RuntimeError("Registro de modelos invalido: " + "; ".join(str(p) for p in unresolved))
    for problem in problems:
        logger.warning("Registro de modelos: %s", problem)


def build_registry(settings: Settings) -> ModelRegistry:
    """Registro de modelos del despliegue (declarativo o implicito).

    Se construye una sola vez: los bindings se leen al arrancar y no cambian en
    caliente (D2 de la iniciativa 005).
    """
    registry = ModelRegistry.from_settings(settings)
    if registry.config.models:
        logger.info(
            "Modelos por rol: %s",
            ", ".join(f"{role}={model}" for role, model in registry.config.bindings.items())
            or "sin bindings",
        )
    return registry


def build_budget(settings: Settings, *, key_prefix: str | None = None) -> BudgetPort:
    """Contador de presupuesto por review (017 B2/B6).

    Con URL de Redis configurada se usa el adaptador atomico; sin ella (o sin Redis),
    el contador en memoria. El fix usa una clave distinta de la review (B11), asi que
    el prefijo es parametro.
    """
    from agent_review.adapters.budget_memory import InMemoryBudget
    from agent_review.adapters.budget_redis import KEY_PREFIX, RedisBudget

    total = settings.llm_budget_tokens_per_pr
    if not settings.redis_url.strip():
        return InMemoryBudget(total)
    return RedisBudget(
        url=settings.redis_url,
        total=total,
        key_prefix=key_prefix or KEY_PREFIX,
    )


def llm_roles(settings: Settings, registry: ModelRegistry) -> list[str]:
    """Roles LLM que de verdad van a revisar: los que tienen modelo bindeado."""
    return [role for role in LLM_REVIEW_ROLES if registry.port_for(role) is not None]


def build_reviewers(
    settings: Settings,
    *,
    tooling_source: ToolingOutputSource | None = None,
    registry: ModelRegistry | None = None,
    budget: BudgetPort | None = None,
) -> list[ReviewerPort]:
    """Personas a partir del perfil del repo y de la configuracion.

    Fase 1: la persona estatica arranca por defecto; las personas LLM son opt-in
    para no gastar tokens hasta validar el publish E2E. Cada persona LLM usa el
    modelo que su **rol** tenga bindeado en el registro: `correctness` y `security`
    pueden correr en modelos distintos. El artefacto de la herramienta estatica lo
    deposita el CI; el worker no ejecuta PHPStan (eso es el sandbox de Fase 3).

    Cada persona LLM recibe el presupuesto **compartido** y su tope blando: el total
    repartido con holgura (el doble del reparto justo) acota lo que una persona con
    prompt grande se comeria del comun (017 B10).
    """
    reviewers: list[ReviewerPort] = []
    profile = load_profile(settings)
    checks = profile.get("checks", {}) or {}
    if isinstance(checks, dict) and checks.get("typecheck"):
        source = tooling_source or FileToolingOutputSource(
            template=settings.tooling_output_template
        )
        reviewers.append(ToolingReviewer(source=source))

    registry = registry if registry is not None else build_registry(settings)
    roles = llm_roles(settings, registry)
    budget = budget if budget is not None else build_budget(settings)
    # B10: «el primero que llega» sobre el contador comun, con un tope blando por
    # persona de `2 * total / N`. Sin él, una persona con un prompt grande se lleva
    # todo el presupuesto y las demas no revisan.
    limit = 2 * settings.llm_budget_tokens_per_pr // len(roles) if roles else None
    for role in roles:
        llm = registry.port_for(role)
        if llm is None:
            continue
        reviewers.append(
            LlmReviewer(
                llm=llm,
                name=role,
                max_findings=settings.llm_max_findings,
                budget=budget,
                budget_limit=limit,
            )
        )

    if not reviewers:
        return [NullReviewer("null")]
    return reviewers


def build_chat(settings: Settings) -> ChatPort | None:
    """Canal de chat: egress por outbox (C10). Sin canal declarado, no hay canal.

    El worker no conoce el transporte: encola el aviso en `reviews:notices` y el
    bot de Teams lo publica. La outbox es **siempre** la de Redis, aunque Redis no
    responda al arrancar: el adaptador registra y descarta cada aviso que no puede
    encolar (asimetria aceptada, D6; el respaldo real es el CLI, C9), y en cuanto
    Redis vuelve los avisos fluyen. Una outbox en memoria aqui los perderia todos
    hasta reiniciar el worker, porque nadie la consume.
    """
    if not settings.teams_enabled:
        return None
    outbox = RedisChatOutbox(url=settings.redis_url, key=settings.teams_notices_key)
    if not outbox.available():
        logger.warning("Redis no disponible al arrancar; los avisos se perderan hasta que vuelva")
    return OutboxChatAdapter(outbox=outbox)


def build_progress(settings: Settings) -> ProgressPort:
    """Puerto de progreso del worker (019 W10): `RedisProgress` o `NullProgress`.

    Con `progress_enabled` el worker anota en Redis el nodo en curso de cada review;
    sin el (o sin Redis configurado), `NullProgress`: la envoltura de nodo queda en
    su sitio pero no toca nada. Es la misma elección por configuración que `build_chat`.
    """
    from agent_review.adapters.progress_null import NullProgress
    from agent_review.adapters.progress_redis import RedisProgress

    if not settings.progress_enabled or not settings.redis_url.strip():
        return NullProgress()
    return RedisProgress(
        url=settings.redis_url,
        ttl_s=settings.progress_ttl_s,
        key_prefix=settings.progress_key,
    )


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


def build_fixer(
    settings: Settings,
    *,
    registry: ModelRegistry | None = None,
    budget: BudgetPort | None = None,
) -> FixerPort | None:
    """Fixer LLM si el rol `fixer` tiene modelo. Sin el, el nodo `fix` marca
    `NOT_VERIFIABLE` (no se puede proponer parche) en vez de fingir un fix.

    Usa **su propio** binding: el modelo que propone el parche no tiene por que
    ser el que revisa el diff (uno puede querer `json_schema` estricto aqui). El
    presupuesto es el **del fix** (clave distinta de la review, 017 B11).
    """
    registry = registry if registry is not None else build_registry(settings)
    llm = registry.port_for("fixer")
    if llm is None:
        return None
    from agent_review.adapters.budget_memory import InMemoryBudget
    from agent_review.adapters.budget_redis import FIX_KEY_PREFIX

    budget = budget if budget is not None else build_budget(settings, key_prefix=FIX_KEY_PREFIX)
    if budget is None:
        budget = InMemoryBudget(settings.llm_budget_tokens_per_pr)
    return LlmFixer(
        llm=llm,
        budget=budget,
        max_fix_lines=settings.max_fix_lines,
    )


def build_delivery(settings: Settings) -> DeliveryFormatter:
    """Formato de entrega del parche verificado (E6). `diff` hasta que R1 confirme
    que Bitbucket aplica `suggestion`."""
    if settings.fix_delivery.strip().lower() == "suggestion":
        return SuggestionFormatter()
    return DiffFormatter()


def build_runtime(
    settings: Settings,
    *,
    vcs: VcsPort | None = None,
    chat: ChatPort | None = None,
    reviewers: list[ReviewerPort] | None = None,
    policy: PolicyEngine | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    fixer: FixerPort | None = None,
    progress: ProgressPort | None = None,
) -> Any:
    validate_tooling_template(settings)
    vcs = vcs or build_vcs(settings)
    # Sin URL de Teams no hay canal: no se construye un adaptador que no publica.
    if chat is None:
        chat = build_chat(settings)
    registry = build_registry(settings)
    # Un unico contador de review para todo el fan-out (017 B1/B10); su tope viaja
    # al grafo para poder derivar el saldo del `ReviewOutcome` sin consultar al
    # puerto durante el `replay`.
    review_budget = build_budget(settings)
    if reviewers is None:
        reviewers = build_reviewers(settings, registry=registry, budget=review_budget)
    validate_models(registry)
    policy = policy or PolicyEngine.from_file(
        settings.autonomy_config, cap_override=settings.max_autonomy_override
    )
    saver = checkpointer or build_checkpointer(
        settings.database_url, required=is_production(settings)
    )
    progress = progress if progress is not None else build_progress(settings)
    # La cancelacion (019 W4) viaja como el progreso: `build_graph` no conoce Redis,
    # asi que el composition root le pasa el chequeo ya construido. Sin Redis,
    # `build_cancellation` es `None` y la frontera no consulta nada (W2).
    cancellation = build_cancellation(settings)
    graph = build_graph(
        vcs=vcs,
        reviewers=reviewers,
        policy=policy,
        chat=chat,
        fixer=fixer if fixer is not None else build_fixer(settings, registry=registry),
        delivery=build_delivery(settings),
        fix_recipes=fix_recipes(settings),
        scope_ignore=settings.scope_ignore,
        retry_policy=node_retry_policy(settings),
        verification_enabled=verification_enabled(settings),
        budget_total=settings.llm_budget_tokens_per_pr,
        progress=progress,
        cancel=cancellation.is_cancelled if cancellation is not None else None,
    )
    return graph.compile(checkpointer=saver)


def verification_enabled(settings: Settings) -> bool:
    """¿Hay quien verifique un parche? `SANDBOX_ENABLED` y la URL del runner.

    Sin ellas el worker de fixes no podria resolver la wave: el fix se marca
    `NOT_VERIFIABLE` al despacharlo en vez de pausar la review hasta el TTL.
    """
    return settings.sandbox_enabled and bool(settings.sandbox_runner_url.strip())


def process_request(
    graph: ReviewGraph,
    request: ReviewRequest,
    *,
    paused: PausedStore | None = None,
    chat: ChatPort | None = None,
    vcs: VcsPort | None = None,
    fix_queue: FixQueue | None = None,
    fix_ttl_s: float | None = None,
) -> ReviewOutcome:
    """Ejecuta la review una vez. No reencola: eso es de `run_forever`.

    Publicar y reencolar no pueden convivir en la misma pasada: `graph.invoke`
    ya corre el nodo `publish`, asi que un reencolado tras una excepcion de
    Teams duplicaria los comentarios. El reintento se decide con la excepcion.

    El grafo se invoca con `thread_id` (= `review_key`): si el mensaje se
    reentrega, el checkpointer devuelve el estado ya terminado y no se vuelve a
    publicar. Tres estados del hilo (ver docs/iniciativas/archivo/002-fase2-hitl/spec.md §4.3):

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
            graph,
            request,
            config=config,
            paused_state=snapshot,
            paused=paused,
            chat=chat,
            vcs=vcs,
            fix_queue=fix_queue,
            fix_ttl_s=fix_ttl_s,
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
        _on_paused(
            result,
            outcome.request,
            paused=paused,
            chat=chat,
            vcs=vcs,
            fix_queue=fix_queue,
            fix_ttl_s=fix_ttl_s,
        )
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
    fix_queue: FixQueue | None = None,
    fix_ttl_s: float | None = None,
) -> ReviewOutcome:
    """Reanuda un hilo pausado, despachando por tipo de pausa (F17).

    ``human``: reanuda con la decision; sin ella, no repite el trabajo.
    ``fix``: reanuda con la wave resuelta por el worker de fixes; sin ella, no-op
    (la reentrega no relanza la verificacion).

    Sin carga, solo comprueba que la pausa siga en el inbox: si el registro se
    perdio (Redis caido al pausar, el job se reencolo), lo repone y, si es de fix,
    re-encola la wave. Sin esa entrada nadie la ve ni el TTL la vence nunca.
    """
    state = cast("ReviewState", paused_state.values)
    human, fix = build_pause(state)
    if fix is not None:
        return _resume_fix_pause(
            graph,
            request,
            config=config,
            paused_state=paused_state,
            state=state,
            fix=fix,
            paused=paused,
            chat=chat,
            vcs=vcs,
            fix_queue=fix_queue,
            fix_ttl_s=fix_ttl_s,
        )
    if request.decision is None:
        logger.info(
            "Review %s esta pausada esperando decision; la reentrega no la re-ejecuta",
            request.review_key,
        )
        _ensure_registered(paused_state, paused=paused, chat=chat, vcs=vcs)
        return outcome_from_state(state, paused=True)
    del human  # la carga humana ya viene validada en `request.decision`
    result = cast("ReviewState", graph.invoke(Command(resume=request.decision), config=config))
    logger.info(
        "Review %s reanudada con decision de %s", request.review_key, request.decision.actor
    )
    return _finish_resume(
        result, request, paused=paused, chat=chat, vcs=vcs, fix_queue=fix_queue, fix_ttl_s=fix_ttl_s
    )


def _resume_fix_pause(
    graph: ReviewGraph,
    request: ReviewRequest,
    *,
    config: RunnableConfig,
    paused_state: Any,
    state: ReviewState,
    fix: FixWaitRequest,
    paused: PausedStore | None,
    chat: ChatPort | None,
    vcs: VcsPort | None,
    fix_queue: FixQueue | None,
    fix_ttl_s: float | None = None,
) -> ReviewOutcome:
    """Reanuda una pausa de fix con la wave resuelta, o no-op si no la hay.

    Antes de invocar, comprueba que la wave pendiente del hilo contenga de verdad
    los `run_key` de la resolucion (R2): si el hilo ya avanzo (o la resolucion es
    de otra wave), no-op. Aplicar una resolucion que el hilo ya no espera
    corromperia el estado del fix.
    """
    if request.fix_resolution is None:
        logger.info(
            "Review %s esta pausada esperando la wave de fix; la reentrega no la re-ejecuta",
            request.review_key,
        )
        _ensure_registered(
            paused_state,
            paused=paused,
            chat=chat,
            vcs=vcs,
            fix_queue=fix_queue,
            fix_ttl_s=fix_ttl_s,
        )
        return outcome_from_state(state, paused=True)
    pending = {job.run_key for job in fix.jobs}
    if not _resolution_matches(pending, request.fix_resolution):
        logger.warning(
            "Resolucion de fix de %s no corresponde a la wave pendiente; no-op",
            request.review_key,
        )
        return outcome_from_state(state, paused=True)
    result = cast(
        "ReviewState", graph.invoke(Command(resume=request.fix_resolution), config=config)
    )
    resolved = len(request.fix_resolution.items)
    logger.info("Review %s reanudada con %d resolucion(es) de fix", request.review_key, resolved)
    return _finish_resume(
        result,
        request,
        paused=paused,
        chat=chat,
        vcs=vcs,
        fix_queue=fix_queue,
        fix_ttl_s=fix_ttl_s,
    )


def _resolution_matches(pending: set[str], resolutions: FixResolutions) -> bool:
    """¿La resolucion cubre la wave pendiente del hilo? (guarda contra R3).

    Sin `pending` (el estado no expone la wave, p. ej. un hilo ya resuelto) se
    acepta: la reanudacion la valida el propio `fix_wait` al aplicar. Con `pending`,
    debe haber al menos un `run_key` comun: una resolucion de otra wave no se aplica.
    """
    if not pending:
        return True
    return any(item.run_key in pending for item in resolutions.items)


def _finish_resume(
    result: ReviewState,
    request: ReviewRequest,
    *,
    paused: PausedStore | None,
    chat: ChatPort | None = None,
    vcs: VcsPort | None = None,
    fix_queue: FixQueue | None = None,
    fix_ttl_s: float | None = None,
) -> ReviewOutcome:
    """Cierra una reanudacion: registra la nueva pausa o limpia el inbox.

    Una reanudacion puede volver a pausar (un `hitl` con decision incompleta, o un
    `fix_wait` que pasa a esperar la wave): la pausa nueva hay que registrarla con
    su `kind` y, si es de fix, encolar su wave. Si ya no queda pausa, se limpia el
    inbox; el estado durable del hilo sigue en el checkpointer.
    """
    outcome = _log_outcome(result)
    if outcome.paused:
        _on_paused(
            result,
            request,
            paused=paused,
            chat=chat,
            vcs=vcs,
            fix_queue=fix_queue,
            fix_ttl_s=fix_ttl_s,
        )
    else:
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
    fix_queue: FixQueue | None = None,
    fix_ttl_s: float | None = None,
    paused_at: datetime | None = None,
) -> None:
    """Registra la pausa en el inbox y, si es de fix, encola la wave (F17).

    Una pausa humana avisa en Teams y espera decision; una de fix registra el
    `FixWaitRequest` **sin** aviso de decision y encola la wave para el worker de
    fixes. En ambos casos la review **no** ha publicado aun sus hallazgos aqui.

    El registro es idempotente por `review_key`: una repo reposicion de una pausa
    perdida no duplica la wave en la cola (`_needs_registration` filtra antes).
    """
    human, fix = build_pause(result)
    if human is None and fix is None:
        # No deberia ocurrir: se pauso con pendientes. Se registra para no quedar mudo.
        logger.warning("Review %s pausada sin carga reconocible en el estado", request.review_key)
        return
    current = paused.get(request.review_key) if paused is not None else None
    if _superseded(current, request):
        # La pasada completa ya esta pausada: manda ella. La degradada queda
        # superada y `reconcile_paused` borra su hilo.
        logger.info(
            "Pausa degradada de %s superada por la pasada completa; no se registra",
            request.review_key,
        )
        return
    new_kind = PauseKind.FIX if fix is not None else PauseKind.HUMAN
    new_pause = fix if fix is not None else human
    # Redundante: misma pausa ya registrada (p. ej. una decision incompleta que
    # vuelve a pausar con los mismos pendientes). No se re-registra ni se re-avisa,
    # para no resetear el `created_at` (TTL) ni duplicar el aviso.
    redundant = (
        current is not None
        and current.request.thread_id == request.thread_id
        and current.kind is new_kind
        and current.approval == new_pause
    )
    if paused is not None and not redundant:
        assert new_pause is not None
        paused.pause(
            request,
            new_pause,
            kind=new_kind,
            created_at=paused_at,
            ttl_s=fix_ttl_s if fix is not None else None,
        )
    if fix is not None:
        if not redundant:
            _enqueue_wave(fix_queue, request, fix)
            logger.info(
                "Review %s pausada con %d verificacion(es) de fix en la cola",
                request.review_key,
                len(fix.jobs),
            )
        return
    assert human is not None  # exclusivo con `fix`
    if redundant:
        return
    if chat is not None:
        # El aviso es cosmético: un fallo del canal (o de la outbox) no puede
        # tumbar una pausa ya registrada en el inbox y el checkpoint.
        try:
            chat.notify_decision_pending(
                repo=request.repo,
                pr_id=request.pr_id,
                review_key=request.review_key,
                approval=_with_code_links(human, vcs=vcs),
                link=_pr_url(vcs, request),
            )
        except Exception as exc:
            logger.exception("No se pudo avisar la pausa de %s", request.review_key)
            if is_transient(exc):
                # Un fallo transitorio que sobrevive al adaptador (p. ej. Redis
                # caido en un cambio futuro) se reintenta con el job, como `notify`.
                raise
    if paused is None:
        logger.info(
            "Review %s pausada con %d pendiente(s) (sin inbox configurado)",
            request.review_key,
            len(human.pending),
        )


def _enqueue_wave(fix_queue: FixQueue | None, request: ReviewRequest, wave: FixWaitRequest) -> None:
    """Encola la wave para el worker de fixes. Sin cola, se registra y nada mas."""
    if fix_queue is None:
        logger.warning(
            "Review %s pausada de fix sin cola de fixes configurada; nadie la verificara",
            request.review_key,
        )
        return
    fix_queue.enqueue(FixJob(review_key=request.review_key, request=request, wave=wave))


def _ensure_registered(
    snapshot: Any,
    *,
    paused: PausedStore | None,
    chat: ChatPort | None,
    vcs: VcsPort | None,
    fix_queue: FixQueue | None = None,
    fix_ttl_s: float | None = None,
) -> bool:
    """Repone en el inbox un hilo pausado que no esta en el; `True` si lo repuso.

    El hilo es la verdad durable y el inbox solo su indice. `paused_at` sale del
    checkpoint para que el TTL cuente desde la pausa real, no desde que se repone.
    Reponer una pausa de fix re-encola su wave (idempotente: el worker de fixes la
    salta si su `run_key` ya esta en el set `done`).
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
        fix_queue=fix_queue,
        fix_ttl_s=fix_ttl_s,
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
    fix_queue: FixQueue | None = None,
    fix_ttl_s: float | None = None,
    thread_ids: list[str] | None = None,
) -> list[str]:
    """Repone en el inbox los hilos pausados que el checkpointer conoce y el inbox no.

    Cubre la caida del worker entre el checkpoint del `interrupt` y `_on_paused`:
    el job ya salio de la cola (`BLPOP`), asi que ninguna reentrega lo arreglaria.
    Reponer una pausa de fix re-encola su wave: el worker de fixes la salta si su
    `run_key` ya esta en el set `done`, asi que la repo reposicion es idempotente.

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
            if _ensure_registered(
                snapshot,
                paused=paused,
                chat=chat,
                vcs=vcs,
                fix_queue=fix_queue,
                fix_ttl_s=fix_ttl_s,
            ):
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


def _with_code_links(approval: ApprovalRequest, *, vcs: VcsPort | None) -> ApprovalRequest:
    """Anade a cada pendiente el permalink a su trozo de codigo.

    El canal solo pinta lo que recibe: quien conoce el formato de permalink del
    host es el worker. Nunca aborta la pausa: un fallo del VCS (o un doble de test
    que no implemente `file_url`) deja el pendiente sin enlace y se sigue.
    """
    resolvers = getattr(vcs, "file_url", None) if vcs is not None else None
    if resolvers is None:
        return approval
    enriched: list[PendingDecision] = []
    for item in approval.pending:
        url = None
        try:
            url = resolvers(approval.repo, item.file, ref=approval.head_sha, line=item.line)
        except Exception:
            logger.exception(
                "No se pudo resolver el enlace al codigo de %s:%s", item.file, item.line
            )
        enriched.append(item.model_copy(update={"code_url": url}) if url else item)
    return approval.model_copy(update={"pending": enriched})


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


def sweep_fix_expired(
    paused: PausedStore,
    queue: ReviewQueue,
    *,
    now: datetime | None = None,
) -> list[ReviewRequest]:
    """Reencola una reanudacion `error` para cada pausa de fix vencida (F20, R4).

    Si el worker de fixes esta apagado o el runner nunca responde, la wave no se
    resuelve. Al vencer `FIX_VERIFY_TTL_S` (el `ttl_s` de la propia entrada) se
    encola una reanudacion con `outcome="error"` por cada `VerifyJob`: el fix queda
    `NOT_VERIFIABLE` y publicado, nunca colgado. No borra el inbox: lo limpia el
    resume. Idempotente como el barrido del HITL (`mark_expired_enqueued`).
    """
    enqueued: list[ReviewRequest] = []
    for review in paused.sweep(now=now):
        if review.kind is not PauseKind.FIX:
            continue
        items = [
            FixResolution(
                run_key=job.run_key,
                outcome="error",
                error="la wave vencio sin resolverse (worker de fixes o runner caido)",
            )
            for job in review.fix_request.jobs
        ]
        request = review.request.model_copy(update={"fix_resolution": FixResolutions(items=items)})
        queue.enqueue(request)
        paused.mark_expired_enqueued(review.review_key, now=now)
        enqueued.append(request)
        logger.warning(
            "Pausa de fix de %s vencida; se reanuda como no verificable (%d job(s))",
            review.review_key,
            len(items),
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
    fix_queue: FixQueue | None = None,
    fix_ttl_s: float | None = None,
    progress: ProgressPort | None = None,
    cancel: CancellationPort | None = None,
) -> None:
    """Procesa un job; ante un fallo transitorio reencola, y al agotar intentos descarta.

    La marca de progreso se limpia al terminar la pasada (OP8): completada o pausada,
    no queda ningún nodo en curso y el hash de la review debe quedar vacío. El
    `finally` cubre también el fallo: la envoltura de nodo ya cierra su propio campo,
    esto es la garantía de que la review no deja una marca colgando.

    Una `Cancelled` (019 OP28) no reencola: borra el hilo del checkpointer, limpia el
    inbox de pausas y, si `cancel` está, retira la marca.
    """
    try:
        process_request(
            graph,
            request,
            paused=paused,
            chat=chat,
            vcs=vcs,
            fix_queue=fix_queue,
            fix_ttl_s=fix_ttl_s,
        )
        # Frontera de nodo (OP27) o comando encolado para una pausa (OP29): con la
        # marca o el marcador, la review se aborta sin publicar.
        if request.cancelled or (cancel is not None and cancel.is_cancelled(request.review_key)):
            _on_cancelled(graph, request, paused=paused, cancel=cancel)
            return
    except Cancelled:
        # Cancelacion pedida por una persona (019 OP28, D5): no se reencola ni va a
        # la DLQ. El hilo se **borra** para que ninguna reentrega reanude un trabajo
        # que se ha dejado de querer; limpiar el inbox evita la pausa huerfana.
        _on_cancelled(graph, request, paused=paused, cancel=cancel)
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
    finally:
        clear_progress(progress, request.review_key)


def _on_cancelled(
    graph: ReviewGraph,
    request: ReviewRequest,
    *,
    paused: PausedStore | None = None,
    cancel: CancellationPort | None = None,
) -> None:
    """Cierra una cancelacion: borra el hilo, limpia el inbox y no reencola (D5/OP28).

    **Borrar el hilo es la guarda durable**: si se dejara, un hilo abortado con
    `next` no vacio se reanudaria en la siguiente reentrega (`process_request`
    continuaria desde el ultimo checkpoint) y la cancelacion no serviria de nada.
    Es el mismo borrado que hace `reconcile_paused` con los hilos `:partial`
    superados. Limpiar el inbox evita la pausa huerfana inmortal (familia de D15).

    Nada de esto puede tumbar al worker: el job ya salio de la cola y el bucle vive.
    """
    saver = getattr(graph, "checkpointer", None)
    thread_id = request.thread_id
    if saver is not None:
        try:
            saver.delete_thread(thread_id)
        except Exception:
            logger.exception("No se pudo borrar el hilo cancelado %s", thread_id)
    _clear_paused(paused, request)
    if cancel is not None:
        cancel.clear(request.review_key)
    logger.info("Review %s cancelada; hilo borrado y fuera del inbox", request.review_key)


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
    fix_queue: FixQueue | None = None,
    poll_s: float = 5.0,
    sweep_s: float = 30.0,
    prune_s: float = 3600.0,
) -> None:
    settings = settings or get_settings()
    require_production_secrets(settings)
    logging.basicConfig(level=settings.log_level)
    queue = queue or build_queue(settings)
    fix_queue = fix_queue if fix_queue is not None else build_fix_queue(settings)
    chat = build_chat(settings)
    # El progreso se construye aparte para poder limpiarlo al cerrar cada pasada
    # (OP8) y para que `build_runtime` reciba el mismo objeto.
    progress = build_progress(settings)
    # Cancelacion (019 OP28): el mismo adaptador que instala `build_runtime` en el
    # *seam* de nodo, aqui para retirar la marca al abortar.
    cancellation = build_cancellation(settings)
    # El worker guarda el VCS para el enlace del aviso de pausa. Con un grafo
    # inyectado (tests) no lo hay y el aviso sale sin enlace.
    vcs: VcsPort | None = None
    if graph is None:
        vcs = build_vcs(settings)
        graph = build_runtime(settings, chat=chat, vcs=vcs, progress=progress)
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
                fix_queue=fix_queue,
                fix_ttl_s=settings.fix_verify_ttl_s,
                progress=progress,
                cancel=cancellation,
            )
            continue
        # La cola esta vacia: es el momento de aplicar los TTL (ingesta y decision).
        now = time.monotonic()
        if now - last_sweep >= sweep_s:
            sweep_pending(settings, queue, pending=pending_store)
            sweep_expired(paused_store, queue)
            sweep_fix_expired(paused_store, queue)
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
                    fix_queue=fix_queue,
                    fix_ttl_s=settings.fix_verify_ttl_s,
                    thread_ids=paused_thread_ids(saver, latest=latest),
                )
                # Dos estrategias sobre el mismo listado: los completados antiguos
                # (politica por defecto) y los `fix_wait` vencidos (F21). Los
                # `human` que sobreviven a la primera quedan intocables en ambas.
                prune_completed_threads(
                    saver,
                    older_than_days=settings.checkpoint_retention_days,
                    paused_threads=[review.review_key for review in paused_store.all()],
                    latest=latest,
                )
                prune_fix_threads(
                    saver,
                    max_age_s=settings.fix_wait_max_age_s,
                    paused_kinds=paused_thread_kinds(saver, latest=latest),
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
