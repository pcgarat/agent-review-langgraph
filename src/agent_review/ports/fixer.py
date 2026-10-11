from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_review.domain.models import Finding, FixResult, ProposedPatch


@runtime_checkable
class FixerPort(Protocol):
    """Genera un parche para un hallazgo aprobado (Strategy, E2).

    El fixer **solo propone**: no ejecuta nada ni escribe el repo. La validacion
    local y la verificacion en el sandbox son del nodo `fix`. En el segundo
    intento recibe el `previous` con los `stderr_tail` reales para corregir.
    """

    def propose(
        self, finding: Finding, *, source: str, previous: FixResult | None
    ) -> ProposedPatch: ...
