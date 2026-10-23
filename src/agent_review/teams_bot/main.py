"""Proceso `teams-bot`: consumo de la outbox y *messaging endpoint* (T6/T7, 013).

Arranca dos cosas en un mismo bucle de eventos:

1. El **consumo de la outbox** (`reviews:notices`): el bucle async que publica los
   avisos del worker en el canal (T5).
2. La **App del SDK**, que sirve `POST /api/messages` (el *messaging endpoint* del
   Bot Framework) y es el ingress que traduce las acciones de la tarjeta a una
   decision encolada (T7).

El *messaging endpoint* lo sirve el propio SDK (`app.start`). El SDK valida el JWT
del Bot Framework cuando hay `client_id` (fail-closed en produccion); un JWT
invalido se rechaza con `401` antes de llegar a los handlers.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any

# A nivel de modulo, no dentro de `_register_healthz`: con `from __future__ import
# annotations` FastAPI resuelve la anotacion `Request` en los globales del modulo;
# importada en local no la encuentra, toma `request` por un query param y responde
# 422 a todo `GET /healthz` (y el healthcheck de Compose nunca pasa).
from fastapi import Request
from fastapi.responses import JSONResponse

from agent_review.chat_outbox import RedisChatOutbox
from agent_review.paused_store import RedisPausedStore
from agent_review.queue import RedisReviewQueue
from agent_review.settings import Settings, get_settings, require_teams_bot_secrets
from agent_review.teams_bot.card_service import DecisionCardService
from agent_review.teams_bot.decisions import DecisionSubmitter
from agent_review.teams_bot.ingress import CardIngress
from agent_review.teams_bot.sdk_client import SDKChannelClient, build_app
from agent_review.teams_bot.sdk_handlers import register_handlers
from agent_review.teams_bot.service import TeamsBotService, build_teams_bot
from agent_review.teams_bot.state import RedisCardStore
from agent_review.teams_bot.threads import RedisChannelStore
from agent_review.teams_bot.transport import ChannelTransport
from agent_review.teams_bot.withdrawal import OutboxPauseWithdrawal

logger = logging.getLogger(__name__)


def run_forever(*, settings: Settings | None = None) -> None:
    """Arranca el bot: valida secretos, monta las piezas y sirve el endpoint.

    El `require_teams_bot_secrets` corta el arranque en produccion si falta la
    identidad del bot o el secreto compartido: fallar ruidoso al arrancar, no en la
    primera tarjeta.
    """
    settings = settings or get_settings()
    require_teams_bot_secrets(settings)
    logging.basicConfig(level=settings.log_level)

    app = build_app(settings)
    _register_healthz(app, secret=settings.teams_bot_shared_secret)

    store = RedisChannelStore(url=settings.redis_url)
    paused = RedisPausedStore(url=settings.redis_url, ttl_s=settings.decision_ttl_s)
    cards = RedisCardStore(url=settings.redis_url, ttl_s=int(settings.decision_ttl_s))
    client = SDKChannelClient(app)
    transport = ChannelTransport(client=client, store=store)
    outbox = RedisChatOutbox(url=settings.redis_url, key=settings.teams_notices_key)
    # El mismo retiro que usa la bandeja: una respuesta en Teams tambien pide
    # borrar la tarjeta, por si el borrado sincronico de abajo no llega.
    withdrawal = OutboxPauseWithdrawal(outbox)

    service = build_teams_bot(
        settings,
        client=client,
        outbox=outbox,
        store=store,
        paused=paused,
        cards=cards,
    )
    # El ingress (T7) reutiliza el mismo transporte y el inbox: marca, encola y
    # retira la tarjeta. No reanuda el grafo (C4).
    ingress = CardIngress(
        paused=paused,
        cards=DecisionCardService(store=cards),
        submitter=DecisionSubmitter(
            paused=paused,
            queue=RedisReviewQueue(url=settings.redis_url),
            cards=cards,
            withdrawal=withdrawal,
        ),
        transport=transport,
    )
    register_handlers(app, ingress=ingress, store=store, tenant_id=settings.teams_bot_tenant_id)

    logger.info("Bot de Teams arrancado (Fase 4, iniciativa 013)")
    asyncio.run(serve_bot(service, app, port=settings.teams_bot_port))


def _register_healthz(app: Any, *, secret: str) -> None:
    """Publica `GET /healthz` en el servidor del SDK, con el bearer interno.

    El SDK solo sirve POST al *messaging endpoint*; el latido de Compose necesita
    una ruta propia. Se protege con el **secreto compartido** del ingress: el
    endpoint es publico (el ingress lo expone), asi que no debe dar informacion sin
    credencial.
    """

    async def healthz(request: Request) -> JSONResponse:
        authorization = request.headers.get("authorization", "")
        expected = f"Bearer {secret}"
        if not hmac.compare_digest(authorization, expected):
            return JSONResponse({"detail": "no autorizado"}, status_code=401)
        return JSONResponse({"status": "ok"})

    app.server.adapter.app.get("/healthz")(healthz)


async def serve_bot(service: TeamsBotService, app: object, *, port: int) -> None:
    """Corre el consumidor de la outbox y el endpoint del SDK en el mismo bucle.

    `app.start` bloquea sirviendo el endpoint; el consumidor corre en paralelo. Si
    uno falla, el `gather` propaga y el proceso sale: mejor reiniciar entero (el
    `restart: unless-stopped` de Compose) que quedar a medias.
    """
    await asyncio.gather(
        service.run_forever(),
        app.start(port=port),  # type: ignore[attr-defined]
    )


def main() -> None:
    run_forever()


if __name__ == "__main__":
    main()
