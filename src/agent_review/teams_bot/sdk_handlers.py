"""Wiring del SDK: handlers de Teams -> `CardIngress` (T7, iniciativa 013).

Conecta `microsoft_teams` con la logica pura del ingress. Junto con `sdk_client.py`
es lo unico que importa el SDK; `ingress.py` no lo conoce, asi que sus reglas se
prueban sin Teams.

Tres handlers:

- **`on_activity`** (catch-all): guarda el `conversation_id` y el `service_url` de
  una actividad en un **canal estandar** (C14). Teams no deja inventarlos y sin
  esa referencia no hay publicacion proactiva. Un chat 1:1 o un grupo no se
  recuerdan: no son el canal dedicado (spec §6).
- **`on_card_action_execute`**: las acciones `Action.Execute` (clientes modernos).
  Devuelve la tarjeta **repintada** para quien pulso (C8).
- **`on_message`**: el fallback `Action.Submit` de clientes antiguos, que el SDK
  entrega como mensaje con el `data` de la accion en `value` (T0).

El guardado del canal es **best-effort**: si Redis falla, se registra y el ingress
sigue (sin canal no habra egress, pero el ingress no se cae por eso).
"""

from __future__ import annotations

import logging
from typing import Any

from microsoft_teams.api.models.adaptive_card.adaptive_card_action_response import (
    AdaptiveCardActionCardResponse,
    AdaptiveCardActionMessageResponse,
)

from agent_review.teams_bot.ingress import CardIngress, CardUpdate
from agent_review.teams_bot.sdk_client import adaptive_card
from agent_review.teams_bot.threads import ChannelContext, ChannelStore

logger = logging.getLogger(__name__)

# Solo el canal estandar admite publicacion proactiva del bot (spec §6).
_CHANNEL_TYPE = "channel"
_UNKNOWN = "Accion no reconocida"


def register_handlers(app: Any, *, ingress: CardIngress, store: ChannelStore) -> None:
    """Registra los handlers del Bot Framework sobre la `App` del SDK."""

    # El catch-all se registra primero: es el unico que ve TODA actividad y de el
    # depende capturar el canal (C14). No es middleware: no bloquea a los demas.
    @app.on_activity  # type: ignore[untyped-decorator]
    async def _record_channel(ctx: Any) -> None:
        # Catch-all: captura el canal (C14) y **sigue** la cadena. Sin `next()`,
        # el SDK no llega a `on_card_action_execute` ni a `on_message`.
        remember_channel(ctx.activity, store=store)
        await ctx.next()

    @app.on_card_action_execute  # type: ignore[untyped-decorator]
    async def _on_card_action(ctx: Any) -> Any:
        activity = ctx.activity
        remember_channel(activity, store=store)
        user_id, actor = _identity(activity)
        update = await ingress.handle(_action_data(activity.value), user_id=user_id, actor=actor)
        if update is not None and update.card is not None:
            # C8: `Action.Execute` repinta la tarjeta solo para quien pulso.
            return AdaptiveCardActionCardResponse(value=adaptive_card(update.card))
        return AdaptiveCardActionMessageResponse(value=_notice(update) or _UNKNOWN)

    @app.on_message  # type: ignore[untyped-decorator]
    async def _on_message(ctx: Any) -> None:
        activity = ctx.activity
        remember_channel(activity, store=store)
        # Fallback `Action.Submit` (T0): el `data` de la accion llega en `value`.
        # Un mensaje normal no lo trae, y se ignora sin responder.
        if not isinstance(activity.value, dict) or "action" not in activity.value:
            return
        user_id, actor = _identity(activity)
        update = await ingress.handle(dict(activity.value), user_id=user_id, actor=actor)
        notice = _notice(update)
        if notice:
            await ctx.send(notice)


def remember_channel(activity: Any, *, store: ChannelStore) -> None:
    """C14: guarda el canal dedicado al ver una actividad en un canal estandar.

    El `conversation_id` de la actividad es el del canal donde esta instalado el
    bot; de ahi cuelga el hilo de cada PR. `service_url` viaja siempre porque toda
    llamada proactiva debe llevarlo (hallazgo del spike, T0). Un chat personal o
    un grupo **no** pisan el canal dedicado.
    """
    conversation = getattr(activity, "conversation", None)
    conversation_id = getattr(conversation, "id", None)
    if not conversation_id:
        return
    conversation_type = getattr(conversation, "conversation_type", None)
    if conversation_type not in (None, "", _CHANNEL_TYPE):
        logger.debug(
            "Actividad en conversacion %s (%s); no se recuerda como canal",
            conversation_id,
            conversation_type,
        )
        return
    context = ChannelContext(
        conversation_id=str(conversation_id),
        service_url=str(getattr(activity, "service_url", "") or ""),
    )
    try:
        store.remember_context(context)
    except Exception as exc:  # noqa: BLE001 - sin Redis, el ingress sigue vivo
        logger.warning("No se pudo recordar el canal %s: %s", conversation_id, exc)


def _action_data(value: Any) -> dict[str, Any]:
    """`data` de la accion de un `Action.Execute` (`value.action.data`)."""
    data = getattr(getattr(value, "action", None), "data", None)
    return dict(data) if isinstance(data, dict) else {}


def _identity(activity: Any) -> tuple[str, str]:
    """`(user_id, actor)` de quien pulsa: id de Entra ID y nombre visible (C5)."""
    sender = getattr(activity, "from_", None)
    user_id = str(getattr(sender, "aad_object_id", None) or getattr(sender, "id", None) or "")
    actor = str(getattr(sender, "name", None) or user_id or "alguien")
    return user_id, actor


def _notice(update: CardUpdate | None) -> str:
    return update.notice if update is not None else ""
