from __future__ import annotations

import logging
from pathlib import Path

from agent_review.globs import matches_any
from agent_review.ports.context import Ref, is_denied

logger = logging.getLogger(__name__)


class FixtureContextReader:
    """`ContextReaderPort` sobre un directorio de ficheros (016 W1, Q2).

    Es el lector **determinista** del *replay*
    ([ADR-0017](../../docs/adr/0017-replay-determinista-con-fingerprint.md)): cada
    turno sirve exactamente los mismos bytes, de modo que el *fingerprint* del
    siguiente prompt se reproduce. Tambien sirve para reproducir una review real a
    mano apuntando a un arbol de trabajo.

    Las lecturas son relativas a `root` y nunca salen de el (`../` se rechaza): un
    caso versionado no puede leer fuera de su directorio. `read_file` con
    `ref="base"` va a `base_root` si se declaro (Q4 de 014); `grep` y `list_dir`
    siempre son el lado `head`.
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
    ) -> str | None:
        root = self._base_root if ref == "base" and self._base_root != self._root else self._root
        target = self._resolve(path, root)
        if target is None or is_denied(path, self._denylist):
            return None
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        if len(text) > self._max_bytes:
            text = text[: self._max_bytes]
        return _slice(text, start, end)

    def grep(self, pattern: str, *, glob: str | None = None) -> list[str]:
        if not pattern:
            return []
        results: list[str] = []
        for target in sorted(self._root.rglob("*")):
            if not target.is_file():
                continue
            relative = target.relative_to(self._root).as_posix()
            if is_denied(relative, self._denylist):
                continue
            if glob and not matches_any(relative, [glob]):
                continue
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if pattern in line:
                    results.append(f"{relative}:{number}: {line.strip()}")
                    if len(results) >= self._max_matches:
                        return results
        return results

    def list_dir(self, path: str) -> list[str]:
        target = self._resolve(path, self._root) if path else self._root
        if target is None or not target.is_dir():
            return []
        return sorted(entry.name for entry in target.iterdir())

    def _resolve(self, path: str, root: Path) -> Path | None:
        candidate = (root / path).resolve()
        if candidate != root and root not in candidate.parents:
            logger.warning("lector fixture: ruta fuera de la raiz (%s)", path)
            return None
        return candidate


def _slice(text: str, start: int | None, end: int | None) -> str:
    """Rango `start..end` (1-based inclusivo), o el texto entero sin rango."""
    if start is None and end is None:
        return text
    lines = text.splitlines()
    first = max(0, (start or 1) - 1)
    last = end if end is not None else len(lines)
    return "\n".join(lines[first:last])
