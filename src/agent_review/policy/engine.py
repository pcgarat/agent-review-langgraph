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
    PublicationMode,
    Severity,
    Verification,
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

_VERIFIED_DEFAULT = frozenset(
    {
        Verification.VERIFIED_STATIC,
        Verification.VERIFIED_GROUNDED,
        Verification.VERIFIED_EXECUTED,
    }
)


@dataclass(frozen=True)
class PolicyResult:
    """Resultado de aplicar la politica a un hallazgo, con trazabilidad."""

    action: FindingAction
    reason: str
    matched_rule: str
    policy_version: str
    severity: Severity
    severity_reason: str = ""
    publication: PublicationMode = PublicationMode.FULL


@dataclass(frozen=True)
class Rule:
    index: int
    action: FindingAction
    reason: str
    when: dict[str, Any]


@dataclass(frozen=True)
class _GateOutcome:
    severity: Severity
    severity_reason: str
    publication: PublicationMode
    degraded: bool


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


def reject_if_cap_raised(ceiling: str, override: str) -> None:
    """El panel no puede guardar un override que suba el techo del YAML.

    `_lower_cap` lo ignora en silencio. Publicar tiene que rechazarlo: si no, la
    página diría que quedó guardado y la review seguiría con el techo del fichero.
    Vacío es válido (manda el YAML). Con techo `escalate` cualquier override es
    un no-op del motor, así que también se rechaza.
    """
    text = override.strip()
    if not text:
        return
    try:
        configured = FindingAction(ceiling.strip())
        candidate = FindingAction(text)
    except ValueError:
        raise PolicyError(f"max_autonomy invalido: {override!r}") from None
    above = _AUTONOMY_RANK[candidate] > _AUTONOMY_RANK[configured]
    if configured is FindingAction.ESCALATE or above:
        raise PolicyError(
            f"max_autonomy_override {candidate.value!r} supera el techo {configured.value!r}"
        )


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


def _parse_severities(raw: Any, *, field: str) -> frozenset[Severity]:
    values = _as_list(raw)
    if not values:
        raise PolicyError(f"severity_gate.{field} no puede estar vacio")
    try:
        return frozenset(Severity(item) for item in values)
    except ValueError as exc:
        raise PolicyError(f"severity_gate.{field} invalido: {raw!r}") from exc


def _parse_verified(raw: Any) -> frozenset[Verification]:
    values = _as_list(raw)
    if not values:
        raise PolicyError("severity_gate.verified no puede estar vacio")
    allowed = {
        Verification.VERIFIED_STATIC,
        Verification.VERIFIED_GROUNDED,
        Verification.VERIFIED_EXECUTED,
    }
    parsed: set[Verification] = set()
    for item in values:
        try:
            value = Verification(item)
        except ValueError as exc:
            raise PolicyError(f"severity_gate.verified invalido: {item!r}") from exc
        if value not in allowed:
            raise PolicyError(
                f"severity_gate.verified solo admite veredictos positivos, no {value.value!r}"
            )
        parsed.add(value)
    return frozenset(parsed)


