from __future__ import annotations

import json
import re
from collections.abc import Sequence

from reviews.adapters.diff_parser import best_line, file_key, parse_unified_diff
from reviews.domain.contracts import validate_finding
from reviews.domain.models import AutofixClass, Finding, Owner, ProtectedSubject, Severity

_SEVERITY_MAP = {"error": Severity.P2, "warning": Severity.P3}

# Herramientas que marcan temas sensibles: nunca se arreglan solos.
_PROTECTED_HINTS = {
    "auth": ProtectedSubject.AUTHORIZATION_AUTHENTICATION,
    "password": ProtectedSubject.SECRETS_EXPOSURE,
    "secret": ProtectedSubject.SECRETS_EXPOSURE,
    "token": ProtectedSubject.SECRETS_EXPOSURE,
    "sql": ProtectedSubject.INJECTION,
    "eval": ProtectedSubject.INJECTION,
}


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:40]


def _protected_subject(text: str) -> ProtectedSubject | None:
    lowered = text.lower()
    for hint, subject in _PROTECTED_HINTS.items():
        if hint in lowered:
            return subject
    return None


class ToolingReviewer:
    """Persona estatica: convierte salida de herramientas del repo en hallazgos.

    No ejecuta las herramientas (eso es el sandbox de Fase 3): consume su salida,
    que en produccion vendra del pipeline y en tests de un fixture. Filtra a lo
    que el PR realmente cambio y ancla a lineas del diff.
    """

    name = "standards"

    def __init__(
        self,
        *,
        phpstan_json: str | Sequence[str] = (),
        allowed_tools: Sequence[str] = ("phpstan", "php-cs-fixer"),
    ) -> None:
        raw = phpstan_json if isinstance(phpstan_json, str) else "".join(phpstan_json)
        self._phpstan_raw = raw
        self._allowed = set(allowed_tools)

    def review(self, *, repo: str, pr_id: int, diff: str) -> list[Finding]:
        del repo, pr_id
        files = parse_unified_diff(diff)
        findings: list[Finding] = []
        for entry in self._entries():
            finding = self._to_finding(entry, files)
            if finding is not None:
                findings.append(finding)
        return findings

    def _entries(self) -> list[dict]:
        if not self._phpstan_raw.strip() or "phpstan" not in self._allowed:
            return []
        try:
            data = json.loads(self._phpstan_raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, dict):
            return []
        entries = data.get("files", {})
        result: list[dict] = []
        if isinstance(entries, dict):
            for path, payload in entries.items():
                messages = payload.get("messages", []) if isinstance(payload, dict) else []
                for message in messages:
                    result.append({"path": path, **message})
        return result

    def _to_finding(self, entry: dict, files: dict) -> Finding | None:
        path = str(entry.get("path", ""))
        key = file_key(files, path)
        if key is None:
            return None  # fuera del diff: no es responsabilidad del PR
        message = str(entry.get("message", "")).strip()
        if not message:
            return None
        added = files[key].added
        hint = int(entry.get("line", 0) or 0) or None
        needle = _needle(added, hint)
        line = best_line(added, hint, needle)
        if line is None:
            return None
        severity = _SEVERITY_MAP.get(str(entry.get("level", "warning")), Severity.P3)
        subject = _protected_subject(message)
        evidence_line = added.get(line, "")
        payload = {
            "id": f"standards-{_slug(path)}-{hint or line}-{_slug(message)[:24]}",
            "title": _title(message),
            "severity": severity.value,
            "file": key,
            "line": line,
            "why_it_matters": message,
            "autofix_class": AutofixClass.MANUAL.value if subject else AutofixClass.ADVISORY.value,
            "owner": Owner.HUMAN.value if subject else Owner.DOWNSTREAM_RESOLVER.value,
            "requires_verification": False,
            "confidence": 100 if entry.get("line") else 75,
            "evidence": [f"{key}:{line} {evidence_line}".strip()],
            "protected_subject": subject.value if subject else None,
        }
        return validate_finding(payload)


def _title(message: str) -> str:
    first = message.split(".")[0].strip() or "Hallazgo de PHPStan"
    return (first[:97] + "...") if len(first) > 100 else first


def _needle(added: dict[int, str], hint: int | None) -> str | None:
    """Frase corta de la linea reportada: la herramienta suele citarla."""
    if hint is None or hint not in added:
        return None
    text = added[hint].strip()
    return text[:40] or None
