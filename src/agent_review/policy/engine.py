from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agent_review.domain.models import (
    AutofixClass,
    Finding,
    FindingAction,
    ProtectedSubject,
    Severity,
)
from agent_review.globs import path_matches


class PolicyError(RuntimeError):
    pass


# Escalera unica de autonomia, compartida por el techo global y su override.
# `escalate` no es un nivel de autonomia (es un freno aparte) pero entra en la
# escala: asi comparar techos nunca lanza KeyError y un override a `escalate`
# queda como no-op (no puede *bajar* nada).
_AUTONOMY_RANK = {
    FindingAction.REPORT: 0,
    FindingAction.FIX_WITH_APPROVAL: 1,
    FindingAction.FIX_AUTO: 2,
    FindingAction.ESCALATE: 3,
}


@dataclass(frozen=True)
class PolicyResult:
    """Resultado de aplicar la politica a un hallazgo, con trazabilidad."""

    action: FindingAction
    reason: str
    matched_rule: str
    policy_version: str


@dataclass(frozen=True)
class Rule:
    index: int
    action: FindingAction
    reason: str
    when: dict[str, Any]


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return [value]


def _lower_cap(configured: FindingAction, override: str | None) -> FindingAction:
    """Techo efectivo: el override solo puede *bajar* el declarado en el YAML (D1).

    Un deploy puede endurecer la politica (menos autonomia) sin tocar el repo,
    pero nunca subirla por encima de lo que el YAML permitio: subir el techo
    exige un cambio versionado y revisable.
    """
    if override is None or not override.strip():
        return configured
    try:
        candidate = FindingAction(override.strip())
    except ValueError:
        raise PolicyError(f"max_autonomy invalido en settings: {override!r}") from None
    # El override solo aplica a techos no-escalate; escalate es un freno aparte.
    if configured is FindingAction.ESCALATE:
        return configured
    if _AUTONOMY_RANK[candidate] <= _AUTONOMY_RANK[configured]:
        return candidate
    return configured


def _match_condition(condition: dict[str, Any], finding: Finding) -> bool:
    """Casa una condicion. Todas las claves del `when` deben cumplirse."""
    for key, expected in condition.items():
        if key == "protected_subject":
            # "*" significa "cualquier tema protegido".
            if expected == "*":
                if finding.protected_subject is None:
                    return False
                continue
            if finding.protected_subject != ProtectedSubject(expected):
                return False
        elif key == "severity":
            if finding.severity not in [Severity(v) for v in _as_list(expected)]:
                return False
        elif key == "autofix_class":
            if finding.autofix_class != AutofixClass(expected):
                return False
        elif key == "confidence":
            if finding.confidence not in _as_list(expected):
                return False
        elif key == "paths":
            patterns = [str(p) for p in _as_list(expected)]
            if not any(path_matches(finding.file, p) for p in patterns):
                return False
        else:
            raise PolicyError(f"Campo no soportado en la politica: {key}")
    return True


class PolicyEngine:
    """Enrutamiento por hallazgo. Determinista y auditable: sin LLM.

    Ver docs/iniciativas/archivo/001-diseno-base/spec.md, apartado 3.
    """

    _RANK = _AUTONOMY_RANK

    def __init__(self, config: dict[str, Any], *, cap_override: str | None = None) -> None:
        configured = FindingAction(config.get("max_autonomy", "report"))
        self._max_autonomy = _lower_cap(configured, cap_override)
        self._default = FindingAction(config.get("default_action", "report"))
        self._version = str(config.get("version", "0"))
        self._guards: dict[str, Any] = config.get("guards", {}) or {}
        # Umbral del gate (011) y de candidatos L2 (009 V10). Mientras 011 no
        # aterrice la clave puede no existir: el nodo `verify` cae al ajuste.
        gate = config.get("severity_gate")
        self._severity_gate: dict[str, Any] = gate if isinstance(gate, dict) else {}
        self._rules: list[Rule] = [
            Rule(
                index=index,
                action=FindingAction(rule["action"]),
                reason=str(rule.get("reason", "")),
                when=rule.get("when", {}) or {},
            )
            for index, rule in enumerate(config.get("rules", []))
        ]

    @classmethod
    def from_file(cls, path: str | Path, *, cap_override: str | None = None) -> PolicyEngine:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(data, cap_override=cap_override)

    @property
    def version(self) -> str:
        return self._version

    @property
    def guards(self) -> dict[str, Any]:
        return self._guards

    @property
    def severity_gate(self) -> dict[str, Any]:
        """Bloque `severity_gate` del YAML (011); vacío si aún no está declarado."""
        return self._severity_gate

    def raw(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": self._version,
            "max_autonomy": self._max_autonomy.value,
            "guards": self._guards,
        }
        if self._severity_gate:
            payload["severity_gate"] = self._severity_gate
        return payload

    def _cap(self, action: FindingAction) -> FindingAction:
        """Aplica el techo global.

        Con `max_autonomy: report` (Fase 1) todo se degrada a report, tambien
        escalate: se informa la razon pero el grafo no pausa ni arregla.
        Con techos mayores, escalate nunca se rebaja (freno de seguridad).
        """
        if self._max_autonomy is FindingAction.REPORT:
            return FindingAction.REPORT
        if action is FindingAction.ESCALATE:
            return action
        if self._RANK[action] <= self._RANK[self._max_autonomy]:
            return action
        return self._max_autonomy

    def is_never_auto(self, file: str) -> bool:
        patterns = [str(p) for p in self._guards.get("never_auto_paths", [])]
        # Herramientas propias del agente: el agente no debe reescribirlas.
        patterns.append(".cursor/**")
        return any(path_matches(file, pattern) for pattern in patterns)

    def decide(self, finding: Finding) -> PolicyResult:
        action = self._default
        reason = "Sin regla aplicable"
        matched = "default"
        for rule in self._rules:
            if _match_condition(rule.when, finding):
                action = rule.action
                reason = rule.reason or "Regla de politica"
                matched = f"rule:{rule.index}"
                break

        # La guarda de rutas nunca-auto frena cualquier arreglo, pero no rebaja un
        # escalado: un P0 o un tema protegido en `migrations/**` debe llegar a una
        # persona como escalado, no como "¿lo arreglo?" sobre algo que el nodo `fix`
        # bloquearia siempre (ADR-0009).
        if self.is_never_auto(finding.file) and action is not FindingAction.ESCALATE:
            return PolicyResult(
                action=self._cap(FindingAction.FIX_WITH_APPROVAL),
                reason=f"Ruta protegida ({finding.file}): requiere aprobacion",
                matched_rule="guard:never_auto_paths",
                policy_version=self._version,
            )

        return PolicyResult(
            action=self._cap(action),
            reason=reason,
            matched_rule=matched,
            policy_version=self._version,
        )
