"""El proceso `web` sirve la SPA (iniciativa 029, bloque R: R4 y R5).

Dos contratos, y los dos son de rutas publicadas:

- **R4:** cada ruta del inventario de `spec.md` §3.3 entrega el armazón de la SPA
  (el `index.html` del build). Hay enlaces vivos en comentarios de pull requests y
  en avisos de Teams, así que una ruta que deje de responder rompe algo fuera del
  código.
- **R5:** `GET /api/inexistente` sigue siendo `404`. El *fallback* de la SPA no
  puede convertir un endpoint que no existe en un `200` con HTML.

Se prueba la app real (`web.main:app`), con un build de mentira: el `dist` no se
versiona y en CI no existe, así que el test no puede depender de él.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_review.web import spa
from agent_review.web.main import app

client = TestClient(app)

_REVIEW_KEY = "cqm/emotivoo-v2:42:abc:7"
# Las rutas de §3.3 que sostienen enlaces ya publicados, más `/login` (029, P6).
_ROUTES = (
    "/",
    f"/review/{_REVIEW_KEY}",
    "/bandeja",
    "/history",
    "/config",
    "/claude",
    "/login",
)
_INDEX = '<!doctype html><html lang="es"><div id="root"></div></html>'


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Un `dist` mínimo, resuelto en cada petición como en producción."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(_INDEX, encoding="utf-8")
    (dist / "assets" / "panel.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setattr(spa, "spa_directory", lambda: dist)
    return dist


# --- R4: las rutas publicadas sirven el armazón ----------------------------------


@pytest.mark.parametrize("route", _ROUTES)
def test_cada_ruta_publicada_sirve_el_index_de_la_spa(route: str, built: Path) -> None:
    response = client.get(route)

    assert response.status_code == 200
    assert response.text == _INDEX
    assert response.headers["content-type"].startswith("text/html")


def test_la_ficha_resuelve_una_review_key_con_barras_y_dos_puntos(built: Path) -> None:
    """La clave no cabe en un segmento: el comodín la recibe entera."""
    key = "cqm/emotivoo-v2:42:8d789e494938abcdef:1"

    assert client.get(f"/review/{key}").text == _INDEX


def test_sin_build_el_proceso_avisa_y_no_finge_un_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un despliegue sin `make frontend-build` no tiene panel, y lo dice."""
    monkeypatch.setattr(spa, "spa_directory", lambda: None)

    response = client.get("/")

    assert response.status_code == 503
    assert "no está construido" in response.json()["detail"]


# --- R5: el fallback no se come los 404 de la API --------------------------------


def test_una_ruta_de_api_que_no_existe_sigue_siendo_404(built: Path) -> None:
    response = client.get("/api/inexistente")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.text != _INDEX


def test_un_comando_a_un_endpoint_que_no_existe_es_404_y_no_405(built: Path) -> None:
    """El comodín de la SPA solo acepta `GET`: sin el de backend sería un `405`."""
    assert client.post("/api/inexistente", json={}).status_code == 404


def test_healthz_no_lo_captura_el_fallback(built: Path) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/healthz/otra").status_code == 404


def test_un_asset_que_falta_es_404_y_no_el_html(built: Path) -> None:
    """Devolver `index.html` con `200` haría creer que el fichero existe."""
    assert client.get("/assets/no-existe.js").status_code == 404


def test_la_ruta_publicada_manda_sobre_el_fallback(
    built: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un endpoint real de `/api` sigue respondiendo lo suyo, no el armazón."""
    from agent_review.settings import get_settings

    monkeypatch.setattr(get_settings(), "panel_session_secret", "un-secreto-real")

    response = client.get("/api/session")

    assert response.status_code == 401
    assert response.json()["detail"]


# --- Criterio 8: el armazón no trae HTML a mano ni assets de otro origen -----------

_FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


def test_la_spa_no_inyecta_html_ni_carga_assets_de_otro_origen() -> None:
    """P13 y §2: React escapa por defecto y todo asset sale del propio origen.

    Se mira el código fuente y no el bundle: el bundle se genera en el CI y este
    test tiene que valer sin haber construido `frontend/`.
    """
    fuentes = [*_FRONTEND.glob("src/**/*.ts"), *_FRONTEND.glob("src/**/*.tsx")]
    assert fuentes, "no se encontraron las fuentes de la SPA"
    for source in fuentes:
        text = source.read_text(encoding="utf-8")
        assert "dangerouslySetInnerHTML" not in text, f"{source} inyecta HTML a mano"
    index = (_FRONTEND / "index.html").read_text(encoding="utf-8")
    for tag in ("<script", "<link"):
        for line in index.splitlines():
            if tag in line:
                assert "http://" not in line and "https://" not in line, (
                    f"index.html carga un asset de otro origen: {line.strip()}"
                )
