from __future__ import annotations

import logging
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
        }
        # Los defaults del registro primero; lo explicito del payload manda.
        for key, value in self._defaults.items():
            if key not in payload:
                payload[key] = value
        if "temperature" not in payload:
            payload["temperature"] = 0
        if effective_json is not None:
            payload["response_format"] = self._response_format(effective_json, response_schema)

        text, usage, finish_reason = self._post(payload)
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

    def _post(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            if self._client is not None:
                response = self._client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=self._timeout,
                )
            else:
                response = httpx.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=self._timeout,
                )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            # Solo red, 5xx y 429 cambian entre intentos; un 4xx es permanente.
            error = LlmTransientError if is_transient(exc) else LlmError
            raise error(f"Fallo del proveedor LLM: {exc}") from exc

        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError("Respuesta del proveedor LLM sin 'choices[0].message.content'") from exc
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise LlmError("El contenido de la respuesta LLM no es texto")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return content, usage, str(choice.get("finish_reason") or "")
