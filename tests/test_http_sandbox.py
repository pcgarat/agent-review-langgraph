from __future__ import annotations

import json

import httpx
import pytest

from agent_review.adapters.http_sandbox import (
    HttpSandboxAdapter,
    SandboxError,
    SandboxUnavailable,
)
from agent_review.domain.models import CommandResult
from agent_review.ports.sandbox import VerifyRequest

_REQUEST = VerifyRequest(
    run_key="cqm/x:1:abc:7:f1:1",
    repo="cqm/x",
    head_sha="abc",
    patch="diff --git a/src/Foo.php b/src/Foo.php\n",
    recipes=["typecheck", "tests"],
    targets=["tests/Foo/BarTest.php"],
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _adapter(handler, *, token: str = "") -> HttpSandboxAdapter:
    return HttpSandboxAdapter(base_url="http://runner:8099/", token=token, client=_client(handler))


def _report_body() -> dict[str, object]:
    return {
        "run_key": _REQUEST.run_key,
        "image_digest": "sha256:abc",
        "results": [
            CommandResult(
                recipe="typecheck", argv=["make", "stan"], exit_code=0, duration_s=1.0
            ).model_dump(mode="json")
        ],
        "passed": True,
    }


def test_verify_interpreta_el_informe_del_runner() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json=_report_body()))
    report = adapter.verify(_REQUEST)
    assert report.passed
    assert report.image_digest == "sha256:abc"
    assert report.results[0].recipe == "typecheck"


def test_verify_envia_el_contrato_completo() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_report_body())

    adapter = _adapter(handler, token="secret")
    adapter.verify(_REQUEST)

    assert captured["url"] == "http://runner:8099/verify"
    assert captured["auth"] == "Bearer secret"
    assert captured["body"]["run_key"] == _REQUEST.run_key
    assert captured["body"]["targets"] == ["tests/Foo/BarTest.php"]
    assert captured["body"]["recipes"] == ["typecheck", "tests"]


def test_sin_token_no_envia_authorization() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_report_body())

    _adapter(handler).verify(_REQUEST)
    assert captured["auth"] is None


def test_5xx_es_transitorio() -> None:
    adapter = _adapter(lambda request: httpx.Response(503, text="runner ocupado"))
    with pytest.raises(SandboxUnavailable):
        adapter.verify(_REQUEST)


def test_429_es_transitorio() -> None:
    adapter = _adapter(lambda request: httpx.Response(429, text="too many"))
    with pytest.raises(SandboxUnavailable):
        adapter.verify(_REQUEST)


def test_4xx_es_permanente_con_detalle() -> None:
    adapter = _adapter(
        lambda request: httpx.Response(409, json={"detail": "perfil sin ci_image_digest"})
    )
    with pytest.raises(SandboxError, match="ci_image_digest"):
        adapter.verify(_REQUEST)


def test_fallo_de_red_es_transitorio() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(SandboxUnavailable):
        _adapter(handler).verify(_REQUEST)


def test_informe_ilegible_es_error_permanente() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json={"nope": True}))
    with pytest.raises(SandboxError):
        adapter.verify(_REQUEST)


def test_timeout_del_cliente_da_holgura_sobre_el_del_perfil() -> None:
    # El POST es bloqueante: si el cliente cortase justo en `sandbox.timeout_s`,
    # un fix correcto se marcaria como NOT_VERIFIABLE por una carrera.
    adapter = HttpSandboxAdapter(base_url="http://runner:8099", timeout_s=100.0)
    assert adapter._timeout > 100.0
