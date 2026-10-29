"""023 C1: la caché de E/S cubre `read_file` y solo `read_file`.

`grep`/`list_dir` dependen de lo sembrado en cada lote (lector VCS): cachear un
«sin coincidencias» del lote 1 haría perder ficheros del lote 2 (E13).
"""

from __future__ import annotations

from typing import Any

from agent_review.adapters.context_cache import CachingReader
from agent_review.ports.context import ReadResult, ToolResult


class _CountingReader:
    """Lector mínimo que cuenta las llamadas que llegan al adaptador."""

    def __init__(self) -> None:
        self.reads = 0
        self.greps = 0
        self.listings = 0

    def read_file(self, path: str, **kwargs: Any) -> ReadResult | None:
        del kwargs
        self.reads += 1
        if path == "falta.py":
            return None
        return ReadResult(content=f"{path}\n", total_bytes=len(path) + 1)

    def grep(self, pattern: str, **kwargs: Any) -> ToolResult:
        del pattern, kwargs
        self.greps += 1
        return ToolResult(items=())

    def list_dir(self, path: str, **kwargs: Any) -> ToolResult:
        del path, kwargs
        self.listings += 1
        return ToolResult(items=())


def test_read_file_repetido_solo_va_al_lector_una_vez() -> None:
    underlying = _CountingReader()
    cached = CachingReader(underlying)

    first = cached.read_file("a.py")
    second = cached.read_file("a.py")

    assert first is second
    assert underlying.reads == 1
    assert (cached.stats.hits, cached.stats.misses) == (1, 1)


def test_un_rango_o_un_modo_distinto_es_otra_clave() -> None:
    underlying = _CountingReader()
    cached = CachingReader(underlying)

    cached.read_file("a.py")
    cached.read_file("a.py", start=1, end=2)
    cached.read_file("a.py", mode="outline")

    assert underlying.reads == 3
    assert cached.stats.misses == 3


def test_un_fichero_ausente_tambien_se_cachea() -> None:
    underlying = _CountingReader()
    cached = CachingReader(underlying)

    assert cached.read_file("falta.py") is None
    assert cached.read_file("falta.py") is None

    assert underlying.reads == 1
    assert cached.stats.hits == 1


def test_grep_y_list_dir_no_se_cachean() -> None:
    # En el lector VCS dependen del seed de cada lote: cachearlos perdería ficheros.
    underlying = _CountingReader()
    cached = CachingReader(underlying)

    cached.grep("needle")
    cached.grep("needle")
    cached.list_dir("")
    cached.list_dir("")

    assert (underlying.greps, underlying.listings) == (2, 2)


def test_la_cache_se_comparte_entre_lectores_de_la_misma_review() -> None:
    # El revisor crea un lector por lote; el diccionario es el mismo.
    underlying = _CountingReader()
    cache: dict = {}
    first = CachingReader(underlying, cache=cache)
    second = CachingReader(underlying, cache=cache, stats=first.stats)

    first.read_file("a.py")
    second.read_file("a.py")

    assert underlying.reads == 1
    assert first.stats.hits == 1
