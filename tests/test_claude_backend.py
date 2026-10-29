"""El panel elige Claude, DeepSeek o GLM; el runner lo aplica en la siguiente pasada."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from agent_review.claude_backend import (
    child_gateway_env,
    gateway_domain,
    load_gateway_token,
    read_choice,
    resolve_backend,
    write_choice,
)
from agent_review.claude_runner.main import (
    build_app,
    runner_sandbox_settings,
    write_runner_settings,
)
from agent_review.queue import InMemoryReviewQueue
from agent_review.settings import Settings
from agent_review.web.main import ClaudeLogin, app


def _login(tmp_path: Path) -> ClaudeLogin:
    return ClaudeLogin(path=tmp_path / "session.json", queue=InMemoryReviewQueue(), ttl_s=60)


def test_sin_eleccion_el_entorno_anthropic_no_cambia() -> None:
    settings = Settings(_env_file=None, claude_upstream="anthropic")
    backend = resolve_backend(None, settings)
    assert backend.kind == "anthropic"
    assert backend.model == ""


def test_el_panel_puede_elegir_glm_aunque_el_entorno_sea_anthropic(tmp_path: Path) -> None:
    path = tmp_path / "backend.json"
    write_choice(path, "glm5.3-flash")
    assert read_choice(path) == "glm5.3-flash"
    settings = Settings(_env_file=None, claude_upstream="anthropic")
    backend = resolve_backend(read_choice(path), settings)
    assert backend.kind == "gateway"
    assert backend.model == "glm5.3-flash"


def test_deepseek_es_el_modelo_de_entorno_cuando_nadie_elige() -> None:
    settings = Settings(
        _env_file=None,
        claude_upstream="gateway",
        claude_gateway_model="deepseek-v4-flash",
    )
    backend = resolve_backend(None, settings)
    assert backend.kind == "gateway"
    assert backend.model == "deepseek-v4-flash"


def test_el_hijo_del_cluster_no_recibe_el_token_oauth() -> None:
    env = child_gateway_env(
        url="http://nan-gateway:4000",
        token="sk-local",
        model="glm5.3-flash",
    )
    assert env["ANTHROPIC_BASE_URL"] == "http://nan-gateway:4000"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-local"
    assert env["ANTHROPIC_MODEL"] == "glm5.3-flash"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "glm5.3-flash"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert gateway_domain("http://nan-gateway:4000") == "nan-gateway"


def test_la_jaula_del_cluster_solo_nombra_al_gateway(tmp_path: Path) -> None:
    path = write_runner_settings(
        tmp_path / "config",
        workspace_root=tmp_path / "checkout",
        allowed_domains=["nan-gateway"],
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["sandbox"]["network"]["allowedDomains"] == ["nan-gateway"]
    assert runner_sandbox_settings(workspace_root=tmp_path)["sandbox"]["network"][
        "allowedDomains"
    ] == ["api.anthropic.com"]


def test_el_token_del_gateway_sale_del_fichero_si_el_entorno_esta_vacio(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text("sk-generado\n", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        claude_gateway_token="",
        claude_gateway_token_file=str(path),
    )
    assert load_gateway_token(settings) == "sk-generado"
    override = Settings(
        _env_file=None,
        claude_gateway_token="sk-fijo",
        claude_gateway_token_file=str(path),
    )
    assert load_gateway_token(override) == "sk-fijo"


def test_un_modelo_desconocido_no_se_guarda(tmp_path: Path) -> None:
    path = tmp_path / "backend.json"
    try:
        write_choice(path, "claude-sonnet")
    except ValueError:
        assert not path.exists()
    else:
        raise AssertionError("aceptó un modelo que no está en la lista")


def test_el_panel_guarda_glm_y_lo_lee(tmp_path: Path) -> None:
    try:
        with TestClient(app) as client:
            app.state.claude_backend_path = tmp_path / "backend.json"
            app.state.claude_login = _login(tmp_path)
            denied = client.post(
                "/api/claude/backend",
                headers={"X-Panel-Session": "ana:lector"},
                json={"choice": "glm5.3-flash"},
            )
            saved = client.post(
                "/api/claude/backend",
                headers={"X-Panel-Session": "ana:operador"},
                json={"choice": "glm5.3-flash"},
            )
            read = client.get(
                "/api/claude/backend",
                headers={"X-Panel-Session": "ana:operador"},
            )
        assert denied.status_code == 403
        assert saved.status_code == 200
        assert saved.json()["effective"] == "glm5.3-flash"
        assert saved.json()["needs_session"] is False
        assert read.json()["choice"] == "glm5.3-flash"
    finally:
        del app.state.claude_backend_path
        del app.state.claude_login


def test_volver_a_claude_pide_sesion(tmp_path: Path) -> None:
    try:
        with TestClient(app) as client:
            app.state.claude_backend_path = tmp_path / "backend.json"
            app.state.claude_login = _login(tmp_path)
            client.post(
                "/api/claude/backend",
                headers={"X-Panel-Session": "ana:operador"},
                json={"choice": "claude"},
            )
            read = client.get(
                "/api/claude/backend",
                headers={"X-Panel-Session": "ana:operador"},
            )
        assert read.json()["needs_session"] is True
        assert read.json()["effective"] == "claude"
    finally:
        del app.state.claude_backend_path
        del app.state.claude_login


def test_el_runner_sin_fichero_sigue_en_anthropic(tmp_path: Path) -> None:
    """Paridad: sin elección y sin CLAUDE_UPSTREAM, la jaula es la de hoy."""
    settings = Settings(
        _env_file=None,
        claude_session_path=str(tmp_path / "session.json"),
        claude_upstream="anthropic",
    )
    app_runner = build_app(settings)
    with TestClient(app_runner) as client:
        response = client.get("/session")
    assert response.status_code == 200
    assert response.json() == {"active": False}
