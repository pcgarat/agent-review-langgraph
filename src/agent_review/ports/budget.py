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
        """Reserva `tokens` para la review. `False` si el saldo no da (B7).

        El resultado es **vinculante**: quien llama no debe gastar si devuelve
        `False`, aunque `remaining` hubiera dado positivo justo antes. Es el único
        punto que cierra la carrera entre ramas en paralelo.
        """
        ...

    def commit(self, review_key: str, reserved: int, actual: int) -> None:
        """Ajusta una reserva previa al gasto **real** del proveedor.

        `actual=0` **devuelve** la reserva entera (B8): es el camino del error, para
        que el reintento del nodo no reserve dos veces. Confirmar más de lo reservado
        confirma el gasto real; devolver más de lo pendiente no altera el saldo.
        """
        ...

    def remaining(self, review_key: str) -> int:
        """Saldo como `total − confirmado − reservado sin confirmar` (B9).

        Descuenta lo reservado y aún no confirmado: si solo contara lo confirmado,
        dos ramas verían el mismo saldo y ambas pasarían la comprobación, que es
        justo la carrera que `reserve` cierra.
        """
        ...


def reserve_tokens(port: BudgetPort | None, review_key: str, tokens: int) -> bool:
    """Reserva `tokens` si hay puerto y clave; si no, deja pasar (016 W1).

    Encapsula la regla B7: `False` es vinculante. Que la decisión viva aquí y no en
    cada consumidor evita que uno de ellos olvide mirar el resultado (deuda D21).
    """
    if port is None or not review_key or tokens <= 0:
        return True
    return port.reserve(review_key, tokens)


def release_tokens(port: BudgetPort | None, review_key: str, reserved: int) -> None:
    """Devuelve una reserva que no llegó a gastarse (B8).

    Es el camino del error: sin él, un fallo transitorio reintentado por el nodo
    ([ADR-0007](../../../docs/adr/0007-reintento-en-dos-capas.md)) reserva dos veces.
    """
    if port is None or not review_key or reserved <= 0:
        return
    port.commit(review_key, reserved, 0)
