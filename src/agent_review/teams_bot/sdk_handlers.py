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

**Frontera de confianza (C5/C14).** Decide quien es miembro del canal dedicado, y
nada mas: la **primera** conversacion capturada es el canal y otra nunca la
sustituye; una accion de tarjeta solo se acepta si llega de ese canal (el id base,
sin el sufijo `;messageid=` de los hilos) y, con `tenant_id` configurado, de ese
tenant. Sin canal capturado no se acepta ninguna accion: no hay tarjeta legitima
que pulsar.
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
_FOREIGN = "Esta conversacion no es el canal de decision de las reviews."
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


def register_handlers(
    app: TeamsApp, *, ingress: CardIngress, store: ChannelStore, tenant_id: str = ""
) -> None:
    """Registra el middleware del canal y los handlers de la tarjeta.

    `tenant_id` es el tenant del bot (`TEAMS_BOT_TENANT_ID`); vacio, no se comprueba.
    """

    async def _remember_channel(ctx: Any) -> None:
        if _tenant_matches(ctx.activity, tenant_id):
            remember_channel(ctx.activity, store=store)
        # Sin esto, la cadena del SDK se corta aqui y la accion de la tarjeta
        # no llegaria nunca a su handler.
        await ctx.next()

    app.use(_remember_channel)

    @app.on_card_action_execute
    async def _on_card_action(ctx: Any) -> Any:
        activity = ctx.activity
        if not _from_decision_channel(activity, store=store, tenant_id=tenant_id):
            return AdaptiveCardActionMessageResponse(value=_FOREIGN)
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
        if not _from_decision_channel(activity, store=store, tenant_id=tenant_id):
            await ctx.send(_FOREIGN)
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

    Tres cautelas:

    - Un **chat privado** no es el canal y no se guarda (seria publicar los avisos en
      la conversacion de una persona).
    - Se guarda el id **base**: una accion pulsada dentro del hilo de un PR trae
      `<canal>;messageid=<raiz>`, y guardarlo tal cual haria que la raiz del
      siguiente PR se publicara como respuesta en el hilo de otro.
    - Gana la **primera** captura: si llega otra conversacion, se registra y se
      ignora. Sustituirla moveria los avisos y la autoridad de decision (C5) a otra
      conversacion; mover el canal es una operacion explicita (borrar la clave).
    """
    conversation = getattr(activity, "conversation", None)
    conversation_id = _base_id(getattr(conversation, "id", None))
    if not conversation_id:
        return
    if str(getattr(conversation, "conversation_type", "") or "") == _PERSONAL:
        logger.debug("Actividad de un chat privado; no se toma como canal de avisos")
        return
    previous = _current_channel(store)
    if previous is not None and previous != conversation_id:
        logger.warning(
            "Actividad de %s ignorada como canal: el canal de avisos es %s",
            conversation_id,
            previous,
        )
        return
    context = ChannelContext(
        conversation_id=conversation_id,
        service_url=str(getattr(activity, "service_url", "") or ""),
    )
    try:
        store.remember_context(context)
    except Exception as exc:  # noqa: BLE001 - sin Redis, el ingress sigue vivo
        logger.warning("No se pudo recordar el canal %s: %s", conversation_id, exc)


def _from_decision_channel(activity: Any, *, store: ChannelStore, tenant_id: str) -> bool:
    """C5: la accion viene del canal capturado (y del tenant del bot, si se fija).

    Falla cerrado: sin canal capturado, o si no se puede leer, se rechaza.
    """
    if not _tenant_matches(activity, tenant_id):
        logger.warning("Accion de tarjeta de otro tenant rechazada")
        return False
    channel = _current_channel(store)
    conversation_id = _base_id(getattr(getattr(activity, "conversation", None), "id", None))
    if channel is None or conversation_id != channel:
        logger.warning(
            "Accion de tarjeta de %s rechazada: el canal de decision es %s",
            conversation_id or "?",
            channel or "(sin capturar)",
        )
        return False
    return True


def _tenant_matches(activity: Any, tenant_id: str) -> bool:
    """`True` sin tenant configurado; si no, el de la actividad debe coincidir."""
    if not tenant_id:
        return True
    tenant = getattr(getattr(activity, "conversation", None), "tenant_id", None) or getattr(
        getattr(getattr(activity, "channel_data", None), "tenant", None), "id", None
    )
    return str(tenant or "") == tenant_id


def _current_channel(store: ChannelStore) -> str | None:
    """Canal capturado, o `None` si no hay o no se puede leer (quien llama decide)."""
    try:
        context = store.context()
    except Exception:  # noqa: BLE001 - un fallo de lectura cuenta como "sin canal"
        return None
    return context.conversation_id if context is not None else None


def _base_id(conversation_id: Any) -> str:
    """Id de la conversacion sin el sufijo `;messageid=` de un hilo (como el SDK)."""
    return str(conversation_id or "").split(";")[0]


def _action_data(value: Any) -> dict[str, Any]:
    """`data` de la accion de un `Action.Execute` (`value.action.data`)."""
    data = getattr(getattr(value, "action", None), "data", None)
    return dict(data) if isinstance(data, dict) else {}


def _identity(activity: Any) -> tuple[str, str]:
    """`(user_id, actor)` de quien pulsa (C5).

    `user_id` es el id de Entra ID (indexa el estado por usuario). `actor` es lo que
    queda auditado en la decision: el nombre visible **y** el id, porque C5 pide la
    identidad de Entra ID y un nombre solo puede cambiar o repetirse.
    """
    sender = getattr(activity, "from_", None)
    user_id = str(getattr(sender, "aad_object_id", None) or getattr(sender, "id", None) or "")
    name = str(getattr(sender, "name", None) or "")
    if name and user_id:
        return user_id, f"{name} <{user_id}>"
    return user_id, name or user_id or "alguien"


def _notice(update: CardUpdate | None) -> str:
    return update.notice if update is not None else ""
