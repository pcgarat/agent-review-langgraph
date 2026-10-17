"""Servicio `web` del panel de operacion (iniciativa 019, OP11/OP12/OP13/OP14).

Proceso aparte, opt-in por el perfil de compose `panel` (W1): lee el estado de Redis
y el recorrido del checkpointer y encola comandos, pero **nunca** escribe el estado
del grafo, que sigue teniendo un unico escritor (el worker, W3).

Arranque: `python -m agent_review.web.main` (uvicorn en `PANEL_PORT`).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from agent_review.domain.history import ReviewSummary
from agent_review.domain.models import FindingDecision
from agent_review.history_store import ReviewHistory
from agent_review.paused_store import RedisPausedStore
from agent_review.ports.reasoning import ReasoningReader
from agent_review.queue import RedisReviewQueue
from agent_review.settings import Settings, get_settings, require_panel_secrets
from agent_review.web.auth import Session, require_command_session, require_session
from agent_review.web.commands import (
    CommandEffect,
    CommandError,
    CommandService,
    build_command_service,
)
from agent_review.web.read_model import PanelReadModel, build_panel_read_model
from agent_review.web.reasoning import reasoning_stream
from agent_review.web.runner_client import RunnerConsoleClient, build_runner_client
from agent_review.web.ui import get_history, recent_reviews
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
    app.state.reasoning = build_reasoning_reader(settings)
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


def build_reasoning_reader(settings: Settings) -> ReasoningReader | None:
    """Lector del razonamiento (021 R5), o `None` sin Redis."""
    if not settings.redis_url.strip():
        return None
    from agent_review.adapters.reasoning_redis import RedisReasoningReader

    return RedisReasoningReader(url=settings.redis_url)


def get_reasoning(request: Request) -> ReasoningReader | None:
    """Lector del razonamiento del proceso. Los tests inyectan uno en memoria."""
    if not hasattr(request.app.state, "reasoning"):
        request.app.state.reasoning = build_reasoning_reader(_settings())
    reader: ReasoningReader | None = request.app.state.reasoning
    return reader


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


@app.post("/api/reviews/{review_key:path}/cancel")
def cancel_review(
    review_key: str,
    session: Annotated[Session, Depends(require_command_session)],
    commands: Annotated[CommandService, Depends(get_commands)],
) -> dict[str, object]:
    """Cancela una review (OP29/OP30): marca para lo que corre, comando para la pausa."""
    return _run_command(commands.cancel, review_key, session)


@app.post("/api/reviews/{review_key:path}/retry")
def retry_review(
    review_key: str,
    session: Annotated[Session, Depends(require_command_session)],
    commands: Annotated[CommandService, Depends(get_commands)],
) -> dict[str, object]:
    """Reencola un descarte de la DLQ con `attempts=0` (OP31)."""
    return _run_command(commands.retry, review_key, session)


@app.post("/api/reviews/{review_key:path}/resume")
def resume_review(
    review_key: str,
    session: Annotated[Session, Depends(require_command_session)],
    commands: Annotated[CommandService, Depends(get_commands)],
) -> dict[str, object]:
    """Reencola la reanudacion de una pausa humana (OP32)."""
    return _run_command(commands.resume, review_key, session)


@app.post("/api/reviews/{review_key:path}/decision")
async def decide_review(
    review_key: str,
    request: Request,
    session: Annotated[Session, Depends(require_command_session)],
    commands: Annotated[CommandService, Depends(get_commands)],
) -> dict[str, object]:
    """Encola una decision humana explicita sobre una pausa (022, H1/H2).

    A diferencia del resto de comandos, lleva cuerpo (`items`): la decision se
    construye con `build_decision`, la misma que el CLI, la API y Teams. El `actor`
    no viaja: es el de la sesion (OP51). Un cuerpo mal formado es `400`; una decision
    que no cubre los pendientes es `409`.
    """
    items = await _decision_items(request)
    return _run_command(
        lambda key, *, actor: commands.decide(key, actor=actor, items=items), review_key, session
    )


async def _decision_items(request: Request) -> list[FindingDecision]:
    """Parsea `{"items": [...]}`; un cuerpo que no encaja con el contrato es `400`."""
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El cuerpo no es JSON") from exc
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Falta `items`")
    try:
        return [
            FindingDecision.model_validate(item) if isinstance(item, dict) else item
            for item in body["items"]
        ]
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Item de decision invalido: {exc}"
        ) from exc


def _run_command(
    operation: Callable[..., CommandEffect],
    review_key: str,
    session: Session,
) -> dict[str, object]:
    # El actor auditado es **siempre** el de la sesion autenticada: un nombre que
    # mandara el cliente permitiria firmar el comando (y la `HitlDecision` de una
    # reanudacion) en nombre de otro.
    actor = session.user
    try:
        effect = operation(review_key, actor=actor)
    except CommandError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except Exception as exc:
        # Un fallo de Redis al marcar no puede devolver `200`: la cancelacion no ocurrio.
        logger.exception("Comando fallido sobre %s", review_key)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return {"review_key": review_key, "effect": effect.value, "actor": actor}


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
    stream = console.open(run_key)
    if stream is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"El runner no tiene la consola de {review_key}"
        )

    def _events() -> Iterator[str]:
        for line in stream:
            yield f"{line}\n"

    return StreamingResponse(_events(), media_type="text/event-stream")


@app.get("/api/history")
def get_history_api(
    session: Annotated[Session, Depends(require_session)],
    history: Annotated[ReviewHistory | None, Depends(get_history)],
    repo: Annotated[str | None, Query()] = None,
    pr_id: Annotated[int | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[ReviewSummary]:
    """Reviews terminadas, la más reciente primero (021 R9). Vacío sin Redis."""
    del session
    return recent_reviews(history, repo=repo, pr_id=pr_id, limit=limit)


@app.get("/api/reviews/{review_key:path}/reasoning")
def review_reasoning(
    review_key: str,
    session: Annotated[Session, Depends(require_session)],
    reader: Annotated[ReasoningReader | None, Depends(get_reasoning)],
) -> StreamingResponse:
    """Razonamiento de los revisores de una review, en vivo (021 R5).

    Cualquier sesion del panel puede leerlo (Q3): no ejecuta nada. Repite lo
    guardado y sigue mientras la review razona; `404` si no hay razonamiento (TTL
    vencido, sink desactivado o una review sin persona LLM).
    """
    del session
    if reader is None or not reader.exists(review_key):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"No hay razonamiento guardado de {review_key}"
        )
    return StreamingResponse(
        reasoning_stream(reader, review_key),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
