from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
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


# --- La imagen y el panel (029, R6) ------------------------------------------------


def _dockerfile() -> str:
    return (_ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_el_build_de_la_spa_va_en_una_etapa_con_node_y_la_imagen_final_no_lo_lleva() -> None:
    """P14: la etapa de Node es solo de build; ningun servicio gana Node."""
    text = _dockerfile()
    etapas = [
        line.split(" AS ", 1)[1]
        for line in text.splitlines()
        if line.startswith("FROM ") and " AS " in line
    ]
    assert "panel" in etapas, "falta la etapa que construye frontend/"
    assert "node:" in text.split("FROM python", 1)[0], "la etapa del panel usa una imagen con node"
    # La imagen final es `app` (y `claude-runner`, que hereda de ella). Node no
    # entra en ninguna: si apareciera, el despliegue crece sin usarlo.
    final = text.rsplit("FROM ", 1)[1].splitlines()[0]
    assert final.strip() == "app", "la etapa por defecto tiene que ser la de la app"
    despues_de_node = text.split("FROM python", 1)[1]
    assert "FROM node" not in despues_de_node, "node no puede estar por debajo de la app"


def test_el_build_de_la_spa_entra_en_el_paquete_del_web() -> None:
    """`web.spa` busca el build en `web/static`, dentro del wheel (029, P5)."""
    text = _dockerfile()
    assert "--from=panel /panel/dist ./src/agent_review/web/static" in text
    # Antes del `pip install .`: si va despues, el wheel no lo incluye y la imagen
    # arranca sin panel.
    assert text.index("--from=panel") < text.index("pip install .")
    ignored = (_ROOT / ".dockerignore").read_text(encoding="utf-8")
    for path in ("frontend/node_modules", "frontend/dist"):
        assert path in ignored, f"{path} no puede entrar en el contexto de build"


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


def test_el_gateway_esta_pineado_y_el_429_solo_se_reintenta_ahi() -> None:
    """032 Q1: tres reintentos del 429 dentro de la llamada; el resto, cero."""
    gateway = _compose()["services"]["nan-gateway"]
    assert gateway["image"] == "ghcr.io/berriai/litellm:v1.106.6"
    assert gateway.get("profiles") == ["claude"]
    assert "ports" not in gateway
    assert "env_file" not in gateway
    assert "NET_ADMIN" not in str(gateway.get("cap_add", []))
    config = yaml.safe_load((_ROOT / "config" / "nan-gateway.yaml").read_text(encoding="utf-8"))
    policy = config["router_settings"]["retry_policy"]
    assert policy["RateLimitErrorRetries"] == 3
    assert config["router_settings"]["num_retries"] == 0
    for key, value in policy.items():
        if key != "RateLimitErrorRetries":
            assert value == 0, key


def test_el_egress_del_gateway_no_lo_puede_abrir_litellm() -> None:
    """032 G1: NET_ADMIN está en el sidecar, y la salida solo nombra al clúster."""
    services = _compose()["services"]
    egress = services["nan-gateway-egress"]
    assert egress.get("profiles") == ["claude"]
    assert egress["network_mode"] == "service:nan-gateway"
    assert egress["cap_drop"] == ["ALL"]
    assert egress["cap_add"] == ["NET_ADMIN"]
    assert "ports" not in egress
    assert "NAN_API_KEY" not in egress.get("environment", {})
    assert egress["build"]["target"] == "nan-egress"
    script_path = _ROOT / "scripts" / "nan-gateway-egress.sh"
    script = script_path.read_text(encoding="utf-8")
    subprocess.run(["sh", "-n", str(script_path)], check=True)
    assert "api.nan.builders" in script
    assert "-P OUTPUT DROP" in script
    assert "/etc/resolv.conf" in script
    # El orden de las reglas y la marca los prueban los tests de comportamiento
    # de abajo; aquí solo el contrato de Compose.
    for name in ("api", "worker", "sandbox-runner", "claude-runner"):
        env = services[name].get("environment", {})
        assert env.get("NAN_API_KEY", "") == "", name
    assert "NAN_API_KEY" in services["nan-gateway"]["environment"]


def test_el_runner_no_espera_al_gateway_y_el_panel_ve_su_destino() -> None:
    """Anthropic no depende del clúster; el panel resuelve el mismo destino."""
    services = _compose()["services"]
    runner = services["claude-runner"]
    assert "nan-gateway" not in runner.get("depends_on", {})
    web = services["web"]["environment"]
    for key in ("CLAUDE_UPSTREAM", "CLAUDE_GATEWAY_MODEL"):
        assert web[key] == runner["environment"][key], key


_EGRESS = _ROOT / "scripts" / "nan-gateway-egress.sh"
_ENTRYPOINT = _ROOT / "scripts" / "nan-gateway-entrypoint.sh"
_TOKEN = _ROOT / "scripts" / "ensure-gateway-token.sh"

# Registra cada llamada y si la marca ya existía en ese momento.
_LOGGER = """#!/bin/sh
if [ -e "$NAN_EGRESS_DIR/ready" ]; then state=listo; else state=cerrado; fi
echo "$state $(basename "$0") $*" >> "$STUB_LOG"
"""

_GETENT = """#!/bin/sh
[ -n "${GETENT_EMPTY:-}" ] && exit 2
case $1 in
  ahostsv4) echo "203.0.113.5     STREAM api.nan.builders" ;;
  ahostsv6) echo "2001:db8::5     STREAM api.nan.builders" ;;
esac
"""


def _stubs(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {
        "iptables": _LOGGER,
        "ip6tables": _LOGGER,
        "getent": _GETENT,
        # El bucle del sidecar no termina: el primer `sleep` corta el script.
        "sleep": "#!/bin/sh\nexit 7\n",
    }.items():
        path = bin_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    return bin_dir


def _run_egress(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = _stubs(tmp_path)
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 127.0.0.11\n", encoding="utf-8")
    log = tmp_path / "calls.log"
    log.touch()
    result = subprocess.run(
        ["sh", str(_EGRESS)],
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "NAN_EGRESS_DIR": str(tmp_path / "egress"),
            "NAN_EGRESS_RESOLV": str(resolv),
            "STUB_LOG": str(log),
            **env,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result, log.read_text(encoding="utf-8").splitlines()


def test_el_sidecar_cierra_antes_de_marcar_y_solo_abre_el_443(tmp_path: Path) -> None:
    """032 G1: DROP antes de la marca, la marca nombra el netns, nada más que el 443."""
    egress_dir = tmp_path / "egress"
    egress_dir.mkdir()
    # Marca de un arranque anterior: no puede sobrevivir.
    (egress_dir / "ready").write_text("ok\n", encoding="utf-8")
    result, calls = _run_egress(tmp_path)
    assert result.returncode == 7, result.stderr
    assert "cerrado iptables -P OUTPUT DROP" in calls
    # Todo se aplica con la marca ausente: la vieja se borra antes.
    assert all(call.startswith("cerrado ") for call in calls), calls
    accepts = [call for call in calls if "-p tcp -d" in call and "--dport 443" in call]
    assert "cerrado iptables -A NAN_EGRESS_B -p tcp -d 203.0.113.5 --dport 443 -j ACCEPT" in accepts
    # El salto cambia a la cadena llena después de llenarla, nunca antes.
    llenar = calls.index(accepts[0])
    saltar = calls.index("cerrado iptables -R OUTPUT 1 -j NAN_EGRESS_B")
    assert llenar < saltar
    assert all("--dport 443 -j ACCEPT" in call for call in accepts)
    dns = [call for call in calls if "--dport 53" in call]
    assert dns and all("-d 127.0.0.11" in call for call in dns)
    marca = (egress_dir / "ready").read_text(encoding="utf-8").strip()
    assert marca == os.readlink("/proc/self/ns/net")


def test_sin_dns_el_sidecar_no_marca_ni_abre(tmp_path: Path) -> None:
    result, calls = _run_egress(tmp_path, GETENT_EMPTY="1")
    assert result.returncode == 7
    assert "cerrado iptables -P OUTPUT DROP" in calls
    assert not any("--dport 443" in call for call in calls)
    assert not (tmp_path / "egress" / "ready").exists()


def _run_entrypoint(
    tmp_path: Path, marca: str, nan_api_key: str = "sk-cluster"
) -> subprocess.CompletedProcess[str]:
    egress_dir = tmp_path / "egress"
    egress_dir.mkdir()
    (egress_dir / "ready").write_text(marca + "\n", encoding="utf-8")
    fake = tmp_path / "docker" / "prod_entrypoint.sh"
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\necho "litellm $*"\n', encoding="utf-8")
    fake.chmod(0o755)
    return subprocess.run(
        ["sh", str(_ENTRYPOINT), "--port", "4000"],
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "NAN_EGRESS_DIR": str(egress_dir),
            "NAN_EGRESS_WAIT_TRIES": "2",
            "LITELLM_MASTER_KEY": "sk-test",
            "NAN_API_KEY": nan_api_key,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_el_gateway_arranca_con_la_marca_de_su_netns(tmp_path: Path) -> None:
    result = _run_entrypoint(tmp_path, os.readlink("/proc/self/ns/net"))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "litellm --port 4000"


@pytest.mark.parametrize("marca", ["ok", "net:[1]"])
def test_una_marca_de_otro_arranque_no_abre_el_gateway(tmp_path: Path, marca: str) -> None:
    """Un reinicio del gateway trae otro netns: la marca vieja del volumen no vale."""
    result = _run_entrypoint(tmp_path, marca)
    assert result.returncode == 1
    assert "litellm" not in result.stdout


def test_sin_clave_del_cluster_el_gateway_no_arranca(tmp_path: Path) -> None:
    """Un gateway sin clave daría health y mandaría cada review a la DLQ."""
    result = _run_entrypoint(tmp_path, os.readlink("/proc/self/ns/net"), nan_api_key="")
    assert result.returncode == 1
    assert "litellm" not in result.stdout


def test_el_token_del_gateway_se_crea_una_vez(tmp_path: Path) -> None:
    token = tmp_path / "gateway" / "token"
    env = {"PATH": os.environ["PATH"], "GATEWAY_TOKEN_FILE": str(token)}
    subprocess.run(["sh", str(_TOKEN)], env=env, check=True, timeout=30)
    first = token.read_text(encoding="utf-8")
    assert first.startswith("sk-") and len(first.strip()) > 20
    # El hijo del CLI no es root: no puede leerlo aunque el volumen esté montado.
    assert token.stat().st_mode & 0o777 == 0o600
    subprocess.run(["sh", str(_TOKEN)], env=env, check=True, timeout=30)
    assert token.read_text(encoding="utf-8") == first


def test_los_modelos_del_cluster_son_los_mismos_en_los_tres_sitios() -> None:
    """Settings, el gateway y el panel nombran los mismos modelos.

    Añadir uno es tocar los tres: este test dice cuál falta.
    """
    import re

    from agent_review.settings import CLUSTER_MODELS

    config = yaml.safe_load((_ROOT / "config" / "nan-gateway.yaml").read_text(encoding="utf-8"))
    gateway = {item["model_name"] for item in config["model_list"]}
    panel_src = (_ROOT / "frontend" / "src" / "pages" / "claude" / "useClaudeBackend.ts").read_text(
        encoding="utf-8"
    )
    panel = set(re.findall(r"id: '([^']+)'", panel_src)) - {"claude"}
    assert gateway == set(CLUSTER_MODELS)
    assert panel == set(CLUSTER_MODELS)
