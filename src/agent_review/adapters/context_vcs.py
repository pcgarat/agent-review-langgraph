from __future__ import annotations

import logging

from agent_review.domain.models import Finding
from agent_review.globs import path_matches
from agent_review.ports.context import Ref, is_denied
from agent_review.ports.vcs import VcsPort
from agent_review.scope import changed_files

logger = logging.getLogger(__name__)


class VcsContextReader:
    """`ContextReaderPort` degradado sobre `VcsPort` (016 W1, W8).

    No hay arbol de trabajo: `read_file` va fichero a fichero por `VcsPort.get_file`
    (un proceso `twg` por lectura) y `grep`/`list_dir` solo pueden mirar **lo que ya
    se sirvio** y los ficheros del diff. Es el modo que queda cuando el
    `sandbox-runner` no esta disponible: da menos alcance, pero la review sigue.
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
    ) -> str | None:
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
        return _slice(self._cache[key], start, end)

    def grep(self, pattern: str, *, glob: str | None = None) -> list[str]:
        if not pattern:
            return []
        results: list[str] = []
        for (path, _ref), text in self._cache.items():
            if is_denied(path, self._denylist):
                continue
            if glob and not path_matches(path, glob):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if pattern in line:
                    results.append(f"{path}:{number}: {line.strip()}")
                    if len(results) >= self._max_matches:
                        return results
        return results

    def list_dir(self, path: str) -> list[str]:
        prefix = path.rstrip("/")
        seen: set[str] = set()
        for candidate in self._diff_files:
            if prefix and not candidate.startswith(f"{prefix}/"):
                continue
            rest = candidate[len(prefix) + 1 :] if prefix else candidate
            seen.add(rest.split("/", 1)[0])
        return sorted(seen)

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


def _slice(text: str, start: int | None, end: int | None) -> str:
    if start is None and end is None:
        return text
    lines = text.splitlines()
    first = max(0, (start or 1) - 1)
    last = end if end is not None else len(lines)
    return "\n".join(lines[first:last])
