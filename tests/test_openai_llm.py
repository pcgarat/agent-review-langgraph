from __future__ import annotations

import json

import httpx
import pytest

from agent_review.adapters.openai_llm import OpenAiCompatibleLlm
from agent_review.ports.llm import LlmError


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok(request: httpx.Request) -> httpx.Response:
    assert request.headers["authorization"] == "Bearer secret"
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": '{"findings": []}'}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 30,
                "completion_tokens": 8,
                "completion_tokens_details": {"reasoning_tokens": 3},
            },
        },
    )


def _payload(request: httpx.Request) -> dict:
    return json.loads(request.content)


def test_adaptador_devuelve_el_contenido() -> None:
    llm = OpenAiCompatibleLlm(
        api_key="secret", model="gpt-x", base_url="https://example.test/v1", client=_client(_ok)
    )
    reply = llm.complete(system="s", prompt="p", max_tokens=10)
    assert reply.text == '{"findings": []}'
    assert reply.finish_reason == "stop"


def test_adaptador_expone_el_usage_real() -> None:
    llm = OpenAiCompatibleLlm(api_key="secret", model="m", client=_client(_ok))
    reply = llm.complete(system="s", prompt="p", max_tokens=10)
    assert (reply.prompt_tokens, reply.completion_tokens) == (30, 8)
    # El razonamiento consume tokens que no aparecen en el texto: la estimacion
    # por caracteres no los cuenta y por eso el `usage` es la fuente de verdad.
    assert reply.reasoning_tokens == 3
    assert reply.total_tokens == 38


def test_sin_usage_los_contadores_son_cero() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    llm = OpenAiCompatibleLlm(api_key="k", model="m", client=_client(handler))
    reply = llm.complete(system="s", prompt="p", max_tokens=1)
    assert (reply.prompt_tokens, reply.completion_tokens, reply.reasoning_tokens) == (0, 0, 0)


def test_adaptador_envia_system_y_prompt() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(_payload(request))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    llm = OpenAiCompatibleLlm(api_key="k", model="m", client=_client(handler))
    llm.complete(system="ERES", prompt="MIRA", max_tokens=7)

    assert captured["messages"][0] == {"role": "system", "content": "ERES"}
    assert captured["messages"][1] == {"role": "user", "content": "MIRA"}
    assert captured["temperature"] == 0


def test_defaults_del_registro_llegan_al_payload() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(_payload(request))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    llm = OpenAiCompatibleLlm(
        api_key="k", model="m", client=_client(handler), defaults={"reasoning_effort": "none"}
    )
    llm.complete(system="s", prompt="p", max_tokens=1)
    assert captured["reasoning_effort"] == "none"


def test_json_object_solo_si_el_modelo_lo_declara() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(_payload(request))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    llm = OpenAiCompatibleLlm(
        api_key="k", model="m", client=_client(handler), json_mode="json_object"
    )
    llm.complete(system="s", prompt="MIRA EL DIFF", max_tokens=1)
    assert captured["response_format"] == {"type": "json_object"}
    # `json_object` exige que el prompt mencione JSON, o el proveedor da 400.
    assert "JSON" in captured["messages"][1]["content"]


def test_json_schema_pedido_a_un_modelo_que_no_lo_declara_se_omite() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(_payload(request))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    # El modelo declara `json_object`; pedirle `json_schema` es un 400 permanente,
    # asi que el adaptador no lo envia.
    llm = OpenAiCompatibleLlm(
        api_key="k", model="m", client=_client(handler), json_mode="json_object"
    )
    llm.complete(system="s", prompt="p", max_tokens=1, json_mode="json_schema")
    assert "response_format" not in captured


def test_json_schema_con_esquema_se_envia() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(_payload(request))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    llm = OpenAiCompatibleLlm(
        api_key="k", model="m", client=_client(handler), json_mode="json_schema"
    )
    llm.complete(
        system="s",
        prompt="p",
        max_tokens=1,
        json_mode="json_schema",
        response_schema={"type": "object"},
    )
    assert captured["response_format"]["type"] == "json_schema"
    assert captured["response_format"]["json_schema"]["schema"] == {"type": "object"}


def _captura(json_mode: str | None) -> tuple[OpenAiCompatibleLlm, dict]:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(_payload(request))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    llm = OpenAiCompatibleLlm(
        api_key="k",
        model="m",
        client=_client(handler),
        json_mode=json_mode,  # type: ignore[arg-type]
    )
    return llm, captured


def test_sin_json_mode_declarado_no_se_envia_response_format_aunque_se_pida() -> None:
    """La persona pide `json_object`, pero un modelo que no lo declara no lo recibe.

    Es la via implicita de `LLM_*` y cualquier modelo sin `json_mode` en el YAML:
    un proveedor que no lo admite responderia 400 en cada review.
    """
    llm, captured = _captura(None)
    llm.complete(system="s", prompt="p", max_tokens=1, json_mode="json_object")
    assert "response_format" not in captured


def test_un_modelo_con_json_schema_admite_json_object() -> None:
    llm, captured = _captura("json_schema")
    llm.complete(system="s", prompt="p", max_tokens=1, json_mode="json_object")
    assert captured["response_format"] == {"type": "json_object"}


def test_json_schema_declarado_sin_esquema_se_omite() -> None:
    """Sin peticion explicita se usa lo declarado, pero sin esquema no es utilizable."""
    llm, captured = _captura("json_schema")
    llm.complete(system="s", prompt="p", max_tokens=1)
    assert "response_format" not in captured


def test_reasoning_sube_el_techo_de_tokens() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(_payload(request))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    llm = OpenAiCompatibleLlm(
        api_key="k", model="m", client=_client(handler), reasoning_min_max_tokens=16384
    )
    llm.complete(system="s", prompt="p", max_tokens=64)
    # Con razonamiento la API sube el limite por su cuenta: declararlo evita que
    # la estimacion de coste mienta.
    assert captured["max_tokens"] == 16384


def test_la_respuesta_vacia_por_longitud_es_un_error() -> None:
    # Un razonador puede gastar todo el presupuesto en `reasoning_content` y
    # devolver `content: null` con 200 OK. Tratarlo como 0 hallazgos publicaria
    # una review limpia que no reviso nada.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": None, "reasoning_content": "..."},
                        "finish_reason": "length",
                    }
                ]
            },
        )

    llm = OpenAiCompatibleLlm(api_key="k", model="m", client=_client(handler))
    with pytest.raises(LlmError, match="finish_reason=length"):
        llm.complete(system="s", prompt="p", max_tokens=64)


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
