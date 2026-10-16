"""Wiring del SDK: handlers de Teams -> `CardIngress` (T7, iniciativa 013).

Conecta `microsoft_teams` con la logica pura del ingress. Junto con `sdk_client.py`
es lo unico que importa el SDK; `ingress.py` no lo conoce, asi que sus reglas se
prueban sin Teams.

Tres piezas:

- **Middleware** (`app.use`) para capturar el canal (C14). El SDK encadena los
  handlers y un handler normal **corta la cadena** si no llama a `ctx.next()`; el
  middleware es justo el contrato para lo transversal y lo llama. Ve **toda**
  actividad (instalacion incluida), que es lo que C14 pide: sin `conversation_id`
  ni `service_url` no hay publicacion proactiva.
- **`on_card_action_execute`**: las acciones `Action.Execute` (clientes modernos).
- **`on_message`**: el fallback `Action.Submit` de clientes antiguos, que el SDK
  entrega como mensaje con el `data` de la accion en `value` (T0).

El guardado del canal es **best-effort**: si Redis falla, se registra y el ingress
sigue (sin canal no habra egress, pero el ingress no se cae por eso).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from microsoft_teams.api.models.adaptive_card.adaptive_card_action_response import (
    AdaptiveCardActionCardResponse,
    AdaptiveCardActionMessageResponse,
)

from agent_review.teams_bot.ingress import CardIngress, CardUpdate
from agent_review.teams_bot.sdk_client import adaptive_card
from agent_review.teams_bot.threads import ChannelContext, ChannelStore

logger = logging.getLogger(__name__)

_UNKNOWN = "Accion no reconocida"
# Un chat privado con el bot no es el canal donde se publican los avisos: si se
# tomara como destino, el egress acabaria en una conversacion de una persona.
_PERSONAL = "personal"


class TeamsApp(Protocol):
    """Superficie del SDK que usa el wiring.

    El SDK no trae `py.typed`, asi que sus decoradores aparecen como `Any` y mypy en
    estricto los rechaza. Declarar la parte que se usa deja los handlers tipados y,
    de paso, documenta el contrato.
    """

    def use(self, middleware: Any) -> None: ...
    def on_card_action_execute(self, handler: Any) -> Any: ...
    def on_message(self, handler: Any) -> Any: ...


def register_handlers(app: TeamsApp, *, ingress: CardIngress, store: ChannelStore) -> None:
    """Registra el middleware del canal y los handlers de la tarjeta."""

    async def _remember_channel(ctx: Any) -> None:
        remember_channel(ctx.activity, store=store)
        # Sin esto, la cadena del SDK se corta aqui y la accion de la tarjeta
        # no llegaria nunca a su handler.
        await ctx.next()

    app.use(_remember_channel)

    @app.on_card_action_execute
    async def _on_card_action(ctx: Any) -> Any:
        activity = ctx.activity
        user_id, actor = _identity(activity)
        update = await ingress.handle(_action_data(activity.value), user_id=user_id, actor=actor)
        if update is not None and update.card is not None:
            # `Action.Execute` repinta la tarjeta de quien pulso (C8).
            return AdaptiveCardActionCardResponse(value=adaptive_card(update.card))
        return AdaptiveCardActionMessageResponse(value=_notice(update) or _UNKNOWN)

    @app.on_message
    async def _on_message(ctx: Any) -> None:
        activity = ctx.activity
        # Fallback `Action.Submit` (T0): el `data` de la accion llega en `value`. Un
        # mensaje normal no lo trae, y se ignora sin responder.
        if not isinstance(activity.value, dict) or "action" not in activity.value:
            return
        user_id, actor = _identity(activity)
        update = await ingress.handle(dict(activity.value), user_id=user_id, actor=actor)
        notice = _notice(update)
        if notice:
            await ctx.send(notice)


def remember_channel(activity: Any, *, store: ChannelStore) -> None:
    """C14: guarda el canal dedicado al ver una actividad.

    El `conversation_id` de la actividad es el del canal donde esta instalado el bot;
    de ahi cuelga el hilo de cada PR. `service_url` viaja siempre porque toda llamada
    proactiva debe llevarlo (hallazgo del spike, T0).

    Dos cautelas: un **chat privado** no es el canal y no se guarda (seria publicar los
    avisos en la conversacion de una persona), y si el destino **cambia** se registra,
    porque el diseño asume un canal dedicado (C14) y un segundo canal seria un error de
    configuracion silencioso.
    """
    conversation = getattr(activity, "conversation", None)
    conversation_id = getattr(conversation, "id", None)
    if not conversation_id:
        return
    if str(getattr(conversation, "conversation_type", "") or "") == _PERSONAL:
        logger.debug("Actividad de un chat privado; no se toma como canal de avisos")
        return
    previous = _current_channel(store)
    if previous is not None and previous != conversation_id:
        logger.warning(
            "El canal de avisos cambia de %s a %s: el bot esta en mas de un canal",
            previous,
            conversation_id,
        )
    context = ChannelContext(
        conversation_id=str(conversation_id),
        service_url=str(getattr(activity, "service_url", "") or ""),
    )
    try:
        store.remember_context(context)
    except Exception as exc:  # noqa: BLE001 - sin Redis, el ingress sigue vivo
        logger.warning("No se pudo recordar el canal %s: %s", conversation_id, exc)


def _current_channel(store: ChannelStore) -> str | None:
    try:
        context = store.context()
    except Exception:  # noqa: BLE001 - la comparacion es solo para avisar
        return None
    return context.conversation_id if context is not None else None


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
