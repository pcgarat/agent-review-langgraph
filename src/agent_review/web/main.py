"""Servicio `web` del panel de operacion (iniciativa 019, OP11/OP12/OP13/OP14).

Proceso aparte, opt-in por el perfil de compose `panel` (W1): lee el estado de Redis
y el recorrido del checkpointer y encola comandos, pero **nunca** escribe el estado
del grafo, que sigue teniendo un unico escritor (el worker, W3).

Arranque: `python -m agent_review.web.main` (uvicorn en `PANEL_PORT`).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status

from agent_review.settings import Settings, get_settings, require_panel_secrets
from agent_review.web.auth import Session, require_session
from agent_review.web.read_model import PanelReadModel, build_panel_read_model

logger = logging.getLogger(__name__)


def _settings() -> Settings:
    return get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Contrato propio del panel: no recibe credencial de VCS ni clave del LLM (solo
    # lee estado y encola comandos), asi que no se le exige el contrato del worker.
    settings = _settings()
    require_panel_secrets(settings)
    app.state.read_model = build_panel_read_model(settings)
    try:
        yield
    finally:
        close = getattr(app.state.read_model, "close", None)
        if callable(close):
            close()


app = FastAPI(title="Panel de operacion", version="0.1.0", lifespan=lifespan)


def get_read_model(request: Request) -> PanelReadModel:
    """Read model del proceso. Los tests inyectan un fake en `app.state.read_model`."""
    model: PanelReadModel | None = getattr(request.app.state, "read_model", None)
    if model is None:
        model = build_panel_read_model(_settings())
        request.app.state.read_model = model
    return model


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/state")
def get_state(
    session: Annotated[Session, Depends(require_session)],
    model: Annotated[PanelReadModel, Depends(get_read_model)],
) -> dict[str, object]:
    """Estado operativo (OP13): cola, en vuelo, pausas por `kind`, descartes y presupuesto."""
    del session  # la sesion autoriza; el estado es el mismo para cualquier rol
    return model.state().model_dump(mode="json")


@app.get("/api/trace/{review_key:path}")
def get_trace(
    review_key: str,
    session: Annotated[Session, Depends(require_session)],
    model: Annotated[PanelReadModel, Depends(get_read_model)],
) -> dict[str, object]:
    """Recorrido de una review desde el checkpointer (OP14); `404` si no existe.

    El `:path` es necesario porque la `review_key` lleva `/` y `:`
    (`cqm/emotivoo-v2:42:<sha>:<pipeline>`); sin el, el routing cortaria en el
    primer `/`.
    """
    del session
    trace = model.trace(review_key)
    if trace is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Sin recorrido para {review_key}")
    return trace.model_dump(mode="json")


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())
    uvicorn.run(app, host="0.0.0.0", port=settings.panel_port)


if __name__ == "__main__":  # pragma: no cover - entrada del proceso
    main()
