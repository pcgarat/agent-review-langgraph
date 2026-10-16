from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock


@dataclass
class _Counter:
    committed: int = 0
    pending: int = 0


@dataclass
class _State:
    total: int
    counters: dict[str, _Counter] = field(default_factory=dict)


class InMemoryBudget:
    """`BudgetPort` en memoria: tests, *replay* y despliegue sin Redis (017 P1/B9).

    Reproduce la semántica del adaptador de Redis para que el *replay* sea fiel: el
    contador es **por review** (B1), `remaining` descuenta también lo reservado y aún
    no confirmado (B9), y una reserva que no cabe devuelve `False` sin tocar el
    contador (B7). Un `Lock` reproduce el reparto atómico entre las ramas del
    fan-out, que en Redis es una operación atómica.
    """

    def __init__(self, total: int) -> None:
        self._state = _State(total=max(0, total))
        self._lock = Lock()

    def _counter(self, review_key: str) -> _Counter:
        return self._state.counters.setdefault(review_key, _Counter())

    def reserve(self, review_key: str, tokens: int) -> bool:
        """Reserva `tokens` si caben. No los confirma: quedan pendientes."""
        wanted = max(0, tokens)
        with self._lock:
            counter = self._counter(review_key)
            if counter.committed + counter.pending + wanted > self._state.total:
                return False
            counter.pending += wanted
            return True

    def commit(self, review_key: str, reserved: int, actual: int) -> None:
        """Confirma el gasto real y devuelve lo reservado que no se gastó (B8).

        Una devolución mayor que lo pendiente no deja el contador en negativo: un
        doble `commit` del mismo tramo es un error del consumidor que no debe alterar
        el presupuesto de las demás ramas.
        """
        with self._lock:
            counter = self._counter(review_key)
            held = min(max(0, reserved), counter.pending)
            counter.pending -= held
            counter.committed += max(0, actual)

    def remaining(self, review_key: str) -> int:
        """Saldo disponible: `total − confirmado − reservado sin confirmar` (B9)."""
        with self._lock:
            counter = self._counter(review_key)
            return max(0, self._state.total - counter.committed - counter.pending)

    def spent(self, review_key: str) -> int:
        """Tokens confirmados de una review. Solo para introspección de tests."""
        with self._lock:
            return self._counter(review_key).committed

    def reserved(self, review_key: str) -> int:
        """Tokens reservados y aún no confirmados. Solo para introspección."""
        with self._lock:
            return self._counter(review_key).pending