def _validate_severity_gate(gate: dict[str, Any]) -> dict[str, Any]:
    """Normaliza y valida el bloque; falla al arrancar si esta mal formado."""
    require = _parse_severities(gate.get("require_verified_for"), field="require_verified_for")
    verified = _parse_verified(gate.get("verified", [v.value for v in _VERIFIED_DEFAULT]))
    degrade_raw = gate.get("degrade_to", "P3")
    try:
        degrade_to = Severity(degrade_raw)
    except ValueError as exc:
        raise PolicyError(f"severity_gate.degrade_to invalido: {degrade_raw!r}") from exc
    on_unverified = str(gate.get("on_unverified", "degrade"))
    if on_unverified == "possibles":
        raise PolicyError(
            "severity_gate.on_unverified=possibles aun no esta implementado; usa degrade"
        )
    if on_unverified != "degrade":
        raise PolicyError(f"severity_gate.on_unverified invalido: {on_unverified!r}")
    on_refuted = str(gate.get("on_refuted", "drop"))
    if on_refuted not in {"drop", "summary"}:
        raise PolicyError(f"severity_gate.on_refuted invalido: {on_refuted!r}")
    return {
        "require_verified_for": require,
        "verified": verified,
        "degrade_to": degrade_to,
        "on_unverified": on_unverified,
        "on_refuted": on_refuted,
    }


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
        gate = config.get("severity_gate")
        if gate is None:
            self._severity_gate: dict[str, Any] = {}
            self._gate: dict[str, Any] | None = None
        elif not isinstance(gate, dict):
            raise PolicyError("severity_gate debe ser un mapa")
        elif not gate:
            self._severity_gate = {}
            self._gate = None
        else:
            self._gate = _validate_severity_gate(gate)
            # Vista cruda para L2 / raw(): listas serializables como en el YAML.
            self._severity_gate = {
                "require_verified_for": [s.value for s in self._gate["require_verified_for"]],
                "verified": [v.value for v in self._gate["verified"]],
                "on_unverified": self._gate["on_unverified"],
                "degrade_to": self._gate["degrade_to"].value,
                "on_refuted": self._gate["on_refuted"],
            }
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
        """Bloque `severity_gate` del YAML (011); vacío si no está declarado."""
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

    def _apply_gate(self, finding: Finding) -> _GateOutcome:
        """Severidad efectiva y modo de publicacion (011). Sin bloque: no-op."""
        if self._gate is None:
            return _GateOutcome(
                severity=finding.severity,
                severity_reason="",
                publication=PublicationMode.FULL,
                degraded=False,
            )

        if finding.verification is Verification.REFUTED:
            mode = (
                PublicationMode.DROP
                if self._gate["on_refuted"] == "drop"
                else PublicationMode.SUMMARY
            )
            return _GateOutcome(
                severity=finding.severity,
                severity_reason="refutado por el verificador",
                publication=mode,
                degraded=False,
            )

        require: frozenset[Severity] = self._gate["require_verified_for"]
        verified: frozenset[Verification] = self._gate["verified"]
        if finding.severity in require and finding.verification not in verified:
            degrade_to: Severity = self._gate["degrade_to"]
            return _GateOutcome(
                severity=degrade_to,
                severity_reason=(
                    f"sin verificacion: {finding.severity.value} degradado a {degrade_to.value}"
                ),
                publication=PublicationMode.FULL,
                degraded=True,
            )

        return _GateOutcome(
            severity=finding.severity,
            severity_reason="",
            publication=PublicationMode.FULL,
            degraded=False,
        )

    def decide(self, finding: Finding) -> PolicyResult:
        gate = self._apply_gate(finding)

        if gate.publication is PublicationMode.DROP:
            return PolicyResult(
                action=FindingAction.REPORT,
                reason=gate.severity_reason or "Hallazgo refutado: no se publica",
                matched_rule="gate:refuted",
                policy_version=self._version,
                severity=gate.severity,
                severity_reason=gate.severity_reason,
                publication=PublicationMode.DROP,
            )

        effective = (
            finding
            if gate.severity is finding.severity
            else finding.model_copy(update={"severity": gate.severity})
        )

        action = self._default
        reason = "Sin regla aplicable"
        matched = "default"
        for rule in self._rules:
            if _match_condition(rule.when, effective):
                action = rule.action
                reason = rule.reason or "Regla de politica"
                matched = f"rule:{rule.index}"
                break

        if gate.degraded:
            # G7: un degradado nunca escala ni entra en fix_*.
            action = FindingAction.REPORT
            reason = gate.severity_reason
            matched = "gate:degrade"
        elif gate.publication is PublicationMode.SUMMARY:
            # Refutado que solo va al resumen: no pide HITL ni fix.
            action = FindingAction.REPORT
            matched = "gate:refuted"
            if not reason or reason == "Sin regla aplicable":
                reason = gate.severity_reason

        # La guarda de rutas nunca-auto frena cualquier arreglo, pero no rebaja un
        # escalado: un P0 o un tema protegido en `migrations/**` debe llegar a una
        # persona como escalado, no como "¿lo arreglo?" sobre algo que el nodo `fix`
        # bloquearia siempre (ADR-0009).
        if (
            not gate.degraded
            and gate.publication is PublicationMode.FULL
            and self.is_never_auto(finding.file)
            and action is not FindingAction.ESCALATE
        ):
            return PolicyResult(
                action=self._cap(FindingAction.FIX_WITH_APPROVAL),
                reason=f"Ruta protegida ({finding.file}): requiere aprobacion",
                matched_rule="guard:never_auto_paths",
                policy_version=self._version,
                severity=gate.severity,
                severity_reason=gate.severity_reason,
                publication=gate.publication,
            )

        return PolicyResult(
            action=self._cap(action),
            reason=reason,
            matched_rule=matched,
            policy_version=self._version,
            severity=gate.severity,
            severity_reason=gate.severity_reason,
            publication=gate.publication,
        )
