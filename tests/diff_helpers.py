"""Diffs unificados mínimos para dobles de VCS en tests.

Publish (008 CX5) exige que `finding.line` esté en las líneas añadidas del diff
parseado; un `diff --git …\\n+x` sin cabecera `@@` no indexa nada.
"""

from __future__ import annotations


def added_lines_diff(*entries: tuple[str, int], text: str = "x") -> str:
    """Un hunk por `(path, line)` con esa línea como añadida."""
    parts: list[str] = []
    for path, line in entries:
        parts.append(
            f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            f"@@ -0,0 +{line},1 @@\n"
            f"+{text}\n"
        )
    return "".join(parts)
