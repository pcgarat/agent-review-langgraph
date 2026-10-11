from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from agent_review.adapters.diff_parser import FileDiff, best_line, file_key, parse_unified_diff
from agent_review.adapters.text_utils import guess_protected_subject, shorten_title, slug
from agent_review.adapters.tooling_source import StaticToolingOutputSource
from agent_review.domain.contracts import validate_finding
from agent_review.domain.models import (
    AutofixClass,
    Finding,
    Owner,
    ReviewerResult,
    Severity,
    ToolingSourceStatus,
)
from agent_review.ports.tooling import ToolingOutputSource

logger = logging.getLogger(__name__)

_SEVERITY_MAP = {"error": Severity.P2, "warning": Severity.P3}


class ToolingReviewer:
    """Persona estatica: convierte salida de herramientas del repo en hallazgos.

    No ejecuta las herramientas: pide su salida al `ToolingOutputSource` inyectado
    (artefacto de CI en Fase 1, sandbox en Fase 3). Filtra a lo que el PR cambio
    y ancla a lineas del diff.
    """

    def __init__(
        self,
        *,
        source: ToolingOutputSource | None = None,
        phpstan_json: str | Sequence[str] = (),
        allowed_tools: Sequence[str] = ("phpstan", "php-cs-fixer"),
        name: str = "standards",
    ) -> None:
        self.name = name
        if source is not None:
            self._source: ToolingOutputSource | None = source
        elif phpstan_json:
            raw = phpstan_json if isinstance(phpstan_json, str) else "".join(phpstan_json)
            self._source = StaticToolingOutputSource(raw)
        else:
            self._source = None
        self._allowed = set(allowed_tools)

    def review(self, *, repo: str, pr_id: int, diff: str, head_sha: str = "") -> ReviewerResult:
        files = parse_unified_diff(diff)
        entries, status = self._entries(repo=repo, pr_id=pr_id, head_sha=head_sha)
        findings: list[Finding] = []
        for entry in entries:
            finding = self._to_finding(entry, files)
            if finding is not None:
                findings.append(finding)
        return ReviewerResult(findings=findings, tooling_status=status)

    def _entries(
        self, *, repo: str, pr_id: int, head_sha: str
    ) -> tuple[list[dict[str, Any]], ToolingSourceStatus]:
        # Fuente no disponible: se propaga el motivo para no reportar "limpio".
        if self._source is None:
            return [], ToolingSourceStatus.MISSING
        if "phpstan" not in self._allowed:
            return [], ToolingSourceStatus.MISSING
        status, raw = self._source.read("phpstan", repo=repo, pr_id=pr_id, head_sha=head_sha)
        if status is not ToolingSourceStatus.PRESENT:
            return [], status
        # Un artefacto presente pero vacio no es un JSON valido: no equivale a limpio.
        if not raw or not raw.strip():
            return [], ToolingSourceStatus.UNREADABLE
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Salida de phpstan ilegible; se ignoran sus hallazgos")
            return [], ToolingSourceStatus.UNREADABLE
        if not isinstance(data, dict):
            return [], ToolingSourceStatus.UNREADABLE
        entries = data.get("files", {})
        result: list[dict[str, Any]] = []
        if isinstance(entries, dict):
            for path, payload in entries.items():
                messages = payload.get("messages", []) if isinstance(payload, dict) else []
                for message in messages:
                    result.append({"path": path, **message})
        return result, ToolingSourceStatus.PRESENT

    def _to_finding(self, entry: dict[str, Any], files: dict[str, FileDiff]) -> Finding | None:
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
        subject = guess_protected_subject(message)
        evidence_line = added.get(line, "")
        payload = {
            "id": f"standards-{slug(path)}-{hint or line}-{slug(message)[:24]}",
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
    return shorten_title(first)


def _needle(added: dict[int, str], hint: int | None) -> str | None:
    """Frase corta de la linea reportada: la herramienta suele citarla."""
    if hint is None or hint not in added:
        return None
    text = added[hint].strip()
    return text[:40] or None
