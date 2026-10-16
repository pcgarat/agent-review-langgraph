"""Servicio de la tarjeta de decision de Teams (T2, iniciativa 013).

Une el render (`cards.py`) con el estado por usuario (`state.py`). Aplica las
acciones de un hallazgo (marcar `aprobar`/`rechazar`/`anotar`) y devuelve la
tarjeta **actualizada para ese usuario**, conservando lo que ya tenia marcado
(C3/C8). Enviar la decision completa y encolarla es T3; aqui solo se gobierna el
estado que decide si Enviar esta habilitado.
"""

from __future__ import annotations

import logging

from agent_review.domain.models import ApprovalRequest, DecisionOutcome
from agent_review.teams_bot.cards import marks_complete, render_decided_card, render_decision_card
from agent_review.teams_bot.state import CardStore

logger = logging.getLogger(__name__)


class DecisionCardService:
    """Tarjeta de decision de una pausa: render, marcas y cierre."""

    def __init__(self, *, store: CardStore) -> None:
        self._store = store

    def render(
        self, approval: ApprovalRequest, *, user_id: str, link: str | None = None
    ) -> dict[str, object]:
        """Tarjeta de `user_id`: si ya decidio, la version cerrada (C11)."""
        actor = self._store.final(approval.review_key)
        if actor is not None:
            return render_decided_card(approval, actor=actor, link=link)
        return render_decision_card(
            approval,
            marks=self._store.marks(approval.review_key, user_id=user_id),
            link=link,
        )

    def apply_mark(
        self,
        approval: ApprovalRequest,
        *,
        user_id: str,
        finding_id: str,
        outcome: DecisionOutcome,
        link: str | None = None,
    ) -> dict[str, object]:
        """Registra una marca y devuelve la tarjeta de ese usuario ya repintada.

        Un `finding_id` ajeno a la pausa no se guarda: la tarjeta solo responde a
        los pendientes que existen (misma regla que `build_decision`).
        """
        if not self._is_pending(approval, finding_id):
            logger.warning(
                "Marca de un hallazgo ajeno a %s (%s); se ignora", approval.review_key, finding_id
            )
            return self.render(approval, user_id=user_id, link=link)
        self._store.mark(
            approval.review_key, user_id=user_id, finding_id=finding_id, outcome=outcome
        )
        return self.render(approval, user_id=user_id, link=link)

    def ready_to_submit(self, approval: ApprovalRequest, *, user_id: str) -> bool:
        """C3: Enviar se habilita cuando *este* usuario ha marcado todos los pendientes."""
        return marks_complete(approval, self._store.marks(approval.review_key, user_id=user_id))

    @staticmethod
    def _is_pending(approval: ApprovalRequest, finding_id: str) -> bool:
        return any(item.finding_id == finding_id for item in approval.pending)
