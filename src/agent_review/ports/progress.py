from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class ProgressPort(Protocol):
    """Marca del nodo en curso de una review (019 W10), para cerrar D16.

    El latido (`HEARTBEAT_PATH`) mide que el worker **vive**, no que **avanza**: un
    nodo lento (LLM, verificación) no se distingue de un worker colgado. Esta marca
    es lo que el panel lee para pintar «qué corre ahora y desde cuándo».

    El contrato es **best-effort**: ninguna operación puede lanzar hacia el grafo.
    Un fallo de Redis al anotar el progreso no puede tumbar una review (019 T6,
    criterio 8). Por eso los adaptadores degradan solos y el puerto nulo no hace nada.

    `start_node` se llama en la **frontera** de cada nodo: el conteo de ramas cuenta
    las que **están en curso** (fan-out paralelo), no las ejecuciones acumuladas.
    """

    def start_node(self, review_key: str, node: str, *, at: datetime | None = None) -> None:
        """Anota que `node` empieza ahora en `review_key` (W10).

        Con el nodo actual ya puesto se cuenta una rama más en curso sin mover
        `started_at`: en un fan-out paralelo, el sello del nodo es el de la primera
        rama, no el de la última que arrancó.
        """
        ...

    def finish_node(self, review_key: str, node: str) -> None:
        """Anota que una rama de `node` terminó.

        Al ser el último en curso, limpia la marca. Un error del nodo también
        llama aquí (lo garantiza el `finally` de la envoltura): el progreso no
        puede quedarse pegado al último nodo que falló.
        """
        ...

    def clear(self, review_key: str) -> None:
        """Borra la marca de la review (hilo terminado o pausado, OP8)."""
        ...


def start_node(port: ProgressPort | None, review_key: str, node: str) -> None:
    """Anota el arranque si hay puerto y clave; nunca propaga un fallo.

    Encapsula la regla «el progreso no rompe la review» (T6) en un solo sitio, para
    que ningún consumidor tenga que recordar el `try`. El puerto ya degrada solo
    (`RedisProgress` captura el fallo de Redis); el `suppress` cubre a un adaptador
    futuro que sí lance.
    """
    if port is None or not review_key:
        return
    with contextlib.suppress(Exception):
        port.start_node(review_key, node)


def finish_node(port: ProgressPort | None, review_key: str, node: str) -> None:
    """Anota el fin de una rama; best-effort como `start_node`."""
    if port is None or not review_key:
        return
    with contextlib.suppress(Exception):
        port.finish_node(review_key, node)


def clear(port: ProgressPort | None, review_key: str) -> None:
    """Borra la marca del hilo; best-effort como `start_node`."""
    if port is None or not review_key:
        return
    with contextlib.suppress(Exception):
        port.clear(review_key)
