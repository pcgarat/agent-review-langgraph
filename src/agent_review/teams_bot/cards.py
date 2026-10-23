"""Render de la tarjeta de decision de Teams (T2, iniciativa 013).

Produce el **payload** de la Adaptive Card (un dict) sin importar el SDK de Teams:
el render es logica pura y testeable, y el SDK solo lo materializa (T5/T6). El
card lleva, por hallazgo, el titulo, la severidad, `fichero:linea` con **enlace al
codigo**, la explicacion del problema y la accion propuesta (resuelve Q1 de la
spec: sin `evidence`, que alarga la tarjeta sin anadir contexto util).

Las acciones son `Action.Execute` con `data` (el SDK las reenvia al ingress del
Bot Framework: T7). El estado marcado lo pasa quien llama, ya resuelto por usuario:
el render no conoce Redis.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from agent_review.domain.models import ApprovalRequest, DecisionOutcome, PendingDecision, Severity

# Discriminantes de la accion, en `data.action`. El ingress despacha por aqui, de
# modo que anadir una accion nueva (p. ej. "preguntar" mas adelante) no toca el
# resto del contrato.
DECISION_ACTION = "teams.decision"
SUBMIT_ACTION = "teams.submit"
# Lote de la pausa `publication`. Se envía solo: el bot lo traduce a una
# `HitlDecision` completa. No es una marca.
PUBLISH_BATCH_ACTION = "teams.publish_batch"

_BUTTONS: tuple[tuple[str, DecisionOutcome], ...] = (
    ("Aprobar", DecisionOutcome.APPROVED),
    ("Rechazar", DecisionOutcome.REJECTED),
    ("Anotar", DecisionOutcome.NOTED),
)

# La cuarta acción de `publication` marca un hallazgo. El lote no pasa por aquí.
_PUBLISH_BUTTONS: tuple[tuple[str, DecisionOutcome], ...] = (
    ("Publicar", DecisionOutcome.APPROVED),
    ("No publicar", DecisionOutcome.DROPPED),
)

_BATCHES: tuple[tuple[str, str], ...] = (
    ("Solo bloqueantes", "blockers"),
    ("Publicar todos", "all"),
    ("No publicar ninguno", "none"),
)


class PublishBatch(StrEnum):
    """Lote que cubre todos los pendientes de una pausa de publicación."""

    BLOCKERS = "blockers"
    ALL = "all"
    NONE = "none"


_OUTCOME_LABEL = {
    DecisionOutcome.APPROVED: "Aprobado por ti",
    DecisionOutcome.REJECTED: "Rechazado por ti",
    DecisionOutcome.NOTED: "Anotado por ti",
    DecisionOutcome.DROPPED: "Dejado fuera por ti",
}

_PUBLISH_OUTCOME_LABEL = {
    DecisionOutcome.APPROVED: "Publicar",
    DecisionOutcome.DROPPED: "No publicar",
}

_SEVERITY_COLOR = {
    Severity.P0: "Attention",
    Severity.P1: "Attention",
    Severity.P2: "Warning",
    Severity.P3: "Default",
}

_CARD_VERSION = "1.5"
_CARD_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"


def decision_action_data(
    review_key: str, finding_id: str, outcome: DecisionOutcome
) -> dict[str, str]:
    """`data` de un boton de hallazgo. Lleva el `outcome` estable, no el verbo."""
    return {
        "action": DECISION_ACTION,
        "review_key": review_key,
        "finding_id": finding_id,
        "outcome": outcome.value,
    }


def submit_action_data(review_key: str) -> dict[str, str]:
    """`data` del boton Enviar: emite la `HitlDecision` completa (T3)."""
    return {"action": SUBMIT_ACTION, "review_key": review_key}


def outcome_from_button(action: str) -> DecisionOutcome | None:
    """Traduce el identificador visible de un boton a su `DecisionOutcome`."""
    normalized = action.strip().lower()
    if normalized == DecisionOutcome.EXPIRED.value:
        return None
    for _label, outcome in (*_BUTTONS, *_PUBLISH_BUTTONS):
        if outcome.value == normalized:
            return outcome
    return None


def allowed_outcomes(approval: ApprovalRequest) -> frozenset[DecisionOutcome]:
    """Resultados que esta tarjeta puede marcar.

    Un `dropped` en la pausa de `llm` se publicaría como silencio en vez de
    «descartado». Un `approved` de lote en esa misma pausa autorizaría un arreglo.
    """
    if approval.pause_kind == "publication":
        return frozenset({DecisionOutcome.APPROVED, DecisionOutcome.DROPPED})
    return frozenset(
        {
            DecisionOutcome.APPROVED,
            DecisionOutcome.REJECTED,
            DecisionOutcome.NOTED,
        }
    )


def publish_batch_data(review_key: str, batch: PublishBatch) -> dict[str, str]:
    """`data` de un lote. No lleva hallazgos: el bot los resuelve al traducirlo."""
    return {
        "action": PUBLISH_BATCH_ACTION,
        "review_key": review_key,
        "batch": batch.value,
    }


def marks_complete(approval: ApprovalRequest, marks: Mapping[str, DecisionOutcome]) -> bool:
    """C3: Enviar solo se habilita cuando *este* usuario ha marcado todo."""
    if not approval.pending:
        return False
    return all(item.finding_id in marks for item in approval.pending)


def render_decision_card(
    approval: ApprovalRequest,
    *,
    marks: Mapping[str, DecisionOutcome] | None = None,
    link: str | None = None,
) -> dict[str, Any]:
    """Tarjeta de decision con las marcas de un usuario y el Enviar gobernado.

    `marks` es el estado **de ese usuario** (C8), ya filtrado por quien llama: la
    tarjeta se repinta por usuario, asi que dos personas marcan en paralelo y cada
    una ve lo suyo. El render no conoce Redis.
    """
    if approval.pause_kind == "publication":
        return _render_publication_card(approval, marks=marks or {}, link=link)

    marks = marks or {}
    complete = marks_complete(approval, marks)
    pending = len(approval.pending)

    body: list[dict[str, Any]] = [
        _text(f"Decision pendiente · {approval.repo}#{approval.pr_id}", size="Large", bold=True),
        _text(
            f"{pending} hallazgo(s) esperan una decision humana.",
            subtle=True,
        ),
    ]
    for item in approval.pending:
        body.append(_finding_block(item, marked=marks.get(item.finding_id)))
    if not complete:
        body.append(
            _text(
                "Marca todos los hallazgos para poder enviar la decision.",
                subtle=True,
                color="Warning",
            )
        )

    actions: list[dict[str, Any]] = []
    for item in approval.pending:
        actions.extend(_finding_actions(approval.review_key, item))
    actions.append(_submit_action(approval.review_key, enabled=complete))
    if link:
        actions.append({"type": "Action.OpenUrl", "title": "Abrir el PR", "url": link})

    return {
        "$schema": _CARD_SCHEMA,
        "type": "AdaptiveCard",
        "version": _CARD_VERSION,
        "body": body,
        "actions": actions,
    }


def render_decided_card(
    approval: ApprovalRequest, *, actor: str, link: str | None = None
) -> dict[str, Any]:
    """C11: la primera decision es final; la tarjeta pasa a «decidido por X».

    Sin acciones: rectificar es cosa del CLI/`POST /decision`.
    """
    body = [
        _text(f"Decision tomada · {approval.repo}#{approval.pr_id}", size="Large", bold=True),
        _text(f"Decidido por {actor} ({len(approval.pending)} hallazgo(s)).", color="Good"),
        _text("Para rectificar, usa el CLI o POST /decision.", subtle=True),
    ]
    actions: list[dict[str, Any]] = []
    if link:
        actions.append({"type": "Action.OpenUrl", "title": "Abrir el PR", "url": link})
    return {
        "$schema": _CARD_SCHEMA,
        "type": "AdaptiveCard",
        "version": _CARD_VERSION,
        "body": body,
        "actions": actions,
    }


def _render_publication_card(
    approval: ApprovalRequest,
    *,
    marks: Mapping[str, DecisionOutcome],
    link: str | None,
) -> dict[str, Any]:
    """Tres lotes que se envían solos, y el marcado suelto con Enviar apagado."""
    complete = marks_complete(approval, marks)
    body: list[dict[str, Any]] = [
        _text(f"Qué se publica · {approval.repo}#{approval.pr_id}", size="Large", bold=True),
        _text(
            f"{len(approval.pending)} hallazgo(s). Un lote cubre todos. "
            "Si marcas uno a uno, Enviar espera al resto.",
            subtle=True,
        ),
    ]
    for item in approval.pending:
        body.append(_finding_block(item, marked=marks.get(item.finding_id), proposal=False))
    if not complete:
        body.append(
            _text(
                "Marca todos los hallazgos para poder enviar la decision.",
                subtle=True,
                color="Warning",
            )
        )
    actions: list[dict[str, Any]] = [
        _execute_action(
            label,
            publish_batch_data(approval.review_key, PublishBatch(batch)),
            style="positive" if batch == PublishBatch.BLOCKERS.value else None,
            verb=PUBLISH_BATCH_ACTION,
        )
        for label, batch in _BATCHES
    ]
    for item in approval.pending:
        actions.extend(_publish_mark_actions(approval.review_key, item))
    actions.append(_submit_action(approval.review_key, enabled=complete))
    if link:
        actions.append({"type": "Action.OpenUrl", "title": "Abrir el PR", "url": link})
    return {
        "$schema": _CARD_SCHEMA,
        "type": "AdaptiveCard",
        "version": _CARD_VERSION,
        "body": body,
        "actions": actions,
    }


def _finding_block(
    item: PendingDecision,
    *,
    marked: DecisionOutcome | None,
    proposal: bool = True,
) -> dict[str, Any]:
    body: list[dict[str, Any]] = [
        _text(
            f"[{item.severity.value}] {item.title}", bold=True, color=_SEVERITY_COLOR[item.severity]
        )
    ]
    body.append(_text(_location(item)))
    why = " ".join(item.why_it_matters.split())
    if why:
        body.append(_text(why))
    if proposal:
        proposed = f"Accion propuesta: {item.proposed_action.value}"
        if item.reason.strip():
            proposed += f" · {item.reason.strip()}"
        body.append(_text(proposed, subtle=True))
    if marked is not None:
        labels = _OUTCOME_LABEL if proposal else _PUBLISH_OUTCOME_LABEL
        body.append(_text(labels.get(marked, marked.value), color="Good"))
    return {"type": "Container", "separator": True, "items": body}


def _location(item: PendingDecision) -> str:
    """`fichero:linea`, con enlace al codigo aceptado si el worker lo resolvio."""
    where = f"`{item.file}:{item.line}`"
    if item.code_url:
        return f"{where} · [Ver el codigo]({item.code_url})"
    return where


def _publish_mark_actions(review_key: str, item: PendingDecision) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for label, outcome in _PUBLISH_BUTTONS:
        actions.append(
            _execute_action(
                f"{label} · {item.file}:{item.line}",
                decision_action_data(review_key, item.finding_id, outcome),
                style="positive" if outcome is DecisionOutcome.APPROVED else None,
            )
        )
    return actions


def _finding_actions(review_key: str, item: PendingDecision) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for label, outcome in _BUTTONS:
        actions.append(
            _execute_action(
                f"{label} · {item.file}:{item.line}",
                decision_action_data(review_key, item.finding_id, outcome),
                style="positive" if outcome is DecisionOutcome.APPROVED else None,
            )
        )
    return actions


def _submit_action(review_key: str, *, enabled: bool) -> dict[str, Any]:
    action: dict[str, Any] = {
        "type": "Action.Execute",
        "title": "Enviar decision",
        "verb": SUBMIT_ACTION,
        "data": submit_action_data(review_key),
        "style": "positive",
    }
    # `isEnabled` es 1.5: el boton viaja en la tarjeta de ESE usuario, asi que el
    # bloqueo es por usuario sin coordinacion entre ellos.
    action["isEnabled"] = enabled
    return action


def _execute_action(
    title: str,
    data: dict[str, str],
    *,
    style: str | None = None,
    verb: str = DECISION_ACTION,
) -> dict[str, Any]:
    # `fallback` a `Action.Submit`: los clientes antiguos no soportan
    # `Action.Execute` y mandan la accion como mensaje (lo valida el spike, T0).
    action: dict[str, Any] = {
        "type": "Action.Execute",
        "title": title,
        "verb": verb,
        "data": data,
        "fallback": {"type": "Action.Submit", "title": title, "data": data},
    }
    if style:
        action["style"] = style
    return action


def _text(
    text: str,
    *,
    size: str | None = None,
    bold: bool = False,
    subtle: bool = False,
    color: str | None = None,
) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "TextBlock", "text": text, "wrap": True}
    if size:
        block["size"] = size
    if bold:
        block["weight"] = "Bolder"
    if subtle:
        block["isSubtle"] = True
    if color:
        block["color"] = color
    return block
