from __future__ import annotations

import logging
from collections.abc import Callable

from agent_review.adapters.context_vcs import VcsContextReader
from agent_review.adapters.context_workspace import (
    ContextError,
    ContextUnavailable,
    WorkspaceContextReader,
)
from agent_review.ports.context import (
    ContextReaderPort,
    GrepMode,
    ReadMode,
    ReadResult,
    Ref,
    ToolResult,
)
from agent_review.ports.vcs import VcsPort
from agent_review.settings import Settings

logger = logging.getLogger(__name__)

# Firma de la fabrica de lectores: el consumidor la recibe ya construida para su
# lote (repo, head_sha, base_sha) y no sabe si detras esta el runner o el VCS.
ReaderFactory = Callable[[str, str, str], ContextReaderPort]


class _DegradingReader:
    """Lector que empieza por el workspace y cae al VCS si este falla (016 W8).

    La primera operacion que el runner no pueda atender (`5xx` reiterado, runner
    caido) degrada a `VcsContextReader` para el resto del lote: una pieza de
    contexto nunca tumba una review. El VCS se construye con el diff del lote para
    poder servir `grep`/`list_dir` sobre lo que ya se leyo y los ficheros del diff.
    """

    def __init__(
        self,
        *,
        primary: ContextReaderPort,
        fallback: VcsContextReader,
    ) -> None:
        self._fallback = fallback
        self._active: ContextReaderPort = primary

    def _degrade(self, exc: Exception) -> None:
        logger.warning("lector de contexto: se degrada al VCS (%s)", exc)
        self._active = self._fallback
        self._fallback.seed()

    def read_file(
        self,
        path: str,
        *,
        ref: Ref = "head",
        start: int | None = None,
        end: int | None = None,
        mode: ReadMode = "full",
    ) -> ReadResult | None:
        try:
            return self._active.read_file(path, ref=ref, start=start, end=end, mode=mode)
        except (ContextUnavailable, ContextError) as exc:
            if self._active is self._fallback:
                raise
            self._degrade(exc)
            return self._active.read_file(path, ref=ref, start=start, end=end, mode=mode)

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
        try:
            return self._active.grep(
                pattern, glob=glob, path=path, ref=ref, mode=mode, files_only=files_only
            )
        except (ContextUnavailable, ContextError) as exc:
            if self._active is self._fallback:
                raise
            self._degrade(exc)
            return self._active.grep(
                pattern, glob=glob, path=path, ref=ref, mode=mode, files_only=files_only
            )

    def list_dir(
        self,
        path: str,
        *,
        glob: str | None = None,
        depth: int = 1,
    ) -> ToolResult:
        try:
            return self._active.list_dir(path, glob=glob, depth=depth)
        except (ContextUnavailable, ContextError) as exc:
            if self._active is self._fallback:
                raise
            self._degrade(exc)
            return self._active.list_dir(path, glob=glob, depth=depth)


def build_reader_factory(
    settings: Settings,
    *,
    vcs: VcsPort,
    diff: str,
    denylist: tuple[str, ...],
) -> ReaderFactory:
    """Fabrica de lectores para un lote (016 W8, H3 de 014).

    Con `SANDBOX_RUNNER_URL`, devuelve un lector que intenta el workspace del runner
    y degrada al VCS si el runner falla; sin runner, devuelve directamente el
    `VcsContextReader`. El `diff` del lote alimenta el modo degradado (ficheros que
    puede listar y sembrar).
    """
    runner_url = settings.sandbox_runner_url.strip()
    token = settings.sandbox_runner_token

    def factory(repo: str, head_sha: str, base_sha: str) -> ContextReaderPort:
        fallback = VcsContextReader(
            vcs=vcs,
            repo=repo,
            head_sha=head_sha,
            base_sha=base_sha,
            diff=diff,
            denylist=denylist,
        )
        if not runner_url:
            return fallback
        primary = WorkspaceContextReader(
            base_url=runner_url,
            repo=repo,
            head_sha=head_sha,
            base_sha=base_sha,
            token=token,
            denylist=denylist,
        )
        return _DegradingReader(primary=primary, fallback=fallback)

    return factory
