from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class PullRequestInfo(BaseModel):
    repo: str
    pr_id: int
    title: str
    description: str = ""
    source_branch: str
    destination_branch: str
    author: str
    head_sha: str
    state: str = "OPEN"
    url: str = ""


class InlineAnchor(BaseModel):
    """Ancla segura para un comentario inline en el diff actual."""

    path: str
    line: int = Field(ge=1)
    from_line: int | None = Field(default=None, ge=1)


class ReviewMarker(BaseModel):
    """Marca que el agente ya publico el resultado de esta review.

    `body` incluye el token de dedupe, asi que el `publish` puede reconocer un
    resultado propio aunque no guarde los ids de los comentarios.
    """

    id: str
    body: str = ""


@runtime_checkable
class VcsPort(Protocol):
    """Adaptador de VCS. Bitbucket via twg hoy; GitHub manana, sin tocar el grafo."""

    def get_pull_request(self, repo: str, pr_id: int) -> PullRequestInfo: ...

    def get_diff(self, repo: str, pr_id: int) -> str:
        """Diff unificado del PR, acotado. Es la entrada de las personas."""
        ...

    def get_diffstat(self, repo: str, pr_id: int) -> list[tuple[str, int, int]]:
        """(fichero, lineas anadidas, lineas borradas) por fichero."""
        ...

    def list_review_markers(self, repo: str, pr_id: int) -> list[ReviewMarker]:
        """Comentarios del agente ya publicados en el PR.

        Se usan para no re-publicar el resultado de una review ya hecha (un
        `pipeline_id` nuevo sobre el mismo commit, o un reintento del worker).
        """
        ...

    def resolve_inline_anchor(
        self, repo: str, pr_id: int, *, path: str, text: str
    ) -> InlineAnchor | None:
        """Resuelve path+texto a una linea presente en el diff (lado nuevo)."""
        ...

    def post_inline_comment(
        self,
        repo: str,
        pr_id: int,
        *,
        path: str,
        line: int,
        body: str,
        from_line: int | None = None,
    ) -> None:
        """Comentario anclado a una linea del diff. Publico (nunca pending)."""
        ...

    def post_comment(self, repo: str, pr_id: int, body: str) -> None:
        """Comentario general del PR (resumen / indice)."""
        ...

    def post_task(self, repo: str, pr_id: int, body: str) -> None:
        """Tarea accionable ligada al PR, para hallazgos que requieren accion."""
        ...
