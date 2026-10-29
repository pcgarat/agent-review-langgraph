"""El panel elige Claude, DeepSeek o GLM; el runner lo aplica en la siguiente pasada."""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_review.adapters.claude_cli import claude_argv
from agent_review.adapters.claude_client import HttpClaudeRunner
from agent_review.claude_backend import (
    child_gateway_env,
    gateway_domain,
    gateway_healthy,
    load_gateway_token,
    read_choice,
    resolve_backend,
    write_choice,
)
from agent_review.claude_runner import main as runner_main
from agent_review.claude_runner.main import (
    build_app,
    runner_sandbox_settings,
    write_runner_settings,
)
from agent_review.claude_session import CHILD_TOKEN_ENV, write_session
from agent_review.domain.models import ReviewRequest
from agent_review.ports.claude import ClaudeClusterLimited
from agent_review.queue import InMemoryReviewQueue
from agent_review.settings import Settings
from agent_review.web.main import ClaudeLogin, app


def _login(tmp_path: Path, queue: InMemoryReviewQueue | None = None) -> ClaudeLogin:
    return ClaudeLogin(
        path=tmp_path / "session.json",
        queue=queue if queue is not None else InMemoryReviewQueue(),
        ttl_s=60,
    )


def _request(head_sha: str) -> ReviewRequest:
    return ReviewRequest(repo="acme/app", pr_id=1, head_sha=head_sha, pipeline_id="7")


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


class _Captura:
    """Sustituye al subproceso y se queda con el argv y el entorno de esa pasada."""

    kwargs: dict[str, object] = {}

    def __init__(self, **kwargs: object) -> None:
        _Captura.kwargs = kwargs

    def run(self, **kwargs: object) -> str:
        _Captura.kwargs = {**_Captura.kwargs, "run": kwargs}
        return "[]"


def _settings_de_pasada(tmp_path: Path, **extra: object) -> Settings:
    return Settings(
        _env_file=None,
        claude_session_path=str(tmp_path / "session" / "session.json"),
        claude_workspace_root=str(tmp_path / "ws"),
        claude_bin="claude",
        claude_runner_token="",
        **extra,
    )


