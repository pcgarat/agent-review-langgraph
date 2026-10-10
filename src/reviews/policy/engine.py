from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from reviews.domain.models import (
    AutofixClass,
    Finding,
    FindingAction,
    ProtectedSubject,
    Severity,
)


class PolicyError(RuntimeError):
    pass


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
            if not any(_path_matches(finding.file, p) for p in patterns):
                return False
        else:
            raise PolicyError(f"Campo no soportado en la politica: {key}")
    return True


def _path_matches(path: str, pattern: str) -> bool:
    """Match simple de glob sobre rutas. `**` cruza directorios."""
    regex = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.fullmatch(regex, path) is not None


class PolicyEngine:
    """Enrutamiento por hallazgo. Determinista y auditable: sin LLM.

    Ver docs/diseno-agente-code-review-20261010.md, apartado 3.
    """

    _RANK = {
        FindingAction.REPORT: 0,
        FindingAction.FIX_WITH_APPROVAL: 1,
        FindingAction.FIX_AUTO: 2,
        FindingAction.ESCALATE: 3,
    }

    def __init__(self, config: dict[str, Any]) -> None:
        self._max_autonomy = FindingAction(config.get("max_autonomy", "report"))
        self._default = FindingAction(config.get("default_action", "report"))
        self._version = str(config.get("version", "0"))
        self._guards: dict[str, Any] = config.get("guards", {}) or {}
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
    def from_file(cls, path: str | Path) -> PolicyEngine:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(data)

    @property
    def version(self) -> str:
        return self._version

    @property
    def guards(self) -> dict[str, Any]:
        return self._guards

    def raw(self) -> dict[str, Any]:
        return {
            "version": self._version,
            "max_autonomy": self._max_autonomy.value,
            "guards": self._guards,
        }

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
        return any(_path_matches(file, pattern) for pattern in patterns)

    def decide(self, finding: Finding) -> PolicyResult:
        # Los guardas preceden a las reglas: son el freno de seguridad.
        if self.is_never_auto(finding.file):
            return PolicyResult(
                action=self._cap(FindingAction.FIX_WITH_APPROVAL),
                reason=f"Ruta protegida ({finding.file}): requiere aprobacion",
                matched_rule="guard:never_auto_paths",
                policy_version=self._version,
            )

        action = self._default
        reason = "Sin regla aplicable"
        matched = "default"
        for rule in self._rules:
            if _match_condition(rule.when, finding):
                action = rule.action
                reason = rule.reason or "Regla de politica"
                matched = f"rule:{rule.index}"
                break

        return PolicyResult(
            action=self._cap(action),
            reason=reason,
            matched_rule=matched,
            policy_version=self._version,
        )
