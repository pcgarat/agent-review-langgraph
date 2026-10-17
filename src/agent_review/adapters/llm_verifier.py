from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from agent_review.adapters.tool_loop import LoopResult, run_tool_loop
from agent_review.domain.models import Finding, Verdict, VerificationResult
from agent_review.ports.budget import BudgetPort
from agent_review.ports.context import ContextLimits, ContextReaderPort
from agent_review.ports.llm import LlmPort
from agent_review.scope import split_diff

# Fabrica por review: (repo, head_sha, base_sha, diff) → lector anclado al commit.
OpenReader = Callable[[str, str, str, str], ContextReaderPort]

# Prompt que **refuta** (009 V3 / §5.5): un verificador que aprueba por defecto
# no verifica. El escenario de fallo obliga a recorrer la ruta.
_SYSTEM = (
    "Eres un verificador adversarial de hallazgos de code review. Tu trabajo es "
    "REFUTAR la hipotesis del revisor, no aprobarla. Intenta construir un escenario "
    "en el que el hallazgo sea falso; si el codigo que lees lo desmiente, refuta. "
    "Si tras leer el codigo la hipotesis se sostiene, confirmala con un "
    "failure_scenario de tres partes (entrada/estado -> file:line -> resultado "
    "incorrecto) y citas file:line de ficheros que hayas leido. "
    "Si no puedes probar ni refutar, marca unproven. "
    "Respondes SOLO con JSON valido, sin texto alrededor."
)

_SCHEMA = """\
Objeto final (cuando tengas veredicto):
{
  "verdict": "confirmed|refuted|unproven",
  "failure_scenario": "obligatorio si confirmed: entrada/estado -> file:line -> resultado",
  "evidence": ["file:line cita textual del codigo leido"],
  "reason": "breve justificacion"
}
No afirmes sobre codigo que no hayas leido con read_file o grep.
Para pedir herramientas responde {"tools": [{"tool": "read_file", "path": "..."}, ...]}.
"""

_VERDICTS = frozenset(v.value for v in Verdict)


class LlmVerifier:
    """Verificador adversarial sobre el bucle *ReAct* de 016 (009 VF5).

    No reimplementa el bucle: aporta el objeto final (el veredicto) y el *prompt*
    que refuta. La adjudicacion mecanica (cita servida, escenario) la hace el
    nodo `verify` (VF6); aqui solo se traduce la respuesta del modelo a
    `VerificationResult`, o `UNPROVEN` si el bucle cierra sin veredicto.

    El lector puede ser fijo (tests con fixture) o una fabrica `open_reader`
    (worker: un lector por review anclado a repo/head/diff, VF8).
    """

    def __init__(
        self,
        llm: LlmPort,
        reader: ContextReaderPort | None = None,
        *,
        open_reader: OpenReader | None = None,
        limits: ContextLimits | None = None,
        budget: BudgetPort | None = None,
        name: str = "verifier",
        max_tokens: int = 4096,
    ) -> None:
        if reader is None and open_reader is None:
            raise ValueError("LlmVerifier exige `reader` o `open_reader`")
        self.name = name
        self._llm = llm
        self._reader = reader
        self._open_reader = open_reader
        self._limits = limits or ContextLimits()
        self._budget = budget
        self._max_tokens = max_tokens

    def verify(
        self,
        finding: Finding,
        *,
        repo: str,
        head_sha: str,
        base_sha: str = "",
        diff: str = "",
        review_key: str = "",
    ) -> VerificationResult:
        if self._open_reader is not None:
            reader: ContextReaderPort = self._open_reader(repo, head_sha, base_sha, diff)
        else:
            fixed = self._reader
            if fixed is None:
                raise RuntimeError("LlmVerifier sin reader ni open_reader")
            reader = fixed
        loop = run_tool_loop(
            self._llm,
            system=_SYSTEM,
            prompt=_build_prompt(finding, diff),
            reader=reader,
            limits=self._limits,
            is_final=_is_verdict,
            budget=self._budget,
            review_key=review_key,
            max_tokens=self._max_tokens,
        )
        return _to_result(finding, loop)


def _is_verdict(data: dict[str, Any]) -> bool:
    """¿Es el objeto final del verificador y no una peticion de herramientas?"""
    if "tools" in data and "verdict" not in data:
        return False
    return str(data.get("verdict", "")).strip().lower() in _VERDICTS


def _build_prompt(finding: Finding, diff: str) -> str:
    files = [path for path, _ in split_diff(diff)] if diff else []
    hunk = _hunk_for(finding.file, diff) if diff else ""
    payload = {
        "finding": {
            "id": finding.id,
            "title": finding.title,
            "severity": finding.severity.value,
            "file": finding.file,
            "line": finding.line,
            "why_it_matters": finding.why_it_matters,
            "evidence": list(finding.evidence),
            "failure_scenario": finding.failure_scenario,
        },
        "diff_files": files,
        "hunk": hunk or "(sin hunk del fichero en el diff)",
    }
    return (
        f"{_SCHEMA}\n\n"
        "Hallazgo a verificar (intenta refutarlo):\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _hunk_for(path: str, diff: str) -> str:
    for file_path, block in split_diff(diff):
        if file_path == path:
            return block
    return ""


def _finding_id(finding: Finding) -> str:
    return finding.id or f"{finding.file}:{finding.line}:{finding.title}"


def _to_result(finding: Finding, loop: LoopResult) -> VerificationResult:
    finding_id = _finding_id(finding)
    files_read = sorted(loop.served)
    if loop.data is None:
        reason = (
            "sin veredicto antes del tope"
            if loop.final_forced or loop.turns or loop.tool_calls
            else "sin veredicto del modelo"
        )
        return VerificationResult(
            finding_id=finding_id,
            verdict=Verdict.UNPROVEN,
            files_read=files_read,
            reason=reason,
        )

    raw = str(loop.data.get("verdict", "")).strip().lower()
    try:
        verdict = Verdict(raw)
    except ValueError:
        return VerificationResult(
            finding_id=finding_id,
            verdict=Verdict.UNPROVEN,
            files_read=files_read,
            reason=f"veredicto desconocido: {raw!r}",
        )

    evidence_raw = loop.data.get("evidence") or []
    evidence = [str(item) for item in evidence_raw] if isinstance(evidence_raw, list) else []
    scenario = loop.data.get("failure_scenario")
    if scenario is not None:
        scenario = str(scenario).strip() or None
    reason = str(loop.data.get("reason", "")).strip()
    return VerificationResult(
        finding_id=finding_id,
        verdict=verdict,
        failure_scenario=scenario,
        evidence=evidence,
        files_read=files_read,
        reason=reason,
    )
