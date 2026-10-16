"""Transporte del bot: resuelve el destino de un aviso y lo publica (T4, 013).

Es la **capa HTTP interna del bot** (C10): el worker no la conoce. Aqui vive la
politica de hilo por PR (C6/C7):

- Sin hilo para el PR, el primer aviso abre el mensaje **raiz** del hilo.
- Con hilo, los avisos van como **reply** de ese hilo.
- La tarjeta de una review se **edita** (`updateActivity`) al volver a aparecer,
  y se identifica por su `review_key`; no se inunda el canal.

El render y el estado de la tarjeta no viven aqui: esto solo sabe *donde* publicar.
Quien llama (`ChatTransport` en el servicio, T5) le pasa el payload ya construido
(un `dict` de Adaptive Card o un texto).

El SDK se usa a traves de `ChannelClient`, un puerto minimo que un fake puede
implementar: asi la logica de hilo es testeable sin Teams ni red.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from agent_review.teams_bot.threads import CardRef, ChannelStore, ThreadRef

logger = logging.getLogger(__name__)

# Payload de una actividad: un texto o el `dict` de una Adaptive Card.
ChannelPayload = dict[str, Any] | str


@runtime_checkable
class ChannelClient(Protocol):
    """Operaciones del SDK de Teams que usa el transporte (implementa el adaptador real).

    Cada metodo devuelve el id del mensaje publicado (o `None` si el SDK no lo
    entrega); con el, el transporte recuerda donde editar despues.
    """

    async def send(
        self, conversation_id: str, payload: ChannelPayload, *, service_url: str = ""
    ) -> str | None:
        """Publica un mensaje **raiz** (fuera de cualquier hilo)."""
        ...

    async def reply(
        self,
        conversation_id: str,
        root_message_id: str,
        payload: ChannelPayload,
        *,
        service_url: str = "",
    ) -> str | None:
        """Publica **dentro** del hilo del mensaje raiz (`;messageid=`)."""
        ...

    async def update(
        self,
        conversation_id: str,
        message_id: str,
        payload: ChannelPayload,
        *,
        service_url: str = "",
    ) -> None:
        """Edita un mensaje ya publicado (la tarjeta que avanza: C7)."""
        ...


class ChannelTransport:
    """Publica avisos en el hilo del PR, abriendo el hilo la primera vez (C6)."""

    def __init__(self, *, client: ChannelClient, store: ChannelStore) -> None:
        self._client = client
        self._store = store

    async def post_text(self, *, repo: str, pr_id: int, text: str) -> bool:
        """Aviso de texto: reply del hilo del PR, o raiz si aun no existe (C6).

        Devuelve si llego a publicarse: sin canal capturado (C14) no hay destino
        proactivo todavia y el aviso no se puede entregar.
        """
        thread = self._store.thread(repo=repo, pr_id=pr_id)
        if thread is not None:
            await self._client.reply(
                thread.conversation_id, thread.root_message_id, text, service_url=thread.service_url
            )
            return True
        return await self._open_thread(repo=repo, pr_id=pr_id, payload=text)

    async def post_card(
        self, review_key: str, *, repo: str, pr_id: int, card: dict[str, Any]
    ) -> bool:
        """Tarjeta de decision: la edita si ya existe, o la publica en el hilo (C7).

        La tarjeta se localiza por `review_key`: el mismo aviso que se reentrega
        (o un cambio de estado del grafo) **repinta** la tarjeta en su sitio en vez
        de abrir otra. Si no consta, se publica —como raiz o como reply del hilo
        del PR— y se recuerda su mensaje para editarla despues.
        """
        existing = self._store.card(review_key)
        if existing is not None:
            await self._client.update(
                existing.conversation_id,
                existing.message_id,
                card,
                service_url=existing.service_url,
            )
            return True

        thread = self._store.thread(repo=repo, pr_id=pr_id)
        if thread is None:
            return await self._open_thread(
                repo=repo, pr_id=pr_id, payload=card, review_key=review_key
            )
        message_id = await self._client.reply(
            thread.conversation_id, thread.root_message_id, card, service_url=thread.service_url
        )
        if message_id is not None:
            self._store.remember_card(
                review_key,
                CardRef(
                    conversation_id=thread.conversation_id,
                    message_id=message_id,
                    service_url=thread.service_url,
                ),
            )
        return True

    async def _open_thread(
        self,
        *,
        repo: str,
        pr_id: int,
        payload: ChannelPayload,
        review_key: str | None = None,
    ) -> bool:
        """Abre el hilo raiz del PR en el canal dedicado (C14).

        Sin canal capturado no hay destino proactivo (C14): se avisa y se
        devuelve `False` para que el servicio decida (T5). El `service_url` hay que
        capturarlo de una actividad entrante: sin el no hay publicacion proactiva.
        """
        context = self._store.context()
        if context is None or not context.conversation_id:
            logger.info("Sin canal capturado todavia; no se puede abrir el hilo del PR %s", repo)
            return False
        message_id = await self._client.send(
            context.conversation_id, payload, service_url=context.service_url
        )
        if message_id is None:
            # El mensaje **ya esta publicado**: devolver `False` haria que el servicio
            # lo reintentara y lo duplicara. Sin id no hay hilo que recordar; el
            # siguiente aviso del PR abrira otro raiz.
            logger.warning("El canal no devolvio el id del mensaje raiz del PR %s#%s", repo, pr_id)
            return True
        self._store.remember_thread(
            repo=repo,
            pr_id=pr_id,
            ref=ThreadRef(
                conversation_id=context.conversation_id,
                root_message_id=message_id,
                service_url=context.service_url,
            ),
        )
        if review_key is not None:
            self._store.remember_card(
                review_key,
                CardRef(
                    conversation_id=context.conversation_id,
                    message_id=message_id,
                    service_url=context.service_url,
                ),
            )
        return True
