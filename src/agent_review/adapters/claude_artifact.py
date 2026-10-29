"""Artefacto validado del motor claude (025 B1).

El worker elige la ruta (`repo + pr_id + head_sha`). El CLI no escribe el
fichero: quien llama pasa el stdout ya capturado y aqui se decide si vale.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import ValidationError

from agent_review.adapters.diff_parser import file_key, parse_unified_diff
from agent_review.adapters.tooling_source import safe_segment
from agent_review.domain.contracts import ContractError, validate_finding
from agent_review.domain.models import DiscardedFinding, Finding, ReviewContext, ReviewerResult
from agent_review.verification.l0 import L0Disposition, cite_text, evaluate_l0


class ClaudeArtifactStore:
    """Lista de hallazgos validada, o nada.

    `load` devuelve `None` cuando no hay artefacto valido para esa clave. Una
    lista vacia es un artefacto valido: no es lo mismo que «no se pudo leer».
    Un cuerpo que no cumple el contrato no se escribe y no borra uno anterior.
    """

    def __init__(self, *, root: str | Path) -> None:
        self._root = Path(root)

    def path_for(self, *, repo: str, pr_id: int, head_sha: str) -> Path:
        relative = Path(
            _segment("repo", repo),
            _segment("pr_id", pr_id),
            f"{_segment('head_sha', head_sha)}.json",
        )
        root = self._root.resolve()
        resolved = (root / relative).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"la ruta del artefacto escapa de {self._root}")
        return resolved

    def _discarded_path(self, *, repo: str, pr_id: int, head_sha: str) -> Path:
        return self.path_for(repo=repo, pr_id=pr_id, head_sha=head_sha).with_suffix(
            ".discarded.json"
        )

    def save(self, body: str, *, repo: str, pr_id: int, head_sha: str) -> bool:
        """Guarda el cuerpo si es una lista de hallazgos. `False` no deja marca."""
        findings = parse_findings(body)
        if findings is None:
            return False
        path = self.path_for(repo=repo, pr_id=pr_id, head_sha=head_sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            [item.model_dump(mode="json") for item in findings],
            ensure_ascii=False,
        )
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, path)
        return True

    def load(self, *, repo: str, pr_id: int, head_sha: str) -> list[Finding] | None:
        """`None` si no hay artefacto valido. `[]` si la lista valida esta vacia."""
        path = self.path_for(repo=repo, pr_id=pr_id, head_sha=head_sha)
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            return None
        return parse_findings(body)

    def save_discarded(
        self, items: list[DiscardedFinding], *, repo: str, pr_id: int, head_sha: str
    ) -> None:
        """Los descartes del contrato, al lado del artefacto (034 CP2).

        Se escribe aunque la lista esté vacía: un reintento que reutiliza el
        artefacto no debe leer los descartes de una pasada anterior.
        """
        path = self._discarded_path(repo=repo, pr_id=pr_id, head_sha=head_sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps([item.model_dump(mode="json") for item in items], ensure_ascii=False)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, path)

    def load_discarded(self, *, repo: str, pr_id: int, head_sha: str) -> list[DiscardedFinding]:
        """Vacío si no hay fichero o no se puede leer: un artefacto anterior a 034."""
        path = self._discarded_path(repo=repo, pr_id=pr_id, head_sha=head_sha)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        try:
            return [DiscardedFinding.model_validate(item) for item in data]
        except ValidationError:
            return []

    def discard(self, *, repo: str, pr_id: int, head_sha: str) -> None:
        """Borra el artefacto, sus descartes y su candado. No falla si no estaban."""
        path = self.path_for(repo=repo, pr_id=pr_id, head_sha=head_sha)
        discarded = self._discarded_path(repo=repo, pr_id=pr_id, head_sha=head_sha)
        for item in (path, discarded, path.with_suffix(".lock")):
            try:
                item.unlink()
            except FileNotFoundError:
                continue


def parse_findings(body: str) -> list[Finding] | None:
    """Lista valida, o `None` si el cuerpo no es esa lista.

    Vacio, no-JSON, un objeto suelto o un elemento que no pasa
    `validate_finding` son lo mismo: no hay artefacto. No se devuelve `[]`
    en esos casos, para no confundirlos con una review sin hallazgos.
    """
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    findings: list[Finding] = []
    for item in data:
        if not isinstance(item, dict):
            return None
        try:
            findings.append(validate_finding(item))
        except (ContractError, ValidationError):
            return None
    return findings


def _segment(name: str, value: object) -> str:
    cleaned = safe_segment(value, default="")
    if not cleaned:
        raise ValueError(f"el segmento {name!r} no puede estar vacio")
    return cleaned


class ClaudeArtifactMissing(RuntimeError):
    """No hay artefacto valido para esta review. No es una review sin hallazgos."""


class ClaudeArtifactReviewer:
    """Persona `claude`: lee el artefacto ya validado y le aplica L0.

    No llama al CLI. Una cita que no esta en el diff se tira aqui, antes del
    merge y de la pausa. La persona estatica no pasa por este filtro.
    """

    name = "claude"

    def __init__(self, store: ClaudeArtifactStore) -> None:
        self._store = store

    def review(
        self,
        *,
        repo: str,
        pr_id: int,
        diff: str,
        head_sha: str = "",
        review_key: str = "",
        context: ReviewContext | None = None,
    ) -> ReviewerResult:
        del review_key, context
        loaded = self._store.load(repo=repo, pr_id=pr_id, head_sha=head_sha)
        if loaded is None:
            raise ClaudeArtifactMissing(f"no hay artefacto valido para {repo}#{pr_id}@{head_sha}")
        files = parse_unified_diff(diff)
        kept: list[Finding] = []
        discarded = self._store.load_discarded(repo=repo, pr_id=pr_id, head_sha=head_sha)
        for finding in loaded:
            key = file_key(files, finding.file)
            if key is None:
                discarded.append(_l0_discard(finding, _NOT_IN_DIFF))
                continue
            cite = cite_text(finding.evidence)
            l0 = evaluate_l0(files[key], hint=finding.line, cite=cite)
            if l0.disposition is L0Disposition.DROP or l0.line is None:
                discarded.append(_l0_discard(finding, _CITE_MISSING if cite else _UNANCHORED))
                continue
            kept.append(
                finding.model_copy(
                    update={"file": key, "line": l0.line, "pre_existing": l0.pre_existing}
                )
            )
        return ReviewerResult(findings=kept, discarded=discarded)


# Motivos de L0 para la ficha (034 §4.3). `l0.reason` es el del motor `llm`.
_NOT_IN_DIFF = "el fichero no está en el diff"
_CITE_MISSING = "la cita no aparece en el diff"
_UNANCHORED = "la línea no se pudo anclar"


def _l0_discard(finding: Finding, reason: str) -> DiscardedFinding:
    return DiscardedFinding(
        stage="l0", id=finding.id, title=finding.title, file=finding.file, reason=reason
    )
