from __future__ import annotations

from typing import Protocol, runtime_checkable

from reviews.errors import PermanentError, TransientError


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


@runtime_checkable
class LlmPort(Protocol):
    """Puerto de modelo de lenguaje, deliberadamente minimo.

    Un unico metodo completo (system + prompt -> texto) basta para las personas y
    mantiene el proveedor fuera del grafo: cambiar de OpenAI a Anthropic o a un
    modelo local es otro adaptador, sin tocar `LlmReviewer`.
    """

    def complete(self, *, system: str, prompt: str, max_tokens: int) -> str: ...
