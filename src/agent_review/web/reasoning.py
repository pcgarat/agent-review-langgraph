"""Razonamiento de una review como SSE para el panel (021 R5, §5.3).

Primero repite todo lo guardado y después sigue en vivo. Se cierra cuando todas
las personas han cerrado todos sus lotes (o se quedaron sin presupuesto, que corta
los siguientes sin emitir nada), o tras `idle_s` sin novedades: una review que
terminó o que cayó deja de emitir y el navegador no se queda colgado.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict

from agent_review.ports.reasoning import ReasoningEvent, ReasoningReader

# Un `XREAD BLOCK` por vuelta: corto para poder cerrar por inactividad sin esperar.
BLOCK_MS = 5000
# Sin eventos durante este tiempo, la review ya no razona: se cierra el stream.
IDLE_S = 300.0
# Motivo con el que el revisor corta el resto de sus lotes (no emite nada mas).
_NO_BUDGET = "sin presupuesto"


class _Progress:
    """Qué lotes ha abierto y cerrado cada persona, para saber cuándo terminar."""

    def __init__(self) -> None:
        self.totals: dict[str, int] = {}
        self.closed: dict[str, set[int]] = {}
        self.cut: set[str] = set()

    def see(self, event: ReasoningEvent) -> None:
        self.totals[event.role] = max(self.totals.get(event.role, 0), event.total)
        # Un aviso de reintento (021 R14) no cierra el lote: viene otro intento.
        if event.kind == "end" or (event.kind == "error" and not event.meta.get("retry")):
            self.closed.setdefault(event.role, set()).add(event.batch)
        if event.kind == "error" and event.text == _NO_BUDGET:
            self.cut.add(event.role)

    @property
    def done(self) -> bool:
        return bool(self.totals) and all(
            role in self.cut or len(self.closed.get(role, ())) >= total
            for role, total in self.totals.items()
        )


def sse_event(entry_id: str, event: ReasoningEvent) -> str:
    data = json.dumps(asdict(event), ensure_ascii=False, default=str)
    # `error` es el evento nativo de `EventSource` para los fallos de conexion: el de
    # un lote viaja como `fallo` para no confundirse con el.
    name = "fallo" if event.kind == "error" else event.kind
    return f"id: {entry_id}\nevent: {name}\ndata: {data}\n\n"


def reasoning_stream(
    reader: ReasoningReader,
    review_key: str,
    *,
    block_ms: int = BLOCK_MS,
    idle_s: float = IDLE_S,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[str]:
    progress = _Progress()
    after = "0-0"
    # 1) Lo guardado, de una vez (por paginas).
    while True:
        page = reader.read(review_key, after=after)
        for entry_id, event in page:
            progress.see(event)
            yield sse_event(entry_id, event)
            after = entry_id
        if not page:
            break
    # 2) En vivo, hasta que todo este cerrado o deje de llegar nada.
    last = clock()
    while not progress.done:
        page = reader.read(review_key, after=after, block_ms=block_ms)
        if not page:
            if clock() - last >= idle_s:
                break
            yield ": sigo aqui\n\n"  # keep-alive para proxies
            continue
        last = clock()
        for entry_id, event in page:
            progress.see(event)
            yield sse_event(entry_id, event)
            after = entry_id
    yield "event: fin\ndata: {}\n\n"
