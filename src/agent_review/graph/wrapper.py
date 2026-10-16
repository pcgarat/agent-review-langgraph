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

La misma frontera sirve para la **cancelación cooperativa** (019 W4): si la review
tiene marca de cancelación, la envoltura aborta **antes** de entrar al nodo. Es el
único punto seguro: LangGraph no cancela un `invoke` a mitad, así que la cancelación
se comprueba entre nodos y nunca dentro de uno (R5).
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Mapping
from typing import Any

from agent_review.errors import Cancelled
from agent_review.ports.progress import ProgressPort, finish_node, start_node

logger = logging.getLogger(__name__)

# Chequeo de cancelación: dado el `review_key`, `True` si hay marca puesta (019 W4).
# Es un callable para que la capa del grafo no dependa de Redis: quien lo construye
# es el worker (adaptador `RedisCancellation`).
CancelCheck = Callable[[str], bool]

# Chequeo instalado por el worker al arrancar (`set_cancel_check`). `build_graph`
# construye los nodos sin un chequeo explícito —no es su capa—, así que el worker lo
# inyecta aquí una sola vez: la resolución ocurre en cada llamada, de modo que el
# grafo ya construido lo ve. Sin instalar, la cancelación no se consulta y todo se
# comporta como antes (W2).
_cancel_check: CancelCheck | None = None


def set_cancel_check(check: CancelCheck | None) -> CancelCheck | None:
    """Instala el chequeo de cancelación por defecto y devuelve el anterior.

    Punto de inyección del *composition root* (`build_runtime`/`run_forever`), que
    es quien conoce Redis. Devuelve el anterior para que un test pueda restaurarlo.
    """
    global _cancel_check
    previous = _cancel_check
    _cancel_check = check
    return previous


def node_wrapper(
    name: str,
    fn: Any,
    *,
    progress: ProgressPort | None = None,
    cancel: CancelCheck | None = None,
) -> Any:
    """Envuelve el nodo `fn` para anotar su frontera, sin alterar su firma.

    `start_node` corre **antes** de `fn` y `finish_node` en un `finally`: el nodo
    que falla también cierra su marca, para que el progreso no se quede pegado al
    último nodo que reventó. La frontera es el único lugar seguro para observar un
    nodo: un `interrupt` (pausa) abandona la ejecución lanzando una excepción, y el
    `finally` la deja pasar sin tragársela.

    `cancel` comprueba la marca de cancelación **antes** de empezar el nodo (W4);
    sin él se usa el chequeo instalado por el worker, y sin ninguno el sistema se
    comporta igual que hoy.

    El `review_key` se lee del estado de la rama: todas llevan su `request` (el
    `Send` del fan-out lo incluye), así que está siempre disponible.
    """

    @functools.wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        review_key = _review_key_of(args[0]) if args else ""
        _guard_cancel(cancel, review_key)
        start_node(progress, review_key, name)
        try:
            return fn(*args, **kwargs)
        finally:
            finish_node(progress, review_key, name)

    return wrapped


def _guard_cancel(cancel: CancelCheck | None, review_key: str) -> None:
    """Aborta la frontera del nodo si la review tiene marca de cancelación (W4).

    Se comprueba **antes** de tocar el nodo, así que el `Cancelled` cae entre dos
    nodos y nunca dentro de uno (R5). El chequeo es best-effort: si lanza, se trata
    como «sin cancelación», para que un fallo del panel no tumbe una review.
    """
    if not review_key:
        return
    check = cancel if cancel is not None else _cancel_check
    if check is None:
        return
    try:
        cancelled = bool(check(review_key))
    except Exception:
        logger.debug("cancelación: fallo al consultar %s; se sigue", review_key, exc_info=True)
        return
    if cancelled:
        raise Cancelled(f"review {review_key} cancelada")


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
