from __future__ import annotations

import logging
from pathlib import Path

from agent_review.globs import matches_any
from agent_review.outline import outline_text
from agent_review.ports.context import (
    GrepMode,
    ReadMode,
    ReadResult,
    Ref,
    ToolError,
    ToolResult,
    is_denied,
    line_matcher,
    slice_lines,
    truncate_bytes,
)

logger = logging.getLogger(__name__)


class FixtureContextReader:
    """`ContextReaderPort` sobre un directorio de ficheros (016 W1, Q2).

    Es el lector **determinista** del *replay*
    ([ADR-0017](../../docs/adr/0017-replay-determinista-con-fingerprint.md)): cada
    turno sirve exactamente los mismos bytes, de modo que el *fingerprint* del
    siguiente prompt se reproduce. Tambien sirve para reproducir una review real a
    mano apuntando a un arbol de trabajo.

    Las lecturas son relativas a `root` y nunca salen de el (`../` se rechaza): un
    caso versionado no puede leer fuera de su directorio. `read_file` y `grep`
    honran `ref` (`base` va a `base_root` si se declaro, Q4 de 014); `list_dir` no
    tiene lado y siempre lista el arbol `head`.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        base_root: str | Path | None = None,
        denylist: tuple[str, ...] = (),
        max_grep_matches: int = 200,
        max_file_bytes: int = 200_000,
    ) -> None:
        self._root = Path(root).resolve()
        # Dos lados (Q4 de 014): `head` y `base` pueden tener arboles distintos
        # (el caso versiona el fichero a cada sha). Sin `base_root`, un solo arbol
        # sirve los dos, que es lo que quieren los tests de contrato y el replay
        # de un caso sin comparacion con la base.
        self._base_root = Path(base_root).resolve() if base_root is not None else self._root
        self._denylist = denylist
        self._max_matches = max_grep_matches
        self._max_bytes = max_file_bytes

    def read_file(
        self,
        path: str,
        *,
        ref: Ref = "head",
        start: int | None = None,
        end: int | None = None,
        mode: ReadMode = "full",
    ) -> ReadResult | None:
        root = self._base_root if ref == "base" and self._base_root != self._root else self._root
        target = self._resolve(path, root)
        if target is None or is_denied(path, self._denylist):
            return None
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        total_bytes = len(text.encode("utf-8"))
        if mode == "outline":
            index = outline_text(path, text)
            if index is None:
                raise ToolError(f"no se puede indexar {path}; pide `start`/`end` tras un `grep`")
            return ReadResult(content=index, total_bytes=total_bytes)
        if mode != "full":
            raise ToolError(f"mode={mode!r} no existe; usa `full` u `outline`")
        content, served_range = slice_lines(text, start, end)
        content, truncated = truncate_bytes(content, self._max_bytes)
        return ReadResult(
            content=content,
            total_bytes=total_bytes,
            truncated=truncated,
            served_range=served_range,
        )

    def grep(
        self,
        pattern: str,
        *,
        glob: str | None = None,
        path: str | None = None,
        ref: Ref = "head",
        mode: GrepMode = "literal",
        files_only: bool = False,
    ) -> ToolResult:
        if not pattern:
            return ToolResult()
        matches_line = line_matcher(pattern, mode)
        root = self._base_root if ref == "base" and self._base_root != self._root else self._root
        prefix = _normalize_dir(path)
        results: list[str] = []
        all_files: set[str] = set()
        listed: set[str] = set()
        total = 0
        for target in sorted(root.rglob("*")):
            if not target.is_file():
                continue
            relative = target.relative_to(root).as_posix()
            if prefix and relative != prefix and not relative.startswith(f"{prefix}/"):
                continue
            if is_denied(relative, self._denylist):
                continue
            # `glob` se mide contra la ruta relativa a `path` (ADR-0027): asi
            # `path="src"` + `glob="*.py"` significa lo mismo en las cuatro superficies.
            if glob and not matches_any(_under(relative, prefix), [glob]):
                continue
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if not matches_line(line):
                    continue
                if files_only:
                    all_files.add(relative)
                    if relative in listed:
                        continue
                    listed.add(relative)
                    if len(results) < self._max_matches:
                        results.append(relative)
                    continue
                total += 1
                if len(results) < self._max_matches:
                    results.append(f"{relative}:{number}: {line.strip()}")
        if files_only:
            total = len(all_files)
        return ToolResult(
            items=tuple(results),
            total=total,
            truncated=total > len(results),
        )

    def list_dir(
        self,
        path: str,
        *,
        glob: str | None = None,
        depth: int = 1,
    ) -> ToolResult:
        if depth < 1:
            raise ToolError(f"`depth` debe ser >= 1 (recibido {depth})")
        target = self._resolve(path, self._root) if path else self._root
        if target is None:
            raise ToolError(f"la ruta `{path}` sale de la raiz del caso")
        # Vacio e inexistente no son lo mismo: el bucle dice cual es (L1).
        if not target.exists():
            raise ToolError(f"no existe el directorio `{path or '.'}`; lista `.` o usa `grep`")
        if not target.is_dir():
            raise ToolError(f"`{path}` es un fichero, no un directorio; usa `read_file`")
        base = _normalize_dir(path)
        names: list[str] = []
        for entry in target.iterdir() if depth == 1 else target.rglob("*"):
            relative = entry.relative_to(target).as_posix()
            if len(relative.split("/")) > depth:
                continue
            if is_denied(f"{base}/{relative}" if base else relative, self._denylist):
                continue
            if glob and not matches_any(relative, [glob]):
                continue
            names.append(relative)
        items = tuple(sorted(names))
        return ToolResult(items=items, total=len(items))

    def _resolve(self, path: str, root: Path) -> Path | None:
        candidate = (root / path).resolve()
        if candidate != root and root not in candidate.parents:
            logger.warning("lector fixture: ruta fuera de la raiz (%s)", path)
            return None
        return candidate


def _normalize_dir(path: str | None) -> str:
    """Ruta de directorio sin `.`/`/` iniciales ni `/` final; `""` es la raiz."""
    normalized = (path or "").strip()
    while normalized.startswith(("./", "/")):
        normalized = normalized[2:] if normalized.startswith("./") else normalized[1:]
    return normalized.rstrip("/")


def _under(relative: str, prefix: str) -> str:
    """Ruta `relative` vista desde el directorio `prefix` (el glob se mide ahi)."""
    return relative[len(prefix) + 1 :] if prefix else relative
