from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from agent_review.domain.models import DecisionOutcome, FindingDecision, HitlDecision
from agent_review.domain.paused import PausedReview


def outcomes_for_pause(
    pause_kind: Literal["publication"] | None,
) -> frozenset[DecisionOutcome]:
    """Resultados que una persona puede marcar en esta pausa.

    En publicación, `approved` escribe el hallazgo y `dropped` lo deja fuera.
    `rejected` y `noted` son de la pausa que autoriza un arreglo: ahí `dropped`
    se publicaría como silencio.
    """
    if pause_kind == "publication":
        return frozenset({DecisionOutcome.APPROVED, DecisionOutcome.DROPPED})
    return frozenset({DecisionOutcome.APPROVED, DecisionOutcome.REJECTED, DecisionOutcome.NOTED})


class DecisionError(ValueError):
    """La decision no es aplicable a la review pausada (se traduce a 409/exit 2).

    No es un error de sintaxis del cliente (eso es 400): es una decision *bien
    formada* que no encaja con el inbox, p.ej. la review no esta pausada o el
    `finding_id` no esta entre los pendientes.
    """


def build_decision(
    paused: PausedReview,
    *,
    actor: str,
    items: Iterable[FindingDecision],
    reviewed_key: str,
) -> HitlDecision:
    """Construye la decision validandola contra el inbox.

    Rechaza una review que no esta pausada (`reviewed_key` no coincide con el
    `review_key` del inbox) o un `finding_id` que no esta entre los pendientes:
    la decision solo responde a los pendientes que ya existen, no los inventa
    (spec §8).
    """
    actor = actor.strip()
    if not actor:
        raise DecisionError("Se requiere un actor (quien decide)")

    collected = list(items)
    if not collected:
        raise DecisionError(
            "No hay ninguna decision: indica al menos un --approve/--reject/--noted/--drop"
        )

    if paused.review_key != reviewed_key:
        raise DecisionError(f"La review pausada es {paused.review_key}, no {reviewed_key}")

    pending_ids = {pending.finding_id for pending in paused.human_request.pending}
    unknown = [item.finding_id for item in collected if item.finding_id not in pending_ids]
    if unknown:
        raise DecisionError("finding_id ajeno a la review pausada: " + ", ".join(sorted(unknown)))
    # Una decision parcial publicaria los no decididos como si nadie los hubiera
    # frenado: se exige responder a todos los pendientes de una vez.
    missing = pending_ids - {item.finding_id for item in collected}
    if missing:
        raise DecisionError("Faltan hallazgos por decidir: " + ", ".join(sorted(missing)))
    if any(item.outcome is DecisionOutcome.EXPIRED for item in collected):
        # `expired` lo emite el barrido del TTL, no una persona.
        raise DecisionError("`expired` no es una decision humana valida")
    allowed = outcomes_for_pause(paused.human_request.pause_kind)
    forbidden = [item.finding_id for item in collected if item.outcome not in allowed]
    if forbidden:
        raise DecisionError("resultado no permitido en esta pausa: " + ", ".join(sorted(forbidden)))

    return HitlDecision(review_key=reviewed_key, actor=actor, items=collected)
