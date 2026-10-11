from __future__ import annotations

from agent_review.domain.models import Finding, ProposedPatch

# Lineas de contexto de cada hunk. `git apply` las usa para localizar el cambio;
# sin ninguna, un hunk a veces no aplica por ambiguedad.
_CONTEXT = 3


def build_unified_diff(
    *,
    path: str,
    source: str,
    start_line: int,
    end_line: int,
    replacement: str,
    context: int = _CONTEXT,
) -> str:
    """Diff unificado de un reemplazo de rango sobre `source` (contenido en head_sha).

    Se deriva localmente del fichero real en vez de fiarse del diff que devuelva
    el modelo: asi los numeros del lado viejo y nuevo son correctos por
    construccion y `git apply` no puede fallar por un header inventado.
    """
    lines = source.splitlines()
    old = lines[start_line - 1 : end_line]
    new = replacement.splitlines()

    before = lines[max(0, start_line - 1 - context) : start_line - 1]
    after = lines[end_line : end_line + context]

    old_start = start_line - len(before)
    old_count = len(before) + len(old) + len(after)
    new_count = len(before) + len(new) + len(after)

    header = f"@@ -{old_start},{old_count} +{old_start},{new_count} @@"
    body = (
        [f" {line}" for line in before]
        + [f"-{line}" for line in old]
        + [f"+{line}" for line in new]
        + [f" {line}" for line in after]
    )
    # La cabecera `diff --git` es lo que ancla el fichero al parser interno
    # (`parse_unified_diff`) y a `git apply`: sin ella, el hunk no tiene dueno.
    head = (f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}", header)
    return "\n".join([*head, *body]) + "\n"


def build_patch(
    finding: Finding,
    *,
    source: str,
    start_line: int,
    end_line: int,
    replacement: str,
) -> ProposedPatch:
    """`ProposedPatch` con el diff derivado del fichero real."""
    return ProposedPatch(
        finding_id=finding.id,
        file=finding.file,
        diff=build_unified_diff(
            path=finding.file,
            source=source,
            start_line=start_line,
            end_line=end_line,
            replacement=replacement,
        ),
        start_line=start_line,
        end_line=end_line,
        replacement=replacement,
    )
