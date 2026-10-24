"""Documento vigente de diales (027, ADR-0031).

Postgres es la fuente. Sin `DATABASE_URL`, o si en desarrollo no se puede
conectar, el almacén es en memoria y empieza vacío: el sistema se comporta
como antes de esta iniciativa. Una lectura que falla después de haber cargado
una revisión devuelve esa, no los defaults del código.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from agent_review.panel_config import RevisionConflict
from agent_review.settings import Settings, is_production

logger = logging.getLogger(__name__)

# Cerrojo consultivo de la publicacion: un entero fijo, porque solo hay una fila
# vigente (`id = 1`). Serializa el camino de la primera insercion, que ningun
# `SELECT ... FOR UPDATE` puede cubrir.
_PUBLISH_LOCK = 0x0260_0001


# Una sentencia por entrada: con `prepare_threshold=0` psycopg prepara cada
# `execute`, y una sentencia preparada no puede llevar dos comandos.
_SCHEMA: tuple[str, ...] = (
    """
CREATE TABLE IF NOT EXISTS panel_config (
    id integer PRIMARY KEY,
    revision integer NOT NULL,
    overrides jsonb NOT NULL,
    actor text NOT NULL,
    updated_at timestamptz NOT NULL
)
""",
    """
CREATE TABLE IF NOT EXISTS panel_config_history (
    revision integer PRIMARY KEY,
    overrides jsonb NOT NULL,
    actor text NOT NULL,
    updated_at timestamptz NOT NULL
)
""",
)


class ConfigReadError(RuntimeError):
    """Postgres no contestó. No es un documento vacío."""


@dataclass(frozen=True)
class Publication:
    revision: int
    overrides: dict[str, object]
    actor: str
    updated_at: datetime | None


@dataclass(frozen=True)
class HistoryEntry:
    revision: int
    overrides: dict[str, object]
    actor: str
    updated_at: datetime


class ConfigStore(Protocol):
    def read(self) -> Publication: ...

    def publish(
        self, *, expected: int, overrides: Mapping[str, object], actor: str
    ) -> Publication: ...

    def history(self) -> tuple[HistoryEntry, ...]: ...


class MemoryConfigStore:
    """Almacén de un proceso. La revisión 0 es «no se ha publicado nada»."""

    def __init__(self) -> None:
        self._revision = 0
        self._overrides: dict[str, object] = {}
        self._actor = ""
        self._updated_at: datetime | None = None
        self._history: list[HistoryEntry] = []
        self._lock = threading.Lock()

    def read(self) -> Publication:
        with self._lock:
            return Publication(self._revision, dict(self._overrides), self._actor, self._updated_at)

    def publish(self, *, expected: int, overrides: Mapping[str, object], actor: str) -> Publication:
        with self._lock:
            if expected != self._revision:
                raise RevisionConflict(self._revision)
            self._revision += 1
            self._overrides = dict(overrides)
            self._actor = actor
            self._updated_at = datetime.now(UTC)
            self._history.append(
                HistoryEntry(self._revision, dict(self._overrides), actor, self._updated_at)
            )
            return Publication(self._revision, dict(self._overrides), self._actor, self._updated_at)

    def history(self) -> tuple[HistoryEntry, ...]:
        with self._lock:
            return tuple(self._history)


class RememberingStore:
    """Si la lectura falla y ya hubo una, devuelve la última. Si no, propaga."""

    def __init__(self, inner: ConfigStore) -> None:
        self._inner = inner
        self._last: Publication | None = None

    def read(self) -> Publication:
        try:
            current = self._inner.read()
        except ConfigReadError:
            if self._last is None:
                raise
            logger.warning("No se pudo leer la configuración publicada; sigo con la última")
            return self._last
        self._last = current
        return current

    def publish(self, *, expected: int, overrides: Mapping[str, object], actor: str) -> Publication:
        current = self._inner.publish(expected=expected, overrides=overrides, actor=actor)
        self._last = current
        return current

    def history(self) -> tuple[HistoryEntry, ...]:
        return self._inner.history()

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()


class PostgresConfigStore:
    def __init__(self, url: str) -> None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        self._pool = ConnectionPool(
            url,
            min_size=1,
            max_size=1,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            timeout=5,
            open=True,
        )

    def ensure_schema(self) -> None:
        with self._pool.connection() as conn:
            for statement in _SCHEMA:
                conn.execute(statement)

    def close(self) -> None:
        self._pool.close()

    def read(self) -> Publication:
        try:
            with self._pool.connection() as conn:
                row: Any = conn.execute(
                    "SELECT revision, overrides, actor, updated_at FROM panel_config WHERE id = 1"
                ).fetchone()
        except Exception as exc:
            raise ConfigReadError("no se pudo leer panel_config") from exc
        if row is None:
            return Publication(0, {}, "", None)
        return Publication(
            int(row["revision"]),
            dict(row["overrides"]),
            str(row["actor"]),
            row["updated_at"],
        )

    def publish(self, *, expected: int, overrides: Mapping[str, object], actor: str) -> Publication:
        payload = json.dumps(dict(overrides))
        try:
            with self._pool.connection() as conn, conn.transaction():
                revision, updated_at = self._write(conn, expected, payload, actor)
                conn.execute(
                    "INSERT INTO panel_config_history (revision, overrides, actor, updated_at) "
                    "VALUES (%s, %s::jsonb, %s, %s)",
                    (revision, payload, actor, updated_at),
                )
        except RevisionConflict:
            raise
        except Exception as exc:
            raise ConfigReadError("no se pudo publicar la configuración") from exc
        return Publication(revision, dict(overrides), actor, updated_at)

    def _write(self, conn: Any, expected: int, payload: str, actor: str) -> tuple[int, Any]:
        """Escribe la fila vigente con compare-and-swap y devuelve `(revision, instante)`.

        La primera publicacion no tiene fila que bloquear: `SELECT ... FOR UPDATE`
        sobre cero filas no excluye a nadie, asi que dos guardados con `expected=0`
        pasarian la comprobacion. Un cerrojo consultivo por transaccion serializa
        los dos caminos (fila existente o no) sin abrir otro hueco.
        """
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_PUBLISH_LOCK,))
        row: Any = conn.execute(
            "SELECT revision FROM panel_config WHERE id = 1 FOR UPDATE"
        ).fetchone()
        current = 0 if row is None else int(row["revision"])
        if current != expected:
            raise RevisionConflict(current)
        revision = current + 1
        if row is None:
            conn.execute(
                "INSERT INTO panel_config (id, revision, overrides, actor, updated_at) "
                "VALUES (1, %s, %s::jsonb, %s, now())",
                (revision, payload, actor),
            )
        else:
            conn.execute(
                "UPDATE panel_config SET revision = %s, overrides = %s::jsonb, "
                "actor = %s, updated_at = now() WHERE id = 1",
                (revision, payload, actor),
            )
        written: Any = conn.execute("SELECT updated_at FROM panel_config WHERE id = 1").fetchone()
        return revision, written["updated_at"]

    def history(self) -> tuple[HistoryEntry, ...]:
        with self._pool.connection() as conn:
            rows: Any = conn.execute(
                "SELECT revision, overrides, actor, updated_at FROM panel_config_history "
                "ORDER BY revision"
            ).fetchall()
        return tuple(
            HistoryEntry(
                int(row["revision"]),
                dict(row["overrides"]),
                str(row["actor"]),
                row["updated_at"],
            )
            for row in rows
        )


def build_config_store(settings: Settings) -> RememberingStore:
    """Almacén del proceso. En producción, sin Postgres, no arranca.

    Una `DATABASE_URL` vacía es el mismo caso que un Postgres inalcanzable: en
    producción, fallar cerrado. Un almacén en memoria en producción aceptaría
    guardados que ningún worker leería y el panel diría que están publicados.
    """
    store: PostgresConfigStore | None = None
    try:
        url = settings.database_url.strip()
        if not url:
            raise ConfigReadError("DATABASE_URL vacía")
        store = PostgresConfigStore(url)
        store.ensure_schema()
    except Exception as exc:
        if store is not None:
            store.close()
        if is_production(settings):
            raise RuntimeError("Postgres es obligatorio para la configuración del panel") from exc
        logger.warning("Sin Postgres para la configuración del panel; uso memoria: %s", exc)
        return RememberingStore(MemoryConfigStore())
    return RememberingStore(store)
