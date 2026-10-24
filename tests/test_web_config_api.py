"""Contrato HTTP de la configuración de diales (027, 029 P9).

`test_panel_config.py` prueba la semántica pura (validación, diff contra el inicial,
conflicto de revisión, candado). Aquí se prueba lo que ve un cliente de la API: los
códigos y el cuerpo de `GET /api/config` y `POST /config`. Nació al retirar
`tests/test_web_ui.py` en el bloque R de la 029: esas comprobaciones se hacían sobre
el HTML y su equivalente es la API, no el formulario.

Los fakes se inyectan en `app.state`, como en `test_web.py`: ni Redis ni Postgres.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_review.config_store import ConfigReadError, MemoryConfigStore
from agent_review.panel_config import initial_values
from agent_review.settings import get_settings
from agent_review.web.main import app

client = TestClient(app)


class _Pins:
    """Read model mínimo: `GET /api/config` solo le pide las revisiones fijadas."""

    def dial_pins(self) -> list[tuple[str, int]]:
        return []


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> MemoryConfigStore:
    memory = MemoryConfigStore()
    monkeypatch.setattr(app.state, "config_store", memory, raising=False)
    # Sin esto, la dependencia del read model intenta un pool de Postgres real.
    monkeypatch.setattr(app.state, "read_model", _Pins(), raising=False)
    monkeypatch.setattr(get_settings(), "panel_config_lock", "")
    return memory


# --- Lectura ---------------------------------------------------------------------


def test_el_documento_vacio_trae_el_inicial_de_este_proceso(store: MemoryConfigStore) -> None:
    body = client.get("/api/config").json()

    assert body["revision"] == 0
    assert body["values"] == {}
    assert body["initial"]["llm_max_findings"] == get_settings().llm_max_findings
    assert body["locked"] == []


# --- Escritura -------------------------------------------------------------------


def test_guardar_publica_solo_la_clave_distinta(store: MemoryConfigStore) -> None:
    values = initial_values(get_settings())
    values["llm_max_findings"] = values["llm_max_findings"] + 1

    response = client.post("/config", json={"revision": 0, "values": values})

    assert response.status_code == 200
    assert response.json() == {"revision": 1}
    assert store.read().overrides == {"llm_max_findings": values["llm_max_findings"]}


def test_una_revision_vieja_no_escribe(store: MemoryConfigStore) -> None:
    values = initial_values(get_settings())
    store.publish(expected=0, overrides={"scope_ignore": "vendor/"}, actor="ana")

    response = client.post("/config", json={"revision": 0, "values": values})

    assert response.status_code == 409
    assert response.json()["detail"]["revision"] == 1
    assert store.read().overrides == {"scope_ignore": "vendor/"}


def test_un_formulario_pintado_con_otros_iniciales_pide_recargar(
    store: MemoryConfigStore,
) -> None:
    """Un default cambiado con la página abierta no se convierte en override."""
    values = initial_values(get_settings())
    painted = dict(values)
    painted["llm_max_findings"] = values["llm_max_findings"] + 5

    response = client.post("/config", json={"revision": 0, "values": values, "initial": painted})

    assert response.status_code == 409
    assert response.json()["detail"]["stale"] is True
    assert store.read().revision == 0


def test_un_entero_vacio_es_400_y_no_publica_cero(store: MemoryConfigStore) -> None:
    values = initial_values(get_settings())
    values["llm_max_findings"] = ""

    response = client.post("/config", json={"revision": 0, "values": values})

    assert response.status_code == 400
    assert response.json()["detail"]["field"] == "llm_max_findings"
    assert store.read().revision == 0


def test_un_cuerpo_que_no_es_objeto_es_400_no_422(store: MemoryConfigStore) -> None:
    """Un array da `422` con pydantic; el resto de comandos del panel da `400`."""
    assert client.post("/config", json=[1, 2, 3]).status_code == 400


# --- Rol y candado ---------------------------------------------------------------


def test_el_lector_lee_la_configuracion_y_no_puede_guardar(
    store: MemoryConfigStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "panel_session_secret", "ana:lector")
    headers = {"X-Panel-Session": "ana:lector"}

    assert client.get("/api/config", headers=headers).status_code == 200
    rejected = client.post("/config", json={"revision": 0, "values": {}}, headers=headers)

    assert rejected.status_code == 403


def test_un_dial_bloqueado_no_se_publica_y_no_deja_sin_guardar_el_resto(
    store: MemoryConfigStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El candado deja el control fijo, no rompe el guardado de los demás (027)."""
    settings = get_settings()
    locked = "llm_max_findings"
    initial = initial_values(settings)
    monkeypatch.setattr(settings, "panel_config_lock", locked)

    body = client.get("/api/config").json()
    assert body["locked"] == [locked]

    values = dict(initial)
    values[locked] = initial[locked] + 1
    rejected = client.post("/config", json={"revision": 0, "values": values})
    assert rejected.status_code == 400
    assert rejected.json()["detail"]["field"] == locked

    values[locked] = initial[locked]
    values["review_tools_max_turns"] = initial["review_tools_max_turns"] + 1
    saved = client.post("/config", json={"revision": 0, "values": values})
    assert saved.status_code == 200
    assert store.read().overrides == {"review_tools_max_turns": values["review_tools_max_turns"]}


# --- Postgres caído --------------------------------------------------------------


class _Caido:
    def read(self) -> object:
        raise ConfigReadError("caido")

    def publish(self, **kwargs: object) -> object:
        del kwargs
        raise ConfigReadError("caido")

    def history(self) -> object:
        return ()


def test_si_el_almacen_no_contesta_es_503_y_no_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un Postgres caído es infraestructura, no un formulario mal rellenado."""
    monkeypatch.setattr(app.state, "config_store", _Caido(), raising=False)
    values = initial_values(get_settings())
    values["llm_max_findings"] = values["llm_max_findings"] + 1

    assert client.get("/api/config").status_code == 503
    assert client.post("/config", json={"revision": 0, "values": values}).status_code == 503
