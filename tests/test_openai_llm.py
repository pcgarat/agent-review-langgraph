from __future__ import annotations

import httpx
import pytest

from reviews.adapters.openai_llm import OpenAiCompatibleLlm
from reviews.ports.llm import LlmError


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok(request: httpx.Request) -> httpx.Response:
    assert request.headers["authorization"] == "Bearer secret"
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": '{"findings": []}'}}]},
    )


def test_adaptador_devuelve_el_contenido() -> None:
    llm = OpenAiCompatibleLlm(
        api_key="secret", model="gpt-x", base_url="https://example.test/v1", client=_client(_ok)
    )
    assert llm.complete(system="s", prompt="p", max_tokens=10) == '{"findings": []}'


def test_adaptador_envia_system_y_prompt() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    llm = OpenAiCompatibleLlm(api_key="k", model="m", client=_client(handler))
    llm.complete(system="ERES", prompt="MIRA", max_tokens=7)

    assert captured["messages"][0] == {"role": "system", "content": "ERES"}
    assert captured["messages"][1] == {"role": "user", "content": "MIRA"}
    assert captured["temperature"] == 0


def test_error_http_se_convierte_en_llm_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    llm = OpenAiCompatibleLlm(api_key="k", model="m", client=_client(handler))
    with pytest.raises(LlmError):
        llm.complete(system="s", prompt="p", max_tokens=1)


def test_respuesta_sin_choices_es_llm_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"nope": True})

    llm = OpenAiCompatibleLlm(api_key="k", model="m", client=_client(handler))
    with pytest.raises(LlmError):
        llm.complete(system="s", prompt="p", max_tokens=1)


def test_modelo_vacio_falla_en_construccion() -> None:
    with pytest.raises(ValueError):
        OpenAiCompatibleLlm(api_key="k", model="")
