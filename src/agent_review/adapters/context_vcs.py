from __future__ import annotations

import logging

from agent_review.domain.models import Finding
from agent_review.globs import path_matches
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
)
from agent_review.ports.vcs import VcsPort
from agent_review.scope import changed_files

logger = logging.getLogger(__name__)


class VcsContextReader:
    """`ContextReaderPort` degradado sobre `VcsPort` (016 W1, W8).

    No hay arbol de trabajo: `read_file` va fichero a fichero por `VcsPort.get_file`
    (un proceso `twg` por lectura) y `grep`/`list_dir` solo pueden mirar **lo que ya
    se sirvio** y los ficheros del diff. Es el modo que queda cuando el
    `sandbox-runner` no esta disponible: da menos alcance, pero la review sigue.

    Es un lector **parcial** (023 E0g): la equivalencia con las demas superficies se
    predica sobre la interseccion visible, no sobre el arbol entero. Un `grep` que
    aqui no ve nada puede verlo el fixture.
    """

    def __init__(
        self,
        *,
        vcs: VcsPort,
        repo: str,
        head_sha: str,
        base_sha: str = "",
        diff: str = "",
        denylist: tuple[str, ...] = (),
        max_grep_matches: int = 200,
    ) -> None:
        self._vcs = vcs
        self._repo = repo
        self._head = head_sha
        self._base = base_sha
        self._denylist = denylist
        self._max_matches = max_grep_matches
        self._diff_files = changed_files(diff)
        # Cache de lo servido: `grep` solo puede buscar donde ya se leyo, y
        # releerlo seria otro proceso `twg` por la misma ruta (R7 de 009).
        self._cache: dict[tuple[str, Ref], str] = {}

    def read_file(
        self,
        path: str,
        *,
        ref: Ref = "head",
        start: int | None = None,
        end: int | None = None,
        mode: ReadMode = "full",
    ) -> ReadResult | None:
        if is_denied(path, self._denylist):
            return None
        sha = self._ref_sha(ref)
        if not sha:
            return None
        key = (path, ref)
        if key not in self._cache:
            try:
                self._cache[key] = self._vcs.get_file(self._repo, path, ref=sha)
            except Exception as exc:  # noqa: BLE001 - el lector nunca tumba la review (W8)
                logger.warning("lector VCS: no se pudo leer %s@%s (%s)", path, ref, exc)
                return None
        text = self._cache[key]
        total_bytes = len(text.encode("utf-8"))
        if mode == "outline":
            index = outline_text(path, text)
            if index is None:
                raise ToolError(f"no se puede indexar {path}; pide `start`/`end` tras un `grep`")
            return ReadResult(content=index, total_bytes=total_bytes)
        if mode != "full":
            raise ToolError(f"mode={mode!r} no existe; usa `full` u `outline`")
        content, served_range = slice_lines(text, start, end)
        return ReadResult(
            content=content,
            total_bytes=total_bytes,
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
        prefix = _normalize_dir(path)
        results: list[str] = []
        all_files: set[str] = set()
        listed: set[str] = set()
        total = 0
        for (cached_path, cached_ref), text in self._cache.items():
            if cached_ref != ref:
                continue
            if prefix and cached_path != prefix and not cached_path.startswith(f"{prefix}/"):
                continue
            if is_denied(cached_path, self._denylist):
                continue
            if glob and not path_matches(_under(cached_path, prefix), glob):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if not matches_line(line):
                    continue
                if files_only:
                    all_files.add(cached_path)
                    if cached_path in listed:
                        continue
                    listed.add(cached_path)
                    if len(results) < self._max_matches:
                        results.append(cached_path)
                    continue
                total += 1
                if len(results) < self._max_matches:
                    results.append(f"{cached_path}:{number}: {line.strip()}")
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
        prefix = _normalize_dir(path)
        under = [candidate for candidate in self._diff_files if candidate.startswith(f"{prefix}/")]
        if prefix and not under:
            if prefix in self._diff_files:
                raise ToolError(f"`{prefix}` es un fichero, no un directorio; usa `read_file`")
            # El lector es parcial: no puede afirmar que el directorio este vacio,
            # solo que el cambio no lo toca. Decirlo evita un «vacio» falso (L1).
            raise ToolError(
                f"`{prefix}` no aparece en los ficheros del cambio; "
                "este lector solo ve el diff (prueba `read_file` sobre una ruta conocida)"
            )
        seen: set[str] = set()
        for candidate in under if prefix else self._diff_files:
            rest = candidate[len(prefix) + 1 :] if prefix else candidate
            parts = rest.split("/")
            for level in range(1, min(depth, len(parts)) + 1):
                name = "/".join(parts[:level])
                full = f"{prefix}/{name}" if prefix else name
                if is_denied(full, self._denylist):
                    continue
                if glob and not path_matches(name, glob):
                    continue
                seen.add(name)
        names = tuple(sorted(seen))
        return ToolResult(items=names, total=len(names))

    def seed(self, paths: list[str] | None = None) -> None:
        """Precarga ficheros al `head_sha` para que `grep` tenga donde buscar.

        Es lo que hace utilizable el modo degradado: sin arbol, el `grep` solo ve
        lo servido. Se puede sembrar con los ficheros del diff o los que L1 ya
        hubiera traido (009 §5.4, 014 H3).
        """
        for path in paths if paths is not None else self._diff_files:
            self.read_file(path)

    def seed_findings(self, findings: list[Finding]) -> None:
        """Precarga los ficheros de los hallazgos de un lote."""
        for finding in findings:
            self.read_file(finding.file)

    def _ref_sha(self, ref: Ref) -> str:
        return self._base if ref == "base" else self._head


def _normalize_dir(path: str | None) -> str:
    normalized = (path or "").strip()
    while normalized.startswith(("./", "/")):
        normalized = normalized[2:] if normalized.startswith("./") else normalized[1:]
    return normalized.rstrip("/")


def _under(relative: str, prefix: str) -> str:
    """Ruta `relative` vista desde `prefix`; el glob se mide ahi (ADR-0027)."""
    return relative[len(prefix) + 1 :] if prefix else relative
