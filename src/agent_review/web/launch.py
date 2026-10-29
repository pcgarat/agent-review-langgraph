"""Lanzar una review desde el panel pegando la URL del pull request.

El panel no resuelve el commit: no tiene credencial de VCS (ADR-0020). Encola un
`ReviewRequest` con `launch=True` y `pipeline_id` fijo (`panel`). El worker lee
el head en el VCS antes de invocar el grafo, y ese commit es la identidad de la
ejecucion. Un segundo clic sobre el mismo commit reutiliza el hilo y no publica
otra vez. El pipeline del CI, si corre, sigue siendo otra ejecucion (ADR-0005).
"""

from __future__ import annotations

import logging

from agent_review.domain.models import ReviewRequest
from agent_review.pr_url import PullRequestUrlError, parse_pull_request_url
from agent_review.queue import ReviewQueue

logger = logging.getLogger(__name__)

# Una ejecucion por commit, distinta de cualquier pipeline del CI.
PANEL_PIPELINE_ID = "panel"


class LaunchError(Exception):
    """La URL no se puede lanzar; el endpoint responde `400`."""


class LaunchService:
    """Encola el lanzamiento. El SHA lo pone el worker."""

    def __init__(self, queue: ReviewQueue, *, expected_repo: str) -> None:
        self._queue = queue
        self._expected = expected_repo.strip()

    def launch(self, url: str, *, actor: str) -> ReviewRequest:
        try:
            parsed = parse_pull_request_url(url)
        except PullRequestUrlError as exc:
            raise LaunchError(str(exc)) from exc
        repo = parsed.repo
        if self._expected:
            if repo.casefold() != self._expected.casefold():
                raise LaunchError(
                    f"Repo {repo!r} no admitido: este despliegue solo revisa {self._expected}"
                )
            repo = self._expected
        request = ReviewRequest(
            repo=repo,
            pr_id=parsed.pr_id,
            head_sha="",
            pipeline_id=PANEL_PIPELINE_ID,
            launch=True,
        )
        self._queue.enqueue(request)
        logger.info("Review lanzada por %s: %s#%s", actor, repo, parsed.pr_id)
        return request
