"""Mapeo de canal, hilos por PR y tarjetas (C6/C7/C14, iniciativa 013).

Tres piezas de Redis, todas **UI, no verdad** (si se pierden, se abre un hilo o
una tarjeta nuevos; nada durable se corrompe):

- **Canal dedicado (C14):** `conversation_id` y `service_url`, capturados al
  instalar el bot (`install.add`) o al ver cualquier actividad. Teams no deja
  inventarlos: sin ellos no hay publicacion proactiva.
- **Hilo por PR (C6):** `repo:pr -> {conversation_id, root_message_id,
  service_url}`. La primera review del PR abre el mensaje **raiz**; el resto va
  como reply de ese mismo hilo.
- **Tarjeta de una review (C7):** `review_key -> {conversation_id, message_id}`.
  La tarjeta de decision se guarda para poder **editarla** (`updateActivity`) al
  avanzar el grafo en vez de publicar una nueva.

Los `repo:pr` y las `review_key` se **hashean** como campo de hash: no caben en el
nombre de una clave (creceria sin fin) y sus `:` y `/` harian ambigua la lectura.
El hash no se puede leer a ojo; para depurar un hilo concreto,
`hget reviews:teams:threads <sha256(repo:pr)[:12]>`.

El store es tolerante a proposito: una entrada ilegible o con un
`root_message_id` no numerico (el SDK lo exige: T0) se trata como "hilo no
iniciado" en vez de propagar el error.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import redis

from agent_review.redis_utils import build_redis_client

logger = logging.getLogger(__name__)

CHANNEL_KEY = "reviews:teams:channel"
THREADS_KEY = "reviews:teams:threads"
CARDS_KEY = "reviews:teams:cards"
# Sin TTL: un hilo de PR y su tarjeta deben vivir mientras viva el PR. Perder uno
# solo cuesta un mensaje nuevo (C6).
_DIGEST_LEN = 12


@dataclass(frozen=True)
class ChannelContext:
    """Contexto del canal dedicado: a donde publica el bot de forma proactiva."""

    conversation_id: str
    service_url: str = ""


@dataclass(frozen=True)
class ThreadRef:
    """Hilo de un PR: el `conversation_id` y el mensaje raiz al que responder.

    `service_url` viaja en el hilo (no se hereda del canal) porque toda llamada
    proactiva debe llevarlo: omitirlo manda el envio al Teams real (T0).
    """

    conversation_id: str
    root_message_id: str
    service_url: str = ""


@dataclass(frozen=True)
class CardRef:
    """Mensaje que sostiene la tarjeta de una review, para poder editarla (C7)."""

    conversation_id: str
    message_id: str
    service_url: str = ""


@runtime_checkable
class ChannelStore(Protocol):
    """Canal, hilos y tarjetas del bot. Se puede perder: la verdad esta en Postgres."""

    def context(self) -> ChannelContext | None:
        """Canal dedicado, o `None` si el bot aun no ha visto una actividad."""
        ...

    def remember_context(self, context: ChannelContext) -> None:
        """Guarda el canal tras una actividad (instalacion o mensaje)."""
        ...

    def thread(self, *, repo: str, pr_id: int) -> ThreadRef | None:
        """Hilo del PR, o `None` si aun no se ha abierto (o el mapeo se perdio)."""
        ...

    def remember_thread(self, *, repo: str, pr_id: int, ref: ThreadRef) -> None:
        """Guarda el hilo recien abierto de un PR."""
        ...

    def card(self, review_key: str) -> CardRef | None:
        """Mensaje de la tarjeta de una review, o `None` si aun no se ha publicado."""
        ...

    def remember_card(self, review_key: str, ref: CardRef) -> None:
        """Guarda el mensaje de la tarjeta recien publicada."""
        ...


def thread_field(repo: str, pr_id: int) -> str:
    """Campo del hash de hilos para un PR. Ver `THREADS_KEY`."""
    return _digest(f"{repo}:{pr_id}")


def card_field(review_key: str) -> str:
    """Campo del hash de tarjetas para una review. Ver `CARDS_KEY`."""
    return _digest(review_key)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:_DIGEST_LEN]


def _dump(**fields: str) -> str:
    return json.dumps(fields)


def _load(raw: object) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
    except (ValueError, TypeError) as exc:
        logger.warning("Entrada ilegible del canal en Redis (%r): %s", raw, exc)
        return None
    return payload if isinstance(payload, dict) else None


def _valid_message_id(message_id: str) -> bool:
    """El SDK exige un id numerico y distinto de cero al construir el *thread id*."""
    return message_id.isdigit() and int(message_id) != 0


def _parse_thread(raw: object) -> ThreadRef | None:
    payload = _load(raw)
    if payload is None:
        return None
    try:
        ref = ThreadRef(
            conversation_id=str(payload["conversation_id"]),
            root_message_id=str(payload["root_message_id"]),
            service_url=str(payload.get("service_url", "")),
        )
    except KeyError as exc:
        logger.warning("Hilo incompleto en Redis (%r): falta %s", raw, exc)
        return None
    if not _valid_message_id(ref.root_message_id):
        logger.warning("root_message_id no numerico (%r); hilo no iniciado", ref.root_message_id)
        return None
    return ref


def _parse_card(raw: object) -> CardRef | None:
    payload = _load(raw)
    if payload is None:
        return None
    try:
        return CardRef(
            conversation_id=str(payload["conversation_id"]),
            message_id=str(payload["message_id"]),
            service_url=str(payload.get("service_url", "")),
        )
    except KeyError as exc:
        logger.warning("Tarjeta incompleta en Redis (%r): falta %s", raw, exc)
        return None


class RedisChannelStore:
    """Canal, hilos y tarjetas en Redis. Un fallo de Redis no propaga: se publica de nuevo."""

    def __init__(
        self,
        *,
        url: str,
        channel_key: str = CHANNEL_KEY,
        threads_key: str = THREADS_KEY,
        cards_key: str = CARDS_KEY,
    ) -> None:
        self._client = build_redis_client(url)
        self._channel_key = channel_key
        self._threads_key = threads_key
        self._cards_key = cards_key

    def available(self) -> bool:
        try:
            return bool(self._client.ping())
        except redis.exceptions.RedisError:
            return False

    def context(self) -> ChannelContext | None:
        try:
            raw = self._client.get(self._channel_key)
        except redis.exceptions.RedisError as exc:
            logger.warning("Canal no disponible en Redis; no hay publicacion proactiva: %s", exc)
            return None
        if raw is None:
            return None
        payload = _load(raw)
        if payload is None:
            return None
        try:
            return ChannelContext(
                conversation_id=str(payload["conversation_id"]),
                service_url=str(payload.get("service_url", "")),
            )
        except KeyError as exc:
            logger.warning("Contexto de canal incompleto en Redis (%r): falta %s", raw, exc)
            return None

    def remember_context(self, context: ChannelContext) -> None:
        self._client.set(
            self._channel_key,
            _dump(conversation_id=context.conversation_id, service_url=context.service_url),
        )

    def thread(self, *, repo: str, pr_id: int) -> ThreadRef | None:
        return _parse_thread(self._hget(self._threads_key, thread_field(repo, pr_id)))

    def remember_thread(self, *, repo: str, pr_id: int, ref: ThreadRef) -> None:
        self._client.hset(
            self._threads_key,
            thread_field(repo, pr_id),
            _dump(
                conversation_id=ref.conversation_id,
                root_message_id=ref.root_message_id,
                service_url=ref.service_url,
            ),
        )

    def card(self, review_key: str) -> CardRef | None:
        return _parse_card(self._hget(self._cards_key, card_field(review_key)))

    def remember_card(self, review_key: str, ref: CardRef) -> None:
        self._client.hset(
            self._cards_key,
            card_field(review_key),
            _dump(
                conversation_id=ref.conversation_id,
                message_id=ref.message_id,
                service_url=ref.service_url,
            ),
        )

    def _hget(self, key: str, field: str) -> bytes | str | None:
        try:
            return self._client.hget(key, field)
        except redis.exceptions.RedisError as exc:
            logger.warning("Mapeo del canal no disponible en Redis; se publicara de nuevo: %s", exc)
            return None


class InMemoryChannelStore:
    """Store de apoyo para tests y para correr sin Redis."""

    def __init__(self) -> None:
        self._context: ChannelContext | None = None
        self._threads: dict[str, str] = {}
        self._cards: dict[str, str] = {}

    def context(self) -> ChannelContext | None:
        return self._context

    def remember_context(self, context: ChannelContext) -> None:
        self._context = context

    def thread(self, *, repo: str, pr_id: int) -> ThreadRef | None:
        raw = self._threads.get(thread_field(repo, pr_id))
        return _parse_thread(raw) if raw is not None else None

    def remember_thread(self, *, repo: str, pr_id: int, ref: ThreadRef) -> None:
        self._threads[thread_field(repo, pr_id)] = _dump(
            conversation_id=ref.conversation_id,
            root_message_id=ref.root_message_id,
            service_url=ref.service_url,
        )

    def card(self, review_key: str) -> CardRef | None:
        raw = self._cards.get(card_field(review_key))
        return _parse_card(raw) if raw is not None else None

    def remember_card(self, review_key: str, ref: CardRef) -> None:
        self._cards[card_field(review_key)] = _dump(
            conversation_id=ref.conversation_id,
            message_id=ref.message_id,
            service_url=ref.service_url,
        )
