"""Contrato del almacén en Postgres (027, ADR-0031).

Sin Postgres alcanzable en local, los casos se saltan: el CI no publica una base,
pero el contrato de `ConfigStore` (fila vigente, historial, compare-and-swap y
primera inserción concurrente) queda escrito y se ejecuta donde haya una.
"""

from __future__ import annotations

import os
import threading

import pytest

from agent_review.config_store import ConfigReadError, PostgresConfigStore
from agent_review.panel_config import RevisionConflict

_URL = os.environ.get("PANEL_CONFIG_TEST_DATABASE_URL", "").strip()


def _store() -> PostgresConfigStore:
    store = PostgresConfigStore(_URL)
    store.ensure_schema()
    with store._pool.connection() as conn:
        conn.execute("DELETE FROM panel_config")
        conn.execute("DELETE FROM panel_config_history")
    return store


@pytest.fixture
def store() -> PostgresConfigStore:
    if not _URL:
        pytest.skip("define PANEL_CONFIG_TEST_DATABASE_URL para probar el almacén real")
    built = _store()
    try:
        yield built
    finally:
        built.close()


@pytest.mark.skipif(not _URL, reason="sin PANEL_CONFIG_TEST_DATABASE_URL")
def test_sin_fila_la_revision_es_cero(store: PostgresConfigStore) -> None:
    publication = store.read()
    assert publication.revision == 0
    assert publication.overrides == {}


@pytest.mark.skipif(not _URL, reason="sin PANEL_CONFIG_TEST_DATABASE_URL")
def test_publicar_devuelve_lo_escrito_y_guarda_el_historial(store: PostgresConfigStore) -> None:
    written = store.publish(expected=0, overrides={"llm_max_findings": 3}, actor="ana")
    assert written.revision == 1
    assert written.overrides == {"llm_max_findings": 3}
    assert written.updated_at is not None
    current = store.read()
    assert current.revision == 1
    assert current.actor == "ana"
    assert store.history()[0].overrides == {"llm_max_findings": 3}


@pytest.mark.skipif(not _URL, reason="sin PANEL_CONFIG_TEST_DATABASE_URL")
def test_una_revision_vieja_no_sustituye(store: PostgresConfigStore) -> None:
    store.publish(expected=0, overrides={"scope_ignore": "a/"}, actor="ana")
    with pytest.raises(RevisionConflict):
        store.publish(expected=0, overrides={"scope_ignore": "b/"}, actor="luis")
    assert store.read().overrides == {"scope_ignore": "a/"}


@pytest.mark.skipif(not _URL, reason="sin PANEL_CONFIG_TEST_DATABASE_URL")
def test_la_primera_insercion_concurrente_solo_entra_una_vez(store: PostgresConfigStore) -> None:
    """Sin fila, `FOR UPDATE` no bloquea a nadie: el cerrojo tiene que cubrirlo."""
    errors: list[BaseException] = []

    def publish() -> None:
        try:
            store.publish(expected=0, overrides={"llm_max_findings": 1}, actor="ana")
        except BaseException as exc:  # noqa: BLE001 - el test quiere al perdedor
            errors.append(exc)

    threads = [threading.Thread(target=publish) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert store.read().revision == 1
    assert len(errors) == 1
    assert isinstance(errors[0], RevisionConflict)


@pytest.mark.skipif(not _URL, reason="sin PANEL_CONFIG_TEST_DATABASE_URL")
def test_crear_el_esquema_dos_veces_no_falla(store: PostgresConfigStore) -> None:
    store.ensure_schema()
    store.ensure_schema()


@pytest.mark.skipif(not _URL, reason="sin PANEL_CONFIG_TEST_DATABASE_URL")
def test_un_postgres_caido_es_config_read_error() -> None:
    store = PostgresConfigStore("postgresql://x:y@127.0.0.1:1/nada?connect_timeout=1")
    try:
        with pytest.raises(ConfigReadError):
            store.read()
    finally:
        store.close()
