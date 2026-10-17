from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from agent_review.domain.models import (
    ApprovalRequest,
    AutofixClass,
    CommandResult,
    ContextScope,
    Decision,
    DecisionOutcome,
    FileClass,
    Finding,
    FindingAction,
    FindingDecision,
    FixResolution,
    FixResolutions,
    FixResult,
    FixStatus,
    FixWaitRequest,
    HitlDecision,
    Owner,
    PendingDecision,
    ProposedPatch,
    ReviewCoverage,
    ReviewRequest,
    Severity,
    SkipReason,
    ToolingSourceStatus,
    ToolUsage,
    Verdict,
    Verification,
    VerificationResult,
    VerifyJob,
)

logger = logging.getLogger(__name__)

# Tipos del dominio que viajan por el estado del grafo. Allowlist explicita para
# que el serializer no dependa del msgpack permisivo (que avisa hoy y bloqueara
# los tipos no registrados en versiones futuras). Incluye los HITL (Fase 2) y los
# del fix (Fase 3): `FixResult` viaja en el estado y debe poder deserializarse al
# reanudar un hilo.
_STATE_TYPES: tuple[type, ...] = (
    ReviewRequest,
    Finding,
    Decision,
    Severity,
    AutofixClass,
    Owner,
    FindingAction,
    ToolingSourceStatus,
    # 007: estado de verificacion y alcance de contexto del hallazgo.
    Verification,
    ContextScope,
    # 009: veredicto del verificador adversarial (viaja en `verifications`).
    Verdict,
    VerificationResult,
    # 012: cobertura de la review.
    ReviewCoverage,
    SkipReason,
    FileClass,
    # 014: uso del bucle de herramientas por persona.
    ToolUsage,
    ApprovalRequest,
    PendingDecision,
    FindingDecision,
    HitlDecision,
    DecisionOutcome,
    FixStatus,
    ProposedPatch,
    CommandResult,
    FixResult,
    # E7: el handoff del fix viaja por el estado (la wave en `fix_wait` y su
    # resolucion en el resume), asi que deben pasar la allowlist.
    VerifyJob,
    FixWaitRequest,
    FixResolution,
    FixResolutions,
)


def build_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=_STATE_TYPES)


def build_checkpointer(
    database_url: str, *, required: bool = False, connect_timeout_s: float = 10.0
) -> BaseCheckpointSaver[Any]:
    """Checkpointer durable si hay Postgres; en memoria si no (tests/local/CI).

    Con `required` (produccion) lanza en vez de caer a memoria: con HITL, un
    checkpointer en memoria pierde todas las pausas al reiniciar, y eso no puede
    descubrirse por un log. Fuera de produccion se mantiene el fallback.
    """
    if not database_url:
        if required:
            raise RuntimeError("DATABASE_URL es obligatoria en produccion (checkpointer durable)")
        logger.info("Sin DATABASE_URL; checkpointer en memoria (no durable)")
        return InMemorySaver(serde=build_serde())
    pool: Any = None
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        # Pool pequeno con `check`: una conexion muerta (reinicio de Postgres) se
        # descarta al pedirla en vez de fallar cada `get_state` hasta reiniciar.
        pool = ConnectionPool(
            database_url,
            min_size=1,
            max_size=2,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            check=ConnectionPool.check_connection,
            timeout=connect_timeout_s,
            open=True,
        )
        # Falla rapido en el arranque si Postgres no responde (sin esto, `setup`
        # esperaria el timeout de `getconn` con un error menos claro).
        pool.wait(timeout=connect_timeout_s)
        saver = PostgresSaver(pool, serde=build_serde())
        saver.setup()
        logger.info("Checkpointer Postgres listo")
        return saver
    except Exception:
        if pool is not None:
            # Sin cerrarlo, sus hilos de fondo seguirian reconectando (y logueando)
            # contra un Postgres que no se va a usar durante toda la vida del proceso.
            try:
                pool.close()
            except Exception:  # pragma: no cover - cierre best effort
                logger.debug("No se pudo cerrar el pool de Postgres", exc_info=True)
        if required:
            raise
        logger.exception("No se pudo abrir el checkpointer Postgres; uso memoria")
        return InMemorySaver(serde=build_serde())


