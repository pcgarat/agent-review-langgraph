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
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from agent_review.claude_activity import (
    activity_paths,
    read_activity,
    review_target,
    transcript_events,
)
from agent_review.claude_session import write_session
from agent_review.decision import require_decision_item
from agent_review.domain.history import ReviewSummary
from agent_review.domain.models import FindingDecision
from agent_review.history_store import ReviewHistory
from agent_review.ports.reasoning import ReasoningReader
from agent_review.queue import RedisReviewQueue, ReviewQueue
from agent_review.settings import Settings, get_settings, require_panel_secrets
from agent_review.web.auth import (
    Session,
    clear_session_cookie,
    require_command_session,
    require_session,
    session_payload,
    set_session_cookie,
    verify_session,
)
from agent_review.web.bandeja import build_bandeja
from agent_review.web.bandeja import router as bandeja_router
from agent_review.web.commands import (
    CommandEffect,
    CommandError,
    CommandService,
    build_command_service,
)
from agent_review.web.launch import LaunchError, LaunchService
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
    # El candado de diales y el almacen se resuelven al arrancar, no en la primera
    # visita a /config: un nombre fuera del catalogo o un Postgres obligatorio que
    # no responde tienen que tumbar el proceso, igual que en el worker (ADR-0031).
    from agent_review.web.ui import build_config_store_for_panel

    app.state.config_store = build_config_store_for_panel(settings)
    app.state.read_model = build_panel_read_model(settings)
    cancellation = build_cancellation(settings)
    if cancellation is not None:
        # La bandeja y el formulario del detalle comparten sello y cola: la
        # primera respuesta, venga de donde venga, es la que se encola.
        bandeja = build_bandeja(settings)
        app.state.bandeja = bandeja
        app.state.commands = build_command_service(
            queue=bandeja.queue,
            paused=bandeja.paused,
            cancellation=cancellation,
            submitter=bandeja.submitter,
        )
    app.state.console = build_runner_client(settings)
    app.state.reasoning = build_reasoning_reader(settings)
    app.state.claude_login = build_claude_login(settings)
    try:
        yield
    finally:
        close = getattr(app.state.read_model, "close", None)
        if callable(close):
            close()
        close_config = getattr(app.state.config_store, "close", None)
        if callable(close_config):
            close_config()


app = FastAPI(title="Panel de operacion", version="0.1.0", lifespan=lifespan)


def get_read_model(request: Request) -> PanelReadModel:
    """Read model del proceso. Los tests inyectan un fake en `app.state.read_model`."""
    model: PanelReadModel | None = getattr(request.app.state, "read_model", None)
    if model is None:
        model = build_panel_read_model(_settings())
        request.app.state.read_model = model
    return model


def get_launcher(request: Request) -> LaunchService:
    """Encola un lanzamiento. Los tests inyectan uno en `app.state.launcher`."""
    launcher: LaunchService | None = getattr(request.app.state, "launcher", None)
    if launcher is None:
        settings = _settings()
        queue = RedisReviewQueue(url=settings.redis_url)
        if not queue.available():
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Lanzar una review necesita Redis, que no esta disponible",
            )
        launcher = LaunchService(queue, expected_repo=settings.configured_repo())
        request.app.state.launcher = launcher
    return launcher


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


# --- Sesion del panel (029, P6/P7) -----------------------------------------------


@app.get("/api/session")
def read_session(session: Annotated[Session, Depends(require_session)]) -> dict[str, str]:
    """Quien ha entrado y que rol tiene (029, P7).

    La SPA no puede leer la cookie (`HttpOnly`), asi que pregunta. Es la unica
    fuente del rol en el cliente: no hay una segunda copia que se desincronice.
    """
    return session_payload(session)


@app.post("/api/session")
async def open_session(request: Request) -> Response:
    """Valida el token del despliegue y deja la cookie de sesion (029, P6).

    Es la unica ruta que acepta la credencial en el cuerpo. Ni el token ni la
    cookie vuelven en la respuesta: la cookie la pone `Set-Cookie` y el cuerpo solo
    lleva la identidad.
    """
    settings = _settings()
    token = await _session_token(request)
    try:
        session = verify_session(settings, token)
    except HTTPException as exc:
        # Sin `WWW-Authenticate`: el navegador no debe abrir su dialogo nativo de
        # credenciales por un token de panel mal escrito.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Ese token no vale") from exc
    payload = session_payload(session)
    response = JSONResponse(payload)
    set_session_cookie(response, settings)
    return response


@app.delete("/api/session", status_code=status.HTTP_204_NO_CONTENT)
def close_session() -> Response:
    """Borra la cookie. Idempotente: sin cookie tambien es `204`."""
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookie(response)
    return response


async def _session_token(request: Request) -> str:
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El cuerpo no es JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El cuerpo no es JSON")
    token = body.get("token")
    if not isinstance(token, str) or not token.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Falta el token")
    return token.strip()


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


class ClaudeLogin:
    """Guarda la sesión y suelta solo lo parqueado por falta de ella."""

    def __init__(self, *, path: Path, queue: ReviewQueue, ttl_s: float) -> None:
        self._path = path
        self._queue = queue
        self._ttl = ttl_s

    def save(self, token: str) -> int:
        write_session(self._path, token, ttl_s=self._ttl)
        released = self._queue.release_parked(reason="session")
        self._queue.clear_notice("session")
        logger.info("sesion de claude guardada; %d reviews vuelven a la cola", len(released))
        return len(released)


