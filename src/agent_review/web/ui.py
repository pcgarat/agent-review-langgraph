"""UI del panel de operacion (iniciativa 019, OP15/OP33).

HTML servido en servidor (SSR) sobre el `PanelReadModel`: la vista global pinta el
estado operativo (cola, en vuelo con el tiempo que lleva cada nodo, pausas por
`kind`, descartes y presupuesto por review) y el detalle pinta el diagrama de pasos
junto al recorrido del hilo. Los comandos (cancelar, reintentar, reanudar) son
botones que solo ve el rol `operador`; el actor auditado lo pone el servidor desde
la sesion, no el navegador. La consola de una pausa de fix sigue el SSE del panel.

Sin plantillas ni CDN (018 §3.5): el HTML, el CSS y el JavaScript van embebidos y
autocontenidos. Los datos que llegan del PR son de terceros, asi que **todo** lo que
se interpola pasa antes por `html.escape`.
"""

from __future__ import annotations

import html
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse

from agent_review.domain.history import ReviewSummary
from agent_review.domain.trace import ReviewTrace
from agent_review.history_store import ReviewHistory
from agent_review.settings import get_settings
from agent_review.web.auth import Session, require_session
from agent_review.web.read_model import (
    ActiveNode,
    ActiveReview,
    BudgetItem,
    DeadLetterItem,
    PanelReadModel,
    PanelState,
    PausedItem,
    QueueItem,
    build_panel_read_model,
)

router = APIRouter()

_CANCEL = "cancel"
_RETRY = "retry"
_RESUME = "resume"
# Etiqueta del boton de cancelacion segun lo que de verdad detiene: detras de una
# publicacion (kind `fix`) los comentarios del PR se quedan, asi que no se cancela
# «la review» sino lo que aun corre (spec §5.4, W6/R2).
_CANCEL_LABEL_PENDING = "Cancelar lo pendiente"

# Pasos del grafo, en el orden en que una review los recorre (ninguno se salta:
# la decision de no hacer nada vive dentro del nodo). El texto de la derecha es
# lo que esta haciendo mientras corre y lo que dejo hecho al cerrar.
_Phase = Literal["running", "paused", "done", "pending", "skipped"]


@dataclass(frozen=True, slots=True)
class _StepSpec:
    name: str
    title: str
    doing: str
    done: str


_STEPS: tuple[_StepSpec, ...] = (
    _StepSpec(
        "scope",
        "Alcance",
        "Leyendo el diff y preparando el contexto",
        "Diff filtrado y contexto preparado",
    ),
    _StepSpec(
        "persona",
        "Personas",
        "Revisando el código con las personas",
        "Revisión de las personas terminada",
    ),
    _StepSpec(
        "merge",
        "Fusión",
        "Uniendo los hallazgos duplicados",
        "Hallazgos unidos",
    ),
    _StepSpec(
        "verify",
        "Verificación",
        "Contrastando los candidatos",
        "Candidatos contrastados",
    ),
    _StepSpec(
        "policy",
        "Política",
        "Aplicando la política",
        "Política aplicada",
    ),
    _StepSpec(
        "hitl",
        "Decisión",
        "Esperando una decisión humana",
        "Decisión humana registrada",
    ),
    _StepSpec(
        "fix_dispatch",
        "Arreglos",
        "Proponiendo arreglos",
        "Arreglos propuestos",
    ),
    _StepSpec(
        "publish",
        "Publicación",
        "Publicando la review en el PR",
        "Review publicada en el PR",
    ),
    _StepSpec(
        "fix_wait",
        "Espera",
        "Esperando la verificación de los arreglos",
        "Verificación de los arreglos recogida",
    ),
    _StepSpec(
        "publish_fix",
        "Seguimiento",
        "Publicando el resultado de los arreglos",
        "Resultado de los arreglos publicado",
    ),
    _StepSpec(
        "notify_fix",
        "Aviso del fix",
        "Avisando del resultado de los arreglos",
        "Aviso de arreglos enviado",
    ),
    _StepSpec(
        "notify",
        "Aviso",
        "Avisando de la review en el canal",
        "Aviso de la review enviado",
    ),
)
_STEP_BY_NAME = {step.name: step for step in _STEPS}
_PHASE_LABEL = {
    "running": "en curso",
    "paused": "en pausa",
    "done": "completado",
    "pending": "pendiente",
    "skipped": "no ejecutado",
}