def build_reader_checkpointer(
    database_url: str, *, connect_timeout_s: float = 10.0
) -> BaseCheckpointSaver[Any] | None:
    """Checkpointer de **solo lectura** para un proceso sin worker (OP2).

    El `web` del panel (019) lee los checkpoints que escribe el worker, pero no
    los crea ni compite por escritura: **sin** `setup()` (nada de DDL) y con un
    pool minimo de una conexion, que basta para un lector y no abre hilos de mas
    contra un Postgres que ya atiende al worker. Sin `DATABASE_URL` devuelve
    `None` (no un saver en memoria): un panel sin la fuente real no inventa un
    recorrido vacio, avisa de que no tiene datos.
    """
    if not database_url.strip():
        return None
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    # `Any`: el tipo generico del pool no expone utilidad aqui y es el mismo
    # patron que `build_checkpointer` (evita declarar el `DictRow` por duplicado).
    pool: Any = ConnectionPool(
        database_url,
        min_size=1,
        max_size=1,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        check=ConnectionPool.check_connection,
        timeout=connect_timeout_s,
        open=True,
    )
    try:
        pool.wait(timeout=connect_timeout_s)
    except Exception:
        try:
            pool.close()
        except Exception:  # pragma: no cover - cierre best effort
            logger.debug("No se pudo cerrar el pool de lectura", exc_info=True)
        raise
    logger.info("Checkpointer de lectura listo (sin setup)")
    return PostgresSaver(pool, serde=build_serde())


class RetentionPolicy(Protocol):
    """Decide si el ultimo checkpoint de un hilo puede podarse.

    Es el **seam** de T10: la regla de negocio (que merece borrarse) queda aislada
    de la API del saver (`list`/`delete_thread`), que los cuatro backends oficiales
    mantienen parcialmente sin implementar (`prune`, `delete_for_runs`,
    `copy_thread` -> `NotImplementedError`). Con la decision tras un Protocol, un
    futuro adaptador puede apoyarse en un `prune` nativo **reusando esta misma
    politica**, sin reescribir el motor ni volver a tocar los tests del contrato.
    """

    def can_prune(
        self, thread_id: str, latest: CheckpointTuple, now: datetime
    ) -> bool:  # pragma: no cover - contrato
        ...


class CompletedOnlyRetention:
    """Politica por defecto: poda completados antiguos; nunca un pausado.

    Un hilo pausado espera una decision humana que puede llegar tarde: podarlo
    dejaria la review indecidible (`Command(resume=...)` sin estado). Por eso los
    pausados se excluyen por **doble via**: los de `paused_threads` (inbox) y los
    que delata el propio checkpoint. La dedupe de publicacion vive en las marcas
    del PR, no en el checkpoint, asi que podar un completado no puede duplicar
    comentarios (spec §4.8).
    """

    def __init__(
        self, *, older_than_days: float, paused_threads: Iterable[str], now: datetime
    ) -> None:
        self._cutoff = now - timedelta(days=older_than_days)
        self._protected = set(paused_threads)
        # Retencion `<= 0` desactiva la poda: evita borrar todo por un arranque mal
        # configurado. `can_prune` corta en el primer hilo sin depender del caller.
        self._enabled = older_than_days > 0

    def can_prune(self, thread_id: str, latest: CheckpointTuple, now: datetime) -> bool:
        del now  # `now` viaja en la configuracion de la politica (permite fakes)
        if not self._enabled or thread_id in self._protected:
            return False
        if not _is_completed(latest):
            return False
        ts = _checkpoint_ts(latest)
        return ts is not None and ts < self._cutoff


