from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

_DIFF_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")
_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


@dataclass(frozen=True)
class FileDiff:
    """Diff de un fichero indexado por numero de linea.

    `added` y `context` usan el lado nuevo; `removed`, el lado viejo. L0 (008) usa
    `context`/`removed` para `pre_existing` y para no recolocar anclas a una
    anadida cercana.
    """

    path: str
    added: dict[int, str] = field(default_factory=dict)
    context: dict[int, str] = field(default_factory=dict)
    removed: dict[int, str] = field(default_factory=dict)


def parse_unified_diff(diff: str) -> dict[str, FileDiff]:
    """Indexa anadidas, contexto y borradas por fichero.

    Las anadidas y el contexto se numeran con el lado nuevo; las borradas, con el
    lado viejo. El anclaje publico (`best_line`, `resolve_added_line`) sigue
    mirando solo `added`.
    """
    files: dict[str, FileDiff] = {}
    current: str | None = None
    new_line = 0
    old_line = 0
    in_hunk = False

    for raw in diff.splitlines():
        header = _DIFF_HEADER.match(raw)
        if header:
            current = header.group("b")
            files.setdefault(current, FileDiff(path=current, added={}, context={}, removed={}))
            in_hunk = False
            continue

        if raw.startswith("+++") or raw.startswith("---"):
            continue

        hunk = _HUNK_HEADER.match(raw)
        if hunk:
            old_line = int(hunk.group("old_start"))
            new_line = int(hunk.group("new_start"))
            in_hunk = True
            continue

        if not in_hunk or current is None:
            continue

        if raw.startswith("+"):
            files[current].added[new_line] = raw[1:]
            new_line += 1
        elif raw.startswith("-"):
            files[current].removed[old_line] = raw[1:]
            old_line += 1
        elif raw.startswith(" "):
            files[current].context[new_line] = raw[1:]
            old_line += 1
            new_line += 1

    return files


def number_diff(diff: str) -> str:
    """El diff con el numero de linea del lado nuevo delante de cada linea (021 R13).

    Es la vista que se manda al modelo: sin ella, el modelo gasta razonamiento en
    contar lineas a mano para rellenar `line`. Las anadidas y las de contexto llevan
    su numero; las borradas, un hueco (no existen en el lado nuevo). Cuenta igual
    que `parse_unified_diff`, que sigue anclando sobre el diff original.
    """
    out: list[str] = []
    new_line = 0
    in_hunk = False
    for raw in diff.splitlines():
        if _DIFF_HEADER.match(raw):
            in_hunk = False
            out.append(raw)
            continue
        hunk = _HUNK_HEADER.match(raw)
        if hunk:
            new_line = int(hunk.group("new_start"))
            in_hunk = True
            out.append(raw)
            continue
        if not in_hunk or raw.startswith(("+++", "---")):
            out.append(raw)
        elif raw.startswith(("+", " ")):
            out.append(f"{new_line:>6} {raw}")
            new_line += 1
        else:
            out.append(f"{'':>6} {raw}")
    return "\n".join(out) + ("\n" if diff.endswith("\n") else "")


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
