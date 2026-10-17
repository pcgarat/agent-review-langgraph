from __future__ import annotations

from agent_review.domain.models import ReviewContext, ReviewerResult


class NullReviewer:
    """Persona nula: mantiene el grafo ejecutable hasta que haya tooling/LLM."""

    def __init__(self, name: str = "null") -> None:
        self.name = name

    def review(
        self,
        *,
        repo: str,
        pr_id: int,
        diff: str,
        head_sha: str = "",
        review_key: str = "",
        context: ReviewContext | None = None,
    ) -> ReviewerResult:
        del repo, pr_id, diff, head_sha, review_key, context
        return ReviewerResult()
