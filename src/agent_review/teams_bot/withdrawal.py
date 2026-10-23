"""Pedir al canal que retire una tarjeta de decision ya respondida.

La bandeja y el bot comparten el sello de la primera respuesta. Quien gana encola
un `ChatNotice` `withdraw`: el bot, que es el unico que habla con Teams, borra el
mensaje. Sin Teams configurado no hay consumidor y no se encola nada.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_review.chat_outbox import ChatOutbox
from agent_review.domain.models import ChatNotice


@runtime_checkable
class PauseWithdrawal(Protocol):
    """Aviso de que la pregunta ya no debe seguir publicada en el canal."""

    def withdraw(self, *, review_key: str, repo: str, pr_id: int) -> None: ...


class OutboxPauseWithdrawal:
    """Deja el aviso `withdraw` en la misma outbox que consume el bot."""

    def __init__(self, outbox: ChatOutbox) -> None:
        self._outbox = outbox

    def withdraw(self, *, review_key: str, repo: str, pr_id: int) -> None:
        self._outbox.enqueue(
            ChatNotice(kind="withdraw", review_key=review_key, repo=repo, pr_id=pr_id)
        )