class FixWaitRetention:
    """Poda hilos pausados en `fix_wait` vencidos; los `human` siguen intocables (R1, §8.1).

    Un hilo pausado en `fix_wait` tiene `__interrupt__` y, con la politica por
    defecto, quedaria **protegido para siempre**. Pero esa pausa es distinta de la
    humana: si la wave vencio, el fix ya quedo `NOT_VERIFIABLE` y publicado, asi
    que el hilo no espera a nadie. Se poda cuando supero `max_age_s`.

    Un hilo `human` (o uno completado) nunca se poda aqui: puede reusar
    `CompletedOnlyRetention` para los completados. La dedupe de publicacion vive en
    las marcas del PR, no en el checkpoint, asi que podar no duplica comentarios.
    """

    def __init__(
        self,
        *,
        max_age_s: float,
        paused_kinds: dict[str, str],
        now: datetime,
    ) -> None:
        self._cutoff = now - timedelta(seconds=max_age_s)
        self._kinds = paused_kinds
        self._enabled = max_age_s > 0

    def can_prune(self, thread_id: str, latest: CheckpointTuple, now: datetime) -> bool:
        del now
        if not self._enabled:
            return False
        if self._kinds.get(thread_id) != "fix":
            return False
        ts = _checkpoint_ts(latest)
        return ts is not None and ts < self._cutoff


def latest_checkpoints(saver: BaseCheckpointSaver[Any]) -> dict[str, CheckpointTuple] | None:
    """Ultima tupla de cada hilo, o `None` si la base no responde. Nunca lanza.

    Es el escaneo caro (lista y deserializa todos los checkpoints): el worker lo
    hace una vez por ciclo y lo comparte entre la reconciliacion y la poda. Se
    recorre de la tupla mas nueva a la mas vieja para que una antigua no marque
    el hilo como pendiente.
    """
    latest: dict[str, CheckpointTuple] = {}
    try:
        for tup in saver.list(None):
            latest.setdefault(tup.config["configurable"]["thread_id"], tup)
    except Exception:  # pragma: no cover - base caida
        logger.exception("No se pudieron listar los hilos del checkpointer")
        return None
    return latest


def prune_threads(
    saver: BaseCheckpointSaver[Any],
    policy: RetentionPolicy,
    *,
    now: datetime | None = None,
    latest: dict[str, CheckpointTuple] | None = None,
) -> list[str]:
    """Poda los hilos que `policy` apruebe. Unico punto que toca el saver.

    Confina las dos llamadas volatiles de LangGraph (`list` y `delete_thread`) en
    este adaptador: el dia que el paquete implemente `prune`, se anade otro motor
    que recorra la politica igual. Nunca lanza: la poda es mantenimiento y una
    base caida no debe tumbar al worker. `latest` reutiliza un listado ya hecho
    (`latest_checkpoints`) para no escanear dos veces.
    """
    moment = now or datetime.now(UTC)
    if latest is None:
        latest = latest_checkpoints(saver)
        if latest is None:
            return []

    pruned: list[str] = []
    for thread_id, tup in latest.items():
        if not policy.can_prune(thread_id, tup, moment):
            continue
        try:
            saver.delete_thread(thread_id)
        except Exception:  # pragma: no cover - base caida
            logger.exception("No se pudo podar el hilo %s", thread_id)
            continue
        pruned.append(thread_id)
    if pruned:
        logger.info("Podados %d hilo(s) por retencion", len(pruned))
    return pruned


def prune_completed_threads(
    saver: BaseCheckpointSaver[Any],
    *,
    older_than_days: float,
    paused_threads: Iterable[str] = (),
    now: datetime | None = None,
    latest: dict[str, CheckpointTuple] | None = None,
) -> list[str]:
    """Atajo de la politica por defecto (`CompletedOnlyRetention`) sobre `prune_threads`."""
    moment = now or datetime.now(UTC)
    policy = CompletedOnlyRetention(
        older_than_days=older_than_days, paused_threads=paused_threads, now=moment
    )
    return prune_threads(saver, policy, now=moment, latest=latest)


