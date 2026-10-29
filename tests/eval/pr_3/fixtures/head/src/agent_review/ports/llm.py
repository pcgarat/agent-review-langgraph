from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from agent_review.errors import PermanentError, TransientError

JsonMode = Literal["json_object", "json_schema"]


class LlmError(PermanentError):
    """Fallo del proveedor de modelo que no se arregla reintentando.

    Respuesta malformada, 4xx (credenciales, modelo inexistente, peticion
    invalida). Los fallos recuperables son `LlmTransientError`.
    """


class LlmTransientError(LlmError, TransientError):
    """Fallo recuperable del proveedor: red, timeout, 5xx o 429.

    Hereda de `LlmError` para que quien captura "cualquier fallo del modelo" lo
    siga haciendo, y de `TransientError` para que `is_transient` lo reintente
    (comprueba `TransientError` antes que `PermanentError`).
    """


@dataclass(frozen=True)
class LlmReply:
    """Respuesta del modelo, con el gasto real que reporta el proveedor.

    El `usage` sustituye a la estimacion `len(text) // 4` como fuente de verdad del
    coste: un modelo que razona gasta tokens en `reasoning_tokens` que no aparecen
    en el texto y que la estimacion no contaba.
    """

    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    finish_reason: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@runtime_checkable
class LlmPort(Protocol):
    """Puerto de modelo de lenguaje, deliberadamente minimo.

    Un unico metodo (system + prompt -> respuesta) basta para las personas y
    mantiene el proveedor fuera del grafo: cambiar de OpenAI a Anthropic o a un
    modelo local es otro adaptador, sin tocar `LlmReviewer`.

    `json_mode` deja que quien llama pida salida estructurada sin conocer el
    proveedor; el adaptador envia lo que su modelo declare soportar (un
    `json_schema` a un modelo que no lo soporta es un 400 permanente).
    """

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        json_mode: JsonMode | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> LlmReply: ...
