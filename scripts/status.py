#!/usr/bin/env python3
"""Estado del stack y de las credenciales con las que habla con otros sistemas.

No imprime valores secretos. Una credencial se prueba con una lectura: un GET,
un ``SELECT 1`` o un ``git ls-remote``. Los secretos compartidos (webhook,
decision, sesion, bearer interno) no tienen un tercero al que preguntar: se
contrastan con el servicio en marcha. Sin la credencial debe responder 401; con
ella, pasar esa comprobacion. No se encola una review ni se lanza una verificacion.

``make status`` sale con codigo 1 si falta una credencial de un modulo activo o
si una prueba la rechaza. Un modulo apagado no cuenta como fallo.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
import yaml

from agent_review.models_config import (
    ModelsConfigError,
    env_for,
    load_models_config,
    resolved_api_key,
    unresolved_secrets,
)
from agent_review.settings import Settings, is_missing

ROOT = Path(__file__).resolve().parents[1]
_TIMEOUT_S = 8.0
_FAIL = frozenset({"falta", "fallo"})
# Puertos que compose no publica. Solo orientan: el estado real sale de `ps`.
_INTERNAL = {
    "sandbox-runner": "red interna sandbox-runner:8099",
    "claude-runner": "red interna claude-runner:8098",
    "teams-bot": "red interna teams-bot:3978",
    "postgres": "red interna postgres:5432",
    "redis": "red interna redis:6379",
    "worker": "sin puerto",
    "fix-worker": "sin puerto",
}
_JUMPHOSTS = ("api", "worker", "web", "fix-worker", "sandbox-runner", "claude-runner")
_PROBE_PY = """
import os, urllib.error, urllib.request
body = os.environ.get("PROBE_BODY", "")
req = urllib.request.Request(
    os.environ["PROBE_URL"],
    data=body.encode() if body else None,
    method=os.environ.get("PROBE_METHOD", "GET"),
)
for env_name, header in (
    ("PROBE_AUTHORIZATION", "Authorization"),
    ("PROBE_SIGNATURE", "X-Reviews-Signature"),
    ("PROBE_PANEL", "X-Panel-Session"),
):
    value = os.environ.get(env_name, "")
    if value:
        req.add_header(header, value)
try:
    with urllib.request.urlopen(req, timeout=8) as resp:
        print(resp.status)
except urllib.error.HTTPError as exc:
    print(exc.code)
except Exception as exc:
    print("ERR", type(exc).__name__)
