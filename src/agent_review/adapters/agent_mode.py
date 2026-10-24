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
from agent_review.domain.models import Finding

_COMPLETE = "complete"
_SCENARIO_SEVERITIES = frozenset({"P0", "P1", "P2"})


class AgentModeRejected(Exception):
    """La salida no es una review terminada que se pueda guardar."""

    def __init__(self, message: str, *, discarded: int = 0) -> None:
        super().__init__(message)
        self.discarded = discarded


@dataclass(frozen=True)
class AgentModeReview:
    findings: list[Finding]
    discarded: int


def adapt_agent_mode(payload: object) -> AgentModeReview:
    """Lista validada, o `AgentModeRejected` si no hay review que guardar."""
    data = _object(payload)
    if data.get("status") != _COMPLETE:
        raise AgentModeRejected(f"status {data.get('status')!r} no es una review terminada")
    findings = data.get("findings")
    if not isinstance(findings, list):
        raise AgentModeRejected("el objeto no trae una lista findings")
    prior = data.get("pre_existing_findings", [])
    if not isinstance(prior, list):
        raise AgentModeRejected("pre_existing_findings no es una lista")

    kept: list[Finding] = []
    discarded = 0
    for item in findings:
        mapped = _map_finding(item, force_pre_existing=False)
        if mapped is None:
            discarded += 1
        else:
            kept.append(mapped)
    for item in prior:
        mapped = _map_finding(item, force_pre_existing=True)
        if mapped is None:
            discarded += 1
        else:
            kept.append(mapped)
    if not kept and (findings or prior):
        raise AgentModeRejected("todos los hallazgos se descartaron", discarded=discarded)
    return AgentModeReview(findings=kept, discarded=discarded)


def _object(payload: object) -> dict[str, Any]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise AgentModeRejected("el stdout no es JSON") from exc
    if not isinstance(payload, dict):
        raise AgentModeRejected("el stdout no es el objeto mode:agent")
    return payload


def _map_finding(item: object, *, force_pre_existing: bool) -> Finding | None:
    if not isinstance(item, dict):
        return None
    ident = _identifier(item.get("#"))
    if ident is None:
        return None
    why = _text(item.get("why_it_matters"))
    if not why:
        return None
    scenario = _text(item.get("failure_scenario"))
    severity = item.get("severity")
    if not scenario and severity in _SCENARIO_SEVERITIES:
        scenario = why
    if severity in _SCENARIO_SEVERITIES and not scenario:
        return None
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
        "evidence": item.get("evidence"),
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
    except (ContractError, ValidationError):
        return None


def _identifier(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    text = str(value).strip()
    return text or None


def _text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()
