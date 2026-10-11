from __future__ import annotations

from reviews.adapters.diff_parser import file_key, parse_unified_diff
from reviews.domain.models import Finding, ProposedPatch


def validate_patch(
    patch: ProposedPatch,
    *,
    finding: Finding,
    diff: str,
    max_fix_lines: int,
) -> str | None:
    """Valida un parche localmente, sin red ni sandbox. Devuelve la razon o `None`.

    Reglas (spec §5.2): un solo fichero (el del hallazgo), un solo hunk, tamano
    acotado y rango dentro de las lineas **anadidas** del diff del PR. Lo ultimo
    es la clave: una sugerencia inline solo puede reemplazar lo que el PR muestra;
    proponer un rango fuera del diff no es aplicable aunque el parche sea correcto.
    """
    if not patch.diff.strip():
        return "el parche esta vacio"

    pr_files = parse_unified_diff(diff)
    key = file_key(pr_files, finding.file)
    if key is None:
        return f"el fichero del hallazgo ({finding.file}) no esta en el diff del PR"

    files = parse_unified_diff(patch.diff)
    if len(files) != 1:
        return f"el parche toca {len(files)} fichero(s); solo se admite uno"
    (path,) = tuple(files)
    if path != key:
        return f"el parche toca {path}, no el fichero del hallazgo ({key})"

    hunks = [line for line in patch.diff.splitlines() if line.startswith("@@ ")]
    if len(hunks) != 1:
        return f"el parche tiene {len(hunks)} hunks; solo se admite uno"

    if patch.end_line < patch.start_line:
        return "rango invalido: end_line es anterior a start_line"
    span = patch.end_line - patch.start_line + 1
    if span > max_fix_lines:
        return f"el rango ({span} lineas) supera max_fix_lines={max_fix_lines}"

    added = pr_files[key].added
    outside = [n for n in range(patch.start_line, patch.end_line + 1) if n not in added]
    if outside:
        preview = ", ".join(str(n) for n in outside[:5])
        return f"el rango sale de las lineas anadidas del PR (faltan: {preview})"
    return None
