"""Proceso `teams-bot`: consumo de la outbox y *messaging endpoint* (T6, 013).

Arranca dos cosas en un mismo bucle de eventos:

1. El **consumo de la outbox** (`reviews:notices`): el bucle async que publica los
   avisos del worker en el canal (T5).
2. La **App del SDK**, que sirve `POST /api/messages` (el *messaging endpoint* del
   Bot Framework) y es el ingress que recibira las acciones de la tarjeta (T7).

El *messaging endpoint* lo sirve el propio SDK (`app.start`). T7 conecta los
handlers (`on_message`, `on_card_action_execute`) y el registro del canal (C14).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any

from agent_review.chat_outbox import RedisChatOutbox
from agent_review.paused_store import RedisPausedStore
from agent_review.settings import Settings, get_settings, require_teams_bot_secrets
from agent_review.teams_bot.sdk_client import SDKChannelClient, build_app
from agent_review.teams_bot.service import TeamsBotService, build_teams_bot
from agent_review.teams_bot.threads import RedisChannelStore

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
    service = build_teams_bot(
        settings,
        client=SDKChannelClient(app),
        outbox=RedisChatOutbox(url=settings.redis_url, key=settings.teams_notices_key),
        store=RedisChannelStore(url=settings.redis_url),
        paused=RedisPausedStore(url=settings.redis_url, ttl_s=settings.decision_ttl_s),
    )

    logger.info("Bot de Teams arrancado (Fase 4, iniciativa 013)")
    asyncio.run(serve_bot(service, app, port=settings.teams_bot_port))


def _register_healthz(app: Any, *, secret: str) -> None:
    """Publica `GET /healthz` en el servidor del SDK, con el bearer interno (T7).

    El SDK solo sirve POST al *messaging endpoint*; el latido de Compose necesita
    una ruta propia. Se protege con el **secreto compartido** del ingress: el
    endpoint es publico (el ingress lo expone), asi que no debe dar informacion sin
    credencial.
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse

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
