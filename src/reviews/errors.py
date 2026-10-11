from __future__ import annotations

import subprocess

import httpx


class TransientError(RuntimeError):
    """Fallo recuperable: reintentarlo puede resolverlo (red, 5xx, 429, timeout).

    El *tipo* es la clasificacion: `is_transient` la usa como `retry_on` de la
    politica de reintento de nodo, asi que heredar de aqui es lo unico que hay que
    hacer para que el grafo reintente un adaptador nuevo.

    Ojo con el default de LangGraph: `langgraph.types.default_retry_on` **no**
    reintenta `RuntimeError` —de la que estos tipos heredan por compatibilidad con
    los adaptadores que ya existian— ni `TimeoutError`, ni un `429`. Por eso el
    grafo usa `is_transient` y nunca `default_retry_on`.
    """


class PermanentError(RuntimeError):
    """Fallo no recuperable: reintentarlo lo repite (bug, 4xx, payload invalido)."""


def is_transient(exc: BaseException) -> bool:
    """¿Merece un reintento? Politica unica de reintento de nodo para el grafo.

    Se clasifica lo que *puede* cambiar entre intentos: los fallos propios
    marcados como recuperables y los de red/transporte (conexion, timeout, 5xx,
    429). Todo lo demas se trata como bug y no se reintenta: repetir un
    `ValueError` o un `TypeError` solo gasta tiempo y llamadas externas. Es una
    decision deliberadamente distinta del default de LangGraph, que reintenta lo
    desconocido y no reintenta `RuntimeError`.
    """
    if isinstance(exc, TransientError):
        return True
    if isinstance(exc, PermanentError):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        # 5xx y 429 son "vuelve mas tarde"; un 4xx no cambia por reintentar.
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    if isinstance(exc, httpx.RequestError):
        # Fallo de transporte: conexion, DNS, timeout de lectura/escritura.
        return True
    if isinstance(exc, subprocess.TimeoutExpired):
        return True
    return isinstance(exc, (TimeoutError, ConnectionError))
