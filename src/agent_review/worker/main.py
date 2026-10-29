from __future__ import annotations

import fcntl
import json
import logging
import threading
import time
import traceback
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, RetryPolicy

from agent_review.adapters.agent_mode import AgentModeRejected, adapt_agent_mode
from agent_review.adapters.claude_artifact import ClaudeArtifactReviewer, ClaudeArtifactStore
from agent_review.adapters.claude_client import HttpClaudeRunner
from agent_review.adapters.context_factory import build_reader_factory
from agent_review.adapters.github_vcs import GithubVcsAdapter
from agent_review.adapters.llm_fixer import LlmFixer
from agent_review.adapters.llm_reviewer import LlmReviewer
from agent_review.adapters.llm_verifier import LlmVerifier
from agent_review.adapters.null_reviewer import NullReviewer
from agent_review.adapters.tool_loop import ReviewToolsOptions
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
from agent_review.domain.history import ReviewSummary
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
    SkipReason,
)
from agent_review.domain.paused import PausedReview, PauseKind
from agent_review.domain.pending import InMemoryPendingStore, PendingStatus
from agent_review.errors import Cancelled, PermanentError, is_transient
from agent_review.fix.delivery import DeliveryFormatter, DiffFormatter, SuggestionFormatter
from agent_review.graph.build import (
    ReviewState,
    build_claude_graph,
    build_graph,
    build_pause,
    outcome_from_state,
)
from agent_review.history_store import ReviewHistory
from agent_review.models_registry import ModelRegistry
from agent_review.paused_store import InMemoryPausedStore, PausedStore, RedisPausedStore
from agent_review.pending_store import PendingStore, RedisPendingStore
from agent_review.policy.engine import PolicyEngine
from agent_review.ports.budget import BudgetPort
from agent_review.ports.chat import ChatPort
from agent_review.ports.claude import (
    ClaudeCeiling,
    ClaudeInvalidOutput,
    ClaudeQuotaExceeded,
    ClaudeRunner,
)
from agent_review.ports.context import ContextLimits, ContextReaderPort
from agent_review.ports.fixer import FixerPort
from agent_review.ports.progress import ProgressPort
from agent_review.ports.progress import clear as clear_progress
from agent_review.ports.progress import finish_node as finish_progress
from agent_review.ports.progress import start_node as start_progress
from agent_review.ports.reasoning import ReasoningSink
from agent_review.ports.reviewer import ReviewerPort
from agent_review.ports.tooling import ToolingOutputSource
from agent_review.ports.vcs import VcsPort
from agent_review.ports.verifier import VerifierPort
from agent_review.profile import (
    context_denylist,
    context_doc_paths,
    context_docs,
    expected_tools,
    fix_recipes,
    load_profile,
    scope_classes,
)
from agent_review.queue import FixQueue, InMemoryReviewQueue, RedisReviewQueue, ReviewQueue
from agent_review.settings import (
    Settings,
    get_settings,
    is_missing,
    is_production,
    require_production_secrets,
)
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


