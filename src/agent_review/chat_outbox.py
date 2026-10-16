from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

import redis

from agent_review.domain.models import ApprovalRequest, ChatNotice
from agent_review.redis_utils import build_redis_client, is_transient_redis_error

logger = logging.getLogger(__name__)

# Outbox de avisos al canal de chat (C10). El worker **encola**; el bot de Teams
# la consume con `BLPOP`. Misma forma que `ReviewQueue`/`FixQueue`: una cola con
# DLQ para lo descartado, y una version en memoria para tests.
NOTICES_KEY = "reviews:notices"
NOTICES_DEAD_LETTER_KEY = "reviews:notices:dead"


@runtime_checkable
class ChatOutbox(Protocol):
    """Cola durable de avisos al canal. La escribe el worker, la lee el bot."""

    def enqueue(self, notice: ChatNotice) -> None: ...

    def requeue(self, notice: ChatNotice) -> None:
        """Devuelve un aviso ya sacado a la **cabeza** de la cola (no pierde su turno)."""
        ...

    def dequeue(self, *, timeout_s: float = 5.0) -> ChatNotice | None:
        """Saca el siguiente aviso o `None` si no hay nada en el timeout."""
        ...

    def dead_letter(self, notice: ChatNotice, *, reason: str) -> None:
        """Registra un aviso descartado (p. ej. la pausa ya vencio: C13)."""
        ...

    def dead_letters(self) -> list[dict[str, object]]:
        """Avisos descartados, para inspeccion o reinyeccion manual."""
        ...

    def __len__(self) -> int: ...


class RedisChatOutbox:
    def __init__(
        self,
        *,
        url: str,
        key: str = NOTICES_KEY,
        dead_letter_key: str = NOTICES_DEAD_LETTER_KEY,
    ) -> None:
        # `blocking=True`: `BLPOP` bloquea y no debe llevar deadline de socket.
        self._client = build_redis_client(url, blocking=True)
        self._key = key
        self._dead_key = dead_letter_key

    def available(self) -> bool:
        try:
            return bool(self._client.ping())
        except redis.exceptions.RedisError:
            return False

    def enqueue(self, notice: ChatNotice) -> None:
        self._client.rpush(self._key, notice.model_dump_json())

    def requeue(self, notice: ChatNotice) -> None:
        self._client.lpush(self._key, notice.model_dump_json())

    def dequeue(self, *, timeout_s: float = 5.0) -> ChatNotice | None:
        """Siguiente aviso, o `None` en el timeout. Mismo trato que las colas.

        La espera vencida, un Redis que cae a mitad y un servidor que se reinicia
        son rutinas de un consumidor de cola: se registran y se devuelve `None`
        para reintentar en la siguiente vuelta, no se propaga una excepcion. Un
        payload ilegible ya salio de la cola con el `BLPOP`: va **en crudo** a la DLQ
        para no perderlo.
        """
        try:
            item = self._client.blpop(self._key, timeout=max(1, int(timeout_s)))
        except Exception as exc:  # noqa: BLE001 - el poll no debe tumbar al bot
            if not is_transient_redis_error(exc):
                raise
            logger.warning("Outbox no disponible al leer; se reintenta en el poll: %s", exc)
            return None
        if item is None:
            return None
        _, payload = item
        notice = _parse_notice(payload)
        if notice is None:
            self._client.rpush(self._dead_key, _raw_dead_letter_payload(payload))
        return notice

    def dead_letter(self, notice: ChatNotice, *, reason: str) -> None:
        self._client.rpush(self._dead_key, _dead_letter_payload(notice, reason=reason))

    def dead_letters(self) -> list[dict[str, object]]:
        return _parse_dead_letters(list(self._client.lrange(self._dead_key, 0, -1)))

    def __len__(self) -> int:
        return int(self._client.llen(self._key))


