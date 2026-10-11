from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_review.domain.models import CommandResult, ProposedPatch


@runtime_checkable
class DeliveryFormatter(Protocol):
    """Formatea el parche verificado para el comentario inline (E6, Strategy).

    Aisla la entrega del contrato del nodo `fix`: si Bitbucket no renderiza el
    bloque `suggestion`, se cambia el adaptador sin tocar la orquestacion.
    """

    def render(
        self, patch: ProposedPatch, *, checks: list[CommandResult], image_digest: str
    ) -> str: ...


class SuggestionFormatter:
    """Bloque `suggestion`, aplicable con un clic donde el proveedor lo soporta."""

    def render(
        self, patch: ProposedPatch, *, checks: list[CommandResult], image_digest: str
    ) -> str:
        fence = f"```suggestion\n{patch.replacement.rstrip()}\n```"
        return f"{fence}\n\n{_verified_line(checks, image_digest)}"


class DiffFormatter:
    """Fallback: bloque `diff` que se copia a mano.

    Verificado que `twg bb prs comment create` acepta `--text` libre pero no tiene
    ningun concepto de `suggestion` (spike R1). Mientras no se confirme el
    renderizado, este es el formato seguro.
    """

    def render(
        self, patch: ProposedPatch, *, checks: list[CommandResult], image_digest: str
    ) -> str:
        fence = f"```diff\n{patch.diff.rstrip()}\n```"
        return f"{fence}\n\n{_verified_line(checks, image_digest)}"


def _verified_line(checks: list[CommandResult], image_digest: str) -> str:
    names = ", ".join(check.recipe for check in checks if check.ok) or "sin checks"
    digest = image_digest[:19] if image_digest else "sin digest"
    return f"_Verificado: {names} · imagen `{digest}`_"
