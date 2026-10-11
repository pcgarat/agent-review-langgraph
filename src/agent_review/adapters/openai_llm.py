from __future__ import annotations

import logging
from typing import Any

import httpx

from agent_review.errors import is_transient
from agent_review.ports.llm import LlmError, LlmTransientError

logger = logging.getLogger(__name__)


class OpenAiCompatibleLlm:
    """Proveedor LLM por HTTP con el contrato `/chat/completions`.

    Es el formato que hablan OpenAI, Azure OpenAI, Groq, vLLM, Ollama y buena
    parte de los gateways internos; con `base_url` se apunta a cualquiera de
    ellos sin tocar el codigo. Se separa del `LlmReviewer` (que solo conoce el
    puerto) para poder inyectar un fake en tests.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "",
        timeout_s: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not model:
            raise ValueError("El modelo LLM no puede estar vacio")
        self._api_key = api_key
        self._model = model
        self._base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self._timeout = timeout_s
        self._client = client

    def complete(self, *, system: str, prompt: str, max_tokens: int) -> str:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": 0,
        }
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
            choices = data["choices"]
            content = choices[0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError("Respuesta del proveedor LLM sin 'choices[0].message.content'") from exc
        if not isinstance(content, str):
            raise LlmError("El contenido de la respuesta LLM no es texto")
        return content
