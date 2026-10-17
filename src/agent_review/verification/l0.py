"""L0: anclaje estricto, cita contrastada y `pre_existing` calculado (008).

Puro y sin I/O: quien llama lee el fichero a `base_sha` si quiere el paso (b) de
`pre_existing` y pasa el contenido. Sin cablear aun al `LlmReviewer` (CX5).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from agent_review.adapters.diff_parser import FileDiff

logger = logging.getLogger(__name__)


class L0Disposition(StrEnum):
    """Que hacer con el hallazgo tras L0."""

    KEEP = "keep"  # anclado a una linea anadida
    SUMMARY = "summary"  # cita de contexto/borrada: al resumen, no inline
    DROP = "drop"  # evidencia fabricada (D9)


@dataclass(frozen=True)
class L0Result:
    disposition: L0Disposition
    line: int | None = None
    pre_existing: bool = False
    reason: str = ""


def normalize_spaces(text: str) -> str:
    return " ".join(text.split())


def _match_line(cite: str, lines: dict[int, str]) -> int | None:
    """Primera linea cuyo texto casa con la cita, normalizando espacios."""
    needle = normalize_spaces(cite)
    if not needle:
        return None
    for line_no, text in lines.items():
        haystack = normalize_spaces(text)
        if not haystack:
            continue
        if needle == haystack or needle in haystack or haystack in needle:
            return line_no
    return None


def _cite_in_file(cite: str, content: str) -> bool:
    needle = normalize_spaces(cite)
    if not needle:
        return False
    for raw in content.splitlines():
        haystack = normalize_spaces(raw)
        if haystack and (needle == haystack or needle in haystack or haystack in needle):
            return True
    return False


def is_pre_existing(
    cite: str,
    file_diff: FileDiff,
    *,
    base_content: str | None = None,
) -> bool:
    """¿El codigo citado es identico en la base? (D2).

    (a) casa con contexto o borrada del propio diff; (b) si hay `base_content`,
    casa con alguna linea de ese fichero. Sin base ni match en el diff → False.
    """
    if _match_line(cite, file_diff.context) is not None:
        return True
    if _match_line(cite, file_diff.removed) is not None:
        return True
    if base_content is not None:
        return _cite_in_file(cite, base_content)
    return False


def evaluate_l0(
    file_diff: FileDiff,
    *,
    hint: int | None,
    cite: str | None,
    base_content: str | None = None,
) -> L0Result:
    """Aplica anclaje estricto, cita contrastada y `pre_existing` (008 §5.1).

    No usa `best_line` por cercania: una cita o `hint` sobre contexto/borrada va a
    SUMMARY; una cita que no existe en el fichero, DROP.
    """
    cite_text = (cite or "").strip()

    if cite_text:
        in_added = _match_line(cite_text, file_diff.added)
        if in_added is not None:
            pre = is_pre_existing(cite_text, file_diff, base_content=base_content)
            return L0Result(
                disposition=L0Disposition.KEEP,
                line=in_added,
                pre_existing=pre,
                reason="cita en linea anadida",
            )
        in_context = _match_line(cite_text, file_diff.context)
        if in_context is not None:
            return L0Result(
                disposition=L0Disposition.SUMMARY,
                line=in_context,
                pre_existing=True,
                reason="cita en linea de contexto",
            )
        in_removed = _match_line(cite_text, file_diff.removed)
        if in_removed is not None:
            return L0Result(
                disposition=L0Disposition.SUMMARY,
                line=in_removed,
                pre_existing=True,
                reason="cita en linea borrada",
            )
        return L0Result(
            disposition=L0Disposition.DROP,
            reason="cita inexistente en el fichero del diff",
        )

    # Sin cita: el hint manda, sin recolocar a la anadida mas cercana.
    if hint is not None and hint in file_diff.added:
        pre = False
        if base_content is not None:
            # Sin cita textual no hay ancla de contenido; no inventamos pre_existing.
            pre = False
        return L0Result(
            disposition=L0Disposition.KEEP,
            line=hint,
            pre_existing=pre,
            reason="hint en linea anadida",
        )
    if hint is not None and hint in file_diff.context:
        return L0Result(
            disposition=L0Disposition.SUMMARY,
            line=hint,
            pre_existing=True,
            reason="hint en linea de contexto",
        )
    if hint is not None and hint in file_diff.removed:
        return L0Result(
            disposition=L0Disposition.SUMMARY,
            line=hint,
            pre_existing=True,
            reason="hint en linea borrada",
        )
    if hint is not None:
        logger.debug(
            "L0: hint %s fuera del diff de %s; se descarta sin recolocar",
            hint,
            file_diff.path,
        )
    return L0Result(
        disposition=L0Disposition.DROP,
        reason="sin cita ni hint anclable a una linea del diff",
    )
