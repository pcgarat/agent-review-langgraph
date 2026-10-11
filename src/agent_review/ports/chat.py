from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ChatPort(Protocol):
    """Canal de comunicacion con personas.

    Fase 1: publicacion unidireccional en Teams (Incoming Webhook).
    Fase 2: hilo por PR con respuestas (Bot de Azure) o decision por otro canal.
    """

    def notify(self, *, title: str, body: str, link: str | None = None) -> None: ...

    def notify_decision_pending(self, *, title: str, body: str, link: str | None = None) -> None:
        """Aviso de que el grafo queda pausado esperando a una persona."""
        ...
