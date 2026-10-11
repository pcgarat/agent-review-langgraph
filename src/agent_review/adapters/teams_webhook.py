from __future__ import annotations

import httpx


class TeamsAdaptiveCardPayload:
    """Tarjeta simple para Incoming Webhook.

    Fase 1 usa MessageCard (soportado por el webhook clasico). En la Fase 2 se
    sustituye por Adaptive Cards interactivas con Bot Framework.
    """

    def __init__(self, *, title: str, body: str, link: str | None = None) -> None:
        self.title = title
        self.body = body
        self.link = link

    def as_dict(self) -> dict[str, object]:
        sections: list[dict[str, object]] = [{"text": self.body}]
        potential_action: list[dict[str, object]] = []
        if self.link:
            potential_action.append(
                {
                    "@type": "OpenUri",
                    "name": "Ver el PR",
                    "targets": [{"os": "default", "uri": self.link}],
                }
            )
        return {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "summary": self.title,
            "title": self.title,
            "sections": sections,
            "potentialAction": potential_action,
        }


class TeamsWebhookAdapter:
    """Publicacion unidireccional en Microsoft Teams.

    Nota de diseno: con solo un Incoming Webhook no se pueden recibir
    respuestas. Las decisiones llegan por otro canal (comentario de PR, Jira o
    CLI). Ver docs/diseno-agente-code-review-20261010.md, apartado 5.
    """

    def __init__(self, *, webhook_url: str) -> None:
        self._webhook_url = webhook_url

    def _post(self, payload: dict[str, object]) -> None:
        if not self._webhook_url:
            # Sin webhook configurado no se falla: se registra y se sigue.
            return
        response = httpx.post(self._webhook_url, json=payload, timeout=10.0)
        response.raise_for_status()

    def notify(self, *, title: str, body: str, link: str | None = None) -> None:
        self._post(TeamsAdaptiveCardPayload(title=title, body=body, link=link).as_dict())

    def notify_decision_pending(self, *, title: str, body: str, link: str | None = None) -> None:
        self._post(
            TeamsAdaptiveCardPayload(
                title=f"Decision pendiente · {title}", body=body, link=link
            ).as_dict()
        )
