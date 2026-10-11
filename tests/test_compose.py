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
    for name in ("api", "worker", "postgres", "redis", "sandbox-runner"):
        assert name in services, f"falta el servicio {name}"


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
