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
