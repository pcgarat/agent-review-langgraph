from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_review.domain.models import ApprovalRequest


@runtime_checkable
class ChatPort(Protocol):
    """Canal de comunicacion con personas.

    El worker **no** conoce el transporte: encola un `ChatNotice` en la outbox y
    el canal (hoy el bot de Teams) lo consume. Por eso el contrato lleva la
    identidad de la review (`repo`, `pr_id`, `review_key`): con ella el canal
    resuelve el hilo por PR (C6); sin ella no habria a donde publicar.

    Fase 1: publicacion unidireccional (Incoming Webhook, retirado).
    Fase 2: aviso de pausa. Iniciativa 013: bot con hilo por PR y tarjeta.
    """

    def notify(
        self,
        *,
        repo: str,
        pr_id: int,
        review_key: str,
        title: str,
        body: str,
        link: str | None = None,
    ) -> None:
        """Aviso de una review o un fix ya publicado en el PR.

        Es un mensaje del hilo: la primera vez abre el hilo raiz del PR; despues
        va como reply (lo decide el canal, no el worker).
        """
        ...

    def notify_decision_pending(
        self,
        *,
        repo: str,
        pr_id: int,
        review_key: str,
        approval: ApprovalRequest,
        link: str | None = None,
    ) -> None:
        """Aviso de que el grafo queda pausado esperando a una persona.

        Lleva la `ApprovalRequest` **estructurada**, no el render en texto
        (C12): el canal pinta la tarjeta con los datos, sin interpretar prosa.
        """
        ...
