"""Traduce el objeto `mode:agent` al contrato de hallazgos.

Funcion pura: no llama al modelo ni escribe el artefacto. Un estado que no
es `complete`, un JSON que no es ese objeto, o una review cuyos hallazgos
se descartan todos no es una lista vacia.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from agent_review.domain.contracts import ContractError, validate_finding
from agent_review.domain.models import DiscardedFinding, Finding

_COMPLETE = "complete"
_SCENARIO_SEVERITIES = frozenset({"P0", "P1", "P2"})


class AgentModeRejected(Exception):
    """La salida no es una review terminada que se pueda guardar."""

    def __init__(self, message: str, *, rejected: tuple[DiscardedFinding, ...] = ()) -> None:
        super().__init__(message)
        self.rejected = rejected

    @property
    def discarded(self) -> int:
        return len(self.rejected)

    @property
    def reasons(self) -> tuple[str, ...]:
        return _reasons(self.rejected)


@dataclass(frozen=True)
class AgentModeReview:
    findings: list[Finding]
    rejected: tuple[DiscardedFinding, ...] = ()

    @property
    def discarded(self) -> int:
        """Un descarte por hallazgo que no cumple el contrato."""
        return len(self.rejected)

    @property
    def reasons(self) -> tuple[str, ...]:
        """`#id: motivo`, una línea por descarte, para el diario."""
        return _reasons(self.rejected)


def adapt_agent_mode(payload: object) -> AgentModeReview:
    """Lista validada, o `AgentModeRejected` si no hay review que guardar."""
    data = _object(payload)
    if data.get("status") != _COMPLETE:
        detail = f"status {data.get('status')!r} no es una review terminada"
        reason = data.get("reason")
        if isinstance(reason, str) and reason.strip():
            detail = f"{detail}: {reason.strip()[:300]}"
        raise AgentModeRejected(detail)
    findings = data.get("findings")
    if not isinstance(findings, list):
        raise AgentModeRejected("el objeto no trae una lista findings")
    prior = data.get("pre_existing_findings", [])
    if not isinstance(prior, list):
        raise AgentModeRejected("pre_existing_findings no es una lista")

    kept: list[Finding] = []
    rejected: list[DiscardedFinding] = []
    for items, force in ((findings, False), (prior, True)):
        for item in items:
            mapped = _map_finding(item, force_pre_existing=force)
            if isinstance(mapped, str):
                rejected.append(_rejected(item, mapped))
            else:
                kept.append(mapped)
    if not kept and (findings or prior):
        raise AgentModeRejected("todos los hallazgos se descartaron", rejected=tuple(rejected))
    return AgentModeReview(findings=kept, rejected=tuple(rejected))


def _object(payload: object) -> dict[str, Any]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise AgentModeRejected("el stdout no es JSON") from exc
    if not isinstance(payload, dict):
        raise AgentModeRejected("el stdout no es el objeto mode:agent")
    return payload


def _map_finding(item: object, *, force_pre_existing: bool) -> Finding | str:
    """El hallazgo validado, o el motivo por el que no entra."""
    if not isinstance(item, dict):
        return "no es un objeto"
    ident = _identifier(item.get("#"))
    if ident is None:
        return "sin identificador #"
    why = _text(item.get("why_it_matters"))
    if not why:
        return "sin why_it_matters"
    scenario = _text(item.get("failure_scenario"))
    severity = item.get("severity")
    if not scenario and severity in _SCENARIO_SEVERITIES:
        scenario = why
    if severity in _SCENARIO_SEVERITIES and not scenario:
        return "sin failure_scenario"
    flag = True if force_pre_existing else item.get("pre_existing", False)
    body: dict[str, Any] = {
        "id": ident,
        "title": item.get("title"),
        "severity": severity,
        "file": item.get("file"),
        "line": item.get("line"),
        "why_it_matters": why,
        "autofix_class": item.get("autofix_class"),
        "owner": item.get("owner"),
        "confidence": item.get("confidence"),
        "evidence": _evidence(item),
        "pre_existing": flag,
    }
    suggested = item.get("suggested_fix")
    if isinstance(suggested, str) and suggested.strip():
        body["suggested_fix"] = suggested.strip()
    subject = item.get("protected_subject")
    if isinstance(subject, str) and subject.strip():
        body["protected_subject"] = subject
    if scenario:
        body["failure_scenario"] = scenario
    try:
        return validate_finding(body)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or "hallazgo"
        return f"{field}: {first['msg']}"
    except ContractError as exc:
        return str(exc)


def _rejected(item: object, reason: str) -> DiscardedFinding:
    data = item if isinstance(item, dict) else {}
    title = _text(data.get("title"))
    file = _text(data.get("file"))
    return DiscardedFinding(
        stage="contract",
        id=_identifier(data.get("#")),
        title=title[:200] or None,
        file=file or None,
        reason=reason[:300],
    )


def _reasons(rejected: tuple[DiscardedFinding, ...]) -> tuple[str, ...]:
    return tuple(
        f"#{item.id}: {item.reason}" if item.id is not None else f"hallazgo sin #: {item.reason}"
        for item in rejected
    )


def _evidence(item: dict[str, Any]) -> object:
    """La skill promete una lista, pero en `lite` a veces la emite como texto.

    Un texto se envuelve en una lista. `first_evidence` (la cita literal de la
    línea) va delante si el primer elemento no la contiene: L0 busca en el
    diff solo la primera cita, y en `lite` esa suele ser prosa. Cualquier otra
    forma pasa tal cual y la rechaza el contrato.
    """
    value = item.get("evidence")
    if isinstance(value, str):
        value = [value.strip()] if value.strip() else []
    quote = _text(item.get("first_evidence"))
    if not quote:
        return value
    if value is None:
        return [quote]
    if isinstance(value, list) and not (value and isinstance(value[0], str) and quote in value[0]):
        return [quote, *value]
    return value


def _identifier(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    text = str(value).strip()
    return text or None


def _text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()
