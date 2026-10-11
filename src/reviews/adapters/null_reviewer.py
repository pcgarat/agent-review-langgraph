from __future__ import annotations

from reviews.domain.models import ReviewerResult


class NullReviewer:
    """Persona nula: mantiene el grafo ejecutable hasta que haya tooling/LLM."""

    def __init__(self, name: str = "null") -> None:
        self.name = name

    def review(self, *, repo: str, pr_id: int, diff: str, head_sha: str = "") -> ReviewerResult:
        del repo, pr_id, diff, head_sha
        return ReviewerResult()
