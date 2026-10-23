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
    # 024 E3: sin campo de cache no se inventa un 0; es «no medido».
    assert reply.cached_tokens is None


@pytest.mark.parametrize(
    "extra",
    [
        {"prompt_tokens_details": {"cached_tokens": 24}},
        {"prompt_cache_hit_tokens": 24},
        {"cache_read_input_tokens": 24},
    ],
    ids=["openai", "deepseek", "anthropic"],
)
def test_el_cache_del_proveedor_se_normaliza(extra: dict) -> None:
    # 024 S2: tres dialectos del `usage`, una sola columna. Si el proveedor lo
    # reporta, se lee; el orden solo decide cuando vienen varios.
    usage = {"prompt_tokens": 30, "completion_tokens": 8, **extra}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": usage,
            },
        )

    llm = OpenAiCompatibleLlm(api_key="k", model="m", client=_client(handler))

    assert llm.complete(system="s", prompt="p", max_tokens=1).cached_tokens == 24


def test_gana_el_dialecto_de_openai_cuando_vienen_varios() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 30,
                    "prompt_tokens_details": {"cached_tokens": 10},
                    "prompt_cache_hit_tokens": 24,
                },
            },
        )

    llm = OpenAiCompatibleLlm(api_key="k", model="m", client=_client(handler))

    assert llm.complete(system="s", prompt="p", max_tokens=1).cached_tokens == 10


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


# --- Streaming (021 R1/R2/R8) ---


def _sse(*chunks: dict, done: bool = True) -> bytes:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    if done:
        body += "data: [DONE]\n\n"
    return body.encode()


_STREAM = _sse(
    {"choices": [{"delta": {"role": "assistant", "reasoning_content": "Miro el "}}]},
    {"choices": [{"delta": {"reasoning_content": "diff."}}]},
    {"choices": [{"delta": {"content": '{"findings"'}}]},
    {"choices": [{"delta": {"content": ": []}"}, "finish_reason": "stop"}]},
    {
        "choices": [],
        "usage": {
            "prompt_tokens": 40,
            "completion_tokens": 12,
            "completion_tokens_details": {"reasoning_tokens": 5},
        },
    },
)


def _stream_handler(body: bytes, captured: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.update(_payload(request))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    return handler


def test_pide_siempre_streaming_con_usage() -> None:
    captured: dict = {}
    llm = OpenAiCompatibleLlm(
        api_key="k", model="m", client=_client(_stream_handler(_STREAM, captured))
    )
    llm.complete(system="s", prompt="p", max_tokens=10)
    assert captured["stream"] is True
    assert captured["stream_options"] == {"include_usage": True}


def test_streaming_acumula_contenido_razonamiento_y_usage() -> None:
    llm = OpenAiCompatibleLlm(api_key="k", model="m", client=_client(_stream_handler(_STREAM)))
    reply = llm.complete(system="s", prompt="p", max_tokens=10)
    assert reply.text == '{"findings": []}'
    assert reply.reasoning == "Miro el diff."
    assert reply.finish_reason == "stop"
    assert (reply.prompt_tokens, reply.completion_tokens, reply.reasoning_tokens) == (40, 12, 5)


def test_on_reasoning_recibe_los_trozos_en_orden() -> None:
    trozos: list[str] = []
    llm = OpenAiCompatibleLlm(api_key="k", model="m", client=_client(_stream_handler(_STREAM)))
    llm.complete(system="s", prompt="p", max_tokens=10, on_reasoning=trozos.append)
    assert trozos == ["Miro el ", "diff."]


def test_un_oyente_que_falla_no_cambia_la_respuesta() -> None:
    def roto(_: str) -> None:
        raise RuntimeError("panel caido")

    llm = OpenAiCompatibleLlm(api_key="k", model="m", client=_client(_stream_handler(_STREAM)))
    reply = llm.complete(system="s", prompt="p", max_tokens=10, on_reasoning=roto)
    assert reply.text == '{"findings": []}'


def test_lineas_vacias_y_sin_done_se_toleran() -> None:
    body = b": keep-alive\n\n" + _sse(
        {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}, done=False
    )
    llm = OpenAiCompatibleLlm(api_key="k", model="m", client=_client(_stream_handler(body)))
    assert llm.complete(system="s", prompt="p", max_tokens=10).text == "ok"


def test_razonamiento_que_agota_max_tokens_sin_respuesta_es_error() -> None:
    # La prueba del 2026-10-17: 43 194 tokens de razonamiento y `length` sin content.
    body = _sse(
        {"choices": [{"delta": {"reasoning_content": "pienso mucho"}}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
    )
    llm = OpenAiCompatibleLlm(api_key="k", model="m", client=_client(_stream_handler(body)))
    with pytest.raises(LlmError, match="finish_reason=length"):
        llm.complete(system="s", prompt="p", max_tokens=10)


def test_un_corte_a_mitad_del_stream_es_transitorio() -> None:
    from agent_review.ports.llm import LlmTransientError

    class _Cortado(httpx.SyncByteStream):
        def __iter__(self):
            yield _sse({"choices": [{"delta": {"reasoning_content": "a"}}]}, done=False)
            raise httpx.ReadError("conexion cerrada")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_Cortado())

    llm = OpenAiCompatibleLlm(api_key="k", model="m", client=_client(handler))
    with pytest.raises(LlmTransientError):
        llm.complete(system="s", prompt="p", max_tokens=10)


def test_sin_streaming_tambien_devuelve_el_razonamiento() -> None:
    # Un proveedor que ignora `stream` responde JSON: se lee igual (sonda §3.1).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "391", "reasoning_content": "17*23=391"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    trozos: list[str] = []
    llm = OpenAiCompatibleLlm(api_key="k", model="m", client=_client(handler))
    reply = llm.complete(system="s", prompt="p", max_tokens=10, on_reasoning=trozos.append)
    assert (reply.text, reply.reasoning, trozos) == ("391", "17*23=391", ["17*23=391"])