def build_claude_login(settings: Settings) -> ClaudeLogin:
    return ClaudeLogin(
        path=Path(settings.claude_session_path),
        queue=RedisReviewQueue(url=settings.redis_url),
        ttl_s=settings.claude_session_ttl_s,
    )


def get_claude_login(request: Request) -> ClaudeLogin:
    login: ClaudeLogin | None = getattr(request.app.state, "claude_login", None)
    if login is None:
        login = build_claude_login(_settings())
        request.app.state.claude_login = login
    return login


@app.post("/api/claude/session")
async def save_claude_session(
    request: Request,
    session: Annotated[Session, Depends(require_command_session)],
    login: Annotated[ClaudeLogin, Depends(get_claude_login)],
) -> dict[str, int]:
    """El operador deja el token en el fichero `0600`. La respuesta no lo devuelve."""
    del session
    token = await _claude_token(request)
    try:
        released = login.save(token)
    except OSError as exc:
        logger.exception("No se pudo guardar la sesion de Claude")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "No se pudo guardar la sesion"
        ) from exc
    return {"released": released}


async def _claude_token(request: Request) -> str:
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El cuerpo no es JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El cuerpo no es JSON")
    token = body.get("token")
    if not isinstance(token, str) or not token.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Falta el token")
    return token.strip()


@app.post("/api/launch", status_code=status.HTTP_202_ACCEPTED)
async def launch_review(
    request: Request,
    session: Annotated[Session, Depends(require_command_session)],
    launcher: Annotated[LaunchService, Depends(get_launcher)],
) -> dict[str, object]:
    """Encola una review a partir de la URL del pull request.

    No resuelve el commit: el worker lo lee del VCS. La respuesta no lleva
    `review_key` porque aun no hay SHA.
    """
    url = await _launch_url(request)
    try:
        launched = launcher.launch(url, actor=session.user)
    except LaunchError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception as exc:
        logger.exception("No se pudo encolar el lanzamiento")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return {
        "status": "encolada",
        "repo": launched.repo,
        "pr_id": launched.pr_id,
        "pipeline_id": launched.pipeline_id,
        "actor": session.user,
    }


async def _launch_url(request: Request) -> str:
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El cuerpo no es JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El cuerpo no es JSON")
    url = body.get("url")
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Falta la URL del pull request")
    return url


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


@app.post("/api/reviews/{review_key:path}/forget")
def forget_review(
    review_key: str,
    session: Annotated[Session, Depends(require_command_session)],
    commands: Annotated[CommandService, Depends(get_commands)],
) -> dict[str, object]:
    """Encola el borrado local de la ejecucion (ADR-0034).

    La transcripcion se retira aqui: el panel es quien tiene el volumen. El
    hilo y el artefacto los borra el worker al tomar el job.
    """
    result = _run_command(commands.forget, review_key, session)
    _drop_claude_activity(review_key)
    return result


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
        return [require_decision_item(item) for item in body["items"]]
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Item de decision invalido: {exc}"
        ) from exc


def _drop_claude_activity(review_key: str) -> None:
    target = review_target(review_key)
    if target is None:
        return
    repo, pr_id, head_sha = target
    status_path, log_path = activity_paths(
        Path(_settings().claude_session_path),
        repo=repo,
        pr_id=pr_id,
        head_sha=head_sha,
    )
    for path in (status_path, log_path):
        try:
            path.unlink()
        except OSError:
            continue


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


@app.get("/api/reviews/{review_key:path}/claude")
def claude_activity(
    review_key: str,
    session: Annotated[Session, Depends(require_session)],
) -> dict[str, object]:
    """Qué hace el CLI en este commit: cuándo empezó, si sigue, y las líneas."""
    del session
    target = review_target(review_key)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "La review no identifica un commit")
    repo, pr_id, head_sha = target
    return read_activity(
        Path(_settings().claude_session_path), repo=repo, pr_id=pr_id, head_sha=head_sha
    )


@app.get("/api/reviews/{review_key:path}/claude/events")
def claude_transcript(
    review_key: str,
    session: Annotated[Session, Depends(require_session)],
) -> StreamingResponse:
    """Líneas ya escritas y las que lleguen, hasta que la ejecución termine."""
    del session
    target = _claude_target(review_key)
    repo, pr_id, head_sha = target
    return StreamingResponse(
        transcript_events(
            Path(_settings().claude_session_path),
            repo=repo,
            pr_id=pr_id,
            head_sha=head_sha,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/reviews/{review_key:path}/claude/download")
def claude_download(
    review_key: str,
    session: Annotated[Session, Depends(require_session)],
) -> FileResponse:
    """El fichero entero de la conversación. No es un canal anónimo."""
    del session
    repo, pr_id, head_sha = _claude_target(review_key)
    _status, log_path = activity_paths(
        Path(_settings().claude_session_path), repo=repo, pr_id=pr_id, head_sha=head_sha
    )
    if not log_path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No hay conversación de esta review")
    return FileResponse(log_path, media_type="text/plain; charset=utf-8", filename=log_path.name)


def _claude_target(review_key: str) -> tuple[str, int, str]:
    target = review_target(review_key)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "La review no identifica un commit")
    return target


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
app.include_router(bandeja_router)


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())
    uvicorn.run(app, host="0.0.0.0", port=settings.panel_port)


if __name__ == "__main__":  # pragma: no cover - entrada del proceso
    main()
