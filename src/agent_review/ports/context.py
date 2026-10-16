from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from agent_review.globs import matches_any

# Lado del diff al que se lee un fichero. `head` es el commit revisado; `base`, el
# destino del PR (para comparar lo que ya estaba antes del cambio).
Ref = Literal["head", "base"]

# Rutas que un lector de contexto nunca sirve, ni por `read_file` ni en un `grep`.
# Es el *default*: el runner lo aplica sobre su `context_denylist` de perfil, de
# modo que el modelo no puede saltarselo pidiendo otra ruta (W7 de 016).
DEFAULT_CONTEXT_DENYLIST: tuple[str, ...] = (
    ".env*",
    "*.pem",
    "*.key",
    "**/secrets/**",
)


def is_denied(path: str, patterns: Sequence[str]) -> bool:
    """¿La ruta esta vetada? Casa por ruta completa o por nombre de fichero.

    Se comprueba tambien el basename para que un `config/.env` lo cace el patron
    `.env*`, que por si solo solo casaria el fichero en la raiz del arbol. Un patron
    `**/x/**` casa tambien `x/...` en la raiz: `**/` admite cero directorios.
    """
    normalized = _normalize(path)
    name = normalized.rsplit("/", 1)[-1]
    expanded = list(patterns) + [p[3:] for p in patterns if p.startswith("**/")]
    return matches_any(normalized, expanded) or matches_any(name, expanded)


def _normalize(path: str) -> str:
    """Quita los prefijos `./` y `/` de una ruta, nunca el punto de `.env`.

    `str.lstrip("./")` quitaria cada `.` y `/` inicial, de modo que `.env` pasaria
    a `env` y escaparia del veto `.env*`.
    """
    normalized = path
    while normalized.startswith(("./", "/")):
        normalized = normalized[2:] if normalized.startswith("./") else normalized[1:]
    return normalized


class ContextLimits(BaseModel):
    """Topes de un bucle de herramientas, por lote (**016** W2 / **014** H5).

    Un turno reenvía toda la conversación, así que el coste crece más rápido que el
    número de turnos: los topes son por lote y el bucle cierra con un turno final
    forzado al agotar cualquiera de ellos.
    """

    max_turns: int = Field(default=4, ge=1)
    max_calls: int = Field(default=8, ge=0)
    max_bytes: int = Field(default=60_000, ge=1)


@runtime_checkable
class ContextReaderPort(Protocol):
    """Lectura acotada del repositorio para un modelo que razona (016 W1).

    Un solo puerto con tres adaptadores (fixture, VCS y workspace del runner): el
    bucle de herramientas no sabe de dónde salen los bytes. Todas las operaciones
    son de **solo lectura** y ninguna ejecuta código del PR.
    """

    def read_file(
        self,
        path: str,
        *,
        ref: Ref = "head",
        start: int | None = None,
        end: int | None = None,
    ) -> str | None:
        """Contenido del fichero (entero, o el rango `start..end`, 1-based inclusivo).

        `None` si el fichero no existe, la ruta está vetada o no se puede leer.
        """
        ...

    def grep(self, pattern: str, *, glob: str | None = None) -> list[str]:
        """Coincidencias **literales** como `path:line: texto`.

        Literal a propósito (Q1 de 016): es más barato y predecible que una expresión
        regular y cierra el riesgo de un patrón caro. `glob` acota los ficheros.
        """
        ...

    def list_dir(self, path: str) -> list[str]:
        """Nombres del directorio `path` (ficheros y subdirectorios), ordenados."""
        ...
