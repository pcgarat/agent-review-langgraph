from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from reviews.domain.models import CommandResult


class VerifyRequest(BaseModel):
    """Peticion de verificacion al sandbox. El ciclo de vida del workspace vive
    entero en el runner: el worker solo pide "aplica este parche y verifica"."""

    # `review_key:finding_id:intento`: idempotencia del runner. Un POST repetido
    # con el mismo `run_key` devuelve el run existente (spec §5.3).
    run_key: str
    repo: str
    head_sha: str
    patch: str
    # Nombres de recipe del perfil, en orden; para al primer fallo.
    recipes: list[str] = Field(default_factory=list)
    # Rutas de test para la recipe dirigida (validadas por el runner).
    targets: list[str] = Field(default_factory=list)


class VerifyReport(BaseModel):
    run_key: str
    image_digest: str
    results: list[CommandResult] = Field(default_factory=list)
    passed: bool


@runtime_checkable
class SandboxPort(Protocol):
    """Verifica un parche en un entorno reproducible (imagen del CI por digest).

    No recibe una linea de shell arbitraria: recibe un parche y nombres de recipe
    del `repo_profile`. La validacion local y la seleccion de recipes son del
    nodo `fix`; el runner solo ejecuta lo declarado. Ver docs, apartado 9.
    """

    def verify(self, request: VerifyRequest) -> VerifyReport: ...
