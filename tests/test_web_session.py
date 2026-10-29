"""Sesion del panel en HTTP (iniciativa 029, bloque S).

Cubre el contrato de `GET`/`POST`/`DELETE /api/session`: el token se valida con
`verify_session` (el mismo de la cabecera, sin una segunda regla), la cookie sale
`HttpOnly` y `SameSite=Lax`, y ni el cuerpo ni la cookie devuelven la credencial.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_review.settings import get_settings
from agent_review.web.auth import SESSION_COOKIE, cookie_secure
from agent_review.web.main import app


@pytest.fixture()
def cliente() -> TestClient:
    # Cookies aisladas por test: `Set-Cookie` de uno no contamina al siguiente.
    return TestClient(app)


def _secreto(monkeypatch: pytest.MonkeyPatch, valor: str = "tok3n-real") -> None:
    monkeypatch.setattr(get_settings(), "panel_session_secret", valor)


def test_post_con_el_token_bueno_deja_la_cookie(
    cliente: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _secreto(monkeypatch)
    response = cliente.post("/api/session", json={"token": "tok3n-real"})

    assert response.status_code == 200
    assert response.json() == {"user": "panel", "role": "operador"}

    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie.lower() or "samesite=lax" in cookie.lower()
    # 200 desde este cliente: la cookie quedo guardada y viaja sola.
    assert cliente.get("/api/session").status_code == 200


def test_el_cuerpo_no_devuelve_el_token(
    cliente: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _secreto(monkeypatch)
    response = cliente.post("/api/session", json={"token": "tok3n-real"})
    assert "tok3n-real" not in response.text
    assert set(response.json()) == {"user", "role"}


def test_un_token_malo_es_401_y_no_deja_cookie(
    cliente: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _secreto(monkeypatch)
    response = cliente.post("/api/session", json={"token": "no-es-el-secreto"})

    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert cliente.get("/api/session").status_code == 401


def test_sin_cuerpo_o_sin_token_es_400(
    cliente: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _secreto(monkeypatch)
    assert cliente.post("/api/session", json={}).status_code == 400
    assert cliente.post("/api/session", json={"token": "   "}).status_code == 400
    assert cliente.post("/api/session", json={"token": 42}).status_code == 400
    assert cliente.post("/api/session", content=b"no soy json").status_code == 400


def test_get_sin_sesion_es_401(cliente: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _secreto(monkeypatch)
    assert cliente.get("/api/session").status_code == 401


def test_delete_borra_la_cookie(cliente: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _secreto(monkeypatch)
    cliente.post("/api/session", json={"token": "tok3n-real"})
    assert cliente.get("/api/session").status_code == 200

    response = cliente.delete("/api/session")
    assert response.status_code == 204
    assert cliente.get("/api/session").status_code == 401


def test_delete_sin_sesion_tambien_es_204(
    cliente: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Salir dos veces no es un error: repetir el `DELETE` no deja estado raro."""
    _secreto(monkeypatch)
    assert cliente.delete("/api/session").status_code == 204


def test_el_rol_es_el_del_despliegue_no_el_del_cliente(
    cliente: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quien llama no puede reclamar un rol: el token identifica, el despliegue autoriza."""
    _secreto(monkeypatch, "lector-token")
    monkeypatch.setattr(get_settings(), "panel_session_role", "lector")
    monkeypatch.setattr(get_settings(), "panel_session_user", "ana")

    response = cliente.post("/api/session", json={"token": "lector-token", "role": "operador"})

    assert response.status_code == 200
    assert response.json() == {"user": "ana", "role": "lector"}


def test_lector_no_puede_comandar(cliente: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """La cookie de un `lector` abre lectura y recibe `403` en un comando.

    `test_web.py` ya cubre esto con la cabecera; aqui se comprueba por la via
    nueva (cookie) para que el login no abra un hueco de permisos.
    """
    _secreto(monkeypatch, "solo-mirar")
    monkeypatch.setattr(get_settings(), "panel_session_role", "lector")
    assert cliente.post("/api/session", json={"token": "solo-mirar"}).status_code == 200

    response = cliente.post("/api/reviews/cqm/repo:1:abc:7/cancel")

    assert response.status_code == 403
    assert "lector" in response.text


def test_secure_solo_con_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "panel_public_url", "https://panel.example")
    assert cookie_secure(get_settings()) is True

    monkeypatch.setattr(get_settings(), "panel_public_url", "http://localhost:8090")
    assert cookie_secure(get_settings()) is False


def test_la_cookie_lleva_secure_cuando_el_despliegue_es_https(
    cliente: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _secreto(monkeypatch)
    monkeypatch.setattr(get_settings(), "panel_public_url", "https://panel.example")
    response = cliente.post("/api/session", json={"token": "tok3n-real"})
    assert "secure" in response.headers["set-cookie"].lower()
