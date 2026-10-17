from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_review.domain.models import Finding, Verdict, VerificationResult

# Reexportados: el contrato del puerto y el tipo de estado/outcome son el mismo
# objeto de dominio (009 VF1/VF10). Los consumidores pueden importar de aqui o
# de `domain.models`.
__all__ = ["Verdict", "VerificationResult", "VerifierPort"]


@runtime_checkable
class VerifierPort(Protocol):
    """Segundo juicio independiente sobre una hipótesis del revisor (009 V1).

    Recibe **un** hallazgo y devuelve **un** veredicto: aislamiento por hallazgo,
    cita comprobable y unidad cacheable. El nodo `verify` lo inyecta; sin puerto
    el nodo es un paso directo (todo `unverified`).
    """

    name: str

    def verify(
        self,
        finding: Finding,
        *,
        repo: str,
        head_sha: str,
        diff: str = "",
        review_key: str = "",
    ) -> VerificationResult: ...
