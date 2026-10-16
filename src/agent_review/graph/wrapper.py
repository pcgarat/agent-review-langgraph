"""Envoltura de nodo: el único *seam* para observar los nodos del grafo (019 D2).

Nace para el progreso del worker (019 W10) y es el mismo punto donde la
[018](../../../docs/iniciativas/018-trazas-de-la-review/spec.md) instrumentará sus
trazas (T3): **un solo** envoltorio sobre el grafo, no dos. Se aplica al registrar
cada nodo en `build_graph`, no editando las funciones de los nodos.

La envoltura es deliberadamente transparente: `functools.wraps` copia la firma del
nodo (vía `__wrapped__`), que es lo que LangGraph lee para inyectar el `Runtime` de
`persona` y lo que hace que la `RetryPolicy` siga aplicándose al nodo, no al
envoltorio (riesgo R1 del plan). Sin puerto de progreso (`progress=None`), la
envoltura solo deja pasar los argumentos: el sistema se comporta como si no
existiera (W2), y `tests/test_progress.py` lo fija.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping
from typing import Any

from agent_review.ports.progress import ProgressPort, finish_node, start_node


def node_wrapper(name: str, fn: Any, *, progress: ProgressPort | None = None) -> Any:
    """Envuelve el nodo `fn` para anotar su frontera, sin alterar su firma.

    `start_node` corre **antes** de `fn` y `finish_node` en un `finally`: el nodo
    que falla también cierra su marca, para que el progreso no se quede pegado al
    último nodo que reventó. La frontera es el único lugar seguro para observar un
    nodo: un `interrupt` (pausa) abandona la ejecución lanzando una excepción, y el
    `finally` la deja pasar sin tragársela.

    El `review_key` se lee del estado de la rama: todas llevan su `request` (el
    `Send` del fan-out lo incluye), así que está siempre disponible.
    """

    @functools.wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        review_key = _review_key_of(args[0]) if args else ""
        start_node(progress, review_key, name)
        try:
            return fn(*args, **kwargs)
        finally:
            finish_node(progress, review_key, name)

    return wrapped


def _review_key_of(state: Any) -> str:
    """`review_key` del estado de la rama, o cadena vacía si no se puede leer.

    Sin clave, los helpers de progreso hacen no-op: una marca imposible se salta
    en vez de tumbar el nodo.
    """
    if not isinstance(state, Mapping):
        return ""
    request = state.get("request")
    if request is None:
        return ""
    if isinstance(request, Mapping):
        return str(request.get("review_key") or "")
    return str(getattr(request, "review_key", "") or "")
