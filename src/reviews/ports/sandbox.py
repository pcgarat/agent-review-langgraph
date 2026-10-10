from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class CommandResult(BaseModel):
    recipe: str
    argv: list[str]
    exit_code: int
    duration_s: float
    stdout_tail: str = ""
    stderr_tail: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@runtime_checkable
class SandboxPort(Protocol):
    """Ejecuta recipes declaradas dentro de un entorno reproducible.

    Nunca recibe una linea de shell arbitraria: recibe un nombre de recipe del
    repo_profile y sus parametros. Ver docs, apartado 9.4.
    """

    def run(self, recipe: str, *args: str) -> CommandResult: ...