def build_budget(
    settings: Settings,
    *,
    key_prefix: str | None = None,
    client: Any = None,
) -> BudgetPort:
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
        client=client,
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
    vcs: VcsPort | None = None,
) -> list[ReviewerPort]:
    """Personas a partir del perfil del repo y de la configuracion.

    Fase 1: la persona estatica arranca por defecto; las personas LLM son opt-in
    para no gastar tokens hasta validar el publish E2E. Cada persona LLM usa el
    modelo que su **rol** tenga bindeado en el registro: `correctness` y `security`
    pueden correr en modelos distintos. Si el CI dejó la salida de la herramienta
    estática, entra en la review. Si no está, esa persona no participa: empezar
    no la exige. El worker no ejecuta PHPStan.

    Cada persona LLM recibe el presupuesto **compartido** y su tope blando: el total
    repartido con holgura (el doble del reparto justo) acota lo que una persona con
    prompt grande se comeria del comun (017 B10).

    Roles en `REVIEW_TOOLS_ROLES` (014) reciben `reader_factory` y topes del bucle;
    el resto sigue con una llamada por lote (008).
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
    reasoning_sink = build_reasoning_sink(settings) if roles else None
    tools_roles = frozenset(settings.review_tools_roles_list)
    tools_vcs = (vcs or build_vcs(settings)) if tools_roles else None
    denylist = context_denylist(profile) if tools_roles else ()
    tool_limits = (
        ContextLimits(
            max_turns=settings.review_tools_max_turns,
            max_calls=settings.review_tools_max_calls,
            max_bytes=settings.review_tools_max_bytes,
            calls_per_turn=settings.review_tools_calls_per_turn,
        )
        if tools_roles
        else None
    )
    tool_options = ReviewToolsOptions(
        grep_mode=settings.review_tools_grep_mode,
        read_outline=settings.review_tools_read_outline,
        read_state=settings.review_tools_read_state,
        truncation_notice=settings.review_tools_truncation_notice,
        strict_calls=settings.review_tools_strict_calls,
        io_cache=settings.review_tools_io_cache,
        precache_context=settings.review_tools_precache_context,
    )
    for role in roles:
        llm = registry.port_for(role)
        if llm is None:
            continue
        reader_factory = None
        limits = None
        if role in tools_roles and tools_vcs is not None and tool_limits is not None:
            # Cierra sobre settings/vcs/denylist; el diff llega en review() (014 RH8).
            def open_reader(
                repo: str,
                head_sha: str,
                base_sha: str,
                diff: str,
                *,
                _vcs: VcsPort = tools_vcs,
                _denylist: tuple[str, ...] = denylist,
            ) -> ContextReaderPort:
                return build_reader_factory(settings, vcs=_vcs, diff=diff, denylist=_denylist)(
                    repo, head_sha, base_sha
                )

            reader_factory = open_reader
            limits = tool_limits
        reviewers.append(
            LlmReviewer(
                llm=llm,
                name=role,
                max_findings=settings.llm_max_findings,
                budget=budget,
                budget_limit=limit,
                batch_max_chars=settings.llm_batch_max_chars,
                context_max_chars=settings.llm_context_max_chars,
                reasoning_sink=reasoning_sink,
                batch_retries=settings.llm_batch_retries,
                reader_factory=reader_factory,
                tool_limits=limits,
                tool_options=tool_options,
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


def build_reasoning_sink(settings: Settings) -> ReasoningSink | None:
    """Destino del razonamiento de los revisores (021 R4), o `None` si no se pide.

    Sin `reasoning_stream_enabled` (o sin Redis) no hay sink: el revisor no escucha
    al modelo y la review es la de siempre (R7).
    """
    if not settings.reasoning_stream_enabled or not settings.redis_url.strip():
        return None
    from agent_review.adapters.reasoning_redis import RedisReasoningSink

    return RedisReasoningSink(
        url=settings.redis_url,
        ttl_s=settings.reasoning_ttl_s,
        max_bytes=settings.reasoning_max_bytes,
    )


def build_history(settings: Settings) -> ReviewHistory | None:
    """Histórico de reviews terminadas para el panel (021 R9), o `None` sin Redis."""
    if not settings.redis_url.strip():
        return None
    from agent_review.history_store import RedisReviewHistory

    return RedisReviewHistory(
        url=settings.redis_url, retention_days=settings.checkpoint_retention_days
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
        if is_missing(settings.github_token):
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


def build_verifier(
    settings: Settings,
    *,
    registry: ModelRegistry | None = None,
    vcs: VcsPort | None = None,
    budget: BudgetPort | None = None,
) -> VerifierPort | None:
    """Verificador adversarial L2 si el rol `verifier` tiene modelo (009 VF8).

    Sin binding el nodo `verify` es paso directo. Con el, abre un lector de
    contexto por review (`build_reader_factory`, 016) y comparte el presupuesto
    de la review (no el del fix). Los topes del bucle salen de Settings (VF7).
    """
    registry = registry if registry is not None else build_registry(settings)
    llm = registry.port_for("verifier")
    if llm is None:
        return None
    vcs = vcs or build_vcs(settings)
    denylist = context_denylist(load_profile(settings))

    def open_reader(repo: str, head_sha: str, base_sha: str, diff: str) -> ContextReaderPort:
        return build_reader_factory(settings, vcs=vcs, diff=diff, denylist=denylist)(
            repo, head_sha, base_sha
        )

    return LlmVerifier(
        llm,
        open_reader=open_reader,
        limits=ContextLimits(
            max_calls=settings.verifier_max_tool_calls,
            max_bytes=settings.verifier_max_context_bytes,
        ),
        budget=budget,
    )


def build_delivery(settings: Settings) -> DeliveryFormatter:
    """Formato de entrega del parche verificado (E6). `diff` hasta que R1 confirme
    que Bitbucket aplica `suggestion`."""
    if settings.fix_delivery.strip().lower() == "suggestion":
        return SuggestionFormatter()
    return DiffFormatter()


# Distingue «no me pasaron la cancelación» de «este proceso no tiene».
_MISSING: Any = object()


def build_runtime(
    settings: Settings,
    *,
    vcs: VcsPort | None = None,
    chat: ChatPort | None = None,
    reviewers: list[ReviewerPort] | None = None,
    policy: PolicyEngine | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    fixer: FixerPort | None = None,
    verifier: VerifierPort | None = None,
    progress: ProgressPort | None = None,
    registry: ModelRegistry | None = None,
    cancellation: Any = _MISSING,
    budget_client: Any = None,
    probe_models: bool = True,
) -> Any:
    if settings.review_engine == "claude":
        return _build_claude_runtime(
            settings,
            vcs=vcs,
            chat=chat,
            checkpointer=checkpointer,
            progress=progress,
        )
    validate_tooling_template(settings)
    vcs = vcs or build_vcs(settings)
    # Sin URL de Teams no hay canal: no se construye un adaptador que no publica.
    if chat is None:
        chat = build_chat(settings)
    registry = registry if registry is not None else build_registry(settings)
    # Un unico contador de review para todo el fan-out (017 B1/B10); su tope viaja
    # al grafo para poder derivar el saldo del `ReviewOutcome` sin consultar al
    # puerto durante el `replay`. L2 (009) comparte el mismo contador.
    review_budget = build_budget(settings, client=budget_client)
    if reviewers is None:
        reviewers = build_reviewers(settings, registry=registry, budget=review_budget, vcs=vcs)
    if probe_models:
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
    # Un montaje por review reutiliza la del proceso (`cancellation` ya resuelto).
    if cancellation is _MISSING:
        cancellation = build_cancellation(settings)
    if verifier is None:
        verifier = build_verifier(settings, registry=registry, vcs=vcs, budget=review_budget)
    graph = build_graph(
        vcs=vcs,
        reviewers=reviewers,
        policy=policy,
        chat=chat,
        fixer=fixer if fixer is not None else build_fixer(settings, registry=registry),
        verifier=verifier,
        delivery=build_delivery(settings),
        fix_recipes=fix_recipes(settings),
        scope_ignore=settings.scope_ignore,
        scope_classes=scope_classes(load_profile(settings)),
        context_doc_paths=context_doc_paths(context_docs(load_profile(settings))),
        retry_policy=node_retry_policy(settings),
        verification_enabled=verification_enabled(settings),
        budget_total=settings.llm_budget_tokens_per_pr,
        progress=progress,
        cancel=cancellation.is_cancelled if cancellation is not None else None,
        verifier_max_findings=settings.verifier_max_findings,
        verifier_require_verified_for=settings.verifier_require_verified_for_list,
    )
    return graph.compile(checkpointer=saver)


def _build_claude_runtime(
    settings: Settings,
    *,
    vcs: VcsPort | None,
    chat: ChatPort | None,
    checkpointer: BaseCheckpointSaver[Any] | None,
    progress: ProgressPort | None,
) -> Any:
    """Compila el grafo claude. No construye personas LLM, verificador ni politica.

    `reviewers`, `fixer` y `verifier` de `build_runtime` no aplican: un doble
    inyectado por esa via no se llama. No entra la persona estática: phpstan y
    el resto de artefactos del CI son del motor `llm`. Aquí revisa el CLI.
    """
    validate_tooling_template(settings)
    vcs = vcs or build_vcs(settings)
    if chat is None:
        chat = build_chat(settings)
    saver = checkpointer or build_checkpointer(
        settings.database_url, required=is_production(settings)
    )
    progress = progress if progress is not None else build_progress(settings)
    cancellation = build_cancellation(settings)
    profile = load_profile(settings)
    reviewers: list[ReviewerPort] = [
        ClaudeArtifactReviewer(ClaudeArtifactStore(root=settings.claude_artifact_root))
    ]
    graph = build_claude_graph(
        vcs=vcs,
        reviewers=reviewers,
        chat=chat,
        scope_ignore=settings.scope_ignore,
        scope_classes=scope_classes(profile),
        retry_policy=node_retry_policy(settings),
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


def build_claude_runner(settings: Settings) -> HttpClaudeRunner:
    """Cliente del servicio. El worker no lanza el binario."""
    url = settings.claude_runner_url.strip()
    if not url:
        raise PermanentError("REVIEW_ENGINE=claude necesita CLAUDE_RUNNER_URL")
    return HttpClaudeRunner(
        url=url,
        token=settings.claude_runner_token,
        timeout_s=settings.claude_timeout_s,
    )


def ensure_claude_artifact(
    request: ReviewRequest,
    *,
    runner: ClaudeRunner,
    store: ClaudeArtifactStore,
    note: Callable[[str], None] | None = None,
    progress: ProgressPort | None = None,
    max_turns: int | None = None,
    timeout_s: float | None = None,
) -> None:
    """Una llamada al CLI por `head_sha`, antes del grafo.

    Artefacto ya válido: no se vuelve a llamar. Stdout inválido o proceso
    caído: no se guarda y se relanza para que el job se reintente. El cupo
    no se reintenta. El grafo no se arranca aquí.
    """
    path = store.path_for(repo=request.repo, pr_id=request.pr_id, head_sha=request.head_sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Dos workers no lanzan el CLI a la vez por el mismo `head_sha`.
    with path.with_suffix(".lock").open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        loaded = store.load(repo=request.repo, pr_id=request.pr_id, head_sha=request.head_sha)
        short = request.head_sha[:12]
        if loaded is not None:
            _say(note, f"Claude: artefacto ya válido para {short}. No se llama al CLI.")
            return
        _say(note, f"Claude: empieza el CLI para {short}.")
        # El grafo aún no existe: sin esta marca la review sale de la cola y
        # no entra en «En vuelo» hasta el primer nodo.
        start_progress(progress, request.review_key, "claude")
        started = time.monotonic()
        try:
            stdout = runner.run(
                repo=request.repo,
                pr_id=request.pr_id,
                head_sha=request.head_sha,
                base_sha=request.base_sha,
                max_turns=max_turns,
                timeout_s=timeout_s,
            )
        finally:
            finish_progress(progress, request.review_key, "claude")
        elapsed = time.monotonic() - started
        _say(note, f"Claude: el CLI terminó en {elapsed:.0f}s.")
        try:
            adapted = adapt_agent_mode(stdout)
        except AgentModeRejected as exc:
            if exc.discarded:
                _say(note, f"Claude: se descartaron {exc.discarded} hallazgos.")
            raise ClaudeInvalidOutput(
                f"stdout de claude no es una review terminada para {request.review_key}"
            ) from exc
        if adapted.discarded:
            _say(note, f"Claude: se descartaron {adapted.discarded} hallazgos.")
        body = json.dumps(
            [item.model_dump(mode="json") for item in adapted.findings],
            ensure_ascii=False,
        )
        if not store.save(body, repo=request.repo, pr_id=request.pr_id, head_sha=request.head_sha):
            raise ClaudeInvalidOutput(
                f"stdout de claude no es una lista de hallazgos para {request.review_key}"
            )


def process_request(
    graph: ReviewGraph,
    request: ReviewRequest,
    *,
    paused: PausedStore | None = None,
    chat: ChatPort | None = None,
    vcs: VcsPort | None = None,
    fix_queue: FixQueue | None = None,
    fix_ttl_s: float | None = None,
    claude_runner: ClaudeRunner | None = None,
    claude_store: ClaudeArtifactStore | None = None,
    dial_snapshot: Mapping[str, object] | None = None,
    note: Callable[[str], None] | None = None,
    progress: ProgressPort | None = None,
    claude_max_turns: int | None = None,
    claude_timeout_s: float | None = None,
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
        finished = outcome_from_state(cast("ReviewState", snapshot.values))
        if _rerun_incomplete(finished, request):
            # Una review incompleta no es un cierre: el siguiente intento (otro
            # lanzamiento o un reintento) tiene que volver a ejecutar. Una completa
            # sigue reutilizando el hilo para no duplicar comentarios.
            logger.info(
                "Review %s termino incompleta; se borra el hilo y se vuelve a ejecutar",
                request.review_key,
            )
            saver = getattr(graph, "checkpointer", None)
            if saver is None:
                snapshot = None
            else:
                try:
                    saver.delete_thread(request.thread_id)
                except Exception:
                    logger.exception("No se pudo borrar el hilo incompleto %s", request.review_key)
                    return finished
                snapshot = None
        else:
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
            return finished
    if snapshot is not None:
        logger.info(
            "Review %s a medias en su hilo (siguiente: %s); se continua desde el checkpoint",
            request.review_key,
            ", ".join(snapshot.next),
        )
        if dial_snapshot is not None and request.repin_dials:
            update_state = getattr(graph, "update_state", None)
            if update_state is not None:
                update_state(config, {"dial_snapshot": dict(dial_snapshot)})
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
        if claude_runner is not None and claude_store is not None:
            ensure_claude_artifact(
                request,
                runner=claude_runner,
                store=claude_store,
                note=note,
                progress=progress,
                max_turns=claude_max_turns,
                timeout_s=claude_timeout_s,
            )
        state: ReviewState = {"request": request, "findings": []}
        if dial_snapshot is not None:
            state["dial_snapshot"] = dict(dial_snapshot)
        result = cast("ReviewState", graph.invoke(state, config=config))
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


def _rerun_incomplete(outcome: ReviewOutcome, request: ReviewRequest) -> bool:
    """Una review que no cubrió lo declarado se puede volver a lanzar.

    Una completa no: repetir la misma ejecución reutiliza el hilo. Una reanudación
    (decisión o fix) tampoco borra el hilo para empezar de cero.
    """
    if outcome.complete or outcome.paused:
        return False
    return request.decision is None and request.fix_resolution is None


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


def resolve_launch(
    request: ReviewRequest,
    vcs: VcsPort | None,
    *,
    expected_repo: str = "",
) -> ReviewRequest:
    """Lee el commit de un lanzamiento del panel antes de invocar el grafo.

    El hilo se ata a ese SHA. Si el reintento llega ya con SHA, no se vuelve a
    leer: un push nuevo no cambia la ejecucion que ya empezo. Un fallo del VCS
    sube y el job se reencola; un repo distinto del configurado no llega a llamar.
    """
    if not request.launch or request.head_sha:
        return request
    expected = expected_repo.strip()
    if expected and request.repo.casefold() != expected.casefold():
        raise PermanentError(
            f"Repo {request.repo!r} no admitido: este despliegue solo revisa {expected}"
        )
    if vcs is None:
        raise PermanentError(
            f"No hay VCS para resolver el commit de {request.repo}#{request.pr_id}"
        )
    info = vcs.get_pull_request(request.repo, request.pr_id)
    if not info.head_sha:
        raise PermanentError(f"El PR {request.repo}#{request.pr_id} no tiene commit")
    return request.model_copy(
        update={"head_sha": info.head_sha, "base_sha": request.base_sha or info.base_sha}
    )


def _with_base_sha(request: ReviewRequest, vcs: VcsPort | None) -> ReviewRequest:
    """Rellena `base_sha` desde el VCS si el webhook no lo mando (008 CX1).

    Best-effort: un fallo del VCS deja el request igual; L0 degradara a calcular
    `pre_existing` solo con el lado viejo del diff.
    """
    if request.base_sha or vcs is None:
        return request
    try:
        base_sha = vcs.get_pull_request(request.repo, request.pr_id).base_sha
    except Exception:
        logger.exception("No se pudo resolver base_sha del PR %s#%s", request.repo, request.pr_id)
        return request
    if not base_sha:
        return request
    return request.model_copy(update={"base_sha": base_sha})


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
        # La pausa de publicación no tiene arreglo que autorizar: el silencio
        # es `dropped`. `expired` seguiría escribiendo el resumen del motor llm.
        publication = review.human_request.pause_kind == "publication"
        outcome = DecisionOutcome.DROPPED if publication else DecisionOutcome.EXPIRED
        expired = HitlDecision(
            review_key=review.review_key,
            actor="system:ttl",
            items=[
                FindingDecision(finding_id=item.finding_id, outcome=outcome)
                for item in review.human_request.pending
            ],
        )
        request = review.request.model_copy(update={"decision": expired})
        queue.enqueue(request)
        paused.mark_expired_enqueued(review.review_key, now=now)
        enqueued.append(request)
        logger.warning(
            "Pausa de %s vencida; se reanuda como %s (%d hallazgo(s))",
            review.review_key,
            "sin publicar" if publication else "sin decision",
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
    counts = outcome.verification_counts
    verification_note = (
        f"; L2 confirmed={counts['confirmed']} refuted={counts['refuted']} "
        f"unproven={counts['unproven']}"
        if outcome.verifications
        else ""
    )
    if outcome.paused:
        logger.info(
            "Review PAUSADA %s; esperando decision humana%s",
            outcome.request.review_key,
            verification_note,
        )
    elif outcome.complete:
        logger.info(
            "Review completada %s%s",
            outcome.request.review_key,
            verification_note,
        )
    else:
        # No se hizo el analisis declarado: no es una review completada. No se
        # reencola: la ingesta diferida ya espero al artefacto (barrera/TTL), asi
        # que reintentar aqui solo duplicaria la publicacion.
        unread = (
            [
                f"{path}={reason.value}"
                for path, reason in outcome.coverage.skipped.items()
                if reason is not SkipReason.IGNORED
            ]
            if outcome.coverage is not None
            else []
        )
        logger.warning(
            "Review INCOMPLETA %s; sin cobertura de: %s%s",
            outcome.request.review_key,
            ", ".join(
                [
                    *(f"{n}={s.value}" for n, s in outcome.unavailable_sources.items()),
                    *unread,
                ]
            ),
            verification_note,
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


# Tras un cupo, los jobs siguientes no vuelven a llamar al CLI hasta que pase.
_QUOTA_COOLDOWN_S = 900.0

# Caducidad del aviso de parqueo. `session` se suelta al iniciar sesion y `quota`
# enfria los siguientes; `ceiling` no se desbloquea solo, asi que sin fecha el
# aviso de Teams no volveria a salir nunca aunque el operador suba el techo.
# Es generosa: pasada, un techo sigue parkeando el job, pero avisa otra vez.
_PARK_NOTICE_TTL_S = 86400.0

_PARK_NOTICES: dict[str, tuple[str, str]] = {
    "session": (
        "Claude sin sesion",
        "Las reviews de este motor esperan fuera de la cola. Inicia sesion en el panel.",
    ),
    "quota": (
        "Cupo de Claude agotado",
        "La review no se ha publicado. El cupo no se reintenta.",
    ),
    "ceiling": (
        "Claude llego al techo",
        "Se agotaron los turnos o el tiempo de pared. No se reintenta ni se publica. "
        "El techo se cambia en /config (sección Claude); este job hay que eliminarlo "
        "y lanzarlo de nuevo.",
    ),
}


def panel_claude_link(settings: Settings) -> str | None:
    """URL del formulario. Vacía si el despliegue no publica el panel."""
    base = settings.panel_public_url.strip().rstrip("/")
    if not base:
        return None
    return f"{base}/claude"


def claude_session_ready(queue: ReviewQueue, probe: Callable[[], bool] | None) -> bool:
    """True si este worker puede correr el CLI.

    Con sesión, suelta lo parqueado por falta de ella. El cupo y el techo se
    quedan: soltarlos volvería a gastar la misma pasada. Sin sonda (motor
    `llm`) no mira el parking.
    """
    if probe is None:
        return True
    try:
        active = probe()
    except Exception:
        logger.warning("No se pudo consultar la sesion de Claude; el job se parkea")
        return False
    if active:
        try:
            queue.release_parked(reason="session")
        except Exception:
            logger.exception("No se pudo soltar el parking de Claude")
    return active


def release_quota_wait(queue: ReviewQueue) -> list[ReviewRequest]:
    """Suelta `quota-wait` cuando el aviso de cupo ya caducó.

    El job que agotó el cupo (`quota`) se queda. Soltarlo volvería a gastar
    el límite.
    """
    try:
        if queue.notice_held("quota"):
            return []
        return queue.release_parked(reason="quota-wait")
    except Exception:
        logger.exception("No se pudo soltar la espera de cupo de Claude")
        return []


def park_claude_job(
    queue: ReviewQueue,
    request: ReviewRequest,
    *,
    reason: str,
    chat: ChatPort | None,
    link: str | None,
) -> None:
    """Mueve el job a `reviews:claude:parked` y, la primera vez, avisa."""
    queue.park(request, reason=reason)
    notice = _PARK_NOTICES.get(reason)
    if notice is None:
        return
    ttl = _QUOTA_COOLDOWN_S if reason == "quota" else _PARK_NOTICE_TTL_S
    if not queue.claim_notice(reason, ttl_s=ttl) or chat is None:
        return
    title, body = notice
    chat.notify(
        repo=request.repo,
        pr_id=request.pr_id,
        review_key=request.review_key,
        title=title,
        body=body,
        link=link,
    )


def dispatch_job(
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
    history: ReviewHistory | None = None,
    reasoning: bool = False,
    claude_runner: ClaudeRunner | None = None,
    claude_store: ClaudeArtifactStore | None = None,
    claude_session: Callable[[], bool] | None = None,
    park_link: str | None = None,
    expected_repo: str = "",
    dial_snapshot: Mapping[str, object] | None = None,
    claude_max_turns: int | None = None,
    claude_timeout_s: float | None = None,
) -> None:
    """Una vuelta del bucle. Sin sesión, el job sale de la cola y no hay `invoke`.

    Una reanudación (`decision` o `fix_resolution`) no necesita el CLI: parquearla
    por la sesión dejaría la decisión detrás de un login que no hace falta.
    """
    release_quota_wait(queue)
    ready = claude_session_ready(queue, claude_session)
    needs_cli = request.decision is None and request.fix_resolution is None
    if needs_cli and not ready:
        try:
            park_claude_job(queue, request, reason="session", chat=chat, link=park_link)
        except Exception:
            logger.exception("No se pudo parquear %s por falta de sesion", request.review_key)
        return
    _consume(
        queue,
        graph,
        request,
        max_attempts=max_attempts,
        paused=paused,
        chat=chat,
        vcs=vcs,
        fix_queue=fix_queue,
        fix_ttl_s=fix_ttl_s,
        progress=progress,
        cancel=cancel,
        history=history,
        reasoning=reasoning,
        claude_runner=claude_runner,
        claude_store=claude_store,
        park_link=park_link,
        expected_repo=expected_repo,
        dial_snapshot=dial_snapshot,
        claude_max_turns=claude_max_turns,
        claude_timeout_s=claude_timeout_s,
    )


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
    history: ReviewHistory | None = None,
    reasoning: bool = False,
    claude_runner: ClaudeRunner | None = None,
    claude_store: ClaudeArtifactStore | None = None,
    park_link: str | None = None,
    expected_repo: str = "",
    dial_snapshot: Mapping[str, object] | None = None,
    claude_max_turns: int | None = None,
    claude_timeout_s: float | None = None,
) -> None:
    """Procesa un job; ante un fallo transitorio reencola, y al agotar intentos descarta.

    La marca de progreso se limpia al terminar la pasada (OP8): completada o pausada,
    no queda ningún nodo en curso y el hash de la review debe quedar vacío. El
    `finally` cubre también el fallo: la envoltura de nodo ya cierra su propio campo,
    esto es la garantía de que la review no deja una marca colgando.

    Una `Cancelled` (019 OP28) no reencola: borra el hilo del checkpointer y limpia el
    inbox de pausas. Una review **ya cancelada** (marca puesta o comando encolado,
    OP29) se aborta antes de tocar el grafo: la marca sola no basta en una pausa,
    donde no hay frontera de nodo que la mire. La marca **no** se retira: es el
    *tombstone* que aborta aquí mismo cualquier job rezagado de la review (ver
    `worker.cancel`).
    """
    requeued = False
    pending_key = request.review_key
    try:
        # Un borrado no es una review: no se resuelve el commit ni se mira la
        # marca de cancelacion, que este mismo job retira (ADR-0034).
        fresh_launch = bool(request.launch and not request.head_sha)
        if request.forget:
            _forget_review(
                graph,
                request,
                queue=queue,
                paused=paused,
                cancel=cancel,
                history=history,
                claude_store=claude_store,
                progress=progress,
            )
            return
        # La cola aun muestra el job sin SHA. Cancelarlo marca esa clave; se
        # mira antes de llamar al VCS para no resolver un lanzamiento ya abortado.
        if request.cancelled or (cancel is not None and cancel.is_cancelled(pending_key)):
            _on_cancelled(graph, request, paused=paused)
            return
        request = resolve_launch(request, vcs, expected_repo=expected_repo)
        if fresh_launch:
            # El clic es posterior a cualquier «Eliminar» ya contado: adopta la
            # generacion vigente. Un reintento que ya trae SHA no entra aqui.
            request = request.model_copy(
                update={"generation": queue.generation(request.review_key)}
            )
        elif request.generation < queue.generation(request.review_key):
            logger.info(
                "Review %s obsoleta (generacion %d); no se ejecuta",
                request.review_key,
                request.generation,
            )
            return
        if cancel is not None and cancel.is_cancelled(request.review_key):
            _on_cancelled(graph, request, paused=paused)
            return
        if _quota_on_cooldown(
            queue, request, claude_runner=claude_runner, claude_store=claude_store
        ):
            try:
                park_claude_job(queue, request, reason="quota-wait", chat=chat, link=park_link)
            except Exception:
                logger.exception("No se pudo parquear la espera de cupo de %s", request.review_key)
            return
        request = _with_base_sha(request, vcs)
        _journal(queue, request.review_key, f"Intento {request.attempts + 1}")
        outcome = process_request(
            graph,
            request,
            paused=paused,
            chat=chat,
            vcs=vcs,
            fix_queue=fix_queue,
            fix_ttl_s=fix_ttl_s,
            claude_runner=claude_runner,
            claude_store=claude_store,
            dial_snapshot=dial_snapshot,
            note=lambda text: _journal(queue, request.review_key, text),
            progress=progress,
            claude_max_turns=claude_max_turns,
            claude_timeout_s=claude_timeout_s,
        )
        # Una review que ya no espera a nadie entra en el histórico del panel (021 R9).
        # Una reentrega del mismo job actualiza su entrada, no la duplica.
        if history is not None and not outcome.paused:
            history.record(
                ReviewSummary.from_outcome(
                    outcome, finished_at=datetime.now(UTC), reasoning=reasoning
                )
            )
        _journal(queue, request.review_key, _outcome_line(outcome))
    except ClaudeInvalidOutput as exc:
        logger.warning("Stdout de Claude invalido en %s; se reintenta", request.review_key)
        if request.attempts == 0 and chat is not None:
            try:
                chat.notify(
                    repo=request.repo,
                    pr_id=request.pr_id,
                    review_key=request.review_key,
                    title="Salida de Claude invalida",
                    body=(
                        "No cumple el contrato de hallazgos. Se reintentara. No se ha publicado."
                    ),
                )
            except Exception:
                logger.exception("No se pudo avisar la salida invalida de %s", request.review_key)
        log = _remember_failure(queue, request, exc)
        try:
            requeued = _requeue_or_drop(queue, request, max_attempts=max_attempts, error=log)
        except Exception:
            logger.exception(
                "No se pudo reencolar ni mandar a la DLQ %s; job perdido: %s",
                request.review_key,
                request.model_dump_json(),
            )
    except ClaudeQuotaExceeded:
        # No se reintenta: otra pasada volvería a gastar el cupo, y entrar al
        # grafo publicaría sin review. El login no lo suelta.
        logger.error("Cupo de Claude agotado en %s; se parkea", request.review_key)
        try:
            park_claude_job(queue, request, reason="quota", chat=chat, link=park_link)
        except Exception:
            logger.exception("No se pudo parquear el cupo de %s", request.review_key)
    except ClaudeCeiling:
        logger.error("Claude llego al techo en %s; se parkea", request.review_key)
        try:
            park_claude_job(queue, request, reason="ceiling", chat=chat, link=park_link)
        except Exception:
            logger.exception("No se pudo parquear el techo de %s", request.review_key)
    except Cancelled:
        # Cancelacion pedida por una persona (019 OP28, D5): no se reencola ni va a
        # la DLQ. El hilo se **borra** para que ninguna reentrega reanude un trabajo
        # que se ha dejado de querer; limpiar el inbox evita la pausa huerfana.
        _on_cancelled(graph, request, paused=paused)
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
        log = _remember_failure(queue, request, exc)
        requeued = _requeue_quietly(queue, request, max_attempts=max_attempts, error=log)
    finally:
        clear_progress(progress, request.review_key)
        if not requeued:
            _forget_failure(queue, pending_key)
            if request.review_key != pending_key:
                _forget_failure(queue, request.review_key)


def _quota_on_cooldown(
    queue: ReviewQueue,
    request: ReviewRequest,
    *,
    claude_runner: ClaudeRunner | None,
    claude_store: ClaudeArtifactStore | None,
) -> bool:
    """Un cupo reciente no debe relanzar el CLI en el job siguiente."""
    if claude_runner is None or claude_store is None:
        return False
    if request.decision is not None or request.fix_resolution is not None:
        return False
    try:
        if (
            claude_store.load(repo=request.repo, pr_id=request.pr_id, head_sha=request.head_sha)
            is not None
        ):
            return False
    except Exception:
        logger.exception("No se pudo leer el artefacto de Claude durante el enfriamiento")
        return False
    try:
        return queue.notice_held("quota")
    except Exception:
        logger.exception("No se pudo leer el enfriamiento del cupo de Claude")
        return False


def _forget_review(
    graph: ReviewGraph,
    request: ReviewRequest,
    *,
    queue: ReviewQueue,
    paused: PausedStore | None,
    cancel: CancellationPort | None,
    history: ReviewHistory | None,
    claude_store: ClaudeArtifactStore | None,
    progress: ProgressPort | None,
) -> None:
    """Deja la ejecucion como si no hubiera corrido. Los comentarios del PR siguen.

    El hilo (y el de la pasada degradada), el artefacto de Claude y el estado
    local desaparecen. Un job encolado antes del borrado lleva una generacion
    menor y `_consume` no lo ejecuta.
    """
    review_key = request.review_key
    saver = getattr(graph, "checkpointer", None)
    if saver is not None:
        for thread_id in (review_key, f"{review_key}:partial"):
            try:
                saver.delete_thread(thread_id)
            except Exception:
                logger.exception("No se pudo borrar el hilo %s", thread_id)
    if claude_store is not None and request.head_sha:
        try:
            claude_store.discard(repo=request.repo, pr_id=request.pr_id, head_sha=request.head_sha)
        except Exception:
            logger.exception("No se pudo borrar el artefacto de Claude de %s", review_key)
    try:
        queue.withdraw(review_key)
    except Exception:
        logger.exception("No se pudo retirar de la cola %s", review_key)
    if paused is not None:
        try:
            paused.resume(review_key)
        except Exception:
            logger.exception("No se pudo sacar del inbox %s", review_key)
    clear_mark = getattr(cancel, "clear", None)
    if callable(clear_mark):
        try:
            clear_mark(review_key)
        except Exception:
            logger.exception("No se pudo retirar la marca de cancelacion de %s", review_key)
    if history is not None:
        try:
            history.forget(review_key)
        except Exception:
            logger.exception("No se pudo quitar %s del historico", review_key)
    clear_progress(progress, review_key)
    logger.info(
        "Review %s eliminada; el siguiente lanzamiento de este commit empieza de cero",
        review_key,
    )


def _on_cancelled(
    graph: ReviewGraph,
    request: ReviewRequest,
    *,
    paused: PausedStore | None = None,
) -> None:
    """Cierra una cancelacion: borra el hilo, limpia el inbox y no reencola (D5/OP28).

    Borrar el hilo evita que una reentrega reanude el trabajo abortado desde el
    ultimo checkpoint; es el mismo borrado que hace `reconcile_paused` con los hilos
    `:partial` superados. Pero sin hilo, un job rezagado (la reanudacion del
    fix-worker, una decision encolada) arrancaria la review **desde cero**: por eso
    la marca de cancelacion se deja puesta hasta su TTL (`CANCEL_TTL_S`), y el
    chequeo previo de `_consume` aborta esos rezagados. Limpiar el inbox evita la
    pausa huerfana inmortal (familia de D15).

    Nada de esto puede tumbar al worker: el job ya salio de la cola y el bucle vive.
    """
    saver = getattr(graph, "checkpointer", None)
    thread_id = request.thread_id
    if saver is not None:
        try:
            saver.delete_thread(thread_id)
        except Exception:
            logger.exception("No se pudo borrar el hilo cancelado %s", thread_id)
    try:
        _clear_paused(paused, request)
    except Exception:
        logger.exception("No se pudo sacar del inbox la review cancelada %s", request.review_key)
    logger.info("Review %s cancelada; hilo borrado y fuera del inbox", request.review_key)


def _remember_failure(queue: ReviewQueue, request: ReviewRequest, exc: BaseException) -> str:
    """Acumula el traceback de este intento y devuelve el log que verá la DLQ."""
    text = _format_failure(request, exc)
    _journal(queue, request.review_key, text)
    try:
        queue.note_failure(request.review_key, text)
        logged = queue.failure_log(request.review_key)
    except Exception:
        logger.exception("No se pudo guardar el log de %s", request.review_key)
        return text
    return logged or text


def _say(note: Callable[[str], None] | None, text: str) -> None:
    logger.info("%s", text)
    if note is None:
        return
    try:
        note(text)
    except Exception:
        logger.exception("No se pudo anotar %s", text)


def _journal(queue: ReviewQueue, review_key: str, text: str) -> None:
    """Anota el log de la ficha. Un fallo de Redis no tumba el job."""
    line = text.strip()
    if not line:
        return
    record = getattr(queue, "append_journal", None)
    if record is None:
        return
    try:
        record(review_key, line)
    except Exception:
        logger.exception("No se pudo anotar el log de %s", review_key)


def _outcome_line(outcome: ReviewOutcome) -> str:
    count = len(outcome.findings)
    if outcome.paused:
        return (
            f"Pausa: {count} hallazgo(s) esperan una decisión antes de escribir en el pull request."
        )
    if outcome.complete and count == 0:
        return (
            "Terminó sin hallazgos. El motor claude no escribe un comentario vacío "
            "en el pull request."
        )
    if outcome.complete:
        return f"Terminó con {count} hallazgo(s)."
    unread = (
        [
            f"{path}={reason.value}"
            for path, reason in outcome.coverage.skipped.items()
            if reason is not SkipReason.IGNORED
        ]
        if outcome.coverage is not None
        else []
    )
    missing = [f"{name}={status.value}" for name, status in outcome.unavailable_sources.items()]
    detail = ", ".join([*missing, *unread]) or "sin detalle"
    return f"Review incompleta {outcome.request.review_key}; sin cobertura de: {detail}"


def _forget_failure(queue: ReviewQueue, review_key: str) -> None:
    try:
        queue.clear_failure(review_key)
    except Exception:
        logger.exception("No se pudo borrar el log de %s", review_key)


def _format_failure(request: ReviewRequest, exc: BaseException) -> str:
    trace = "".join(traceback.format_exception(exc)).strip()
    if len(trace) > 4000:
        trace = "…\n" + trace[-4000:]
    return f"intento {request.attempts + 1}\n{trace}"


def _requeue_or_drop(
    queue: ReviewQueue,
    request: ReviewRequest,
    *,
    max_attempts: int,
    keep_repin: bool = False,
    error: str = "",
) -> bool:
    """Reencola o descarta. True si el job sigue en la cola."""
    if request.attempts + 1 >= max_attempts:
        # Job descartado a proposito tras fallos persistentes: no desaparece en el
        # log, va a la DLQ para poder reinyectarlo o inspeccionarlo (B2). El log
        # de los intentos viaja en la entrada: el panel lo pinta al abrir el enlace.
        logger.error(
            "Review %s agoto los %d intentos; se manda a la DLQ",
            request.review_key,
            max_attempts,
        )
        queue.dead_letter(request, reason=f"intentos agotados ({max_attempts})", error=error)
        return False
    # `repin_dials` es la orden de un reintento del panel de releer lo publicado.
    # Conservarla en un reencolado interno re-fijaria la foto a mitad del hilo: el
    # job arrancaria con la revision N y el `update_state` la sellaria como N+1.
    # Solo se conserva (`keep_repin`) si el fallo fue antes de tocar el hilo: ahi
    # la foto nueva aun no se escribio y quitarla dejaria el reintento con la vieja.
    repin = request.repin_dials and keep_repin
    retry = request.model_copy(update={"attempts": request.attempts + 1, "repin_dials": repin})
    queue.enqueue(retry)
    logger.warning(
        "Review %s reencolada para el intento %d/%d",
        retry.review_key,
        retry.attempts + 1,
        max_attempts,
    )
    return True


def _checkpoint_values(saver: Any, thread_id: str) -> dict[str, object] | None:
    """Valores del hilo, o `None` si todavía no existe."""
    if saver is None:
        return None
    tup = saver.get_tuple({"configurable": {"thread_id": thread_id}})
    if tup is None:
        return None
    checkpoint = getattr(tup, "checkpoint", None)
    if not isinstance(checkpoint, dict):
        return {}
    values = checkpoint.get("channel_values")
    if not isinstance(values, dict):
        return {}
    return values


def _claude_ceilings(snapshot: Mapping[str, object] | None) -> tuple[int | None, float | None]:
    """Techos publicados en la foto de esta review. `None` deja el entorno del runner."""
    if not snapshot:
        return None, None
    overrides = snapshot.get("overrides")
    if not isinstance(overrides, dict):
        return None, None
    turns = overrides.get("claude_max_turns")
    timeout = overrides.get("claude_timeout_s")
    max_turns = turns if isinstance(turns, int) and not isinstance(turns, bool) else None
    timeout_s = (
        float(timeout)
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
        else None
    )
    return max_turns, timeout_s


def prepare_review(
    settings: Settings,
    store: Any,
    saver: Any,
    request: ReviewRequest,
) -> tuple[Settings, dict[str, object]]:
    """Settings con los que montar esta review, y la foto que viaja en el estado."""
    from agent_review.config_store import ConfigReadError, Publication
    from agent_review.panel_config import apply_document, pinned_snapshot

    try:
        published = store.read()
    except ConfigReadError:
        # CFG7: sin lectura y sin última buena, la review sigue con `Settings`.
        # Dejarlo mudo haria indistinguible «no se publicó nada» de «no pude leer».
        logger.warning(
            "Review %s: no se pudo leer la configuración publicada; monto con Settings",
            request.review_key,
        )
        published = Publication(0, {}, "", None)
    snapshot = pinned_snapshot(
        published_revision=published.revision,
        published=published.overrides,
        checkpoint=_checkpoint_values(saver, request.thread_id),
        repin=request.repin_dials,
    )
    overrides = snapshot.get("overrides")
    document = overrides if isinstance(overrides, dict) else {}
    return apply_document(settings, document), snapshot


def _requeue_quietly(
    queue: ReviewQueue,
    request: ReviewRequest,
    *,
    max_attempts: int,
    keep_repin: bool = False,
    error: str = "",
) -> bool:
    """Reencola o descarta sin poder tumbar el bucle. True si el job sigue en la cola."""
    try:
        return _requeue_or_drop(
            queue, request, max_attempts=max_attempts, keep_repin=keep_repin, error=error
        )
    except Exception:
        logger.exception(
            "No se pudo reencolar ni mandar a la DLQ %s; job perdido: %s",
            request.review_key,
            request.model_dump_json(),
        )
        return False


def _on_mount_failure(
    queue: ReviewQueue, request: ReviewRequest, exc: BaseException, *, settings: Settings
) -> None:
    """Un fallo al montar el grafo de una review no pierde el job.

    `prepare_review` lee el almacén y el checkpointer y `build_runtime` relee el
    perfil y el YAML: cualquiera puede fallar por la misma causa por la que
    fallaría la review. El job ya salió de la cola, así que el camino es el mismo
    que el de un fallo de trabajo: reencolar o, agotados los intentos, la DLQ.

    Montar no escribe en el hilo, así que un reintento del panel conserva
    `repin_dials`: el siguiente intento tiene que releer lo publicado.
    """
    if is_transient(exc):
        logger.warning(
            "Fallo transitorio montando la review %s (%s); se reencola",
            request.review_key,
            type(exc).__name__,
        )
    else:
        logger.exception("Fallo montando la review %s", request.review_key)
    log = _remember_failure(queue, request, exc)
    kept = _requeue_quietly(
        queue,
        request,
        max_attempts=settings.max_review_attempts,
        keep_repin=True,
        error=log,
    )
    if not kept:
        _forget_failure(queue, request.review_key)


def _mount_or_requeue(
    settings: Settings,
    store: Any,
    saver: Any,
    queue: ReviewQueue,
    request: ReviewRequest,
    *,
    vcs: VcsPort | None,
    chat: ChatPort | None,
    progress: ProgressPort | None,
    registry: ModelRegistry | None,
    cancellation: Any,
    budget_client: Any,
) -> tuple[ReviewGraph, dict[str, object] | None, tuple[int | None, float | None]] | None:
    """Grafo y foto de esta review, o `None` si no se pudo montar y ya se reencoló.

    Montar (leer la foto, releer perfil y YAML, compilar) puede fallar igual que
    el trabajo. El job ya salió de la cola, así que un fallo pasa por el mismo
    camino de error y el bucle sigue con el siguiente.
    """
    try:
        return _review_runtime(
            settings,
            store,
            saver,
            request,
            vcs=vcs,
            chat=chat,
            progress=progress,
            registry=registry,
            cancellation=cancellation,
            budget_client=budget_client,
        )
    except Exception as exc:
        _on_mount_failure(queue, request, exc, settings=settings)
        return None


def _review_runtime(
    settings: Settings,
    store: Any,
    saver: Any,
    request: ReviewRequest,
    *,
    vcs: VcsPort | None,
    chat: ChatPort | None,
    progress: ProgressPort | None,
    registry: ModelRegistry | None,
    cancellation: Any,
    budget_client: Any,
) -> tuple[ReviewGraph, dict[str, object] | None, tuple[int | None, float | None]]:
    """Grafo de *esta* review, montado con la foto de diales que le toca.

    Fuera del bucle para poder probarlo con una cola y un checkpointer falsos: es
    la costura que decide si lo publicado llega de verdad a una review.
    """
    effective, dial_snapshot = prepare_review(settings, store, saver, request)
    graph = build_runtime(
        effective,
        vcs=vcs,
        chat=chat,
        progress=progress,
        checkpointer=saver,
        registry=registry,
        cancellation=cancellation,
        budget_client=budget_client,
        probe_models=False,
    )
    # El grafo de claude no fija la foto: no monta personas ni política. Los techos
    # del CLI sí salen de esa foto y el worker los manda en la llamada.
    ceilings = _claude_ceilings(dial_snapshot)
    if effective.review_engine == "claude":
        return graph, None, ceilings
    return graph, dial_snapshot, ceilings


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
    # *seam* de nodo, aqui para abortar al desencolar los jobs ya cancelados.
    cancellation = build_cancellation(settings)
    # El worker guarda el VCS para el enlace del aviso de pausa. Con un grafo
    # inyectado (tests) no lo hay y el aviso sale sin enlace.
    vcs: VcsPort | None = None
    owns_runtime = graph is None
    saver: Any = None
    registry: ModelRegistry | None = None
    budget_client: Any = None
    config_store: Any = None
    if owns_runtime:
        from agent_review.config_store import build_config_store
        from agent_review.panel_config import lock_names
        from agent_review.redis_utils import build_redis_client

        lock_names(settings)
        vcs = build_vcs(settings)
        registry = build_registry(settings)
        validate_models(registry)
        config_store = build_config_store(settings)
        if settings.redis_url.strip():
            budget_client = build_redis_client(settings.redis_url)
        # Cola, checkpointer y registro: una vez. El grafo de cada review se monta
        # en el bucle. Este sirve para reconciliar hilos: `get_state` no usa diales.
        saver = build_checkpointer(settings.database_url, required=is_production(settings))
        graph = build_runtime(
            settings,
            vcs=vcs,
            chat=chat,
            progress=progress,
            checkpointer=saver,
            registry=registry,
            cancellation=cancellation,
            budget_client=budget_client,
            probe_models=False,
        )
    if graph is None:
        # Solo un grafo inyectado puede llegar aquí sin runtime montado, y ya se
        # comprobó arriba; el `raise` deja el tipo estrechado para el bucle.
        raise RuntimeError("el worker no tiene grafo")
    pending_store = pending if pending is not None else build_pending(settings)
    paused_store = paused if paused is not None else build_paused(settings)
    history = build_history(settings)
    claude_runner: ClaudeRunner | None = None
    claude_store: ClaudeArtifactStore | None = None
    claude_session: Callable[[], bool] | None = None
    if settings.review_engine == "claude":
        claude_store = ClaudeArtifactStore(root=settings.claude_artifact_root)
        runner = build_claude_runner(settings)
        claude_runner = runner
        claude_session = runner.active
    park_link = panel_claude_link(settings)
    saver = saver if saver is not None else getattr(graph, "checkpointer", None)
    logger.info("Worker de reviews arrancado (Fase 2)")
    logger.info("Politica: %s", settings.autonomy_config)
    last_sweep = time.monotonic()
    last_prune = time.monotonic()
    start_heartbeat()
    while True:
        queue.reclaim_inflight()
        request = queue.dequeue(timeout_s=poll_s)
        if request is not None:
            try:
                active = graph
                dial_snapshot: Mapping[str, object] | None = None
                claude_max_turns: int | None = None
                claude_timeout_s: float | None = None
                if owns_runtime:
                    mounted = _mount_or_requeue(
                        settings,
                        config_store,
                        saver,
                        queue,
                        request,
                        vcs=vcs,
                        chat=chat,
                        progress=progress,
                        registry=registry,
                        cancellation=cancellation,
                        budget_client=budget_client,
                    )
                    if mounted is None:
                        continue
                    active, dial_snapshot, (claude_max_turns, claude_timeout_s) = mounted
                dispatch_job(
                    queue,
                    active,
                    request,
                    max_attempts=settings.max_review_attempts,
                    paused=paused_store,
                    chat=chat,
                    vcs=vcs,
                    fix_queue=fix_queue,
                    fix_ttl_s=settings.fix_verify_ttl_s,
                    progress=progress,
                    cancel=cancellation,
                    history=history,
                    reasoning=settings.reasoning_stream_enabled,
                    claude_runner=claude_runner,
                    claude_store=claude_store,
                    claude_session=claude_session,
                    park_link=park_link,
                    expected_repo=settings.configured_repo(),
                    dial_snapshot=dial_snapshot,
                    claude_max_turns=claude_max_turns,
                    claude_timeout_s=claude_timeout_s,
                )
            finally:
                queue.ack()
            continue
        # La cola esta vacia: es el momento de aplicar los TTL (ingesta y decision).
        now = time.monotonic()
        if now - last_sweep >= sweep_s:
            if claude_session is not None:
                claude_session_ready(queue, claude_session)
            release_quota_wait(queue)
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
            # El histórico vive lo mismo que el detalle al que enlaza (021 R9).
            if history is not None:
                history.prune(older_than_days=settings.checkpoint_retention_days)
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
