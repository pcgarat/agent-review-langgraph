from __future__ import annotations

import json
import logging
from typing import Any

from agent_review.domain.models import Finding, FixResult, ProposedPatch
from agent_review.fix.patch import build_patch
from agent_review.ports.llm import LlmPort

logger = logging.getLogger(__name__)

_SYSTEM = (
    "Eres un ingeniero que aplica un arreglo puntual sobre un fichero. Recibes un "
    "hallazgo y el contenido completo del fichero en su version revisada. "
    "Respondes SOLO con JSON valido, sin texto alrededor."
)

_SCHEMA_HINT = """\
Devuelve:
{
  "start_line": <entero, primera linea a reemplazar, 1-indexada en el fichero>,
  "end_line": <entero, ultima linea a reemplazar, inclusive>,
  "replacement": "el texto nuevo de esas lineas (puede ser multilinea)"
}
Reglas: cambia lo minimo para resolver el hallazgo; no reescribas el fichero; el
rango debe ser un fragmento contiguo pequeno; `replacement` usa la indentacion del
fichero. Si no hay un cambio defendible, devuelve {"start_line": 0}.
"""

_MAX_SOURCE_CHARS = 60_000
_MAX_TOKENS = 2048
_CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    return len(text) // _CHARS_PER_TOKEN


class LlmFixer:
    """Fixer sobre el `LlmPort` existente (E2: LLM primero).

    Comparte el presupuesto de tokens con las personas: no se gasta en proponer un
    parche si la review ya agoto el presupuesto. Un fallo transitorio sube para que
    el nodo `fix` lo reintente; los agotados se registran y el fix queda `FAILED`.
    """

    def __init__(
        self,
        *,
        llm: LlmPort,
        max_tokens: int = _MAX_TOKENS,
        budget_tokens_per_pr: int | None = None,
        max_fix_lines: int = 30,
    ) -> None:
        self._llm = llm
        self._max_tokens = max_tokens
        self._budget = budget_tokens_per_pr
        self._max_fix_lines = max_fix_lines
        self.last_estimated_tokens = 0

    def propose(
        self, finding: Finding, *, source: str, previous: FixResult | None
    ) -> ProposedPatch:
        prompt = self._prompt(finding, source, previous)
        estimated = _estimate_tokens(_SYSTEM) + _estimate_tokens(prompt) + self._max_tokens
        self.last_estimated_tokens = estimated
        if self._budget is not None and estimated > self._budget:
            raise FixBudgetExceeded(
                f"el coste estimado ({estimated} tokens) supera el presupuesto ({self._budget})"
            )
        raw = self._llm.complete(system=_SYSTEM, prompt=prompt, max_tokens=self._max_tokens)
        return self._to_patch(raw, finding, source)

    def _prompt(self, finding: Finding, source: str, previous: FixResult | None) -> str:
        parts = [
            _SCHEMA_HINT,
            f"FICHERO: {finding.file}",
            f"LINEA DEL HALLAZGO: {finding.line}",
            f"TITULO: {finding.title}",
            f"POR QUE IMPORTA: {finding.why_it_matters}",
        ]
        if finding.suggested_fix:
            parts.append(f"PISTA (de la persona que reviso): {finding.suggested_fix}")
        if previous is not None:
            parts.append(self._previous_context(previous))
        parts.append(f"CONTENIDO DEL FICHERO:\n{source[:_MAX_SOURCE_CHARS]}")
        return "\n\n".join(parts)

    def _previous_context(self, previous: FixResult) -> str:
        """Salida real del intento anterior, acotada (spec §5.2)."""
        details: list[str] = ["INTENTO ANTERIOR FALLIDO:"]
        if previous.patch is not None:
            details.append(f"rango {previous.patch.start_line}-{previous.patch.end_line}")
        if previous.reason:
            details.append(f"motivo: {previous.reason}")
        for check in previous.checks:
            if check.ok:
                continue
            tail = (check.stderr_tail or check.stdout_tail).strip()
            if tail:
                details.append(f"{check.recipe} (exit {check.exit_code}):\n{tail}")
        return "\n".join(details)

    def _to_patch(self, raw: str, finding: Finding, source: str) -> ProposedPatch:
        data = _parse_json(raw)
        if not isinstance(data, dict):
            raise FixNotProposed("el modelo no devolvio un objeto JSON")
        start = _as_int(data.get("start_line"))
        end = _as_int(data.get("end_line"))
        replacement = data.get("replacement")
        if start is None or start <= 0 or end is None or end < start:
            raise FixNotProposed("rango invalido o ausente en la respuesta del modelo")
        if end - start + 1 > self._max_fix_lines:
            raise FixNotProposed(
                f"el modelo propuso {end - start + 1} lineas "
                f"(> max_fix_lines={self._max_fix_lines})"
            )
        if not isinstance(replacement, str):
            raise FixNotProposed("falta el texto `replacement`")
        if start > len(source.splitlines()):
            raise FixNotProposed("el rango empieza mas alla del final del fichero")
        return build_patch(
            finding, source=source, start_line=start, end_line=end, replacement=replacement
        )


class FixerError(RuntimeError):
    """Fallo del fixer (no transitorio salvo el heredado de `LlmPort`)."""


class FixNotProposed(FixerError):
    """El modelo no produjo un parche utilizable; se cuenta como intento."""


class FixBudgetExceeded(FixerError):
    """El presupuesto de tokens no permite proponer el parche."""


def _parse_json(raw: str) -> Any:
    text = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


__all__ = [
    "FixBudgetExceeded",
    "FixNotProposed",
    "FixerError",
    "LlmFixer",
]
