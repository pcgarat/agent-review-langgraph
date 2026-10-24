"""`make status`: estado del stack y pruebas de credencial, sin red ni Docker."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from agent_review.settings import Settings

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "status.py"
_SPEC = importlib.util.spec_from_file_location("stack_status", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
status = importlib.util.module_from_spec(_SPEC)
sys.modules["stack_status"] = status
_SPEC.loader.exec_module(status)


class Scripted:
    """Respuestas fijadas. Registra la llamada, nunca el cuerpo ni el secreto."""

    def __init__(self) -> None:
        self.http_results: dict[tuple[str, str, bool], status.ProbeResult] = {}
        self.calls: list[tuple[object, ...]] = []
        self.twg_result = status.ProbeResult(0, "")
        self.git_result = status.ProbeResult(0, "")

    def http(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> status.ProbeResult:
        names = ("Authorization", "X-Reviews-Signature", "X-Panel-Session")
        marked = bool(headers and any(headers.get(name) for name in names))
        self.calls.append((method, url, marked, body))
        fallback = status.ProbeResult(None, "no preparado")
        return self.http_results.get((method, url, marked), fallback)

    def container_http(
        self,
        service: str,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> status.ProbeResult:
        self.calls.append(("exec", service))
        return self.http(method, url, headers=headers, body=body)

    def postgres(self, *, password: str, user: str, database: str) -> status.ProbeResult:
        self.calls.append(("postgres", user, database, bool(password)))
        return status.ProbeResult(0, "")

    def redis(self, *, password: str) -> status.ProbeResult:
        self.calls.append(("redis", bool(password)))
        return status.ProbeResult(0, "")

    def twg(
        self, *, workspace: str, repo: str, token: str, site: str, user: str
    ) -> status.ProbeResult:
        self.calls.append(("twg", workspace, repo, bool(token)))
        return self.twg_result

    def git_ls_remote(
        self, url: str, token: str, *, username: str = "x-token-auth"
    ) -> status.ProbeResult:
        self.calls.append(("git", url, bool(token)))
        return self.git_result


def _service(
    name: str,
    *,
    state: str = "running",
    health: str = "healthy",
    profiles: tuple[str, ...] = (),
    published: tuple[str, ...] = (),
) -> status.ServiceInfo:
    return status.ServiceInfo(name, state, health, profiles, published)


def test_parse_ps_deduplica_el_puerto_publicado() -> None:
    blob = "\n".join(
        [
            '{"Service":"api","State":"running","Health":"healthy","Publishers":['
            '{"PublishedPort":8000,"URL":"0.0.0.0"},{"PublishedPort":8000,"URL":"::"}]}',
            '{"Service":"worker","State":"running","Health":"","Publishers":[]}',
        ]
    )
    parsed = status.parse_ps(blob)
    assert parsed["api"] == ("running", "healthy", ("http://127.0.0.1:8000",))
    assert parsed["worker"][2] == ()


def test_stack_separa_perfil_apagado_de_contenedor_roto() -> None:
    rows = status.stack_rows(
        [("api", ()), ("worker", ()), ("web", ("panel",))],
        {"api": ("running", "unhealthy", ("http://127.0.0.1:8000",))},
    )
    by_name = {row.name: row for row in rows}
    assert by_name["api"].level == "fallo"
    assert "http://127.0.0.1:8000" in by_name["api"].detail
    assert by_name["worker"].level == "aviso"
    assert by_name["web"].level == "omitido"
    assert status.exit_code(rows) == 1


def test_redacta_la_contrasena_de_una_url() -> None:
    shown = status.redact_url("postgresql://reviews:s3cret@localhost:5432/reviews")
    assert "s3cret" not in shown
    assert shown == "postgresql://reviews:***@localhost:5432/reviews"


def test_github_acepta_el_token_sin_imprimirlo() -> None:
    token = "ghp_super_secret_value"
    io = Scripted()
    io.http_results[("GET", "https://api.github.com/repos/acme/app", True)] = status.ProbeResult(
        200, ""
    )
    settings = Settings(
        vcs_provider="github",
        github_owner="acme",
        github_repo="app",
        github_token=token,
    )
    rows = status._github_rows(settings, io)
    text = status.render(rows)
    assert token not in text
    assert {row.name: row.level for row in rows}["GITHUB_TOKEN"] == "ok"


def test_github_rechaza_un_401() -> None:
    io = Scripted()
    io.http_results[("GET", "https://api.github.com/repos/acme/app", True)] = status.ProbeResult(
        401, ""
    )
    settings = Settings(
        vcs_provider="github",
        github_owner="acme",
        github_repo="app",
        github_token="ghp_real",
    )
    rows = status._github_rows(settings, io)
    assert {row.name: row.level for row in rows}["GITHUB_TOKEN"] == "fallo"


def test_webhook_de_ejemplo_no_se_prueba() -> None:
    io = Scripted()
    rows = status._webhook_rows(Settings(), [], io)
    assert rows[0].level == "falta"
    assert io.calls == []


def test_webhook_exige_401_sin_firma_y_400_con_ella() -> None:
    io = Scripted()
    url = "http://127.0.0.1:8000/webhook/tooling"
    io.http_results[("POST", url, False)] = status.ProbeResult(401, "")
    io.http_results[("POST", url, True)] = status.ProbeResult(400, "")
    services = [_service("api", published=("http://127.0.0.1:8000",))]
    rows = status._webhook_rows(Settings(tooling_webhook_secret="whsec-real"), services, io)
    assert rows[0].level == "ok"
    assert io.calls[0][2] is False
    assert io.calls[1][2] is True


def test_webhook_con_firma_rechazada_es_fallo() -> None:
    io = Scripted()
    url = "http://127.0.0.1:8000/webhook/tooling"
    io.http_results[("POST", url, False)] = status.ProbeResult(401, "")
    io.http_results[("POST", url, True)] = status.ProbeResult(401, "")
    services = [_service("api", published=("http://127.0.0.1:8000",))]
    rows = status._webhook_rows(Settings(tooling_webhook_secret="whsec-real"), services, io)
    assert rows[0].level == "fallo"


def test_servicio_relajado_no_cuenta_como_credencial_valida() -> None:
    io = Scripted()
    url = "http://127.0.0.1:8000/webhook/tooling"
    io.http_results[("POST", url, False)] = status.ProbeResult(400, "")
    io.http_results[("POST", url, True)] = status.ProbeResult(400, "")
    services = [_service("api", published=("http://127.0.0.1:8000",))]
    rows = status._webhook_rows(Settings(tooling_webhook_secret="whsec-real"), services, io)
    assert rows[0].level == "aviso"


def test_llm_apagado_no_llama_al_proveedor() -> None:
    io = Scripted()
    rows = status._llm_rows(Settings(llm_reviewer_enabled=False, llm_api_key="sk-real"), io)
    assert rows[0].level == "omitido"
    assert io.calls == []


def test_sandbox_apagado_no_hace_ls_remote() -> None:
    io = Scripted()
    settings = Settings(
        sandbox_enabled=False,
        sandbox_runner_url="http://sandbox-runner:8099",
        sandbox_runner_token="runner-real",
        sandbox_git_token="git-real",
    )
    services = [_service("api"), _service("sandbox-runner")]
    target = "http://sandbox-runner:8099/workspaces/status-probe/file?path=README"
    io.http_results[("GET", target, False)] = status.ProbeResult(401, "")
    io.http_results[("GET", target, True)] = status.ProbeResult(404, "")
    rows = status._sandbox_rows(settings, services, io)
    assert {row.name: row.level for row in rows}["SANDBOX_RUNNER_TOKEN"] == "ok"
    assert {row.name: row.level for row in rows}["SANDBOX_ENABLED"] == "omitido"
    assert not any(call[0] == "git" for call in io.calls)


def test_twg_aceptado_no_vuelca_el_token() -> None:
    token = "twg-super-secret"
    io = Scripted()
    settings = Settings(
        bitbucket_workspace="acme",
        bitbucket_repo="app",
        twg_bbc_token=token,
        twg_user="svc",
        twg_site="bitbucket.org",
    )
    rows = status._bitbucket_rows(settings, io)
    assert token not in status.render(rows)
    assert {row.name: row.level for row in rows}["TWG_BBC_TOKEN"] == "ok"
    assert ("twg", "acme", "app", True) in io.calls
