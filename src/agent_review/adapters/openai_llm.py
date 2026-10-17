from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from agent_review.errors import is_transient
from agent_review.ports.llm import JsonMode, LlmError, LlmReply, LlmTransientError

logger = logging.getLogger(__name__)


class OpenAiCompatibleLlm:
    """Proveedor LLM por HTTP con el contrato `/chat/completions`.

    Es el formato que hablan OpenAI, Azure OpenAI, Groq, vLLM, Ollama y buena
    parte de los gateways internos; con `base_url` se apunta a cualquiera de
    ellos sin tocar el codigo. Se separa del `LlmReviewer` (que solo conoce el
    puerto) para poder inyectar un fake en tests.

    El modelo declara sus capacidades en el registro (`json_mode`, y los
    `defaults` del proveedor: `temperature`, `reasoning_effort`...). El adaptador
    **no** adivina: pide al modelo lo que el modelo declara saber hacer.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "",
        timeout_s: float = 60.0,
        defaults: dict[str, Any] | None = None,
        json_mode: JsonMode | None = None,
        reasoning_min_max_tokens: int | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not model:
            raise ValueError("El modelo LLM no puede estar vacio")
        self._api_key = api_key
        self._model = model
        self._base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self._timeout = timeout_s
        self._defaults = dict(defaults or {})
        # Capacidad declarada del modelo; solo se envia si el modelo la soporta.
        self._json_mode = json_mode
        self._reasoning_min_max_tokens = reasoning_min_max_tokens
        self._client = client

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        json_mode: JsonMode | None = None,
        response_schema: dict[str, Any] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> LlmReply:
        effective_json = self._effective_json_mode(json_mode, response_schema)
        effective_max = self._effective_max_tokens(max_tokens)
        # `json_object` exige que el prompt mencione la palabra JSON, o el proveedor
        # lo rechaza con 400. Se anade al prompt, no a la respuesta.
        if effective_json == "json_object" and "JSON" not in prompt:
            prompt = f"{prompt}\n\nResponde con JSON valido."
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": effective_max,
            # Siempre en streaming (021 R8): una llamada que razona minutos no se
            # queda muda, y el gateway no la corta a los ~116 s con un 408. Si el
            # proveedor ignora `stream`, se lee la respuesta normal (`_read`).
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        # Los defaults del registro primero; lo explicito del payload manda.
        for key, value in self._defaults.items():
            if key not in payload:
                payload[key] = value
        if "temperature" not in payload:
            payload["temperature"] = 0
        if effective_json is not None:
            payload["response_format"] = self._response_format(effective_json, response_schema)

        text, reasoning, usage, finish_reason = self._post(payload, on_reasoning)
        if finish_reason == "length" and not text.strip():
            # Un razonador agota el presupuesto en `reasoning_content` y devuelve
            # `content: null` con 200 OK. Tratarlo como 0 hallazgos publicaria una
            # review limpia que no reviso nada: es un error trazable.
            raise LlmError(
                f"el modelo {self._model!r} agoto max_tokens={effective_max} sin contenido "
                f"(finish_reason=length)"
            )
        return LlmReply(
            text=text,
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            reasoning_tokens=int(
                (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0
            ),
            finish_reason=str(finish_reason or ""),
            reasoning=reasoning,
        )

    def _effective_json_mode(
        self, requested: JsonMode | None, response_schema: dict[str, Any] | None
    ) -> JsonMode | None:
        """Modo JSON que se pide de verdad, segun lo que el modelo declare.

        Si el modelo no declara `json_mode`, no se envia `response_format`, lo pida
        quien lo pida: pedir un formato a quien no lo soporta es un 400 permanente,
        no un reintento. Un modelo que declara `json_schema` tambien admite
        `json_object`.
        """
        if self._json_mode is None:
            return None
        mode = requested if requested is not None else self._json_mode
        if mode == "json_schema" and self._json_mode != "json_schema":
            logger.warning(
                "El modelo %s no declara json_schema; se omite response_format",
                self._model,
            )
            return None
        if mode == "json_schema" and response_schema is None:
            # Sin esquema, `json_schema` no es utilizable; se degrada a sin formato.
            logger.warning("json_schema pedido sin esquema; se omite response_format")
            return None
        return mode

    def _effective_max_tokens(self, max_tokens: int) -> int:
        """Techo real: si el modelo razona por defecto, la API puede subirlo.

        Se declara en el registro (`reasoning_min_max_tokens`) para que la
        estimacion de coste no mienta cuando el proveedor sube el limite.
        """
        if self._reasoning_min_max_tokens and max_tokens < self._reasoning_min_max_tokens:
            return self._reasoning_min_max_tokens
        return max_tokens

    def _response_format(
        self, json_mode: JsonMode, response_schema: dict[str, Any] | None
    ) -> dict[str, Any]:
        if json_mode == "json_object":
            return {"type": "json_object"}
        return {
            "type": "json_schema",
            "json_schema": {"name": "response", "schema": response_schema or {}, "strict": False},
        }

    def _post(
        self, payload: dict[str, Any], on_reasoning: Callable[[str], None] | None
    ) -> tuple[str, str, dict[str, Any], str]:
        """Contenido, razonamiento, `usage` y `finish_reason` de una llamada."""
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        url = f"{self._base_url}/chat/completions"
        client = self._client if self._client is not None else httpx.Client()
        try:
            with client.stream(
                "POST", url, json=payload, headers=headers, timeout=self._timeout
            ) as response:
                response.raise_for_status()
                if response.headers.get("content-type", "").startswith("text/event-stream"):
                    return _read_stream(response.iter_lines(), on_reasoning)
                response.read()
                return _read_message(response.json(), on_reasoning)
        except httpx.HTTPError as exc:
            # Solo red, 5xx y 429 cambian entre intentos; un 4xx es permanente. Un
            # corte a mitad del stream es de transporte: tambien transitorio.
            error = LlmTransientError if is_transient(exc) else LlmError
            raise error(f"Fallo del proveedor LLM: {exc}") from exc
        finally:
            if self._client is None:
                client.close()


def _read_message(
    data: Any, on_reasoning: Callable[[str], None] | None
) -> tuple[str, str, dict[str, Any], str]:
    """Respuesta sin streaming (proveedor que ignora `stream`, o dobles de test)."""
    try:
        choice = data["choices"][0]
        message = choice["message"]
        content = message["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmError("Respuesta del proveedor LLM sin 'choices[0].message.content'") from exc
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise LlmError("El contenido de la respuesta LLM no es texto")
    reasoning = str(message.get("reasoning_content") or "")
    if reasoning:
        _emit(on_reasoning, reasoning)
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return content, reasoning, usage, str(choice.get("finish_reason") or "")


def _read_stream(
    lines: Iterator[str], on_reasoning: Callable[[str], None] | None
) -> tuple[str, str, dict[str, Any], str]:
    """Acumula un stream SSE de `/chat/completions` (021 R1).

    Cada `data:` trae un `delta` con trozos de `reasoning_content` o de `content`;
    el `usage` llega en el ultimo *chunk* (`stream_options.include_usage`).
    """
    content: list[str] = []
    reasoning: list[str] = []
    usage: dict[str, Any] = {}
    finish = ""
    for line in lines:
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError as exc:
            raise LlmError(f"Chunk del stream LLM ilegible: {data[:80]!r}") from exc
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            piece = delta.get("reasoning_content")
            if isinstance(piece, str) and piece:
                reasoning.append(piece)
                _emit(on_reasoning, piece)
            text = delta.get("content")
            if isinstance(text, str):
                content.append(text)
            finish = str(choice.get("finish_reason") or finish)
    return "".join(content), "".join(reasoning), usage, finish


def _emit(on_reasoning: Callable[[str], None] | None, piece: str) -> None:
    """Entrega un trozo de razonamiento. *Best-effort*: el oyente nunca corta la
    llamada ni cambia la respuesta (021 R2)."""
    if on_reasoning is None:
        return
    try:
        on_reasoning(piece)
    except Exception:
        logger.exception("El oyente del razonamiento fallo; se ignora")
