from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class BudgetPort(Protocol):
    """Presupuesto de tokens **por review**, compartido por todo lo que llama a un
    modelo en esa review (017 B1/B6).

    El contador es externo (Redis en el despliegue) porque las ramas del fan-out y
    el `fix-worker` no comparten memoria. Antes de cada llamada se **reserva** la
    estimación; después se **ajusta** con el `usage` real (B3): sin reserva, varias
    ramas en paralelo pasarían la comprobación a la vez y lo superarían entre todas.
    """

    def reserve(self, review_key: str, tokens: int) -> bool:
        """Reserva `tokens` para la review. `False` si ya no queda presupuesto."""
        ...

    def commit(self, review_key: str, reserved: int, actual: int) -> None:
        """Ajusta una reserva previa al gasto **real** del proveedor."""
        ...

    def remaining(self, review_key: str) -> int:
        """Tokens disponibles para la review, como cota superior."""
        ...
