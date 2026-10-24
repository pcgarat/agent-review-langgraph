"""Ficha agregada de una review en la API del panel (iniciativa 029, P8).

La ficha HTML hacia seis peticiones para pintarse y parte de sus datos (`notes`,
`journal`, disponibilidad de consola y de razonamiento) solo existia dentro del
HTML. Este recurso los devuelve juntos, para que la SPA pinte el **Log** y sepa
que canales ofrecer sin abrir los SSE: la disponibilidad es un booleano, no un
stream.

El `:path` de la ruta es necesario porque la `review_key` lleva `/` y `:`
(`cqm/emotivoo-v2:42:<sha>:<pipeline>`); sin el, el routing cortaria en el primer
`/`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from agent_review.claude_activity import activity_paths, review_target
from agent_review.domain.trace import ReviewTrace
from agent_review.ports.reasoning import ReasoningReader
from agent_review.settings import Settings, get_settings
from agent_review.web.auth import Session, require_session
from agent_review.web.read_model import (
    DeadLetterItem,
    PanelReadModel,
    PanelState,
    PausedItem,
    build_panel_read_model,
)
from agent_review.web.runner_client import RunnerConsoleClient, build_runner_client

router = APIRouter()


class ClaudeAvailability(BaseModel):
    """Qué se puede hacer con la actividad del CLI de Claude en esta review.

    `available` dice si la ficha de Claude aplica: el motor es `claude` y la clave
    identifica un commit, así que hay algo que consultar aunque el CLI aún no haya
    escrito nada. `events_available` dice si ya hay transcripción que seguir por
    SSE (sin fichero de estado, el stream se quedaría esperando para siempre) y
    `download_available` si existe el log entero, que es la misma condición que el
    `404` de `claude/download`.
    """

    available: bool
    events_available: bool
    download_available: bool


class ReviewDetail(BaseModel):
    """Todo lo que la ficha necesita en una petición (spec 029 §5.3).

    `claude` es `None` cuando `REVIEW_ENGINE` no es `claude`: sin ese motor no hay
    CLI que enseñar y una sección vacía en la SPA fingiría un canal que el
    despliegue no tiene. Con el motor encendido, el objeto dice qué partes existen.
    """

    review_key: str
    trace: ReviewTrace | None = None
    paused: PausedItem | None = None
    notes: list[str]
    journal: list[str]
    console_available: bool
    reasoning_available: bool
    claude: ClaudeAvailability | None = None


# Estas tres dependencias no viven en `web.main` porque `main` incluye este router
# y el import de vuelta sería circular; el contrato de inyección (fake en
# `app.state`) es el mismo que usan `ui.py` y la propia app.


def get_read_model(request: Request) -> PanelReadModel:
    """Read model del proceso. Los tests inyectan un fake en `app.state.read_model`."""
    model: PanelReadModel | None = getattr(request.app.state, "read_model", None)
    if model is None:
        model = build_panel_read_model(get_settings())
        request.app.state.read_model = model
    return model


def get_console(request: Request) -> RunnerConsoleClient:
    """Cliente del runner del proceso. Construirlo no abre ningún stream."""
    console: RunnerConsoleClient | None = getattr(request.app.state, "console", None)
    if console is None:
        console = build_runner_client(get_settings())
        request.app.state.console = console
    return console


def get_reasoning(request: Request) -> ReasoningReader | None:
    """Lector del razonamiento del proceso, o `None` sin Redis."""
    if not hasattr(request.app.state, "reasoning"):
        request.app.state.reasoning = build_reasoning_reader(get_settings())
    reader: ReasoningReader | None = request.app.state.reasoning
    return reader


def build_reasoning_reader(settings: Settings) -> ReasoningReader | None:
    """Lector del razonamiento (021 R5), o `None` sin Redis."""
    if not settings.redis_url.strip():
        return None
    from agent_review.adapters.reasoning_redis import RedisReasoningReader

    return RedisReasoningReader(url=settings.redis_url)


@router.get("/api/reviews/{review_key:path}")
def review_detail(
    review_key: str,
    session: Annotated[Session, Depends(require_session)],
    model: Annotated[PanelReadModel, Depends(get_read_model)],
    console: Annotated[RunnerConsoleClient, Depends(get_console)],
    reasoning: Annotated[ReasoningReader | None, Depends(get_reasoning)],
) -> ReviewDetail:
    """Traza, pausa, log y canales disponibles de una review.

    `404` solo cuando no queda rastro por ningún lado: sin recorrido en el
    checkpointer, sin `notes` ni `journal` y sin descarte en la DLQ. Es la misma
    condición que usaba la ficha HTML (`review_detail` de `ui.py`): un descarte
    que nunca arrancó el grafo sigue teniendo ficha, con `trace: null`.
    """
    del session  # cualquier rol lee; los comandos son los que exigen `operador`
    state = model.state()
    trace = model.trace(review_key)
    dead = _dead_item(state, review_key)
    notes = model.notes(review_key)
    journal = model.journal(review_key)
    if trace is None and dead is None and not _log_text(notes, journal):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Sin recorrido para {review_key}")
    return ReviewDetail(
        review_key=review_key,
        trace=trace,
        paused=_paused_item(state, review_key),
        notes=notes,
        journal=journal,
        console_available=console.available() and _run_key_of(state, review_key) is not None,
        reasoning_available=reasoning is not None and reasoning.exists(review_key),
        claude=_claude_availability(review_key, get_settings()),
    )


def _log_text(notes: list[str], journal: list[str]) -> str:
    """Mismo criterio que el Log de la ficha HTML: líneas vacías no son contenido."""
    lines = [line.strip() for line in (*notes, *journal) if line.strip()]
    return "\n\n".join(lines)


def _dead_item(state: PanelState, review_key: str) -> DeadLetterItem | None:
    for item in state.dead:
        if item.review_key == review_key:
            return item
    return None


def _paused_item(state: PanelState, review_key: str) -> PausedItem | None:
    for item in (*state.paused.human, *state.paused.fix):
        if item.review_key == review_key:
            return item
    return None


def _run_key_of(state: PanelState, review_key: str) -> str | None:
    """Primer `run_key` de la wave de fix, o `None` si la review no está en `fix`.

    Es la misma resolución que hace el endpoint SSE de la consola en `main`; aquí
    solo se consulta para decir si el canal existe, sin abrirlo.
    """
    for item in state.paused.fix:
        if item.review_key != review_key:
            continue
        for job in item.jobs:
            run_key = str(job.get("run_key", ""))
            if run_key:
                return run_key
    return None


def _claude_availability(review_key: str, settings: Settings) -> ClaudeAvailability | None:
    """Actividad del CLI para este commit, o `None` si el motor no es `claude`."""
    if settings.review_engine != "claude":
        return None
    target = review_target(review_key)
    if target is None:
        return ClaudeAvailability(available=False, events_available=False, download_available=False)
    repo, pr_id, head_sha = target
    status_path, log_path = activity_paths(
        Path(settings.claude_session_path), repo=repo, pr_id=pr_id, head_sha=head_sha
    )
    return ClaudeAvailability(
        available=True,
        events_available=status_path.is_file() or log_path.is_file(),
        download_available=log_path.is_file(),
    )
