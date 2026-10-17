"""T6 (iniciativa 013): arranque del servicio `teams-bot`, secretos y Compose.

Cubre lo que se puede comprobar sin Azure ni Teams real: el contrato de secretos
(fail-closed en produccion), el adaptador del SDK (que envuelve la tarjeta y pasa
el `service_url`, hallazgos del spike), el proceso (`/healthz` con bearer, la `App`
que monta `build_app` y `serve_bot`) y el servicio en Compose.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from agent_review.settings import (
    Settings,
    require_teams_bot_secrets,
    teams_bot_insecure_secrets,
)
from agent_review.teams_bot.sdk_client import SDKChannelClient, _to_activity

_ROOT = Path(__file__).resolve().parents[1]
_COMPOSE = _ROOT / "docker-compose.yml"
_ENV_EXAMPLE = _ROOT / ".env.example"


# --- Secretos del bot (fail-closed en produccion) -----------------------------


def _prod(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "environment": "production",
        "teams_enabled": True,
        "teams_bot_id": "bot-id",
        "teams_bot_tenant_id": "tenant-id",
        "teams_bot_token": "secreto-real",
        "teams_bot_shared_secret": "bearer-real",
    }
    base.update(overrides)
    return Settings(**base)


def test_sin_canal_activo_el_bot_no_exige_secretos() -> None:
    """Con TEAMS_ENABLED=false el bot no arranca; su ausencia no es un fallo."""
    assert teams_bot_insecure_secrets(Settings(teams_enabled=False)) == []


def test_el_contrato_completo_arranca() -> None:
    require_teams_bot_secrets(_prod())


@pytest.mark.parametrize(
    ("field", "missing"),
    [
        ("teams_bot_id", "TEAMS_BOT_ID"),
        ("teams_bot_tenant_id", "TEAMS_BOT_TENANT_ID"),
        ("teams_bot_token", "TEAMS_BOT_TOKEN"),
    ],
)
def test_falta_la_identidad_del_bot_en_produccion(field: str, missing: str) -> None:
    with pytest.raises(RuntimeError, match=missing):
        require_teams_bot_secrets(_prod(**{field: ""}))


def test_el_bearer_del_ingress_no_puede_quedarse_en_el_valor_de_ejemplo() -> None:
    with pytest.raises(RuntimeError, match="TEAMS_BOT_SHARED_SECRET"):
        require_teams_bot_secrets(_prod(teams_bot_shared_secret="change-me"))


def test_el_bearer_del_ingress_vacio_tambien_es_inseguro() -> None:
    """Vacio (variable sin asignar) no puede dejar el ingress sin autenticacion."""
    with pytest.raises(RuntimeError, match="TEAMS_BOT_SHARED_SECRET"):
        require_teams_bot_secrets(_prod(teams_bot_shared_secret=""))


def test_el_token_del_bot_en_su_valor_de_ejemplo_es_inseguro() -> None:
    with pytest.raises(RuntimeError, match="TEAMS_BOT_TOKEN"):
        require_teams_bot_secrets(_prod(teams_bot_token="change-me"))


def test_en_desarrollo_no_se_exige_el_contrato_del_bot() -> None:
    """En local se permite arrancar sin la identidad (el SDK usa modo no-auth)."""
    require_teams_bot_secrets(Settings(environment="development", teams_enabled=True))


# --- Adaptador del SDK (payload -> actividad) ---------------------------------


def test_un_texto_se_convierte_en_una_actividad_de_mensaje() -> None:
    activity = _to_activity("hola")
    assert activity.text == "hola"


def test_una_tarjeta_se_envuelve_con_card_attachment() -> None:
    """Hallazgo del spike: un `AdaptiveCardAttachment` suelto no valida."""
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5",
        "body": [{"type": "TextBlock", "text": "hola"}],
        "actions": [{"type": "Action.Execute", "title": "x", "verb": "v", "data": {"a": "b"}}],
    }
    activity = _to_activity(card)

    assert activity.attachments is not None and len(activity.attachments) == 1
    assert activity.attachments[0].content_type == "application/vnd.microsoft.card.adaptive"


class _Sent:
    def __init__(self, message_id: str | None) -> None:
        self.id = message_id


class _FakeApp:
    """App del SDK en memoria: registra llamadas y permite fijar el id devuelto."""

    def __init__(self, *, message_id: str | None = "77") -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.activities: list[Any] = []
        self._message_id = message_id

    async def send(
        self, conversation_id: str, activity: Any, *, service_url: str | None = None
    ) -> _Sent:
        self.calls.append(("send", conversation_id, service_url))
        self.activities.append(activity)
        return _Sent(self._message_id)

    async def reply(
        self,
        conversation_id: str,
        root: str,
        activity: Any,
        *,
        service_url: str | None = None,
    ) -> _Sent:
        self.calls.append(("reply", conversation_id, service_url))
        return _Sent(self._message_id)


async def test_el_adaptador_pasa_el_service_url_en_toda_llamada_proactiva() -> None:
    """Hallazgo del spike: sin `service_url` el envio acaba en el Teams real (400)."""
    app = _FakeApp()
    client = SDKChannelClient(app)  # type: ignore[arg-type]

    assert await client.send("c", "hola", service_url="https://smba") == "77"
    assert await client.reply("c", "77", "hola", service_url="https://smba") == "77"

    assert app.calls == [("send", "c", "https://smba"), ("reply", "c", "https://smba")]


async def test_el_reemplazo_de_la_tarjeta_lleva_el_id_del_mensaje() -> None:
    app = _FakeApp()
    client = SDKChannelClient(app)  # type: ignore[arg-type]

    await client.update("c", "77", {"type": "AdaptiveCard", "version": "1.5", "body": []})

    # El reemplazo usa `send`, no `reply`: publica la actividad con el mismo id.
    assert app.calls == [("send", "c", None)]
    assert app.activities[0].id == "77", "sin el id, Teams publicaria otra tarjeta"


async def test_un_envio_normal_no_fija_id() -> None:
    app = _FakeApp()
    client = SDKChannelClient(app)  # type: ignore[arg-type]

    await client.send("c", {"type": "AdaptiveCard", "version": "1.5", "body": []})

    assert getattr(app.activities[0], "id", None) is None


# --- Proceso `teams-bot`: /healthz, build_app y serve_bot ---------------------


async def _bot_client(settings: Settings) -> httpx.AsyncClient:
    from agent_review.teams_bot.main import _register_healthz
    from agent_review.teams_bot.sdk_client import build_app

    app = build_app(settings)
    _register_healthz(app, secret=settings.teams_bot_shared_secret)
    await app.initialize()
    transport = httpx.ASGITransport(app=app.server.adapter.app)
    return httpx.AsyncClient(transport=transport, base_url="http://bot")


@pytest.mark.parametrize(
    ("headers", "status"),
    [({}, 401), ({"Authorization": "Bearer otro"}, 401), ({"Authorization": "Bearer s3"}, 200)],
)
async def test_healthz_exige_el_bearer_interno(headers: dict[str, str], status: int) -> None:
    """El endpoint es publico (lo expone el ingress): sin bearer no da informacion."""
    async with await _bot_client(Settings(teams_bot_shared_secret="s3")) as client:
        response = await client.get("/healthz", headers=headers)

    assert response.status_code == status


@pytest.mark.parametrize("bot_id", ["", "bot-id"])
async def test_la_app_de_build_app_rechaza_un_post_sin_jwt(bot_id: str) -> None:
    """Fail-closed: la `App` que arranca el proceso no acepta actividades sin JWT."""
    settings = Settings(teams_bot_id=bot_id, teams_bot_tenant_id="t" if bot_id else "")
    async with await _bot_client(settings) as client:
        response = await client.post("/api/messages", json={"type": "message", "text": "hola"})

    assert response.status_code == 401


async def test_serve_bot_propaga_el_fallo_del_consumidor() -> None:
    """Si una mitad cae, el proceso sale entero y Compose lo reinicia."""
    import asyncio

    from agent_review.teams_bot.main import serve_bot

    class _Service:
        async def run_forever(self) -> None:
            raise RuntimeError("consumidor caido")

    class _App:
        async def start(self, *, port: int) -> None:
            await asyncio.sleep(10)

    with pytest.raises(RuntimeError, match="consumidor caido"):
        await asyncio.wait_for(serve_bot(_Service(), _App(), port=0), timeout=2)  # type: ignore[arg-type]


# --- Compose y .env.example ---------------------------------------------------


def _compose() -> dict:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


def test_compose_declara_el_servicio_teams_bot() -> None:
    assert "teams-bot" in _compose()["services"]


def test_el_bot_no_monta_socket_ni_publica_puertos() -> None:
    """El messaging endpoint se expone por el ingress (T10), no por Compose."""
    service = _compose()["services"]["teams-bot"]
    assert "ports" not in service
    volumes = service.get("volumes", [])
    assert not any("docker.sock" in str(volume) for volume in volumes)


def test_el_bot_no_tiene_credenciales_de_vcs_ni_del_llm() -> None:
    """Frontera de seguridad: el bot solo publica; no revisa ni escribe el PR."""
    service = _compose()["services"]["teams-bot"]
    assert "env_file" not in service, "el bot no hereda el .env entero"
    env = service.get("environment", {})
    for secret in (
        "GITHUB_TOKEN",
        "TWG_BBC_TOKEN",
        "TWG_TOKEN",
        "REVIEWS_DECISION_TOKEN",
        "TOOLING_WEBHOOK_SECRET",
        "LLM_API_KEY",
        "SANDBOX_GIT_TOKEN",
        "SANDBOX_REGISTRY_TOKEN",
        "DATABASE_URL",
        "POSTGRES_PASSWORD",
    ):
        assert secret not in env, f"el bot no debe recibir {secret}"
    for expected in ("TEAMS_BOT_ID", "TEAMS_BOT_TOKEN", "TEAMS_BOT_SHARED_SECRET"):
        assert expected in env, f"el bot necesita {expected}"


def test_las_variables_del_bot_estan_en_env_example() -> None:
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    for key in (
        "TEAMS_BOT_ID=",
        "TEAMS_BOT_TENANT_ID=",
        "TEAMS_BOT_TOKEN=",
        "TEAMS_BOT_PORT=",
        "TEAMS_BOT_SHARED_SECRET=",
        "TEAMS_BOT_POLL_S=",
    ):
        assert key in text, f"falta {key} en .env.example"
