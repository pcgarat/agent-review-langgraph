from __future__ import annotations

from typing import Protocol, runtime_checkable

from reviews.errors import TransientError


class LlmError(TransientError):
    """Fallo del proveedor de modelo (red, cuota, respuesta invalida).

    Recuperable por diseno: el nodo de la persona LLM ya degrada a 0 hallazgos
    ante su fallo, pero cuando la clasificacion se use para reintentar, el tipo
    correcto es este y no un `RuntimeError` generico.
    """


@runtime_checkable
class LlmPort(Protocol):
    """Puerto de modelo de lenguaje, deliberadamente minimo.

    Un unico metodo completo (system + prompt -> texto) basta para las personas y
    mantiene el proveedor fuera del grafo: cambiar de OpenAI a Anthropic o a un
    modelo local es otro adaptador, sin tocar `LlmReviewer`.
    """

    def complete(self, *, system: str, prompt: str, max_tokens: int) -> str: ...