class InMemoryChatOutbox:
    """Outbox compartible en tests y fallback sin Redis."""

    def __init__(self) -> None:
        self._items: deque[str] = deque()
        self._dead: list[str] = []

    def enqueue(self, notice: ChatNotice) -> None:
        self._items.append(notice.model_dump_json())

    def requeue(self, notice: ChatNotice) -> None:
        self._items.appendleft(notice.model_dump_json())

    def dequeue(self, *, timeout_s: float = 5.0) -> ChatNotice | None:
        del timeout_s  # sin espera en memoria
        if not self._items:
            return None
        payload = self._items.popleft()
        notice = _parse_notice(payload)
        if notice is None:
            self._dead.append(_raw_dead_letter_payload(payload))
        return notice

    def dead_letter(self, notice: ChatNotice, *, reason: str) -> None:
        self._dead.append(_dead_letter_payload(notice, reason=reason))

    def dead_letters(self) -> list[dict[str, object]]:
        return _parse_dead_letters(self._dead)

    def __len__(self) -> int:
        return len(self._items)

    @property
    def dead_letter_count(self) -> int:
        return len(self._dead)


class OutboxChatAdapter:
    """`ChatPort` que **encola** en la outbox en vez de llamar por HTTP (C10).

    El worker no conoce el transporte: deja el aviso en `reviews:notices` y el bot
    decide el hilo y la tarjeta. Si Redis no responde, el aviso se pierde (misma
    asimetria aceptada que la ingesta, D6): se registra y **no** se propaga, para
    que una caida de la outbox no tumbe una review ya publicada ni la pausa. El
    respaldo real es el CLI (C9).
    """

    def __init__(self, *, outbox: ChatOutbox) -> None:
        self._outbox = outbox

    def _enqueue(self, notice: ChatNotice) -> None:
        try:
            self._outbox.enqueue(notice)
        except Exception as exc:  # noqa: BLE001 - el egress no tumba el worker
            if not is_transient_redis_error(exc):
                raise
            logger.warning(
                "Outbox de avisos no disponible; se pierde el aviso de %s: %s",
                notice.review_key,
                exc,
            )

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
        self._enqueue(
            ChatNotice(
                kind="published",
                review_key=review_key,
                repo=repo,
                pr_id=pr_id,
                title=title,
                body=body,
                link=link,
            )
        )

    def notify_decision_pending(
        self,
        *,
        repo: str,
        pr_id: int,
        review_key: str,
        approval: ApprovalRequest,
        link: str | None = None,
    ) -> None:
        self._enqueue(
            ChatNotice(
                kind="pause",
                review_key=review_key,
                repo=repo,
                pr_id=pr_id,
                approval=approval,
                link=link,
            )
        )


def _parse_notice(payload: bytes | str) -> ChatNotice | None:
    try:
        return ChatNotice.model_validate_json(payload)
    except ValueError:
        logger.error("Aviso ilegible en la outbox; va a la DLQ en crudo")
        return None


def _raw_dead_letter_payload(payload: bytes | str) -> str:
    import json

    text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
    return json.dumps(
        {"reason": "Aviso ilegible en la outbox", "discarded_at": time.time(), "raw": text}
    )


def _dead_letter_payload(notice: ChatNotice, *, reason: str) -> str:
    import json

    return json.dumps(
        {
            "review_key": notice.review_key,
            "kind": notice.kind,
            "reason": reason,
            "discarded_at": time.time(),
            "notice": notice.model_dump(mode="json"),
        }
    )


def _parse_dead_letters(raw: Sequence[bytes | str]) -> list[dict[str, object]]:
    import json

    parsed: list[dict[str, object]] = []
    for item in raw:
        text = item.decode("utf-8", errors="replace") if isinstance(item, bytes) else item
        try:
            entry = json.loads(text)
        except ValueError:
            logger.warning("Entrada ilegible en la DLQ de avisos: %s", text)
            continue
        if isinstance(entry, dict):
            parsed.append(entry)
    return parsed