def prune_fix_threads(
    saver: BaseCheckpointSaver[Any],
    *,
    max_age_s: float,
    paused_kinds: dict[str, str],
    now: datetime | None = None,
    latest: dict[str, CheckpointTuple] | None = None,
) -> list[str]:
    """Atajo de `FixWaitRetention`: poda solo los hilos pausados en `fix_wait` vencidos."""
    moment = now or datetime.now(UTC)
    policy = FixWaitRetention(max_age_s=max_age_s, paused_kinds=paused_kinds, now=moment)
    return prune_threads(saver, policy, now=moment, latest=latest)


def paused_thread_ids(
    saver: BaseCheckpointSaver[Any],
    *,
    latest: dict[str, CheckpointTuple] | None = None,
) -> list[str]:
    """Hilos cuyo ultimo checkpoint tiene un `interrupt` sin reanudar.

    Es la verdad durable de "que esta pausado", independiente del inbox de Redis:
    sirve para reconciliar las pausas que el inbox perdio (caida del worker entre
    el checkpoint y el registro). Nunca lanza, como la poda.
    """
    if latest is None:
        latest = latest_checkpoints(saver)
        if latest is None:
            return []
    return [thread_id for thread_id, tup in latest.items() if not _is_completed(tup)]


def _is_completed(tup: CheckpointTuple) -> bool:
    """Un hilo terminado no tiene un `__interrupt__` en su ultimo checkpoint.

    Una reanudacion que termina escribe checkpoints nuevos sin escrituras
    pendientes, asi que el `__interrupt__` desaparece de la ultima tupla. Un
    `__resume__` junto al `__interrupt__` **no** indica que haya terminado: es una
    reanudacion que fallo (`__error__`) o que volvio a pausar, y el hilo sigue
    esperando una decision. Podarlo dejaria la review indecidible.
    """
    keys = {write[1] for write in (tup.pending_writes or [])}
    return "__interrupt__" not in keys


def pause_kind_of(tup: CheckpointTuple) -> str | None:
    """`kind` de la pausa pendiente del checkpoint (`human`/`fix`), o `None`.

    La carga del `interrupt` (un `ApprovalRequest` o un `FixWaitRequest`) lleva su
    `kind`. Segun el backend y la fase, el value llega como objeto de dominio
    (`Interrupt.value`) o como diccionario crudo: se cubren ambos. Un hilo sin
    pausa o con un payload sin `kind` se trata como `human`, el default tolerante
    de la Fase 2.
    """
    for write in tup.pending_writes or []:
        if write[1] != "__interrupt__":
            continue
        payload = write[2]
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            value = getattr(item, "value", item)
            kind = value.get("kind") if isinstance(value, dict) else getattr(value, "kind", None)
            if kind == "fix":
                return "fix"
            if kind is not None:
                return str(kind)
            # Value sin `kind` (payload antiguo de Fase 2): se asume `human`.
            if isinstance(value, dict) or hasattr(value, "pending"):
                return "human"
    return None


def paused_thread_kinds(
    saver: BaseCheckpointSaver[Any],
    *,
    latest: dict[str, CheckpointTuple] | None = None,
) -> dict[str, str]:
    """`kind` de cada hilo pausado, indexado por `thread_id`. Nunca lanza.

    Alimenta la poda `kind-aware`: un hilo pausado en `fix_wait` si se puede podar
    (su verificacion pudo vencer y la review quedo publicada); uno `human` no.
    """
    if latest is None:
        latest = latest_checkpoints(saver)
        if latest is None:
            return {}
    kinds: dict[str, str] = {}
    for thread_id, tup in latest.items():
        if _is_completed(tup):
            continue
        kinds[thread_id] = pause_kind_of(tup) or "human"
    return kinds


def _checkpoint_ts(tup: CheckpointTuple) -> datetime | None:
    raw = (tup.checkpoint or {}).get("ts")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
