"""Servicio `web` del panel de operacion (iniciativa 019, OP11/OP12/OP13/OP14).

Proceso aparte, opt-in por el perfil de compose `panel` (W1): lee el estado de Redis
y el recorrido del checkpointer y encola comandos, pero **nunca** escribe el estado
del grafo, que sigue teniendo un unico escritor (el worker, W3).

Arranque: `python -m agent_review.web.main` (uvicorn en `PANEL_PORT`).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from agent_review.paused_store import RedisPausedStore
from agent_review.queue import RedisReviewQueue
from agent_review.settings import Settings, get_settings, require_panel_secrets
from agent_review.web.auth import Session, require_operador, require_session
from agent_review.web.commands import (
    CommandEffect,
    CommandError,
    CommandService,
    build_command_service,
)
from agent_review.web.read_model import PanelReadModel, build_panel_read_model
from agent_review.web.runner_client import RunnerConsoleClient, build_runner_client
from agent_review.web.ui import router as ui_router
from agent_review.worker.cancel import build_cancellation

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
    cancellation = build_cancellation(settings)
    if cancellation is not None:
        app.state.commands = build_command_service(
            queue=RedisReviewQueue(url=settings.redis_url),
            paused=RedisPausedStore(url=settings.redis_url),
            cancellation=cancellation,
        )
    app.state.console = build_runner_client(settings)
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


def get_commands(request: Request) -> CommandService:
    """Servicio de comandos del proceso (OP29-OP32). Los tests inyectan uno propio."""
    commands: CommandService | None = getattr(request.app.state, "commands", None)
    if commands is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Los comandos necesitan Redis, que no esta disponible",
        )
    return commands


def get_console(request: Request) -> RunnerConsoleClient:
    """Cliente de la consola del runner (OP24)."""
    console: RunnerConsoleClient | None = getattr(request.app.state, "console", None)
    if console is None:
        console = build_runner_client(_settings())
        request.app.state.console = console
    return console


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


def _actor_of(session: Session, body: dict[str, object]) -> str:
    """Actor auditado del comando: el que manda el cliente, o el de la sesion."""
    raw = str(body.get("actor", "")).strip()
    return raw or session.user


@app.post("/api/reviews/{review_key:path}/cancel")
async def cancel_review(
    review_key: str,
    request: Request,
    session: Annotated[Session, Depends(require_operador)],
    commands: Annotated[CommandService, Depends(get_commands)],
) -> dict[str, object]:
    """Cancela una review (OP29/OP30): marca para lo que corre, comando para la pausa."""
    return _run_command(commands.cancel, review_key, await _body(request), session)


@app.post("/api/reviews/{review_key:path}/retry")
async def retry_review(
    review_key: str,
    request: Request,
    session: Annotated[Session, Depends(require_operador)],
    commands: Annotated[CommandService, Depends(get_commands)],
) -> dict[str, object]:
    """Reencola un descarte de la DLQ con `attempts=0` (OP31)."""
    return _run_command(commands.retry, review_key, await _body(request), session)


@app.post("/api/reviews/{review_key:path}/resume")
async def resume_review(
    review_key: str,
    request: Request,
    session: Annotated[Session, Depends(require_operador)],
    commands: Annotated[CommandService, Depends(get_commands)],
) -> dict[str, object]:
    """Reencola la reanudacion de una pausa humana (OP32)."""
    return _run_command(commands.resume, review_key, await _body(request), session)


def _run_command(
    operation: object,
    review_key: str,
    body: dict[str, object],
    session: Session,
) -> dict[str, object]:
    actor = _actor_of(session, body)
    try:
        effect = operation(review_key, actor=actor)  # type: ignore[operator]
    except CommandError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except Exception as exc:
        # Un fallo de Redis al marcar no puede devolver `200`: la cancelacion no ocurrio.
        logger.exception("Comando fallido sobre %s", review_key)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return {"review_key": review_key, "effect": CommandEffect(effect).value, "actor": actor}


async def _body(request: Request) -> dict[str, object]:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


@app.get("/api/reviews/{review_key:path}/console")
def review_console(
    review_key: str,
    session: Annotated[Session, Depends(require_session)],
    model: Annotated[PanelReadModel, Depends(get_read_model)],
    console: Annotated[RunnerConsoleClient, Depends(get_console)],
) -> StreamingResponse:
    """Consola en vivo de la verificacion de una review (OP24).

    Resuelve el `run_key` de la wave pausada y reenvia el SSE del runner. Si la
    review no tiene una verificacion en curso, o el runner ya no tiene su buffer
    (efimero), `404`: la consola es best-effort y nunca altera el informe (W8).
    """
    del session
    if not console.available():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "El runner no esta configurado")
    run_key = _run_key_of(model, review_key)
    if run_key is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Sin verificacion en curso para {review_key}"
        )
    stream = console.stream(run_key)

    def _events() -> Iterator[str]:
        for line in stream:
            yield f"{line}\n"

    return StreamingResponse(_events(), media_type="text/event-stream")


def _run_key_of(model: PanelReadModel, review_key: str) -> str | None:
    """Primer `run_key` de la wave de fix de la review, o `None` si no esta en `fix`."""
    for item in model.state().paused.fix:
        if item.review_key != review_key:
            continue
        for job in item.jobs:
            run_key = str(job.get("run_key", ""))
            if run_key:
                return run_key
    return None


# La UI (OP15/OP33) vive en su propio router para no engordar este modulo; la app
# la monta aqui para que el panel sea un unico proceso con una unica sesion.
app.include_router(ui_router)


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())
    uvicorn.run(app, host="0.0.0.0", port=settings.panel_port)


if __name__ == "__main__":  # pragma: no cover - entrada del proceso
    main()