"""


@dataclass(frozen=True)
class ProbeResult:
    """Resultado de una prueba. ``status`` es un HTTP o un codigo de proceso.

    ``None`` significa que no hubo respuesta (red, binario, contenedor). ``detail``
    ya va redactado: quien lo rellena no mete el secreto.
    """

    status: int | None
    detail: str = ""


@dataclass(frozen=True)
class ServiceInfo:
    name: str
    state: str
    health: str
    profiles: tuple[str, ...]
    published: tuple[str, ...]


@dataclass(frozen=True)
class Row:
    section: str
    level: str
    name: str
    detail: str


class LiveIO:
    """Pruebas reales. Los secretos viajan por entorno, no por la linea de comandos."""

    def __init__(self, compose: list[str]) -> None:
        self._compose = compose

    def http(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> ProbeResult:
        try:
            response = httpx.request(
                method,
                url,
                headers=dict(headers or {}),
                content=body,
                timeout=_TIMEOUT_S,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            return ProbeResult(None, type(exc).__name__)
        return ProbeResult(response.status_code, "")

    def container_http(
        self,
        service: str,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> ProbeResult:
        incoming = dict(headers or {})
        env = os.environ.copy()
        env.update(
            {
                "PROBE_URL": url,
                "PROBE_METHOD": method,
                "PROBE_BODY": body.decode() if body else "",
                "PROBE_AUTHORIZATION": incoming.get("Authorization", ""),
                "PROBE_SIGNATURE": incoming.get("X-Reviews-Signature", ""),
                "PROBE_PANEL": incoming.get("X-Panel-Session", ""),
            }
        )
        argv = [
            *self._compose,
            "exec",
            "-T",
            "-e",
            "PROBE_URL",
            "-e",
            "PROBE_METHOD",
            "-e",
            "PROBE_BODY",
            "-e",
            "PROBE_AUTHORIZATION",
            "-e",
            "PROBE_SIGNATURE",
            "-e",
            "PROBE_PANEL",
            service,
            "python",
            "-c",
            _PROBE_PY,
        ]
        result = _run(argv, env)
        if result.status is not None and result.status >= 100 and not result.detail:
            return result
        if result.status is None:
            return result
        return ProbeResult(None, result.detail or "sin respuesta")

    def postgres(self, *, password: str, user: str, database: str) -> ProbeResult:
        env = os.environ.copy()
        env["PGPASSWORD"] = password
        argv = [
            *self._compose,
            "exec",
            "-T",
            "-e",
            "PGPASSWORD",
            "postgres",
            "psql",
            "-h",
            "127.0.0.1",
            "-U",
            user,
            "-d",
            database,
            "-c",
            "select 1",
        ]
        return _run(argv, env, redact=[password])

    def redis(self, *, password: str) -> ProbeResult:
        env = os.environ.copy()
        argv = [*self._compose, "exec", "-T"]
        if password:
            env["REDISCLI_AUTH"] = password
            argv.extend(["-e", "REDISCLI_AUTH"])
        argv.extend(["redis", "redis-cli", "ping"])
        return _run(argv, env, redact=[password])

    def twg(self, *, workspace: str, repo: str, token: str, site: str, user: str) -> ProbeResult:
        env = os.environ.copy()
        env["TWG_BBC_TOKEN"] = token
        if site:
            env["TWG_SITE"] = site
        if user:
            env["TWG_USER"] = user
        argv = [
            "twg",
            "bb",
            "repo",
            "get",
            repo,
            "-w",
            workspace,
            "-r",
            repo,
            "--output",
            "json",
            "--output-summary",
            "none",
        ]
        return _run(argv, env, redact=[token], timeout=20.0)

    def git_ls_remote(self, url: str, token: str) -> ProbeResult:
        # Askpass evita el token en la URL, que git repetiria en el error.
        with tempfile.TemporaryDirectory() as tmp:
            askpass = Path(tmp) / "askpass"
            askpass.write_text(
                "#!/bin/sh\n"
                'case "$1" in\n'
                "  Username*) printf '%s\\n' 'x-token-auth' ;;\n"
                "  *) printf '%s\\n' \"$STATUS_GIT_TOKEN\" ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            askpass.chmod(0o700)
            env = os.environ.copy()
            env["STATUS_GIT_TOKEN"] = token
            env["GIT_ASKPASS"] = str(askpass)
            env["GIT_TERMINAL_PROMPT"] = "0"
            return _run(["git", "ls-remote", url, "HEAD"], env, redact=[token], timeout=20.0)


def _run(
    argv: list[str],
    env: Mapping[str, str],
    *,
    redact: list[str] | None = None,
    timeout: float = _TIMEOUT_S,
) -> ProbeResult:
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            env=dict(env),
            timeout=timeout,
            check=False,
            cwd=ROOT,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(None, "tiempo agotado")
    except FileNotFoundError:
        return ProbeResult(None, "binario ausente")
    if completed.returncode == 0:
        text = (completed.stdout or "").strip()
        # El salto HTTP dentro del contenedor imprime solo el codigo.
        if text.isdigit():
            return ProbeResult(int(text), "")
        if text.startswith("ERR "):
            return ProbeResult(None, text.split(maxsplit=1)[1])
        return ProbeResult(0, "")
    message = _redact((completed.stderr or completed.stdout or "").strip(), redact or [])
    first = message.splitlines()[0][:180] if message else "fallo"
    return ProbeResult(completed.returncode, first)


def _redact(text: str, secrets: list[str]) -> str:
    redacted = text
    for secret in secrets:
        if len(secret) >= 4:
            redacted = redacted.replace(secret, "***")
    return redacted


def redact_url(url: str) -> str:
    """URL sin la contrasena. Un valor vacio no se disfraza de configurado."""
    raw = url.strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    if parts.username is None and parts.password is None:
        return raw
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    user = f"{parts.username}:***@" if parts.username else "***@"
    return urlunsplit((parts.scheme, f"{user}{host}{port}", parts.path, "", ""))


def parse_ps(blob: str) -> dict[str, tuple[str, str, tuple[str, ...]]]:
    """``docker compose ps --format json``: una linea JSON por contenedor."""
    found: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    for line in blob.splitlines():
        line = line.strip()
        if not line:
            continue
        item = json.loads(line)
        urls: list[str] = []
        for publisher in item.get("Publishers") or []:
            port = int(publisher.get("PublishedPort") or 0)
            if port <= 0:
                continue
            url = f"http://127.0.0.1:{port}"
            if url not in urls:
                urls.append(url)
        found[str(item.get("Service") or "")] = (
            str(item.get("State") or ""),
            str(item.get("Health") or ""),
            tuple(urls),
        )
    return found


def services_from_config(payload: Mapping[str, object]) -> list[tuple[str, tuple[str, ...]]]:
    services = payload.get("services")
    if not isinstance(services, dict):
        return []
    listed: list[tuple[str, tuple[str, ...]]] = []
    for name, spec in services.items():
        profiles: tuple[str, ...] = ()
        if isinstance(spec, dict):
            raw = spec.get("profiles") or ()
            if isinstance(raw, list):
                profiles = tuple(str(item) for item in raw)
        listed.append((str(name), profiles))
    return listed


def stack_rows(
    defined: list[tuple[str, tuple[str, ...]]],
    running_ps: Mapping[str, tuple[str, str, tuple[str, ...]]],
) -> list[Row]:
    rows: list[Row] = []
    for name, profiles in defined:
        state, health, published = running_ps.get(name, ("missing", "", ()))
        where = ", ".join(published) if published else _INTERNAL.get(name, "sin puerto")
        if state == "missing":
            if profiles:
                rows.append(
                    Row(
                        "contenedores",
                        "omitido",
                        name,
                        f"perfil {', '.join(profiles)} apagado",
                    )
                )
            else:
                rows.append(Row("contenedores", "aviso", name, f"no creado  {where}"))
            continue
        health_bit = f" ({health})" if health else ""
        detail = f"{state}{health_bit}  {where}"
        if state != "running" or health == "unhealthy":
            level = "fallo"
        elif health == "starting":
            level = "aviso"
        else:
            level = "ok"
        rows.append(Row("contenedores", level, name, detail))
    return rows


def _configured_secret(value: str) -> bool:
    return not is_missing(value)


def _plain(section: str, name: str, value: str, *, redacted: str | None = None) -> Row:
    shown = value.strip() if redacted is None else redacted
    if not shown:
        return Row(section, "falta", name, "sin configurar")
    return Row(section, "ok", name, shown)


def _auth_row(
    section: str,
    name: str,
    without: ProbeResult,
    with_secret: ProbeResult,
    *,
    ok_status: frozenset[int],
    ok_detail: str,
) -> Row:
    """El servicio exige la credencial y la nuestra la atraviesa."""
    if without.status is None:
        return Row(section, "aviso", name, f"configurado; no alcanzable ({without.detail})")
    if without.status != 401:
        return Row(
            section,
            "aviso",
            name,
            f"configurado; el servicio no exige credencial (HTTP {without.status})",
        )
    if with_secret.status is None:
        return Row(
            section,
            "aviso",
            name,
            f"configurado; no alcanzable ({with_secret.detail})",
        )
    if with_secret.status == 401:
        return Row(section, "fallo", name, "credencial rechazada")
    if with_secret.status not in ok_status:
        return Row(
            section,
            "aviso",
            name,
            f"configurado; respuesta no concluyente (HTTP {with_secret.status})",
        )
    return Row(section, "ok", name, ok_detail)


def _hit(
    io: LiveIO,
    services: list[ServiceInfo],
    method: str,
    host_url: str | None,
    internal_url: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
) -> ProbeResult:
    if host_url:
        return io.http(method, host_url, headers=headers, body=body)
    running = {item.name for item in services if item.state == "running"}
    host = next((name for name in _JUMPHOSTS if name in running), None)
    if host is None:
        return ProbeResult(None, "ningun contenedor en la red")
    return io.container_http(host, method, internal_url, headers=headers, body=body)


def _published(services: list[ServiceInfo], name: str) -> str | None:
    for item in services:
        if item.name == name and item.published:
            return item.published[0]
    return None


def _running(services: list[ServiceInfo], name: str) -> bool:
    return any(item.name == name and item.state == "running" for item in services)


def build_rows(
    settings: Settings,
    services: list[ServiceInfo],
    io: LiveIO,
    dotenv: Mapping[str, str],
) -> list[Row]:
    rows = _service_rows(services)
    rows.extend(_postgres_rows(settings, services, io, dotenv))
    rows.extend(_redis_rows(settings, services, io))
    rows.extend(_vcs_rows(settings, io))
    rows.extend(_webhook_rows(settings, services, io))
    rows.extend(_decision_rows(settings, services, io))
    rows.extend(_llm_rows(settings, io))
    rows.extend(_sandbox_rows(settings, services, io))
    rows.extend(_teams_rows(settings, services, io))
    rows.extend(_claude_rows(settings, services, io))
    rows.extend(_panel_rows(settings, services, io))
    return rows


def _service_rows(services: list[ServiceInfo]) -> list[Row]:
    return stack_rows(
        [(item.name, item.profiles) for item in services],
        {
            item.name: (item.state, item.health, item.published)
            for item in services
            if item.state != "missing"
        },
    )


def _postgres_rows(
    settings: Settings,
    services: list[ServiceInfo],
    io: LiveIO,
    dotenv: Mapping[str, str],
) -> list[Row]:
    section = "Postgres"
    url = settings.database_url
    rows = [_plain(section, "DATABASE_URL", url, redacted=redact_url(url))]
    password = dotenv.get("POSTGRES_PASSWORD", "").strip()
    from_url = urlsplit(url).password or ""
    if password and from_url and password != from_url:
        rows.append(
            Row(
                section,
                "aviso",
                "POSTGRES_PASSWORD",
                "no coincide con la de DATABASE_URL; la prueba usa POSTGRES_PASSWORD",
            )
        )
    elif not password:
        password = from_url
    if not password or is_missing(password):
        rows.append(Row(section, "falta", "POSTGRES_PASSWORD", "sin configurar"))
        return rows
    parts = urlsplit(url)
    user = parts.username or "reviews"
    database = parts.path.lstrip("/") or "reviews"
    if not _running(services, "postgres"):
        rows.append(Row(section, "aviso", "POSTGRES_PASSWORD", "configurado; contenedor parado"))
        return rows
    result = io.postgres(password=password, user=user, database=database)
    if result.status == 0:
        rows.append(Row(section, "ok", "POSTGRES_PASSWORD", "aceptada (select 1)"))
    elif result.status is None:
        rows.append(Row(section, "aviso", "POSTGRES_PASSWORD", f"configurado; {result.detail}"))
    else:
        rows.append(Row(section, "fallo", "POSTGRES_PASSWORD", result.detail or "rechazada"))
    return rows


def _redis_rows(settings: Settings, services: list[ServiceInfo], io: LiveIO) -> list[Row]:
    section = "Redis"
    url = settings.redis_url
    shown = redact_url(url)
    if not shown:
        return [Row(section, "falta", "REDIS_URL", "sin configurar")]
    if not _running(services, "redis"):
        return [Row(section, "aviso", "REDIS_URL", f"{shown}  contenedor parado")]
    result = io.redis(password=urlsplit(url).password or "")
    if result.status == 0:
        return [Row(section, "ok", "REDIS_URL", f"{shown}  PONG")]
    return [Row(section, "fallo", "REDIS_URL", result.detail or "redis no respondio PONG")]


def _vcs_rows(settings: Settings, io: LiveIO) -> list[Row]:
    if settings.vcs_provider == "github":
        return _github_rows(settings, io)
    return _bitbucket_rows(settings, io)


def _github_rows(settings: Settings, io: LiveIO) -> list[Row]:
    section = "GitHub"
    rows = [
        _plain(section, "GITHUB_OWNER", settings.github_owner),
        _plain(section, "GITHUB_REPO", settings.github_repo),
        _plain(section, "GITHUB_API_URL", settings.github_api_url),
    ]
    token = settings.github_token
    if not _configured_secret(token):
        rows.append(Row(section, "falta", "GITHUB_TOKEN", "sin configurar"))
        return rows
    owner = settings.github_owner.strip()
    repo = settings.github_repo.strip()
    if not owner or not repo:
        rows.append(
            Row(section, "aviso", "GITHUB_TOKEN", "configurado; falta owner o repo para probarlo")
        )
        return rows
    base = (settings.github_api_url or "https://api.github.com").rstrip("/")
    result = io.http(
        "GET",
        f"{base}/repos/{quote(owner)}/{quote(repo)}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-review-status",
        },
    )
    rows.append(_http_secret_row(section, "GITHUB_TOKEN", result, ok=frozenset({200})))
    return rows


def _bitbucket_rows(settings: Settings, io: LiveIO) -> list[Row]:
    section = "Bitbucket"
    rows = [
        _plain(section, "BITBUCKET_WORKSPACE", settings.bitbucket_workspace),
        _plain(section, "BITBUCKET_REPO", settings.bitbucket_repo),
        _plain(section, "TWG_SITE", settings.twg_site),
        _plain(section, "TWG_USER", settings.twg_user),
    ]
    token = settings.twg_bbc_token
    if not _configured_secret(token):
        rows.append(Row(section, "falta", "TWG_BBC_TOKEN", "sin configurar"))
        return rows
    workspace = settings.bitbucket_workspace.strip()
    repo = settings.bitbucket_repo.strip()
    if not workspace or not repo:
        rows.append(
            Row(
                section,
                "aviso",
                "TWG_BBC_TOKEN",
                "configurado; falta workspace o repo para probarlo",
            )
        )
        return rows
    result = io.twg(
        workspace=workspace,
        repo=repo,
        token=token,
        site=settings.twg_site.strip(),
        user=settings.twg_user.strip(),
    )
    if result.detail == "binario ausente":
        result = _bitbucket_rest(io, workspace, repo, token, settings.twg_user.strip())
        rows.append(_http_secret_row(section, "TWG_BBC_TOKEN", result, ok=frozenset({200})))
        return rows
    if result.status == 0:
        rows.append(Row(section, "ok", "TWG_BBC_TOKEN", "twg bb repo get acepto el token"))
    elif result.status is None:
        rows.append(Row(section, "aviso", "TWG_BBC_TOKEN", f"configurado; {result.detail}"))
    else:
        rows.append(Row(section, "fallo", "TWG_BBC_TOKEN", result.detail or "twg rechazo el token"))
    return rows


def _bitbucket_rest(io: LiveIO, workspace: str, repo: str, token: str, user: str) -> ProbeResult:
    url = f"https://api.bitbucket.org/2.0/repositories/{quote(workspace)}/{quote(repo)}"
    bearer = io.http("GET", url, headers={"Authorization": f"Bearer {token}"})
    if bearer.status != 401 or not user:
        return bearer
    encoded = base64.b64encode(f"{user}:{token}".encode()).decode()
    return io.http("GET", url, headers={"Authorization": f"Basic {encoded}"})


def _http_secret_row(section: str, name: str, result: ProbeResult, *, ok: frozenset[int]) -> Row:
    if result.status is None:
        return Row(section, "aviso", name, f"configurado; no se pudo probar ({result.detail})")
    if result.status in ok:
        return Row(section, "ok", name, f"aceptada (HTTP {result.status})")
    if result.status in {401, 403}:
        return Row(section, "fallo", name, f"credencial rechazada (HTTP {result.status})")
    return Row(section, "fallo", name, f"HTTP {result.status}")


def _webhook_rows(settings: Settings, services: list[ServiceInfo], io: LiveIO) -> list[Row]:
    section = "CI (webhook)"
    secret = settings.tooling_webhook_secret
    if not _configured_secret(secret):
        return [Row(section, "falta", "TOOLING_WEBHOOK_SECRET", "sin configurar")]
    body = b"{}"
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    without, with_secret = _pair(
        io,
        services,
        "POST",
        _join(_published(services, "api"), "/webhook/tooling"),
        "http://api:8000/webhook/tooling",
        body=body,
        headers={"X-Reviews-Signature": signature},
    )
    return [
        _auth_row(
            section,
            "TOOLING_WEBHOOK_SECRET",
            without,
            with_secret,
            ok_status=frozenset({400}),
            ok_detail="la firma coincide; el cuerpo vacio no encola nada",
        )
    ]


def _decision_rows(settings: Settings, services: list[ServiceInfo], io: LiveIO) -> list[Row]:
    section = "Decision"
    secret = settings.reviews_decision_token
    if not _configured_secret(secret):
        return [Row(section, "falta", "REVIEWS_DECISION_TOKEN", "sin configurar")]
    body = b"{}"
    without, with_secret = _pair(
        io,
        services,
        "POST",
        _join(_published(services, "api"), "/reviews/status-probe/decision"),
        "http://api:8000/reviews/status-probe/decision",
        body=body,
        headers={"Authorization": f"Bearer {secret}"},
    )
    return [
        _auth_row(
            section,
            "REVIEWS_DECISION_TOKEN",
            without,
            with_secret,
            ok_status=frozenset({400}),
            ok_detail="el bearer coincide; el cuerpo vacio no decide nada",
        )
    ]


def _join(base: str | None, path: str) -> str | None:
    if not base:
        return None
    return f"{base.rstrip('/')}{path}"


def _pair(
    io: LiveIO,
    services: list[ServiceInfo],
    method: str,
    host_url: str | None,
    internal_url: str,
    *,
    body: bytes | None = None,
    headers: Mapping[str, str],
) -> tuple[ProbeResult, ProbeResult]:
    without = _hit(io, services, method, host_url, internal_url, body=body)
    with_secret = _hit(io, services, method, host_url, internal_url, headers=headers, body=body)
    return without, with_secret


def _llm_rows(settings: Settings, io: LiveIO) -> list[Row]:
    section = "LLM"
    if not settings.llm_reviewer_enabled:
        return [Row(section, "omitido", "LLM_REVIEWER_ENABLED", "apagado")]
    if not settings.models_config.strip():
        rows = [
            _plain(section, "LLM_MODEL", settings.llm_model),
            _plain(section, "LLM_BASE_URL", settings.llm_base_url or "https://api.openai.com/v1"),
        ]
        if not _configured_secret(settings.llm_api_key):
            rows.append(Row(section, "falta", "LLM_API_KEY", "sin configurar"))
            return rows
        base = (settings.llm_base_url or "https://api.openai.com/v1").rstrip("/")
        result = io.http(
            "GET",
            f"{base}/models",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        )
        rows.append(_http_secret_row(section, "LLM_API_KEY", result, ok=frozenset({200})))
        return rows
    try:
        config = load_models_config(settings.models_config)
    except ModelsConfigError as exc:
        return [Row(section, "fallo", "MODELS_CONFIG", str(exc).splitlines()[0][:180])]
    env = env_for(settings)
    rows = [_plain(section, "MODELS_CONFIG", settings.models_config)]
    for name in unresolved_secrets(config, env):
        rows.append(Row(section, "falta", name, "referenciado por el registro y sin configurar"))
    for provider_name, provider in config.providers.items():
        key = resolved_api_key(provider, env)
        if not key or is_missing(key):
            continue
        base = provider.base_url.rstrip("/")
        result = io.http("GET", f"{base}/models", headers={"Authorization": f"Bearer {key}"})
        rows.append(_http_secret_row(section, f"LLM:{provider_name}", result, ok=frozenset({200})))
    return rows


def _sandbox_rows(settings: Settings, services: list[ServiceInfo], io: LiveIO) -> list[Row]:
    section = "Sandbox"
    rows: list[Row] = []
    url = settings.sandbox_runner_url.strip() or (
        "http://sandbox-runner:8099" if _running(services, "sandbox-runner") else ""
    )
    if not settings.sandbox_runner_url.strip():
        detail = "sin configurar"
        if url:
            detail = "sin configurar; la prueba usa el DNS interno del compose"
        rows.append(Row(section, "falta", "SANDBOX_RUNNER_URL", detail))
    else:
        rows.append(Row(section, "ok", "SANDBOX_RUNNER_URL", url))
    token = settings.sandbox_runner_token
    if not _configured_secret(token):
        rows.append(Row(section, "falta", "SANDBOX_RUNNER_TOKEN", "sin configurar"))
    elif not url:
        rows.append(
            Row(section, "aviso", "SANDBOX_RUNNER_TOKEN", "configurado; no hay URL que probar")
        )
    else:
        target = f"{url.rstrip('/')}/workspaces/status-probe/file?path=README"
        host = target if _host_reachable(url) else None
        without = _hit(io, services, "GET", host, target)
        with_secret = _hit(
            io,
            services,
            "GET",
            host,
            target,
            headers={"Authorization": f"Bearer {token}"},
        )
        rows.append(
            _auth_row(
                section,
                "SANDBOX_RUNNER_TOKEN",
                without,
                with_secret,
                # 410: el id no existe. 404: el fichero no esta. Los dos pasan el bearer.
                ok_status=frozenset({404, 410}),
                ok_detail="el bearer coincide; no hay ese workspace",
            )
        )
    if not settings.sandbox_enabled:
        rows.append(Row(section, "omitido", "SANDBOX_ENABLED", "apagado (git y registro)"))
        return rows
    rows.extend(
        _git_token_rows(section, "SANDBOX_GIT_TOKEN", settings.sandbox_git_token, settings, io)
    )
    rows.extend(_registry_rows(settings, io))
    return rows


def _host_reachable(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


def _git_token_rows(
    section: str, name: str, token: str, settings: Settings, io: LiveIO
) -> list[Row]:
    if not _configured_secret(token):
        return [Row(section, "falta", name, "sin configurar")]
    repo = ""
    if settings.bitbucket_workspace.strip() and settings.bitbucket_repo.strip():
        repo = f"{settings.bitbucket_workspace.strip()}/{settings.bitbucket_repo.strip()}"
    elif settings.vcs_provider != "github":
        repo = settings.configured_repo()
    if not repo:
        return [
            Row(
                section,
                "aviso",
                name,
                "configurado; el clon es de Bitbucket y no hay workspace/repo",
            )
        ]
    result = io.git_ls_remote(f"https://bitbucket.org/{repo}.git", token)
    if result.status == 0:
        return [Row(section, "ok", name, "ls-remote acepto el token")]
    if result.status is None:
        return [Row(section, "aviso", name, f"configurado; {result.detail}")]
    return [Row(section, "fallo", name, result.detail or "git rechazo el token")]


def _registry_rows(settings: Settings, io: LiveIO) -> list[Row]:
    section = "Sandbox"
    user = settings.sandbox_registry_user.strip()
    token = settings.sandbox_registry_token
    host = _registry_host(settings)
    rows = [_plain(section, "SANDBOX_REGISTRY_USER", user)]
    if not _configured_secret(token):
        rows.append(Row(section, "falta", "SANDBOX_REGISTRY_TOKEN", "sin configurar"))
        return rows
    if not user:
        rows.append(
            Row(section, "aviso", "SANDBOX_REGISTRY_TOKEN", "configurado; falta el usuario")
        )
        return rows
    if not host:
        rows.append(
            Row(
                section,
                "aviso",
                "SANDBOX_REGISTRY_TOKEN",
                "configurado; el perfil no declara un registro",
            )
        )
        return rows
    url = f"https://{host}/v2/"
    without = io.http("GET", url)
    encoded = base64.b64encode(f"{user}:{token}".encode()).decode()
    with_secret = io.http("GET", url, headers={"Authorization": f"Basic {encoded}"})
    if without.status == 200 and with_secret.status == 200:
        rows.append(
            Row(section, "aviso", "SANDBOX_REGISTRY_TOKEN", "el registro no exige credencial")
        )
        return rows
    rows.append(
        _auth_row(
            section,
            "SANDBOX_REGISTRY_TOKEN",
            without,
            with_secret,
            ok_status=frozenset({200}),
            ok_detail="el registro acepto el usuario",
        )
    )
    return rows


def _registry_host(settings: Settings) -> str:
    path = Path(settings.repo_profile_config)
    if not path.is_file():
        return ""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return ""
    if not isinstance(data, dict):
        return ""
    image = str(data.get("ci_image") or "")
    host = image.split("/")[0]
    if "." not in host:
        return ""
    return host


def _teams_rows(settings: Settings, services: list[ServiceInfo], io: LiveIO) -> list[Row]:
    section = "Teams"
    if not settings.teams_enabled:
        return [Row(section, "omitido", "TEAMS_ENABLED", "apagado")]
    rows = [
        _plain(section, "TEAMS_BOT_ID", settings.teams_bot_id),
        _plain(section, "TEAMS_BOT_TENANT_ID", settings.teams_bot_tenant_id),
        _plain(section, "TEAMS_NOTICES_KEY", settings.teams_notices_key),
    ]
    token = settings.teams_bot_token
    tenant = settings.teams_bot_tenant_id.strip()
    bot_id = settings.teams_bot_id.strip()
    if not _configured_secret(token):
        rows.append(Row(section, "falta", "TEAMS_BOT_TOKEN", "sin configurar"))
    elif not tenant or not bot_id:
        rows.append(
            Row(section, "aviso", "TEAMS_BOT_TOKEN", "configurado; falta id o tenant para probarlo")
        )
    else:
        body = (
            f"grant_type=client_credentials&client_id={quote(bot_id)}"
            f"&client_secret={quote(token)}"
            "&scope=https%3A%2F%2Fapi.botframework.com%2F.default"
        ).encode()
        result = io.http(
            "POST",
            f"https://login.microsoftonline.com/{quote(tenant)}/oauth2/v2.0/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=body,
        )
        rows.append(_http_secret_row(section, "TEAMS_BOT_TOKEN", result, ok=frozenset({200})))
    shared = settings.teams_bot_shared_secret
    if not _configured_secret(shared):
        rows.append(Row(section, "falta", "TEAMS_BOT_SHARED_SECRET", "sin configurar"))
        return rows
    without, with_secret = _pair(
        io,
        services,
        "GET",
        _join(_published(services, "teams-bot"), "/healthz"),
        f"http://teams-bot:{settings.teams_bot_port}/healthz",
        headers={"Authorization": f"Bearer {shared}"},
    )
    rows.append(
        _auth_row(
            section,
            "TEAMS_BOT_SHARED_SECRET",
            without,
            with_secret,
            ok_status=frozenset({200}),
            ok_detail="el bearer coincide",
        )
    )
    return rows


def _claude_rows(settings: Settings, services: list[ServiceInfo], io: LiveIO) -> list[Row]:
    section = "Claude"
    if settings.review_engine != "claude":
        return [Row(section, "omitido", "REVIEW_ENGINE", settings.review_engine or "llm")]
    url = settings.claude_runner_url.strip() or (
        "http://claude-runner:8098" if _running(services, "claude-runner") else ""
    )
    rows = []
    if not settings.claude_runner_url.strip():
        rows.append(Row(section, "falta", "CLAUDE_RUNNER_URL", "sin configurar"))
    else:
        rows.append(Row(section, "ok", "CLAUDE_RUNNER_URL", url))
    token = settings.claude_runner_token
    if not _configured_secret(token):
        rows.append(Row(section, "falta", "CLAUDE_RUNNER_TOKEN", "sin configurar"))
    elif not url:
        rows.append(Row(section, "aviso", "CLAUDE_RUNNER_TOKEN", "configurado; no hay URL"))
    else:
        target = f"{url.rstrip('/')}/session"
        host = target if _host_reachable(url) else None
        without = _hit(io, services, "GET", host, target)
        with_secret = _hit(
            io,
            services,
            "GET",
            host,
            target,
            headers={"Authorization": f"Bearer {token}"},
        )
        rows.append(
            _auth_row(
                section,
                "CLAUDE_RUNNER_TOKEN",
                without,
                with_secret,
                ok_status=frozenset({200}),
                ok_detail="el bearer coincide",
            )
        )
    rows.extend(
        _git_token_rows(section, "CLAUDE_GIT_TOKEN", settings.claude_git_token, settings, io)
    )
    return rows


def _panel_rows(settings: Settings, services: list[ServiceInfo], io: LiveIO) -> list[Row]:
    section = "Panel"
    active = settings.panel_enabled or _running(services, "web")
    if not active:
        return [Row(section, "omitido", "PANEL_ENABLED", "apagado")]
    secret = settings.panel_session_secret
    rows: list[Row] = []
    if not _configured_secret(secret):
        rows.append(Row(section, "falta", "PANEL_SESSION_SECRET", "sin configurar"))
    else:
        without, with_secret = _pair(
            io,
            services,
            "GET",
            _join(_published(services, "web"), "/api/state"),
            f"http://web:{settings.panel_port}/api/state",
            headers={"X-Panel-Session": secret},
        )
        rows.append(
            _auth_row(
                section,
                "PANEL_SESSION_SECRET",
                without,
                with_secret,
                ok_status=frozenset({200}),
                ok_detail="la sesion coincide",
            )
        )
    console = settings.sandbox_console_token
    if not _configured_secret(console):
        rows.append(Row(section, "falta", "SANDBOX_CONSOLE_TOKEN", "sin configurar"))
        return rows
    url = settings.sandbox_runner_url.strip() or (
        "http://sandbox-runner:8099" if _running(services, "sandbox-runner") else ""
    )
    if not url:
        rows.append(
            Row(section, "aviso", "SANDBOX_CONSOLE_TOKEN", "configurado; no hay URL del runner")
        )
        return rows
    target = f"{url.rstrip('/')}/verify/status-probe/console"
    host = target if _host_reachable(url) else None
    without = _hit(io, services, "GET", host, target)
    with_secret = _hit(
        io, services, "GET", host, target, headers={"Authorization": f"Bearer {console}"}
    )
    rows.append(
        _auth_row(
            section,
            "SANDBOX_CONSOLE_TOKEN",
            without,
            with_secret,
            ok_status=frozenset({404}),
            ok_detail="el bearer coincide; no hay esa consola",
        )
    )
    return rows


def render(rows: list[Row]) -> str:
    lines = [
        "== status ==",
        "Prueba de lectura: no imprime secretos ni encola reviews.",
        "Un secreto compartido vale si el servicio responde 401 sin el y deja pasar con el.",
        "",
    ]
    current = ""
    for row in rows:
        if row.section != current:
            if current:
                lines.append("")
            current = row.section
            lines.append(f"-- {current} --")
        lines.append(f"  [{row.level}]".ljust(13) + f"{row.name:<24} {row.detail}")
    lines.append("")
    return "\n".join(lines)


def exit_code(rows: list[Row]) -> int:
    return 1 if any(row.level in _FAIL for row in rows) else 0


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        value = value.strip().strip("'").strip('"')
        found[key.strip()] = value
    return found


def _load_stack(compose: list[str]) -> list[ServiceInfo]:
    config = subprocess.run(
        [*compose, "--profile", "panel", "--profile", "claude", "config", "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    if config.returncode != 0:
        raise RuntimeError("docker compose config fallo")
    defined = services_from_config(json.loads(config.stdout))
    ps = subprocess.run(
        [*compose, "ps", "-a", "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    if ps.returncode != 0:
        raise RuntimeError("docker compose ps fallo")
    live = parse_ps(ps.stdout)
    services: list[ServiceInfo] = []
    for name, profiles in defined:
        state, health, published = live.get(name, ("missing", "", ()))
        services.append(
            ServiceInfo(
                name=name,
                state=state,
                health=health,
                profiles=profiles,
                published=published,
            )
        )
    return services


def main() -> int:
    os.chdir(ROOT)
    compose = os.environ.get("COMPOSE", "docker compose").split()
    dotenv = _read_dotenv(ROOT / ".env")
    settings = Settings()
    try:
        services = _load_stack(compose)
    except (RuntimeError, json.JSONDecodeError, OSError) as exc:
        rows = [Row("contenedores", "aviso", "docker", str(exc)[:180])]
        rows.extend(build_rows(settings, [], LiveIO(compose), dotenv))
        print(render(rows), end="")
        return exit_code(rows)
    rows = build_rows(settings, services, LiveIO(compose), dotenv)
    print(render(rows), end="")
    return exit_code(rows)


if __name__ == "__main__":
    raise SystemExit(main())
