from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# Estado del hilo reconstruido del checkpointer: no queda nada por ejecutar
# (`completed`), espera a una persona o a una wave de fix (`paused`) o quedo a
# medias (`in_progress`: un nodo fallo o el worker murio a mitad).
TraceState = Literal["completed", "paused", "in_progress"]
# `kind` de la pausa segun el payload del `interrupt` (`human`/`fix`). Se aliasa
# para no repetir el `Literal` entre el dominio y el calculo de la traza.
PauseKindName = Literal["human", "fix"]


class TraceNode(BaseModel):
    """Un nodo ejecutado del recorrido, reconstruido del checkpointer.

    `started_at` es el instante del checkpoint que dejo el nodo al terminar: con
    nodos consecutivos da la duracion de cada uno salvo el ultimo, que aun no
    tiene sucesor. El nodo **en curso ahora** no se deduce de aqui (el
    checkpointer es historia): lo dice la marca de progreso del worker.
    """

    name: str
    # Posicion en el orden real de ejecucion (0 = primer nodo). Es el ordinal del
    # recorrido, no el `step` de LangGraph (que cuenta tambien la entrada).
    index: int
    started_at: datetime | None = None
    # Claves del estado que escribio el nodo (canales de LangGraph, no valores):
    # suficiente para saber que aporto cada paso sin arrastrar el estado entero.
    writes: list[str] = Field(default_factory=list)


class ReviewTrace(BaseModel):
    """Recorrido de una review: nodos ejecutados, orden real y punto de parada.

    Es un **read model** derivado del checkpointer (iniciativa 019, OP1/OP3): no
    viaja por el estado del grafo, asi que no entra en la allowlist del serde.
    """

    review_key: str
    # Hilo del checkpointer. Diverge de `review_key` en la pasada degradada
    # (`:partial`), que comparte review_key con la completa pero no hilo.
    thread_id: str
    state: TraceState
    nodes: list[TraceNode] = Field(default_factory=list)
    # Nodo(s) pendientes: donde esta parada la review (`__start__` solo si aun no
    # corrio nada). Vacio cuando el hilo termino.
    next: list[str] = Field(default_factory=list)
    # `kind` de la pausa pendiente (`human`/`fix`), o `None` si no esta pausada.
    pause_kind: PauseKindName | None = None
    # Instante del ultimo checkpoint: la ultima actividad conocida del hilo.
    updated_at: datetime | None = None
