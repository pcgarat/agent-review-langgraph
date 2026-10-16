from __future__ import annotations

from datetime import datetime


class NullProgress:
    """`ProgressPort` que no hace nada: el sistema sin panel (019 W2).

    Es el default del worker. Con él la envoltura de nodo sigue en su sitio (el
    *seam* es único, D2) pero no toca Redis: el progreso es observabilidad y su
    ausencia no cambia el comportamiento de una review.
    """

    def start_node(self, review_key: str, node: str, *, at: datetime | None = None) -> None:
        del review_key, node, at

    def finish_node(self, review_key: str, node: str) -> None:
        del review_key, node

    def clear(self, review_key: str) -> None:
        del review_key
