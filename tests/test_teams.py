"""T2 (iniciativa 013): tarjeta de decision, estado por usuario y Enviar bloqueado."""

from __future__ import annotations

from typing import Any

from agent_review.domain.models import (
    ApprovalRequest,
    DecisionOutcome,
    FindingAction,
    PendingDecision,
    Severity,
)
from agent_review.teams_bot.card_service import DecisionCardService
from agent_review.teams_bot.cards import (
    DECISION_ACTION,
    SUBMIT_ACTION,
    decision_action_data,
    marks_complete,
    outcome_from_button,
    render_decided_card,
    render_decision_card,
)
from agent_review.teams_bot.state import InMemoryCardStore


def _pending(
    finding_id: str = "f1",
    *,
    line: int = 12,
    code_url: str | None = None,
    why: str = "",
) -> PendingDecision:
    return PendingDecision(
        finding_id=finding_id,
        proposed_action=FindingAction.FIX_WITH_APPROVAL,
        reason="Tema protegido",
        matched_rule="rule:3",
        policy_version="1",
        title="Escritura sin comprobar el resultado",
        file="src/Payments/Refund.php",
        line=line,
        severity=Severity.P1,
        why_it_matters=why,
        code_url=code_url,
    )


def _approval(*pending: PendingDecision) -> ApprovalRequest:
    return ApprovalRequest(
        review_key="r:1:abc",
        repo="cqm/emotivoo-v2",
        pr_id=1,
        head_sha="abc",
        pending=list(pending) or [_pending()],
    )


def _actions(card: dict[str, Any]) -> list[dict[str, Any]]:
    return list(card["actions"])


def _submit(card: dict[str, Any]) -> dict[str, Any]:
    return next(
        action for action in _actions(card) if action["data"].get("action") == SUBMIT_ACTION
    )


def _texts(card: dict[str, Any]) -> str:
    """Todo el texto de la tarjeta, para afirmar sobre el contenido visible."""
    chunks: list[str] = []
    for block in card["body"]:
        if block["type"] == "Container":
            chunks.extend(item.get("text", "") for item in block["items"])
        else:
            chunks.append(block.get("text", ""))
    return "\n".join(chunks)


def test_la_tarjeta_refleja_todos_los_pendientes_con_su_contexto() -> None:
    card = render_decision_card(
        _approval(_pending("f1", line=12), _pending("f2", line=44)),
    )
    text = _texts(card)
    assert "2 hallazgo(s)" in text
    assert "Escritura sin comprobar el resultado" in text
    assert "src/Payments/Refund.php:12" in text
    assert "src/Payments/Refund.php:44" in text
    assert "[P1]" in text
    assert "Accion propuesta: fix_with_approval" in text


def test_la_tarjeta_explica_el_problema_si_hay_why_it_matters() -> None:
    card = render_decision_card(
        _approval(_pending("f1", why="El refund se marca pagado aunque el PSP falle")),
    )
    assert "El refund se marca pagado aunque el PSP falle" in _texts(card)


def test_la_tarjeta_enlaza_al_trozo_de_codigo_cuando_hay_code_url() -> None:
    card = render_decision_card(
        _approval(_pending("f1", code_url="https://bb/cqm/emotivoo-v2/src/abc/f.php#lines-12")),
    )
    assert "https://bb/cqm/emotivoo-v2/src/abc/f.php#lines-12" in _texts(card)


def test_la_tarjeta_sin_code_url_no_inventa_enlace() -> None:
    card = render_decision_card(_approval(_pending("f1")))
    assert "Ver el codigo" not in _texts(card)
    assert "src/Payments/Refund.php:12" in _texts(card)


def test_hay_un_boton_por_hallazgo_y_accion() -> None:
    card = render_decision_card(_approval(_pending("f1"), _pending("f2")))
    decisions = [
        action
        for action in _actions(card)
        if action.get("data", {}).get("action") == DECISION_ACTION
    ]
    assert len(decisions) == 6, "3 acciones (aprobar/rechazar/anotar) por cada pendiente"
    # Cada boton lleva a quien y que decide, y su fallback para clientes antiguos.
    first = decisions[0]
    assert first["data"]["finding_id"] == "f1"
    assert first["data"]["outcome"] == DecisionOutcome.APPROVED.value
    assert first["fallback"]["type"] == "Action.Submit"


def test_enviar_esta_deshabilitado_mientras_quede_un_pendiente() -> None:
    approval = _approval(_pending("f1"), _pending("f2"))
    card = render_decision_card(approval, marks={"f1": DecisionOutcome.APPROVED})
    assert _submit(card)["isEnabled"] is False
    assert "Marca todos los hallazgos" in _texts(card)
    assert not marks_complete(approval, {"f1": DecisionOutcome.APPROVED})


