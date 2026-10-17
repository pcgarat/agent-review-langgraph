from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Prefijo del stream de razonamiento de una review (021 R4). La clave completa es
# `reviews:reasoning:<review_key>`: la escribe el worker y la lee el panel.
REASONING_KEY_PREFIX = "reviews:reasoning:"

ReasoningKind = Literal["start", "reasoning", "end", "error"]


def reasoning_key(review_key: str) -> str:
    return f"{REASONING_KEY_PREFIX}{review_key}"


@dataclass(frozen=True)
class ReasoningEvent:
    """Un evento del razonamiento de una llamada al modelo (021 §5.2).

    `start` y `end` delimitan cada llamada (lote `batch` de `total`); `reasoning`
    lleva un trozo de texto; `error`, el motivo de que la llamada no respondiera.
    `meta` lleva lo que no es texto: los ficheros del lote en `start` y el `usage`
    y el `finish_reason` en `end`.
    """

    kind: ReasoningKind
    role: str
    batch: int
    total: int
    text: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ReasoningSink(Protocol):
    """Destino del razonamiento de los revisores (021 R3/R4).

    *Best-effort*: emitir nunca lanza ni frena la review. Un adaptador que no pueda
    escribir registra el fallo y sigue; quien emite no comprueba nada.
    """

    def emit(self, review_key: str, event: ReasoningEvent) -> None: ...


@runtime_checkable
class ReasoningReader(Protocol):
    """Lectura del razonamiento de una review (la usa el panel, 021 R5).

    `read` devuelve `(id, evento)` posteriores a `after` (`"0-0"` = desde el
    principio); con `block_ms` espera a que llegue alguno.
    """

    def exists(self, review_key: str) -> bool: ...

    def read(
        self, review_key: str, *, after: str = "0-0", block_ms: int | None = None, count: int = 500
    ) -> list[tuple[str, ReasoningEvent]]: ...


class InMemoryReasoningSink:
    """Sink y lector en memoria, para tests y para leer el razonamiento sin Redis."""

    def __init__(self) -> None:
        self.events: dict[str, list[ReasoningEvent]] = {}

    def emit(self, review_key: str, event: ReasoningEvent) -> None:
        self.events.setdefault(review_key, []).append(event)

    def exists(self, review_key: str) -> bool:
        return bool(self.events.get(review_key))

    def read(
        self, review_key: str, *, after: str = "0-0", block_ms: int | None = None, count: int = 500
    ) -> list[tuple[str, ReasoningEvent]]:
        del block_ms  # en memoria no hay nada que esperar
        start = int(after.split("-")[0]) if after != "0-0" else 0
        events = self.events.get(review_key, [])
        return [(f"{i}-0", events[i - 1]) for i in range(start + 1, len(events) + 1)][:count]

    def text(self, review_key: str, *, batch: int | None = None) -> str:
        """El razonamiento acumulado de una review (o de un lote)."""
        return "".join(
            event.text
            for event in self.events.get(review_key, [])
            if event.kind == "reasoning" and (batch is None or event.batch == batch)
        )


def emit_safely(sink: ReasoningSink | None, review_key: str, event: ReasoningEvent) -> None:
    """Emite sin que un sink roto afecte a la review (021 R2)."""
    if sink is None or not review_key:
        return
    try:
        sink.emit(review_key, event)
    except Exception:
        logger.exception("El sink de razonamiento fallo; se ignora")
