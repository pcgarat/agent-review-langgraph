"""Puerto del CLI de Claude (025 R1).

El worker pide stdout y valida el JSON. Este puerto no guarda el artefacto ni
conoce el grafo. El tiempo agotado, el cupo y un proceso caído son errores
distintos: el caller decide si reintenta, y un JSON que todavía no ha validado
vuelve como texto, no como «cero hallazgos».
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_review.errors import PermanentError, TransientError


class ClaudeTimeout(TransientError):
    """El cliente HTTP no obtuvo respuesta. No es el techo del subproceso."""


class ClaudeCeiling(PermanentError):
    """Se agotaron los turnos o el tiempo de pared. No se reintenta."""


class ClaudeProcessCrashed(TransientError):
    """El proceso no entregó stdout (caída, binario ausente, runner inalcanzable)."""


class ClaudeQuotaExceeded(PermanentError):
    """Cupo de la suscripción. No se reintenta: otra pasada volvería a gastarlo."""


class ClaudeInvalidOutput(TransientError):
    """El stdout no es una lista de hallazgos. Lo decide el caller, no el CLI."""


@runtime_checkable
class ClaudeRunner(Protocol):
    """Ejecuta Claude una vez y devuelve stdout sin validar.

    `head_sha` identifica la review. Quien llama no vuelve a invocar si ya
    guardó un artefacto válido para esa clave.
    """

    def run(self, *, repo: str, pr_id: int, head_sha: str, base_sha: str) -> str: ...
