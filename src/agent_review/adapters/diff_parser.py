from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

_DIFF_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")
_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


@dataclass(frozen=True)
class FileDiff:
    path: str
    added: dict[int, str]


def parse_unified_diff(diff: str) -> dict[str, FileDiff]:
    """Indexa lineas anadidas por fichero y numero de linea del lado nuevo.

    Solo se indexan las lineas `+` reales (no `+++`). Sirve para que las personas
    anclen hallazgos a lineas que existen en el diff, requisito de Bitbucket.
    """
    files: dict[str, FileDiff] = {}
    current: str | None = None
    new_line = 0
    in_hunk = False

    for raw in diff.splitlines():
        header = _DIFF_HEADER.match(raw)
        if header:
            current = header.group("b")
            files.setdefault(current, FileDiff(path=current, added={}))
            in_hunk = False
            continue

        if raw.startswith("+++") or raw.startswith("---"):
            continue

        hunk = _HUNK_HEADER.match(raw)
        if hunk:
            new_line = int(hunk.group("new_start"))
            in_hunk = True
            continue

        if not in_hunk or current is None:
            continue

        if raw.startswith("+"):
            files[current].added[new_line] = raw[1:]
            new_line += 1
        elif raw.startswith("-"):
            continue
        elif raw.startswith(" "):
            new_line += 1

    return files


def file_key(files: dict[str, FileDiff], path: str) -> str | None:
    """Casa la ruta del linter con una del diff (basename como ultimo recurso)."""
    if path in files:
        return path
    normalized = path.lstrip("./")
    if normalized in files:
        return normalized
    target = PurePosixPath(normalized).name
    matches = [candidate for candidate in files if PurePosixPath(candidate).name == target]
    if len(matches) == 1:
        return matches[0]
    return None


def best_line(added: dict[int, str], hint: int | None, needle: str | None) -> int | None:
    """Elige la linea ancla: por `needle`, o la mas cercana al hint."""
    if not added:
        return None
    if needle:
        stripped = needle.strip()
        for line_no, text in added.items():
            if stripped and stripped in text:
                return line_no
    if hint is not None:
        return min(added, key=lambda line_no: abs(line_no - hint))
    return sorted(added)[0]


def resolve_added_line(added: dict[int, str], text: str) -> int | None:
    """Linea del lado nuevo que contiene `text`, o `None`.

    Es la resolucion de ancla *en cliente*: mismo criterio para todo adaptador
    (Bitbucket la delega al servidor como `prs diff-line`, GitHub la calcula
    aqui). Sin coincidencia no hay ancla: el publish manda el hallazgo al resumen
    en vez de colocar el comentario en una linea inventada.
    """
    needle = (text or "").strip()
    if not needle:
        return None
    for line_no, line in added.items():
        if needle in line:
            return line_no
    return None
