from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_COMPOSE = _ROOT / "docker-compose.yml"
_ENV_EXAMPLE = _ROOT / ".env.example"


def _compose() -> dict:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


def test_compose_es_yaml_valido_con_servicios_esperados() -> None:
    services = _compose()["services"]
    for name in ("api", "worker", "fix-worker", "postgres", "redis", "sandbox-runner"):
        assert name in services, f"falta el servicio {name}"


def test_el_worker_de_fixes_no_monta_socket_ni_publica_puertos() -> None:
    """Fase 3-E7: el fix-worker vive aislado; no crea contenedores ni publica."""
    service = _compose()["services"]["fix-worker"]
    volumes = service.get("volumes", [])
    assert not any("docker.sock" in str(volume) for volume in volumes)
    assert "ports" not in service, "el fix-worker no expone nada al host"


def test_el_worker_de_fixes_no_tiene_credenciales_de_vcs_ni_postgres() -> None:
    """Frontera de seguridad: solo Redis + token del runner; nunca publica.

    Sin `env_file`: el `.env` del despliegue lleva todos los secretos, y bastaria
    con heredarlo para que un fix-worker comprometido tuviera la pluma del PR.
    """
    service = _compose()["services"]["fix-worker"]
    assert "env_file" not in service, "el fix-worker no hereda el .env entero"
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
        assert secret not in env, f"el fix-worker no debe recibir {secret}"
    assert "SANDBOX_RUNNER_TOKEN" in env, "necesita el token compartido con el runner"


def test_el_worker_no_monta_ningun_socket_de_docker() -> None:
    """Criterio 7: un worker comprometido no puede crear contenedores."""
    worker = _compose()["services"]["worker"]
    volumes = worker.get("volumes", [])
    assert not any("docker.sock" in str(volume) for volume in volumes)
    assert not any(str(volume).endswith("docker.sock") for volume in volumes)


def test_el_runner_no_publica_puertos() -> None:
    """El runner solo es alcanzable por el worker en la red interna de compose."""
    runner = _compose()["services"]["sandbox-runner"]
    assert "ports" not in runner, "el runner no debe exponer puertos al host"


def test_el_runner_monta_config_en_solo_lectura() -> None:
    runner = _compose()["services"]["sandbox-runner"]
    config = [v for v in runner.get("volumes", []) if "config" in str(v)]
    assert config, "el runner necesita el perfil del repo"
    assert all(str(volume).endswith(":ro") for volume in config)


def test_los_secretos_del_sandbox_estan_declarados_en_env_example() -> None:
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    for key in ("SANDBOX_GIT_TOKEN", "SANDBOX_REGISTRY_TOKEN", "SANDBOX_RUNNER_TOKEN"):
        assert f"{key}=" in text, f"falta {key} en .env.example"


def test_el_servicio_web_solo_arranca_con_el_perfil_panel() -> None:
    """019 R7: el panel es un servicio mas, opt-in; el arranque de siempre no cambia."""
    web = _compose()["services"]["web"]
    assert web.get("profiles") == ["panel"], "el `web` solo debe existir con el perfil `panel`"
    assert web["command"] == "python -m agent_review.web.main"


def test_sin_el_perfil_panel_no_se_levanta_ningun_servicio_nuevo() -> None:
    """Un `docker compose up` sin perfil no arranca el panel (los servicios con
    `profiles` quedan fuera); los servicios de siempre siguen sin perfil."""
    services = _compose()["services"]
    sin_perfil = [name for name, service in services.items() if not service.get("profiles")]
    assert "web" not in sin_perfil
    for name in ("api", "worker", "fix-worker", "postgres", "redis", "sandbox-runner"):
        assert name in sin_perfil, f"{name} debe seguir arrancando sin perfil"


def test_el_web_depende_de_redis_y_postgres_y_esta_en_la_red() -> None:
    web = _compose()["services"]["web"]
    assert "redis" in web.get("depends_on", {})
    assert "postgres" in web.get("depends_on", {})
    assert "reviews" in web.get("networks", [])


def test_el_web_no_hereda_el_env_entero_ni_credenciales() -> None:
    """019 §5.8: el panel solo lee estado; no recibe credencial de VCS ni clave del LLM."""
    web = _compose()["services"]["web"]
    assert "env_file" not in web, "el panel no puede heredar el .env entero"
    env = web.get("environment", {})
    for secret in (
        "TWG_BBC_TOKEN",
        "TWG_TOKEN",
        "GITHUB_TOKEN",
        "REVIEWS_DECISION_TOKEN",
        "TOOLING_WEBHOOK_SECRET",
        "LLM_API_KEY",
        "SANDBOX_GIT_TOKEN",
        "SANDBOX_RUNNER_TOKEN",
    ):
        assert secret not in env, f"el panel no debe recibir {secret}"
    assert "PANEL_SESSION_SECRET" in env, "necesita su propia sesion"


def test_el_web_recibe_la_url_y_el_token_de_solo_lectura_de_la_consola() -> None:
    """Sin ellos, la consola en vivo del panel siempre responde `404` (OP24)."""
    env = _compose()["services"]["web"].get("environment", {})
    assert "SANDBOX_RUNNER_URL" in env
    assert "SANDBOX_CONSOLE_TOKEN" in env


def test_las_variables_del_panel_estan_en_env_example() -> None:
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    for key in (
        "PANEL_ENABLED",
        "PANEL_PORT",
        "PANEL_SESSION_SECRET",
        "SANDBOX_CONSOLE_BUFFER_LINES",
        "SANDBOX_CONSOLE_TOKEN",
        "CANCEL_TTL_S",
    ):
        assert f"{key}=" in text, f"falta {key} en .env.example"


def test_el_panel_monta_config_y_recibe_los_diales_que_pinta() -> None:
    """027: sin `config/` no puede validar el techo; sin los diales, el inicial miente."""
    web = _compose()["services"]["web"]
    config = [v for v in web.get("volumes", []) if "config" in str(v)]
    assert config, "el panel necesita autonomy.yaml para validar MAX_AUTONOMY_OVERRIDE"
    assert all(str(volume).endswith(":ro") for volume in config), "solo lectura"

    from agent_review.panel_config import catalog

    worker_env = _compose()["services"]["worker"]
    web_env = web.get("environment", {})
    # El catalogo entero, no una lista a mano: un dial nuevo sin su variable en el
    # `web` pintaria el default del codigo mientras el worker usa el del `.env`.
    keys = [item.name.upper() for item in catalog()]
    keys += ["PANEL_CONFIG_LOCK", "AUTONOMY_CONFIG"]
    for key in keys:
        assert key in web_env, f"el panel tiene que resolver el mismo {key} que el worker"
    # El worker los toma del `.env` completo; el panel los interpola del host.
    assert "env_file" in worker_env