def _post_run(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    monkeypatch.setattr(runner_main, "prepare_checkout", lambda *args, **kwargs: "fork")
    monkeypatch.setattr(runner_main, "ensure_bundled_skill", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner_main, "SubprocessClaudeRunner", _Captura)
    _Captura.kwargs = {}
    with TestClient(build_app(settings)) as client:
        response = client.post(
            "/run",
            json={"repo": "acme/app", "pr_id": 1, "head_sha": "abc", "base_sha": "def"},
        )
    assert response.status_code == 200, response.text
    return _Captura.kwargs


def test_anthropic_conserva_el_argv_y_el_entorno_del_hijo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin elección, el argv y el entorno son los de hoy: OAuth y un solo dominio."""
    write_session(tmp_path / "session" / "session.json", "oauth-token-anthropic-1234", ttl_s=3600)
    visto = _post_run(_settings_de_pasada(tmp_path, claude_upstream="anthropic"), monkeypatch)
    assert visto["argv_prefix"] == ["claude"]
    assert visto["quota_is_cluster"] is False
    run = visto["run"]
    assert isinstance(run, dict)
    # El CLI diffea contra el merge-base del checkout, no contra la punta de destino.
    assert run["base_sha"] == "fork"
    argv = claude_argv(visto["argv_prefix"], max_turns=visto["max_turns"], base_sha=run["base_sha"])
    assert "--model" not in argv
    assert "ANTHROPIC" not in " ".join(str(item) for item in argv)
    extra = visto["extra_env"]
    assert isinstance(extra, dict)
    assert set(extra) == {
        CHILD_TOKEN_ENV,
        "CLAUDE_CONFIG_DIR",
        "HOME",
        "TMPDIR",
        "DISABLE_UPDATES",
    }
    assert extra[CHILD_TOKEN_ENV] == "oauth-token-anthropic-1234"
    assert "ANTHROPIC_BASE_URL" not in extra
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" not in extra
    jaula = json.loads((tmp_path / "claude-config" / "settings.json").read_text(encoding="utf-8"))
    assert jaula["sandbox"]["network"]["allowedDomains"] == ["api.anthropic.com"]


def test_el_cluster_pasa_el_gateway_y_un_429_no_es_cupo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_session(tmp_path / "session" / "session.json", "oauth-token-anthropic-1234", ttl_s=3600)
    write_choice(tmp_path / "session" / "backend.json", "glm5.3-flash")
    settings = _settings_de_pasada(
        tmp_path,
        claude_upstream="anthropic",
        claude_gateway_url="http://nan-gateway:4000",
        claude_gateway_token="sk-gateway-local-1234",
    )
    visto = _post_run(settings, monkeypatch)
    assert visto["argv_prefix"] == ["claude"]
    assert visto["quota_is_cluster"] is True
    extra = visto["extra_env"]
    assert isinstance(extra, dict)
    assert extra["ANTHROPIC_BASE_URL"] == "http://nan-gateway:4000"
    assert extra["ANTHROPIC_MODEL"] == "glm5.3-flash"
    assert extra["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert CHILD_TOKEN_ENV not in extra
    jaula = json.loads((tmp_path / "claude-config" / "settings.json").read_text(encoding="utf-8"))
    assert jaula["sandbox"]["network"]["allowedDomains"] == ["nan-gateway"]


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
    assert response.json() == {"active": False, "reason": "session"}


def test_pasar_al_cluster_suelta_la_espera_de_cupo(tmp_path: Path) -> None:
    """El enfriamiento protege la suscripción de Anthropic, no al clúster."""
    queue = InMemoryReviewQueue()
    queue.park(_request("agoto"), reason="quota")
    queue.park(_request("espera"), reason="quota-wait")
    queue.park(_request("sesion"), reason="session")
    queue.claim_notice("quota", ttl_s=900)
    try:
        with TestClient(app) as client:
            app.state.claude_backend_path = tmp_path / "backend.json"
            app.state.claude_login = _login(tmp_path, queue)
            saved = client.post(
                "/api/claude/backend",
                headers={"X-Panel-Session": "ana:operador"},
                json={"choice": "deepseek-v4-flash"},
            )
        assert saved.status_code == 200
    finally:
        del app.state.claude_backend_path
        del app.state.claude_login
    assert not queue.notice_held("quota")
    assert [request.head_sha for request in queue.parked()] == ["agoto"]


def test_volver_a_claude_no_suelta_el_cupo(tmp_path: Path) -> None:
    queue = InMemoryReviewQueue()
    queue.park(_request("espera"), reason="quota-wait")
    queue.claim_notice("quota", ttl_s=900)
    try:
        with TestClient(app) as client:
            app.state.claude_backend_path = tmp_path / "backend.json"
            app.state.claude_login = _login(tmp_path, queue)
            client.post(
                "/api/claude/backend",
                headers={"X-Panel-Session": "ana:operador"},
                json={"choice": "claude"},
            )
    finally:
        del app.state.claude_backend_path
        del app.state.claude_login
    assert queue.notice_held("quota")
    assert [request.head_sha for request in queue.parked()] == ["espera"]


@pytest.mark.parametrize(
    "body",
    [b'{"choice": "gpt-5"}', b'["glm5.3-flash"]', b"no es json", b'{"choice": 3}'],
)
def test_un_destino_invalido_responde_400(tmp_path: Path, body: bytes) -> None:
    try:
        with TestClient(app) as client:
            app.state.claude_backend_path = tmp_path / "backend.json"
            app.state.claude_login = _login(tmp_path)
            response = client.post(
                "/api/claude/backend",
                headers={"X-Panel-Session": "ana:operador", "Content-Type": "application/json"},
                content=body,
            )
    finally:
        del app.state.claude_backend_path
        del app.state.claude_login
    assert response.status_code == 400
    assert not (tmp_path / "backend.json").exists()


@pytest.mark.parametrize("raw", ["{", "[]", '{"choice": "x"}', '{"choice": 1}'])
def test_un_fichero_de_destino_roto_no_elige(tmp_path: Path, raw: str) -> None:
    path = tmp_path / "backend.json"
    path.write_text(raw, encoding="utf-8")
    assert read_choice(path) is None


class _Health(BaseHTTPRequestHandler):
    status_code = 200

    def do_GET(self) -> None:
        self.send_response(self.status_code)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        del args


@pytest.fixture
def health_server() -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_gateway_healthy_solo_con_2xx(
    health_server: ThreadingHTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"http://127.0.0.1:{health_server.server_address[1]}/"
    assert gateway_healthy(url) is True
    monkeypatch.setattr(_Health, "status_code", 500)
    assert gateway_healthy(url) is False
    assert gateway_healthy(f"http://127.0.0.1:{_free_port()}") is False
    assert gateway_healthy("") is False


@pytest.mark.parametrize("healthy", [True, False])
def test_con_el_cluster_la_sesion_es_la_salud_del_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, healthy: bool
) -> None:
    """Sin token OAuth: con GLM elegido, /session solo mira el gateway."""
    write_choice(tmp_path / "backend.json", "glm5.3-flash")
    vistos: list[str] = []

    def _health(url: str) -> bool:
        vistos.append(url)
        return healthy

    monkeypatch.setattr(runner_main, "gateway_healthy", _health)
    settings = Settings(
        _env_file=None,
        claude_session_path=str(tmp_path / "session.json"),
        claude_gateway_url="http://nan-gateway:4000",
    )
    with TestClient(build_app(settings)) as client:
        response = client.get("/session")
    assert response.json() == {"active": healthy, "reason": None if healthy else "gateway"}
    assert vistos == ["http://nan-gateway:4000"]


@pytest.mark.parametrize(
    ("url", "token"),
    [("", "sk-gateway"), ("http://nan-gateway:4000", ""), ("nan-gateway:4000", "sk-gateway")],
)
def test_el_cluster_sin_url_o_token_no_lanza_el_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str, token: str
) -> None:
    """Fallo de configuración: 502 antes del checkout, nunca un bearer vacío."""
    write_choice(tmp_path / "session" / "backend.json", "deepseek-v4-flash")
    settings = _settings_de_pasada(
        tmp_path,
        claude_gateway_url=url,
        claude_gateway_token=token,
        claude_gateway_token_file="",
    )
    lanzado: list[object] = []
    monkeypatch.setattr(runner_main, "prepare_checkout", lambda *a, **k: lanzado.append(a))
    monkeypatch.setattr(runner_main, "SubprocessClaudeRunner", _Captura)
    _Captura.kwargs = {}
    with TestClient(build_app(settings)) as client:
        response = client.post(
            "/run",
            json={"repo": "acme/app", "pr_id": 1, "head_sha": "abc", "base_sha": "def"},
        )
    assert response.status_code == 502
    assert lanzado == []
    assert _Captura.kwargs == {}


def test_con_anthropic_el_master_key_se_redacta_y_la_jaula_no_lo_lee(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El volumen del token está montado aunque el destino sea Anthropic."""
    write_session(tmp_path / "session" / "session.json", "oauth-token-anthropic-1234", ttl_s=3600)
    token_file = tmp_path / "gateway" / "token"
    token_file.parent.mkdir()
    token_file.write_text("sk-gateway-master-5678\n", encoding="utf-8")
    settings = _settings_de_pasada(
        tmp_path,
        claude_upstream="anthropic",
        claude_gateway_token_file=str(token_file),
    )
    visto = _post_run(settings, monkeypatch)
    redact = visto["redact"]
    assert isinstance(redact, list)
    assert "sk-gateway-master-5678" in redact
    extra = visto["extra_env"]
    assert isinstance(extra, dict)
    assert "sk-gateway-master-5678" not in extra.values()
    jaula = json.loads((tmp_path / "claude-config" / "settings.json").read_text(encoding="utf-8"))
    assert str(token_file) in jaula["sandbox"]["filesystem"]["denyRead"]


def test_volver_a_claude_suelta_lo_que_esperaba_al_cluster(tmp_path: Path) -> None:
    queue = InMemoryReviewQueue()
    queue.park(_request("caido"), reason="gateway")
    queue.park(_request("limitado"), reason="cluster-wait")
    queue.park(_request("agoto"), reason="quota")
    queue.claim_notice("cluster-wait", ttl_s=300)
    try:
        with TestClient(app) as client:
            app.state.claude_backend_path = tmp_path / "backend.json"
            app.state.claude_login = _login(tmp_path, queue)
            client.post(
                "/api/claude/backend",
                headers={"X-Panel-Session": "ana:operador"},
                json={"choice": "claude"},
            )
    finally:
        del app.state.claude_backend_path
        del app.state.claude_login
    assert not queue.notice_held("cluster-wait")
    assert [request.head_sha for request in queue.parked()] == ["agoto"]


def test_el_runner_responde_503_si_el_cluster_limita(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """503 y no 429: el worker no debe confundirlo con el cupo de Anthropic."""

    class _Limitado(_Captura):
        def run(self, **kwargs: object) -> str:
            del kwargs
            raise ClaudeClusterLimited("el cluster limito la llamada")

    write_choice(tmp_path / "session" / "backend.json", "glm5.3-flash")
    settings = _settings_de_pasada(
        tmp_path,
        claude_gateway_url="http://nan-gateway:4000",
        claude_gateway_token="sk-gateway-local-1234",
    )
    monkeypatch.setattr(runner_main, "prepare_checkout", lambda *args, **kwargs: "fork")
    monkeypatch.setattr(runner_main, "ensure_bundled_skill", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner_main, "SubprocessClaudeRunner", _Limitado)
    with TestClient(build_app(settings)) as client:
        response = client.post(
            "/run",
            json={"repo": "acme/app", "pr_id": 1, "head_sha": "abc", "base_sha": "def"},
        )
    assert response.status_code == 503


def test_el_cliente_lee_el_503_como_limite_del_cluster() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503, json={"detail": "el cluster limito la llamada"})

    runner = HttpClaudeRunner(
        url="http://claude-runner",
        token="",
        timeout_s=1,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ClaudeClusterLimited):
        runner.run(repo="acme/app", pr_id=1, head_sha="abc", base_sha="def")
