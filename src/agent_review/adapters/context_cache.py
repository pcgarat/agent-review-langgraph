"""Caché de E/S del lector de contexto (023 C1).

Cachea **solo `read_file`**: el contenido de una ruta a una `ref` fija no cambia
durante la review, así que repetir la lectura en otro lote es E/S tirada. `grep` y
`list_dir` **no** se cachean: en el modo degradado dependen de lo sembrado en cada
lote y un «sin coincidencias» cacheado haría perder ficheros del siguiente
([ADR-0026](../../../docs/adr/0026-contrato-de-lectura-con-tamano-real.md), E13).

No cambia el *prompt*: ahorra E/S, no tokens de contexto.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_review.ports.context import (
    ContextReaderPort,
    GrepMode,
    ReadMode,
    ReadResult,
    Ref,
    ToolResult,
)

CacheKey = tuple[str, Ref, int | None, int | None, ReadMode]


@dataclass
class CacheStats:
    """Contador de E/S del caché; la medida de C1 sale de aquí."""

    hits: int = 0
    misses: int = 0


@dataclass
class CachingReader:
    """Decorador de `ContextReaderPort` que cachea `read_file` (023 C1).

    El `cache` y las `stats` se comparten entre los lectores de cada lote: la
    caché es **por review**, no por lote, y el ahorro aparece en la segunda
    lectura. Un acierto devuelve el mismo `ReadResult` (inmutable).
    """

    reader: ContextReaderPort
    cache: dict[CacheKey, ReadResult | None] = field(default_factory=dict)
    stats: CacheStats = field(default_factory=CacheStats)

    def read_file(
        self,
        path: str,
        *,
        ref: Ref = "head",
        start: int | None = None,
        end: int | None = None,
        mode: ReadMode = "full",
    ) -> ReadResult | None:
        key = (path, ref, start, end, mode)
        if key in self.cache:
            self.stats.hits += 1
            return self.cache[key]
        self.stats.misses += 1
        reading = self.reader.read_file(path, ref=ref, start=start, end=end, mode=mode)
        self.cache[key] = reading
        return reading

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
        return self.reader.grep(
            pattern, glob=glob, path=path, ref=ref, mode=mode, files_only=files_only
        )

    def list_dir(
        self,
        path: str,
        *,
        glob: str | None = None,
        depth: int = 1,
    ) -> ToolResult:
        return self.reader.list_dir(path, glob=glob, depth=depth)
