"""Dependencias compartidas por los routers del panel.

Viven aquí y no en `web.main` porque `main` incluye los routers: si un router
importara `main` para pedir su read model, el import sería circular. El contrato
de inyección es el mismo en todos los casos: un fake en `app.state` para los tests
y, si no hay, la construcción perezosa a partir de la configuración.

Una sola definición por dependencia: tres copias de `get_read_model` son tres
sitios donde cambiar qué se inyecta.
"""

from __future__ import annotations

from fastapi import Request

from agent_review.domain.history import ReviewSummary
from agent_review.history_store import ReviewHistory
from agent_review.ports.reasoning import ReasoningReader
from agent_review.settings import Settings, get_settings
from agent_review.web.read_model import PanelReadModel, build_panel_read_model
from agent_review.web.runner_client import RunnerConsoleClient, build_runner_client


def get_read_model(request: Request) -> PanelReadModel:
    """Read model del proceso. Los tests inyectan un fake en `app.state.read_model`."""
    model: PanelReadModel | None = getattr(request.app.state, "read_model", None)
    if model is None:
        model = build_panel_read_model(get_settings())
        request.app.state.read_model = model
    return model


def get_console(request: Request) -> RunnerConsoleClient:
    """Cliente de la consola del runner (OP24). Construirlo no abre ningún stream."""
    console: RunnerConsoleClient | None = getattr(request.app.state, "console", None)
    if console is None:
        console = build_runner_client(get_settings())
        request.app.state.console = console
    return console


def build_reasoning_reader(settings: Settings) -> ReasoningReader | None:
    """Lector del razonamiento (021 R5), o `None` sin Redis."""
    if not settings.redis_url.strip():
        return None
    from agent_review.adapters.reasoning_redis import RedisReasoningReader

    return RedisReasoningReader(url=settings.redis_url)


def get_reasoning(request: Request) -> ReasoningReader | None:
    """Lector del razonamiento del proceso. Los tests inyectan uno en memoria."""
    if not hasattr(request.app.state, "reasoning"):
        request.app.state.reasoning = build_reasoning_reader(get_settings())
    reader: ReasoningReader | None = request.app.state.reasoning
    return reader


# --- Histórico de reviews (021 R9) ------------------------------------------------


def build_history_reader(settings: object) -> ReviewHistory | None:
    """Histórico de reviews para el panel, o `None` sin Redis."""
    url = str(getattr(settings, "redis_url", "") or "").strip()
    if not url:
        return None
    from agent_review.history_store import RedisReviewHistory

    return RedisReviewHistory(
        url=url, retention_days=float(getattr(settings, "checkpoint_retention_days", 30.0))
    )


def get_history(request: Request) -> ReviewHistory | None:
    """Histórico del proceso, con el *seam* en `app.state.history` (tests)."""
    if not hasattr(request.app.state, "history"):
        request.app.state.history = build_history_reader(get_settings())
    history: ReviewHistory | None = request.app.state.history
    return history


def recent_reviews(
    history: ReviewHistory | None, *, repo: str | None, pr_id: int | None, limit: int
) -> list[ReviewSummary]:
    if history is None:
        return []
    return history.recent(limit=limit, repo=repo or None, pr_id=pr_id)