def test_enviar_se_habilita_con_todos_los_pendientes_marcados() -> None:
    approval = _approval(_pending("f1"), _pending("f2"))
    marks = {"f1": DecisionOutcome.APPROVED, "f2": DecisionOutcome.REJECTED}
    card = render_decision_card(approval, marks=marks)
    assert _submit(card)["isEnabled"] is True
    assert marks_complete(approval, marks)


def test_la_tarjeta_muestra_lo_ya_marcado_por_ese_usuario() -> None:
    card = render_decision_card(_approval(_pending("f1")), marks={"f1": DecisionOutcome.NOTED})
    assert "Anotado por ti" in _texts(card)


def test_la_tarjeta_decidida_no_tiene_acciones_de_decision() -> None:
    card = render_decided_card(_approval(), actor="Paco")
    assert "Decidido por Paco" in _texts(card)
    types = {action["type"] for action in _actions(card)}
    assert "Action.Execute" not in types


def test_outcome_from_button_reconoce_los_outcomes_validos() -> None:
    assert outcome_from_button("approved") is DecisionOutcome.APPROVED
    assert outcome_from_button("rejected") is DecisionOutcome.REJECTED
    assert outcome_from_button("noted") is DecisionOutcome.NOTED
    assert outcome_from_button("expired") is None
    assert outcome_from_button("lo que sea") is None


def test_decision_action_data_lleva_el_outcome_estable() -> None:
    data = decision_action_data("r:1:abc", "f1", DecisionOutcome.REJECTED)
    assert data == {
        "action": DECISION_ACTION,
        "review_key": "r:1:abc",
        "finding_id": "f1",
        "outcome": "rejected",
    }


# ---------------------------------------------------------------------------
# Estado por usuario y repintado
# ---------------------------------------------------------------------------


def test_marcar_un_hallazgo_devuelve_la_tarjeta_con_esa_marca_conservada() -> None:
    service = DecisionCardService(store=InMemoryCardStore())
    approval = _approval(_pending("f1"), _pending("f2"))

    card = service.apply_mark(
        approval, user_id="u1", finding_id="f1", outcome=DecisionOutcome.APPROVED
    )

    assert "Aprobado por ti" in _texts(card)
    assert _submit(card)["isEnabled"] is False, "aun falta f2"
    assert service.ready_to_submit(approval, user_id="u1") is False
    assert service.ready_to_submit(approval, user_id="u2") is False, "otro usuario no ve las marcas"


def test_el_estado_es_por_usuario_y_no_se_pisa() -> None:
    store = InMemoryCardStore()
    service = DecisionCardService(store=store)
    approval = _approval(_pending("f1"), _pending("f2"))

    service.apply_mark(approval, user_id="u1", finding_id="f1", outcome=DecisionOutcome.APPROVED)
    service.apply_mark(approval, user_id="u2", finding_id="f2", outcome=DecisionOutcome.NOTED)

    assert store.marks("r:1:abc", user_id="u1") == {"f1": DecisionOutcome.APPROVED}
    assert store.marks("r:1:abc", user_id="u2") == {"f2": DecisionOutcome.NOTED}
    # Nadie tiene la decision completa: Enviar sigue bloqueado para ambos.
    assert service.ready_to_submit(approval, user_id="u1") is False
    assert service.ready_to_submit(approval, user_id="u2") is False


def test_cuando_el_usuario_marca_todo_enviar_se_habilita() -> None:
    service = DecisionCardService(store=InMemoryCardStore())
    approval = _approval(_pending("f1"), _pending("f2"))
    service.apply_mark(approval, user_id="u1", finding_id="f1", outcome=DecisionOutcome.APPROVED)
    card = service.apply_mark(
        approval, user_id="u1", finding_id="f2", outcome=DecisionOutcome.REJECTED
    )

    assert _submit(card)["isEnabled"] is True
    assert service.ready_to_submit(approval, user_id="u1") is True


def test_una_marca_de_un_hallazgo_ajeno_se_ignora() -> None:
    store = InMemoryCardStore()
    service = DecisionCardService(store=store)
    approval = _approval(_pending("f1"))

    service.apply_mark(approval, user_id="u1", finding_id="f9", outcome=DecisionOutcome.APPROVED)

    assert store.marks("r:1:abc", user_id="u1") == {}


def test_tras_cerrar_la_tarjeta_queda_decidida_y_no_se_reabre() -> None:
    store = InMemoryCardStore()
    service = DecisionCardService(store=store)
    approval = _approval(_pending("f1"))
    service.apply_mark(approval, user_id="u1", finding_id="f1", outcome=DecisionOutcome.APPROVED)
    store.close("r:1:abc", actor="Paco")

    card = service.render(approval, user_id="u1")
    assert "Decidido por Paco" in _texts(card)
    # Otra marca no reabre la tarjeta: la primera decision es final (C11).
    card = service.apply_mark(
        approval, user_id="u2", finding_id="f1", outcome=DecisionOutcome.REJECTED
    )
    assert "Decidido por Paco" in _texts(card)
    assert store.final("r:1:abc") == "Paco"
