from __future__ import annotations

import logging

from reviews.adapters.diff_parser import parse_unified_diff
from reviews.globs import matches_any

logger = logging.getLogger(__name__)

# Ficheros que no aportan senal a una review: generados, lockfiles, vendor y
# artefactos de build. Filtrarlos antes de las personas evita gastar tokens (o
# inlines) en ruido que nadie leeria. Es un conjunto minimo y revisable; se puede
# ampliar por deploy con `SCOPE_IGNORE`.
DEFAULT_IGNORE_PATTERNS = (
    "composer.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "uv.lock",
    "vendor/**",
    "node_modules/**",
    "var/**",
    "public/build/**",
    "**/*.min.js",
    "**/*.min.css",
    "**/*.map",
    "**/*.generated.*",
)

_DIFF_HEADER = "diff --git "


def merge_ignore_patterns(configured: str) -> list[str]:
    """Patrones de ignorado: los por defecto mas los declarados por deploy."""
    extra = [item.strip() for item in configured.split(",") if item.strip()]
    return [*DEFAULT_IGNORE_PATTERNS, *extra]


def is_ignored(path: str, patterns: list[str]) -> bool:
    return matches_any(path.lstrip("./"), patterns)


def limit_files(files: list[tuple[str, int, int]], max_files: int) -> tuple[list[str], bool]:
    """Recorta la lista de ficheros por tamano.

    Devuelve los ficheros que se revisan (ordenados por churn descendente) y si
    se truncaron. El orden determinista evita que dos ejecuciones del mismo PR
    revisen subconjuntos distintos.
    """
    ordered = sorted(files, key=lambda item: (item[1] + item[2], item[0]), reverse=True)
    if max_files > 0 and len(ordered) > max_files:
        return [path for path, _added, _removed in ordered[:max_files]], True
    return [path for path, _added, _removed in ordered], False


def filter_diff(diff: str, patterns: list[str], *, allowed: list[str] | None = None) -> str:
    """Trocea el diff unificado y descarta los ficheros ignorados o fuera de `allowed`."""
    blocks: list[str] = []
    current: list[str] = []
    keep = False
    for line in diff.splitlines(keepends=True):
        if line.startswith(_DIFF_HEADER):
            if current and keep:
                blocks.append("".join(current))
            current = [line]
            path = _block_path(line)
            keep = path is not None and not is_ignored(path, patterns)
            if keep and allowed is not None:
                keep = path in allowed
        elif current:
            current.append(line)
    if current and keep:
        blocks.append("".join(current))
    return "".join(blocks)


def changed_files(diff: str) -> list[str]:
    """Rutas del lado nuevo que aparecen en el diff (deduplicadas, en orden)."""
    seen: dict[str, None] = {}
    for line in diff.splitlines():
        if not line.startswith(_DIFF_HEADER):
            continue
        path = _block_path(line)
        if path is not None:
            seen.setdefault(path, None)
    return list(seen)


def _block_path(header_line: str) -> str | None:
    parsed = parse_unified_diff(header_line + "\n")
    return next(iter(parsed), None)
