from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel


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

    def post_comment(self, repo: str, pr_id: int, body: str) -> None:
        """Comentario general del PR. En Fase 1 es el canal de publicacion."""
        ...

    def post_task(self, repo: str, pr_id: int, body: str) -> None:
        """Tarea accionable ligada al PR, para hallazgos que requieren accion."""
        ...
