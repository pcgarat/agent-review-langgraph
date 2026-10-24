"""Adaptador del SDK de Teams: implementa `ChannelClient` (T6, iniciativa 013).

Es la unica pieza del repo que importa `microsoft_teams`; el resto del paquete
(`transport.py`, `service.py`) habla con el puerto `ChannelClient`. Se aisla aqui
para que el SDK sea sustituible y para que **el worker no lo conozca** (C2).

Tres cosas que el spike (T0) demostro y que este adaptador respeta:

1. `service_url` en **toda** llamada proactiva. Omitirlo hace que el SDK use el de
   la app y el envio acabe en el Teams real, con `400`.
2. Los adjuntos de tarjeta se envuelven con `card_attachment(...)`; un
   `AdaptiveCardAttachment` suelto no valida en `MessageActivityInput`.
3. El id del mensaje raiz debe ser **numerico**: se valida al leer de Redis (T4).
"""

from __future__ import annotations

import logging
from typing import Any

from microsoft_teams.api.activities.message.message import MessageActivityInput
from microsoft_teams.api.models.attachment.card_attachment import (
    AdaptiveCardAttachment,
    card_attachment,
)
from microsoft_teams.apps import App
from microsoft_teams.cards import AdaptiveCard

logger = logging.getLogger(__name__)


class SDKChannelClient:
    """`ChannelClient` sobre `microsoft_teams.apps.App` (publicacion proactiva)."""

    def __init__(self, app: App) -> None:
        self._app = app

    async def send(
        self, conversation_id: str, payload: Any, *, service_url: str = ""
    ) -> str | None:
        sent = await self._app.send(
            conversation_id, _to_activity(payload), service_url=service_url or None
        )
        return _message_id(sent)

    async def reply(
        self,
        conversation_id: str,
        root_message_id: str,
        payload: Any,
        *,
        service_url: str = "",
    ) -> str | None:
        sent = await self._app.reply(
            conversation_id, root_message_id, _to_activity(payload), service_url=service_url or None
        )
        return _message_id(sent)

    async def update(
        self, conversation_id: str, message_id: str, payload: Any, *, service_url: str = ""
    ) -> None:
        """Edita la tarjeta reemplazando el mensaje por id (C7).

        El SDK no expone un `update_activity` en esta version; el reemplazo se hace
        publicando una actividad **con el mismo id** (probado en el spike: `PUT
        .../activities/<id>`). Los adjuntos van envueltos con `card_attachment(...)`.
        """
        activity = _to_activity(payload).with_id(message_id)
        await self._app.send(conversation_id, activity, service_url=service_url or None)

    async def delete(self, conversation_id: str, message_id: str, *, service_url: str = "") -> None:
        """Borra la actividad. Si Teams ya no la tiene, no es un fallo."""
        try:
            await self._app.api.conversations.delete_activity(
                conversation_id,
                message_id,
                service_url=service_url or None,
            )
        except Exception as exc:  # noqa: BLE001 - el conector no comparte una sola clase
            if _already_gone(exc):
                return
            raise


def _already_gone(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 404


def adaptive_card(payload: dict[str, Any]) -> AdaptiveCard:
    """`dict` de Adaptive Card -> modelo del SDK.

    Se valida aqui (ruidoso si el render produjo algo que el SDK no admite) y es el
    unico punto que conoce el modelo de tarjeta del SDK; el render (`cards.py`) y el
    ingress siguen devolviendo `dict`.
    """
    return AdaptiveCard.model_validate(payload)


def _to_activity(payload: Any) -> Any:
    """Texto o `dict` de tarjeta -> actividad del SDK.

    Un `dict` se valida contra `AdaptiveCard` (si lleva algo que el SDK no admite,
    falla aqui, ruidoso) y se envia como adjunto.
    """
    if isinstance(payload, str):
        return MessageActivityInput(text=payload)
    attachment = card_attachment(AdaptiveCardAttachment(content=adaptive_card(payload)))
    return MessageActivityInput(attachments=[attachment])


def _message_id(sent: Any) -> str | None:
    """Id del mensaje publicado, o `None` si el SDK no lo entrega."""
    message_id = getattr(sent, "id", None)
    if message_id is None:
        logger.warning("El canal no devolvio el id del mensaje publicado")
        return None
    return str(message_id)


def build_app(settings: Any) -> App:
    """Crea la `App` del SDK con la identidad del bot (T6).

    El SDK elige el metodo de auth por entorno: con `client_secret` usa *client
    secret*; sin el, identidad gestionada. Sin credenciales (local, tests) el SDK
    arranca en modo no-auth; en produccion `require_teams_bot_secrets` falla antes.
    """
    options: dict[str, Any] = {"client_id": settings.teams_bot_id or None}
    if settings.teams_bot_tenant_id:
        options["tenant_id"] = settings.teams_bot_tenant_id
    if settings.teams_bot_token:
        options["client_secret"] = settings.teams_bot_token
    return App(**options)
