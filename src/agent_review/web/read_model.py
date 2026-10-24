"""Read model del panel (iniciativa 019, OP13/OP14, W3).

El panel es CQRS-lite: lee el estado operativo de Redis y el recorrido del
checkpointer de Postgres, y nunca escribe el estado del grafo (el worker sigue siendo
el unico escritor). Este modulo aisla esas dos fuentes tras el `Protocol
PanelReadModel`, de modo que los endpoints se prueban con un fake en memoria, sin
Redis ni Postgres.

Dos adaptadores, uno por fuente: `RedisPanelReadModel` (cola, en vuelo, pausas,
descartes y presupuesto) y `PgTraceReader` (el recorrido, con el saver de solo
lectura de OP2). `CompositePanelReadModel` los une bajo el mismo protocolo.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar, runtime_checkable

import redis
from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import BaseModel, Field

from agent_review.checkpointer import build_reader_checkpointer
from agent_review.domain.models import PendingDecision, ReviewRequest
from agent_review.domain.paused import PausedReview, PauseKind
from agent_review.domain.trace import PauseKindName, ReviewTrace
from agent_review.queue import DEAD_LETTER_KEY, QUEUE_KEY
from agent_review.redis_utils import build_redis_client
from agent_review.settings import Settings
from agent_review.trace import read_trace

logger = logging.getLogger(__name__)

# Prefijos de las claves que el panel lee. El de progreso es configurable
# (`PROGRESS_KEY`); los demas son los del worker y la API (arquitectura.md §5).
PAUSED_KEY_PREFIX = "reviews:paused"
BUDGET_KEY_PREFIX = "budget"
# Sub-claves del presupuesto que no son el contador de una review (017 B11): el
# contador del fix y lo reservado sin confirmar no se listan como reviews.
_BUDGET_SKIP_PREFIXES = ("reserved:", "fix:")

_T = TypeVar("_T")


class QueueItem(BaseModel):
    """Una review encolada en `reviews:jobs`, por antiguedad."""

    review_key: str
    repo: str = ""
    pr_id: int = 0
    head_sha: str = ""
    pipeline_id: str = ""
    attempts: int = 0
    launch: bool = False


class ActiveNode(BaseModel):
    """Un nodo en curso del progreso del worker (W10), con su rama del fan-out."""

    node: str
    started_at: datetime | None = None
    branches: int = 1


class ActiveReview(BaseModel):
    """Review con algo en vuelo ahora mismo (`reviews:active:<review_key>`)."""

    review_key: str
    nodes: list[ActiveNode] = Field(default_factory=list)


class DeadLetterItem(BaseModel):
    """Una review descartada en la DLQ (`reviews:jobs:dead`), reintentable."""

    review_key: str
    attempts: int = 0
    reason: str = ""
    discarded_at: datetime | None = None


class BudgetItem(BaseModel):
    """Gasto de tokens de una review (`budget:<review_key>`), 017."""

    review_key: str
    committed: int = 0
    reserved: int = 0
    total: int = 0
    remaining: int = 0


class PausedItem(BaseModel):
    """Una pausa del inbox; su carga segun `kind` (`human`/`fix`)."""

    review_key: str
    kind: PauseKindName
    created_at: datetime
    state: str
    pending: list[PendingDecision] = Field(default_factory=list)
    command_hint: str = ""
    jobs: list[dict[str, Any]] = Field(default_factory=list)
    publication: bool = False


class PausedGroups(BaseModel):
    """Las pausas separadas por `kind`: las humanas esperan decision, las de fix, la wave."""

    human: list[PausedItem] = Field(default_factory=list)
    fix: list[PausedItem] = Field(default_factory=list)


class PanelState(BaseModel):
    """Todo lo que el panel pinta de un tiron (OP13)."""

    generated_at: datetime
    queue: list[QueueItem] = Field(default_factory=list)
    active: list[ActiveReview] = Field(default_factory=list)
    paused: PausedGroups = Field(default_factory=PausedGroups)
    dead: list[DeadLetterItem] = Field(default_factory=list)
    budget: list[BudgetItem] = Field(default_factory=list)


@runtime_checkable
class PanelReadModel(Protocol):
    """Lo que el panel puede leer: estado operativo y recorrido de una review."""

    def state(self) -> PanelState: ...

    def trace(self, review_key: str) -> ReviewTrace | None:
        """Recorrido del hilo, o `None` si no existe (o no hay fuente de datos)."""
        ...


class RedisPanelReadModel:
    """Estado operativo desde Redis (cola, en vuelo, pausas, descartes, presupuesto).

    Best-effort por seccion: si Redis no responde, esa seccion vuelve vacia y se
    registra un aviso, en vez de tumbar todo el panel por una fuente que puede
    volver (mismo criterio que el progreso del worker, criterio 8 de la 019).
    """

    def __init__(
        self,
        *,
        url: str,
        progress_key: str = "reviews:active",
        budget_total: int = 0,
        queue_key: str = QUEUE_KEY,
        dead_key: str = DEAD_LETTER_KEY,
        paused_key: str = PAUSED_KEY_PREFIX,
        budget_key: str = BUDGET_KEY_PREFIX,
        client: redis.Redis | None = None,
    ) -> None:
        self._client = client if client is not None else build_redis_client(url)
        self._queue_key = queue_key
        self._dead_key = dead_key
        self._paused_key = paused_key
        self._progress_key = progress_key
        self._budget_key = budget_key
        self._budget_total = max(0, budget_total)

    def state(self) -> PanelState:
        return PanelState(
            generated_at=datetime.now(UTC),
            queue=self._guard("cola", self._read_queue, []),
            active=self._guard("en vuelo", self._read_active, []),
            paused=self._guard("pausas", self._read_paused, PausedGroups()),
            dead=self._guard("descartes", self._read_dead, []),
            budget=self._guard("presupuesto", self._read_budget, []),
        )

    def _guard(self, label: str, operation: Callable[[], _T], fallback: _T) -> _T:
        try:
            return operation()
        except redis.exceptions.RedisError as exc:
            logger.warning("panel: Redis no respondio en %s (%s); seccion vacia", label, exc)
            return fallback

    def _read_queue(self) -> list[QueueItem]:
        items: list[QueueItem] = []
        for raw in self._client.lrange(self._queue_key, 0, -1):
            request = _parse_request(raw)
            if request is None:
                continue
            items.append(
                QueueItem(
                    review_key=request.review_key,
                    repo=request.repo,
                    pr_id=request.pr_id,
                    head_sha=request.head_sha,
                    pipeline_id=request.pipeline_id,
                    attempts=request.attempts,
                    launch=request.launch,
                )
            )
        return items

    def _read_active(self) -> list[ActiveReview]:
        reviews: list[ActiveReview] = []
        for key in self._scan(f"{self._progress_key}:*"):
            review_key = key[len(self._progress_key) + 1 :]
            nodes = [_active_node(name, raw) for name, raw in self._client.hgetall(key).items()]
            reviews.append(ActiveReview(review_key=review_key, nodes=nodes))
        return reviews

    def _read_paused(self) -> PausedGroups:
        groups = PausedGroups()
        for key in self._scan(f"{self._paused_key}:*"):
            raw = self._client.get(key)
            if raw is None:
                continue
            try:
                review = PausedReview.model_validate_json(raw)
            except ValueError:
                logger.warning("panel: entrada de pausa ilegible en %s", key)
                continue
            item = _paused_item(review)
            (groups.fix if review.kind is PauseKind.FIX else groups.human).append(item)
        return groups

    def _read_dead(self) -> list[DeadLetterItem]:
        items: list[DeadLetterItem] = []
        for raw in self._client.lrange(self._dead_key, 0, -1):
            entry = _parse_object(raw)
            if entry is None:
                continue
            items.append(
                DeadLetterItem(
                    review_key=str(entry.get("review_key", "")),
                    attempts=_as_int(entry.get("attempts")),
                    reason=str(entry.get("reason", "")),
                    discarded_at=_dt_from_epoch(entry.get("discarded_at")),
                )
            )
        return items

    def _read_budget(self) -> list[BudgetItem]:
        items: list[BudgetItem] = []
        prefix = f"{self._budget_key}:"
        for key in self._scan(f"{self._budget_key}:*"):
            review_key = key[len(prefix) :]
            if not review_key or review_key.startswith(_BUDGET_SKIP_PREFIXES):
                continue
            committed = _as_int(self._client.get(key))
            reserved = _as_int(self._client.get(f"{prefix}reserved:{review_key}"))
            items.append(
                BudgetItem(
                    review_key=review_key,
                    committed=committed,
                    reserved=reserved,
                    total=self._budget_total,
                    remaining=max(0, self._budget_total - committed - reserved),
                )
            )
        return items

    def _scan(self, pattern: str) -> list[str]:
        # `SCAN` en vez de `KEYS`: no bloquea el Redis que comparte con el worker.
        return [str(key) for key in self._client.scan_iter(match=pattern)]


class PgTraceReader:
    """Recorrido desde el checkpointer de solo lectura (OP2), sin construir el grafo (D1).

    El saver se abre **de forma perezosa**: si Postgres no responde, el panel sigue
    sirviendo el estado de Redis y el endpoint de recorrido da `404`, en vez de
    tumbar el arranque por una fuente que puede volver. Un solo `threading.Lock`
    porque los endpoints sincronos de FastAPI corren en un pool de hilos.
    """

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url
        self._lock = threading.Lock()
        self._saver: BaseCheckpointSaver[Any] | None = None
        self._resolved = False

    def _reader(self) -> BaseCheckpointSaver[Any] | None:
        if self._resolved:
            return self._saver
        with self._lock:
            if not self._resolved:
                try:
                    self._saver = build_reader_checkpointer(self._database_url)
                except Exception:
                    logger.exception("No se pudo abrir el checkpointer de lectura del panel")
                    self._saver = None
                self._resolved = True
        return self._saver

    def reader_unavailable(self) -> bool:
        """¿Se sabe ya que no hay fuente de recorrido? (Postgres no respondió)."""
        return self._reader() is None

    def trace(self, review_key: str) -> ReviewTrace | None:
        return read_trace(self._reader(), review_key)

    def dial_revision(self, review_key: str) -> int | None:
        """Revisión de diales que fijó el hilo, o `None` si no hay foto."""
        from agent_review.panel_config import snapshot_revision

        saver = self._reader()
        if saver is None:
            return None
        for thread_id in (review_key, f"{review_key}:partial"):
            try:
                tup = saver.get_tuple({"configurable": {"thread_id": thread_id}})
            except Exception:
                logger.exception("No se pudo leer la foto de diales de %s", review_key)
                return None
            if tup is None:
                continue
            checkpoint = getattr(tup, "checkpoint", None)
            values = checkpoint.get("channel_values") if isinstance(checkpoint, dict) else None
            revision = snapshot_revision(values if isinstance(values, dict) else None)
            if revision is not None:
                return revision
        return None

    def close(self) -> None:
        """Cierra el pool de lectura. Best-effort: el panel es observabilidad."""
        with self._lock:
            saver = self._saver
            self._saver = None
            self._resolved = True
        pool = getattr(saver, "conn", None)
        if pool is None:
            return
        try:
            pool.close()
        except Exception:  # pragma: no cover - cierre best effort
            logger.debug("No se pudo cerrar el pool de lectura del panel", exc_info=True)


class CompositePanelReadModel:
    """Une las dos fuentes bajo el mismo `PanelReadModel`: estado (Redis) y recorrido (Postgres)."""

    def __init__(self, *, state_model: RedisPanelReadModel, trace_reader: PgTraceReader) -> None:
        self._state_model = state_model
        self._trace_reader = trace_reader

    def state(self) -> PanelState:
        return self._state_model.state()

    def trace(self, review_key: str) -> ReviewTrace | None:
        return self._trace_reader.trace(review_key)

    def dial_pins(self) -> list[tuple[str, int]] | None:
        """Reviews en curso y la revisión de diales que cada una fijó.

        `None` significa «no pude leer»: el panel lo dice en vez de enseñar una
        lista vacía, que se leería como «ninguna review en curso».
        """
        if self._trace_reader.reader_unavailable():
            return None
        state = self.state()
        keys = [item.review_key for item in state.active]
        keys.extend(item.review_key for item in state.paused.human)
        keys.extend(item.review_key for item in state.paused.fix)
        pins: list[tuple[str, int]] = []
        for key in keys:
            revision = self._trace_reader.dial_revision(key)
            if revision is not None:
                pins.append((key, revision))
        return pins

    def close(self) -> None:
        self._trace_reader.close()


def build_panel_read_model(settings: Settings) -> PanelReadModel:
    """Raiz de composicion del read model del panel.

    Se construye una vez (en el arranque del `web`), no por peticion: el pool de
    lectura de Postgres es caro y el panel es un proceso de vida larga (R6).
    """
    return CompositePanelReadModel(
        state_model=RedisPanelReadModel(
            url=settings.redis_url,
            progress_key=settings.progress_key,
            budget_total=settings.llm_budget_tokens_per_pr,
        ),
        trace_reader=PgTraceReader(database_url=settings.database_url),
    )


def _parse_request(raw: object) -> ReviewRequest | None:
    if not isinstance(raw, str):
        return None
    try:
        return ReviewRequest.model_validate_json(raw)
    except ValueError:
        logger.warning("panel: request de cola ilegible")
        return None


def _parse_object(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, str):
        return None
    try:
        entry = json.loads(raw)
    except ValueError:
        logger.warning("panel: entrada de la DLQ ilegible")
        return None
    return entry if isinstance(entry, dict) else None


def _active_node(name: object, raw: object) -> ActiveNode:
    """Un campo del hash de progreso: `{node, started_at, branches}` en JSON."""
    data = _parse_object(raw) or {}
    return ActiveNode(
        node=str(data.get("node", name)),
        started_at=_parse_dt(data.get("started_at")),
        branches=max(1, _as_int(data.get("branches")) or 1),
    )


def _paused_item(review: PausedReview) -> PausedItem:
    """Forma comun por `kind`: `human` expone pendientes; `fix`, la wave en curso."""
    if review.kind is PauseKind.FIX:
        wave = review.fix_request
        return PausedItem(
            review_key=review.review_key,
            kind="fix",
            created_at=review.created_at,
            state="VERIFYING",
            jobs=[{"run_key": job.run_key, "finding_id": job.finding_id} for job in wave.jobs],
        )
    approval = review.human_request
    return PausedItem(
        review_key=review.review_key,
        kind="human",
        created_at=review.created_at,
        state="PAUSED",
        pending=list(approval.pending),
        command_hint=approval.command_hint,
        publication=approval.pause_kind == "publication",
    )


def _as_int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _parse_dt(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _dt_from_epoch(value: object) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None