def get_read_model(request: Request) -> PanelReadModel:
    """Read model del proceso, con el *seam* en `app.state.read_model`.

    No se reutiliza el de `web.main` porque `main` incluye este `router` y el import
    seria circular; el contrato de inyeccion (fake en `app.state`) es el mismo.
    """
    model: PanelReadModel | None = getattr(request.app.state, "read_model", None)
    if model is None:
        model = build_panel_read_model(get_settings())
        request.app.state.read_model = model
    return model


def build_history_reader(settings: object) -> ReviewHistory | None:
    """Histórico de reviews para el panel (021 R9), o `None` sin Redis."""
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


@router.get("/history", response_class=HTMLResponse)
def history_page(
    session: Annotated[Session, Depends(require_session)],
    history: Annotated[ReviewHistory | None, Depends(get_history)],
    repo: Annotated[str | None, Query()] = None,
    pr_id: Annotated[int | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> HTMLResponse:
    """Histórico de reviews terminadas (021 R9, §5.5)."""
    items = recent_reviews(history, repo=repo, pr_id=pr_id, limit=limit)
    return HTMLResponse(_history_page(items, session, repo=repo, pr_id=pr_id))


@router.get("/", response_class=HTMLResponse)
def index(
    session: Annotated[Session, Depends(require_session)],
    model: Annotated[PanelReadModel, Depends(get_read_model)],
) -> HTMLResponse:
    """Vista global del panel (OP15)."""
    return HTMLResponse(_global_page(model.state(), session))


@router.get("/review/{review_key:path}", response_class=HTMLResponse)
def review_detail(
    review_key: str,
    session: Annotated[Session, Depends(require_session)],
    model: Annotated[PanelReadModel, Depends(get_read_model)],
) -> HTMLResponse:
    """Detalle de una review: pasos, recorrido y, si es pausa de fix, consola (OP15)."""
    trace = model.trace(review_key)
    if trace is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Sin recorrido para {review_key}")
    state = model.state()
    return HTMLResponse(
        _detail_page(trace, _paused_item(state, review_key), session, _active_of(state, review_key))
    )


# --- Paginas ---------------------------------------------------------------------


def _global_page(state: PanelState, session: Session) -> str:
    budgets = {item.review_key: item for item in state.budget}
    body = "".join(
        (
            _queue_section(state.queue, session),
            _active_section(state.active, budgets, session),
            _paused_section("Pausas humanas (esperan decisión)", state.paused.human, session),
            _paused_section("Pausas de fix (verificación de la wave)", state.paused.fix, session),
            _dead_section(state.dead, session),
            _budget_section(state.budget),
        )
    )
    return _page(
        title="Panel de operación",
        meta=f"Estado generado el {_fmt_dt(state.generated_at)}",
        body='<p class="breadcrumb"><a href="/history">Histórico de reviews &rarr;</a></p>' + body,
        session=session,
        script=_COMMAND_SCRIPT + _ELAPSED_SCRIPT,
    )


def _history_page(
    items: list[ReviewSummary], session: Session, *, repo: str | None, pr_id: int | None
) -> str:
    rows = [
        "<tr>"
        f"<td>{_fmt_dt(item.finished_at)}</td>"
        f"<td>{_review_link(item.review_key)}</td>"
        f"<td>{_esc(item.repo)}#{_esc(item.pr_id)} <code>{_esc(item.head_sha[:8])}</code></td>"
        f'<td><span class="end end-{_esc(item.end.value)}">{_esc(item.end.value)}</span></td>'
        f"<td>{_esc(item.findings)} {_severities(item.by_severity)}</td>"
        f"<td>{_esc(item.files_reviewed)} / {_esc(item.files_skipped)}</td>"
        f"<td>{_esc(item.tokens)}</td>"
        f"<td>{'sí' if item.reasoning else '—'}</td>"
        "</tr>"
        for item in items
    ]
    form = (
        '<form class="filters" method="get" action="/history">'
        f'<label>Repo <input name="repo" value="{_esc(repo or "")}"></label> '
        f'<label>PR <input name="pr_id" type="number" value="{_esc(pr_id or "")}"></label> '
        '<button class="cmd" type="submit">Filtrar</button> <a href="/history">Quitar</a>'
        "</form>"
    )
    table = _table(
        [
            "Terminada",
            "Review",
            "PR",
            "Resultado",
            "Hallazgos",
            "Revisados / sin revisar",
            "Tokens",
            "Razonamiento",
        ],
        rows,
    )
    body = (
        '<p class="breadcrumb"><a href="/">&larr; Volver al panel</a></p>'
        + form
        + _section("Reviews terminadas", table)
    )
    return _page(
        title="Histórico de reviews",
        meta=f"{len(items)} reviews, la más reciente primero",
        body=body,
        session=session,
        script="",
    )


def _severities(by_severity: dict[str, int]) -> str:
    if not by_severity:
        return ""
    return "(" + ", ".join(f"{_esc(k)}: {_esc(v)}" for k, v in by_severity.items()) + ")"


def _detail_page(
    trace: ReviewTrace,
    paused: PausedItem | None,
    session: Session,
    active: list[ActiveNode],
) -> str:
    panel = (
        '<div class="detail-panel" id="detail-panel">'
        '<div class="panel-tools">'
        '<button type="button" id="maximize" class="maximize" aria-pressed="false" '
        'aria-controls="detail-panel">Maximizar</button>'
        "</div>"
        f"{_trace_section(trace)}"
        f"{_detail_commands(trace, session)}"
        f"{_console_section(trace, paused)}"
        f"{_reasoning_section(trace)}"
        "</div>"
    )
    body = (
        '<p class="breadcrumb"><a href="/">&larr; Volver al panel</a></p>'
        f'<div class="ficha">{_steps_panel(trace, active)}{panel}</div>'
    )
    return _page(
        title=f"Review {trace.review_key}",
        meta=f"Estado {trace.state} · actualizado el {_fmt_dt(trace.updated_at)}",
        body=body,
        session=session,
        script=_COMMAND_SCRIPT + _CONSOLE_SCRIPT + _REASONING_SCRIPT + _FICHA_SCRIPT,
    )


# --- Secciones de la vista global (OP15) -----------------------------------------


def _queue_section(items: list[QueueItem], session: Session) -> str:
    rows = [
        "<tr>"
        f"<td>{_review_link(item.review_key)}</td>"
        f"<td>{_esc(item.repo)}</td>"
        f"<td>{_esc(item.pr_id)}</td>"
        f"<td>{_esc(item.head_sha)}</td>"
        f"<td>{_esc(item.pipeline_id)}</td>"
        f"<td>{_esc(item.attempts)}</td>"
        f"<td>{_buttons(item.review_key, ((_CANCEL, 'Cancelar'),), session)}</td>"
        "</tr>"
        for item in items
    ]
    headers = ["Review", "Repo", "PR", "SHA", "Pipeline", "Intentos", "Comandos"]
    return _section("Cola", _table(headers, rows))


def _active_section(
    reviews: list[ActiveReview], budgets: dict[str, BudgetItem], session: Session
) -> str:
    rows = []
    for review in reviews:
        nodes = "<br>".join(_active_node(node) for node in review.nodes) or "—"
        rows.append(
            "<tr>"
            f"<td>{_review_link(review.review_key)}</td>"
            f"<td>{nodes}</td>"
            f"<td>{_budget_cell(budgets.get(review.review_key))}</td>"
            f"<td>{_buttons(review.review_key, ((_CANCEL, 'Cancelar'),), session)}</td>"
            "</tr>"
        )
    headers = ["Review", "Nodo en curso", "Presupuesto", "Comandos"]
    return _section("En vuelo", _table(headers, rows))


def _active_node(node: ActiveNode) -> str:
    text = f"{_esc(node.node)} · {_elapsed_html(node.started_at)}"
    if node.branches > 1:
        text += f" ({_esc(node.branches)} ramas)"
    return text


def _active_of(state: PanelState, review_key: str) -> list[ActiveNode]:
    for review in state.active:
        if review.review_key == review_key:
            return review.nodes
    return []


def _step_names(trace: ReviewTrace, active: list[ActiveNode]) -> list[str]:
    """Orden canónico del grafo, más cualquier nodo que el recorrido haya visto."""
    names = [step.name for step in _STEPS]
    for node in trace.nodes:
        if node.name not in names:
            names.append(node.name)
    for name in (*trace.next, *(node.node for node in active)):
        if name not in names:
            names.append(name)
    return names


def _phases(trace: ReviewTrace, active: list[ActiveNode]) -> dict[str, _Phase]:
    """Estado de cada paso a partir del recorrido y de la marca de progreso.

    La pausa gana al «en curso»: en un `interrupt` el worker ya no ejecuta el nodo,
    aunque el checkpoint lo deje en `next`. Sin marca de progreso, `next` es el nodo
    vivo de una review que sigue `in_progress`.
    """
    completed = {node.name for node in trace.nodes}
    active_names = {node.node for node in active}
    pending = set(trace.next)
    use_next = trace.state == "in_progress" and not active_names
    phases: dict[str, _Phase] = {}
    for name in _step_names(trace, active):
        if trace.state == "paused" and name in pending:
            phases[name] = "paused"
        elif trace.state != "paused" and (name in active_names or (use_next and name in pending)):
            phases[name] = "running"
        elif name in completed:
            phases[name] = "done"
        elif trace.state == "completed":
            phases[name] = "skipped"
        else:
            phases[name] = "pending"
    return phases


def _caption(name: str, phase: _Phase, branches: int) -> str:
    spec = _STEP_BY_NAME.get(name)
    if phase == "running":
        text = spec.doing if spec is not None else f"Ejecutando {name}"
        if branches > 1:
            return f"{text} ({branches} ramas)"
        return text
    if phase == "paused":
        return spec.doing if spec is not None else f"En pausa en {name}"
    if phase == "done":
        return spec.done if spec is not None else f"Paso {name} terminado"
    if phase == "skipped":
        return "No se ejecutó"
    return ""


def _branches_of(active: list[ActiveNode], name: str) -> int:
    return sum(node.branches for node in active if node.node == name)


def _mark(phase: _Phase) -> str:
    if phase == "done":
        return '<span class="mark" aria-hidden="true">✓</span>'
    if phase == "paused":
        return (
            '<span class="mark" aria-hidden="true">'
            '<span class="pause-bar"></span><span class="pause-bar"></span>'
            "</span>"
        )
    return '<span class="mark" aria-hidden="true"></span>'


def _steps_panel(trace: ReviewTrace, active: list[ActiveNode]) -> str:
    phases = _phases(trace, active)
    items: list[str] = []
    for name in _step_names(trace, active):
        phase = phases[name]
        spec = _STEP_BY_NAME.get(name)
        title = spec.title if spec is not None else name
        caption = _caption(name, phase, _branches_of(active, name))
        label = f"{title}, {_PHASE_LABEL[phase]}"
        if caption:
            label = f"{label}. {caption}"
        if phase == "running" and caption:
            text = (
                '<p class="step-text"><span class="spinner" aria-hidden="true"></span> '
                f"{_esc(caption)}</p>"
            )
        elif caption:
            text = f'<p class="step-text">{_esc(caption)}</p>'
        else:
            text = ""
        current = ' aria-current="step"' if phase in {"running", "paused"} else ""
        items.append(
            f'<li class="step {phase}" data-step="{_esc(name)}" '
            f'aria-label="{_esc(label)}"{current}>'
            f"{_mark(phase)}"
            f'<span class="step-name">{_esc(title)}</span>'
            f"{text}"
            "</li>"
        )
    return (
        '<aside class="steps-panel">'
        "<h2>Pasos</h2>"
        f'<ol class="steps" data-state="{_esc(trace.state)}">{"".join(items)}</ol>'
        "</aside>"
    )


def _paused_section(title: str, items: list[PausedItem], session: Session) -> str:
    rows = [
        "<tr>"
        f"<td>{_review_link(item.review_key)}</td>"
        f"<td>{_esc(item.state)}</td>"
        f"<td>{_fmt_dt(item.created_at)}</td>"
        f"<td>{_paused_detail(item)}</td>"
        f"<td>{_buttons(item.review_key, _pause_commands(item), session)}</td>"
        "</tr>"
        for item in items
    ]
    headers = ["Review", "Estado", "Desde", "Detalle", "Comandos"]
    return _section(title, _table(headers, rows))


def _dead_section(items: list[DeadLetterItem], session: Session) -> str:
    rows = [
        "<tr>"
        f"<td>{_review_link(item.review_key)}</td>"
        f"<td>{_esc(item.attempts)}</td>"
        f"<td>{_esc(item.reason)}</td>"
        f"<td>{_fmt_dt(item.discarded_at)}</td>"
        f"<td>{_buttons(item.review_key, ((_RETRY, 'Reintentar'),), session)}</td>"
        "</tr>"
        for item in items
    ]
    headers = ["Review", "Intentos", "Motivo", "Descartada", "Comandos"]
    return _section("Descartes (DLQ)", _table(headers, rows))


def _budget_section(items: list[BudgetItem]) -> str:
    rows = [
        "<tr>"
        f"<td>{_review_link(item.review_key)}</td>"
        f"<td>{_esc(item.committed)}</td>"
        f"<td>{_esc(item.reserved)}</td>"
        f"<td>{_esc(item.total)}</td>"
        f"<td>{_esc(item.remaining)}</td>"
        "</tr>"
        for item in items
    ]
    headers = ["Review", "Gastado", "Reservado", "Total", "Restante"]
    return _section("Presupuesto por review", _table(headers, rows))


# --- Detalle (OP15) y comandos (OP33) --------------------------------------------


def _trace_section(trace: ReviewTrace) -> str:
    rows = []
    for node in trace.nodes:
        writes = ", ".join(node.writes) if node.writes else "—"
        rows.append(
            "<tr>"
            f"<td>{_esc(node.index)}</td>"
            f"<td>{_esc(node.name)}</td>"
            f"<td>{_fmt_dt(node.started_at)}</td>"
            f"<td>{_esc(writes)}</td>"
            "</tr>"
        )
    pending = ", ".join(trace.next) if trace.next else "—"
    summary = (
        f'<p class="trace-state">Estado <strong>{_esc(trace.state)}</strong> · '
        f"pausa {_esc(trace.pause_kind or '—')} · siguiente {_esc(pending)}</p>"
    )
    table = _table(["#", "Nodo", "Iniciado", "Escribe"], rows)
    return f"<section><h2>Recorrido</h2>{summary}{table}</section>"


def _detail_commands(trace: ReviewTrace, session: Session) -> str:
    buttons = _buttons(trace.review_key, _trace_commands(trace), session)
    if not buttons:
        return ""
    return f"<section><h2>Comandos</h2>{buttons}</section>"


def _console_section(trace: ReviewTrace, paused: PausedItem | None) -> str:
    if trace.pause_kind != "fix" and (paused is None or paused.kind != "fix"):
        return ""
    endpoint = f"/api/reviews/{quote(trace.review_key, safe='/:')}/console"
    run_keys = ", ".join(str(job.get("run_key", "")) for job in (paused.jobs if paused else []))
    return (
        "<section><h2>Consola en vivo</h2>"
        f'<p class="meta">Run keys: {_esc(run_keys or "—")}</p>'
        f'<pre id="console" data-endpoint="{_esc(endpoint)}"></pre>'
        "</section>"
    )


def _reasoning_section(trace: ReviewTrace) -> str:
    """Razonamiento de los revisores, lote a lote y en vivo (021 §5.3)."""
    endpoint = f"/api/reviews/{quote(trace.review_key, safe='/:')}/reasoning"
    return (
        "<section><h2>Razonamiento</h2>"
        '<p class="meta" id="reasoning-status">Conectando…</p>'
        f'<div id="reasoning" data-endpoint="{_esc(endpoint)}"></div>'
        "</section>"
    )


def _trace_commands(trace: ReviewTrace) -> tuple[tuple[str, str], ...]:
    """Comandos del detalle segun el estado del hilo (spec §5.4/§5.5)."""
    if trace.state == "completed":
        return ()
    if trace.pause_kind == "fix":
        return ((_CANCEL, _CANCEL_LABEL_PENDING),)
    if trace.pause_kind == "human":
        return ((_CANCEL, "Cancelar la review"), (_RESUME, "Reanudar"))
    return ((_CANCEL, "Cancelar"),)


def _pause_commands(item: PausedItem) -> tuple[tuple[str, str], ...]:
    if item.kind == "fix":
        return ((_CANCEL, _CANCEL_LABEL_PENDING),)
    return ((_CANCEL, "Cancelar la review"), (_RESUME, "Reanudar"))


def _buttons(review_key: str, commands: tuple[tuple[str, str], ...], session: Session) -> str:
    """Botones de comando, solo para el rol `operador` (OP33).

    El endpoint viaja como `data-endpoint` (ya escapado) porque la `review_key` lleva
    `/` y `:`: asi el JavaScript no tiene que recomponer la ruta. El actor no viaja:
    el servidor lo toma de la sesion.
    """
    if not session.can_command():
        return ""
    base = f"/api/reviews/{quote(review_key, safe='/:')}"
    buttons = []
    for name, label in commands:
        endpoint = _esc(f"{base}/{name}")
        buttons.append(
            f'<button type="button" class="cmd" data-command="{name}" '
            f'data-endpoint="{endpoint}">{_esc(label)}</button>'
        )
    return f'<span class="commands">{"".join(buttons)}</span>'


def _paused_item(state: PanelState, review_key: str) -> PausedItem | None:
    for item in (*state.paused.human, *state.paused.fix):
        if item.review_key == review_key:
            return item
    return None


def _paused_detail(item: PausedItem) -> str:
    parts: list[str] = []
    if item.pending:
        parts.append(_inline_list("Pendientes", (_json(value) for value in item.pending)))
    if item.jobs:
        parts.append(_inline_list("Run keys", (_json(job.get("run_key", "")) for job in item.jobs)))
    if item.command_hint:
        parts.append(f"<code>{_esc(item.command_hint)}</code>")
    return "".join(parts) or "—"


# --- Primicas de marcado ---------------------------------------------------------


def _page(*, title: str, meta: str, body: str, session: Session, script: str) -> str:
    return (
        "<!doctype html>\n"
        '<html lang="es">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_esc(title)}</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        "<header>\n"
        f"<h1>{_esc(title)}</h1>\n"
        f'<p class="meta">{_esc(meta)}</p>\n'
        f'<p class="session">Sesión: {_esc(session.user)} ({_esc(session.role.value)})</p>\n'
        "</header>\n"
        '<p id="error" class="error" hidden></p>\n'
        f"<main>{body}</main>\n"
        f"{script}\n"
        "</body>\n"
        "</html>\n"
    )


def _section(title: str, content: str) -> str:
    return f"<section><h2>{_esc(title)}</h2>{content}</section>"


def _table(headers: list[str], rows: list[str]) -> str:
    if not rows:
        return '<p class="empty">Sin entradas.</p>'
    head = "".join(f"<th>{_esc(header)}</th>" for header in headers)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def _inline_list(label: str, values: Iterable[str]) -> str:
    items = "".join(f"<li>{_esc(value)}</li>" for value in values)
    return f'<span class="tag">{_esc(label)}</span><ul class="inline">{items}</ul>'


def _review_link(review_key: str) -> str:
    href = quote(review_key, safe="/:")
    return f'<a href="/review/{_esc(href)}">{_esc(review_key)}</a>'


def _budget_cell(budget: BudgetItem | None) -> str:
    if budget is None:
        return "—"
    return _esc(f"{budget.committed + budget.reserved}/{budget.total}")


def _fmt_dt(value: datetime | None) -> str:
    return "—" if value is None else _esc(value.isoformat(timespec="seconds"))


def _fmt_elapsed(started_at: datetime | None, *, now: datetime | None = None) -> str:
    """Cuánto lleva un nodo en curso, a partir de su `started_at`.

    El panel no muestra la fecha de inicio: el operador quiere la duración. Un
    reloj adelantado no puede pintar un tiempo negativo.
    """
    if started_at is None:
        return "—"
    current = now or datetime.now(UTC)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    total = max(0, int((current - started_at).total_seconds()))
    days, rem = divmod(total, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes, seconds = divmod(rem, 60)
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {seconds} s"
    return f"{seconds} s"


def _elapsed_html(started_at: datetime | None) -> str:
    label = _esc(_fmt_elapsed(started_at))
    if started_at is None:
        return label
    stamp = _esc(started_at.isoformat(timespec="seconds"))
    # El `title` conserva el inicio para diagnosticar; lo que se lee es la duración.
    return f'<span class="elapsed" data-started="{stamp}" title="Inicio {stamp}">{label}</span>'


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


_CSS = """
:root { color-scheme: light dark; --line: #8883; --accent: #2b6cb0; --bad: #b02b2b; }
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 1100px; padding: 1rem 1.25rem 3rem;
  font: 15px/1.5 system-ui, sans-serif; }
h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
h2 { font-size: 1.05rem; margin: 0 0 .5rem; border-bottom: 1px solid var(--line); }
section { margin: 1.75rem 0 0; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; vertical-align: top; padding: .35rem .5rem;
  border-bottom: 1px solid var(--line); }
th { font-weight: 600; opacity: .8; }
a { color: var(--accent); }
code, pre { font-family: ui-monospace, monospace; font-size: .85rem; }
pre { background: #8881; padding: .75rem; max-height: 22rem; overflow: auto;
  white-space: pre-wrap; }
.meta, .session, .empty { opacity: .7; margin: .15rem 0; }
.tag { display: inline-block; margin-right: .35rem; opacity: .7; }
ul.inline { display: inline; margin: 0; padding: 0; list-style: none; }
ul.inline li { display: inline-block; margin: 0 .4rem 0 0; }
.commands { display: flex; gap: .4rem; }
button.cmd { font: inherit; padding: .25rem .6rem; border: 1px solid var(--line);
  border-radius: .35rem; cursor: pointer; }
button.cmd:disabled { opacity: .5; cursor: default; }
.error { color: var(--bad); }
.filters { display: flex; gap: .75rem; align-items: center; margin: 1rem 0 0; }
.filters input { font: inherit; padding: .15rem .35rem; }
.end { padding: 0 .4rem; border-radius: .3rem; border: 1px solid var(--line); }
.end-parcial, .end-incompleta { color: var(--bad); }
#reasoning details { margin: .5rem 0; border: 1px solid var(--line); border-radius: .35rem;
  padding: .25rem .6rem; }
#reasoning summary { cursor: pointer; }
#reasoning details.live summary::after { content: " · en curso"; color: var(--accent); }
#reasoning details.failed summary { color: var(--bad); }
#reasoning pre { margin: .4rem 0; }
body:has(.ficha) { max-width: 1280px; }
.ficha { display: grid; grid-template-columns: minmax(18rem, 26rem) minmax(0, 1fr);
  gap: 1rem; align-items: start; margin-top: 1.25rem; }
.steps-panel, .detail-panel { border: 1px solid var(--line); border-radius: .6rem;
  padding: .85rem 1rem 1.1rem; min-width: 0; }
.detail-panel { position: relative; }
.panel-tools { display: flex; justify-content: flex-end; position: sticky; top: 0;
  z-index: 2; background: Canvas; padding-bottom: .35rem; }
button.maximize { font: inherit; padding: .25rem .7rem; border: 1px solid var(--line);
  border-radius: .35rem; cursor: pointer; background: transparent; color: inherit; }
.detail-panel.is-max { position: fixed; inset: 0; z-index: 30; margin: 0;
  border-radius: 0; overflow: auto; background: Canvas; color: CanvasText;
  padding: 1rem 1.5rem 2rem; }
ol.steps { list-style: none; margin: .4rem 0 0; padding: 0; }
.step { display: grid; grid-template-columns: 1.15rem 6.6rem minmax(0, 1fr);
  column-gap: .55rem; align-items: start; position: relative; padding: 0 0 .85rem; }
.step:not(:last-child)::before { content: ""; position: absolute;
  left: calc(.575rem - 1px); top: 1.15rem; bottom: 0; width: 2px; background: var(--line); }
.mark { width: 1.15rem; height: 1.15rem; border-radius: 50%; border: 2px solid var(--line);
  display: grid; place-items: center; background: Canvas; font-size: .72rem; line-height: 1;
  z-index: 1; margin-top: .12rem; }
.step.done .mark { background: #2f855a; border-color: #2f855a; color: #fff; }
.step.running .mark { border-color: var(--accent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 28%, transparent); }
.step.paused .mark { display: flex; gap: 2px; align-items: center; justify-content: center;
  border-color: #b7791f; color: #b7791f; }
.pause-bar { width: 2px; height: .5rem; background: currentColor; display: block; }
.step.skipped { opacity: .55; }
.step-name { font-weight: 600; }
.step-text { display: flex; gap: .4rem; align-items: flex-start; margin: 0;
  font-size: .82rem; line-height: 1.35; }
.step.running .step-text { color: var(--accent); }
.step.paused .step-text { color: #b7791f; }
.spinner { flex: none; margin-top: .15rem; width: .85rem; height: .85rem; display: block;
  box-sizing: border-box; border: 2px solid var(--accent); border-right-color: transparent;
  border-radius: 50%; animation: spin .75s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) {
  .spinner { animation: none; }
}
@media (max-width: 800px) {
  .ficha { grid-template-columns: 1fr; }
  .step { grid-template-columns: 1.15rem 6.2rem minmax(0, 1fr); }
}
"""

_COMMAND_SCRIPT = """<script>
(() => {
  const error = document.getElementById("error");
  const send = async (button) => {
    button.disabled = true;
    error.hidden = true;
    try {
      const response = await fetch(button.dataset.endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
        credentials: "same-origin",
      });
      if (!response.ok) {
        throw new Error((await response.text()) || response.statusText);
      }
      button.textContent = "Enviado";
    } catch (cause) {
      error.textContent = String(cause);
      error.hidden = false;
      button.disabled = false;
    }
  };
  document.querySelectorAll("button.cmd").forEach((button) => {
    button.addEventListener("click", () => send(button));
  });
})();
</script>"""

_REASONING_SCRIPT = """<script>
(() => {
  const root = document.getElementById("reasoning");
  const status = document.getElementById("reasoning-status");
  if (!root) {
    return;
  }
  const blocks = new Map();
  let seen = 0;
  const block = (ev) => {
    const key = `${ev.role}/${ev.batch}`;
    if (!blocks.has(key)) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      const text = document.createElement("pre");
      details.append(summary, text);
      root.append(details);
      const label = `${ev.role} · lote ${ev.batch}/${ev.total}`;
      blocks.set(key, { details, summary, text, label });
      blocks.get(key).summary.textContent = blocks.get(key).label;
    }
    return blocks.get(key);
  };
  const source = new EventSource(root.dataset.endpoint);
  source.addEventListener("start", (event) => {
    const ev = JSON.parse(event.data);
    seen += 1;
    const b = block(ev);
    const files = (ev.meta.files || []).join(", ");
    b.summary.textContent = `${b.label} · ${files}`;
    b.text.textContent = "";
    b.details.className = "live";
    b.details.open = true;
    status.textContent = "En vivo";
  });
  source.addEventListener("reasoning", (event) => {
    const ev = JSON.parse(event.data);
    seen += 1;
    const b = block(ev);
    b.text.textContent += ev.text;
    b.text.scrollTop = b.text.scrollHeight;
  });
  source.addEventListener("end", (event) => {
    const ev = JSON.parse(event.data);
    seen += 1;
    const b = block(ev);
    const m = ev.meta || {};
    b.details.className = "";
    b.details.open = false;
    const tokens = m.reasoning_tokens || 0;
    b.summary.textContent += ` · ${m.findings} hallazgos · ${tokens} tokens de razonamiento`;
  });
  source.addEventListener("fallo", (event) => {
    const ev = JSON.parse(event.data);
    seen += 1;
    const b = block(ev);
    b.details.className = "failed";
    b.summary.textContent += ` · ${ev.text}`;
  });
  source.addEventListener("fin", () => {
    source.close();
    status.textContent = "Terminado";
  });
  source.onerror = () => {
    if (source.readyState === EventSource.CLOSED || seen === 0) {
      source.close();
      status.textContent = seen === 0 ? "No hay razonamiento guardado." : "Conexión cerrada.";
    }
  };
})();
</script>"""

_CONSOLE_SCRIPT = """<script>
(() => {
  const output = document.getElementById("console");
  if (!output) {
    return;
  }
  const source = new EventSource(output.dataset.endpoint);
  source.addEventListener("line", (event) => {
    const line = JSON.parse(event.data);
    output.textContent += line.text + "\\n";
    output.scrollTop = output.scrollHeight;
  });
  source.addEventListener("end", () => source.close());
})();
</script>"""

# El intervalo vive fuera del <ol>: al refrescar los pasos no se reinicia el temporizador.
_FICHA_SCRIPT = """<script>
(() => {
  const panel = document.getElementById("detail-panel");
  const button = document.getElementById("maximize");
  if (panel && button) {
    const apply = (on) => {
      panel.classList.toggle("is-max", on);
      button.setAttribute("aria-pressed", on ? "true" : "false");
      button.textContent = on ? "Restaurar" : "Maximizar";
    };
    button.addEventListener("click", () => apply(!panel.classList.contains("is-max")));
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        apply(false);
      }
    });
  }
  const list = document.querySelector("ol.steps");
  if (!list || list.dataset.state === "completed") {
    return;
  }
  const signature = (node) => [...node.querySelectorAll("li")].map((item) =>
    `${item.dataset.step}|${item.className}|${item.textContent}`).join("\\n");
  let busy = false;
  let timer = 0;
  const stop = () => {
    if (timer) {
      clearInterval(timer);
      timer = 0;
    }
  };
  const tick = async () => {
    const current = document.querySelector("ol.steps");
    if (!current || current.dataset.state === "completed") {
      stop();
      return;
    }
    if (busy) {
      return;
    }
    busy = true;
    try {
      const response = await fetch(location.pathname, {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "text/html" },
      });
      if (!response.ok) {
        return;
      }
      const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
      const next = parsed.querySelector("ol.steps");
      if (!next || !current.isConnected || signature(current) === signature(next)) {
        return;
      }
      current.replaceWith(next);
      if (next.dataset.state === "completed") {
        stop();
      }
    } catch (cause) {
      /* la ficha sigue mostrando el ultimo recorrido conocido */
    } finally {
      busy = false;
    }
  };
  timer = setInterval(tick, 2000);
})();
</script>"""

_ELAPSED_SCRIPT = """<script>
(() => {
  const label = (ms) => {
    const total = Math.max(0, Math.floor(ms / 1000));
    const days = Math.floor(total / 86400);
    const hours = Math.floor((total % 86400) / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = total % 60;
    if (days) return `${days} d ${hours} h`;
    if (hours) return `${hours} h ${minutes} min`;
    if (minutes) return `${minutes} min ${seconds} s`;
    return `${seconds} s`;
  };
  const tick = () => {
    document.querySelectorAll(".elapsed").forEach((node) => {
      const started = Date.parse(node.dataset.started || "");
      if (Number.isNaN(started)) {
        return;
      }
      node.textContent = label(Date.now() - started);
    });
  };
  tick();
  setInterval(tick, 1000);
})();
</script>"""
