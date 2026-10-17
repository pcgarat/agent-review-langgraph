from __future__ import annotations

from collections.abc import Callable
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


class LlmTruncatedError(LlmError):
    """El modelo agoto `max_tokens` y cerro sin contenido (`finish_reason=length`).

    No es un fallo del proveedor sino del presupuesto de salida de *esa* llamada
    (un razonador que se come el turno en `reasoning_content` y devuelve
    `content: null` con 200 OK). El bucle de herramientas lo trata como un turno
    perdido —pide el objeto final— en vez de dar el lote entero por caido.
    """


class FingerprintMismatch(LlmError):
    """La grabacion no corresponde al prompt o al caso: hay que regrabar.

    Vive aqui, y no en el harness de evaluacion, porque `LlmReviewer` necesita
    distinguirla y **no** tragarsela: un *replay* que no casa es un fallo de
    medida, no una review vacia (ADR-0017). Hereda de `LlmError` para que
    `is_transient` la clasifique como permanente y ningun reintento la repita.
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
    # Tokens del prefijo que el proveedor sirvio de su cache (024 E3). `None` es
    # «no medido» y **no** es 0: un proveedor que no lo reporta no es un proveedor
    # que no cachea, y confundirlos cierra la pregunta al reves.
    cached_tokens: int | None = None
    finish_reason: str = ""
    # Lo que el modelo razono antes de responder, si el proveedor lo devuelve
    # (021). Es material de operacion: no se publica ni entra en el estado.
    reasoning: str = ""

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

    `on_reasoning` (021 R2) recibe el razonamiento a trozos mientras se genera. Es
    *best-effort*: un oyente que falla nunca cambia la respuesta. Quien llama solo
    lo pasa si alguien escucha, asi que un adaptador que no razona puede ignorarlo.
    """

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        json_mode: JsonMode | None = None,
        response_schema: dict[str, Any] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> LlmReply: ...
