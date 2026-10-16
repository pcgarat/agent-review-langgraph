"""UI del panel de operacion (iniciativa 019, OP15/OP33).

HTML servido en servidor (SSR) sobre el `PanelReadModel`: la vista global pinta el
estado operativo (cola, en vuelo con su presupuesto, pausas por `kind`, descartes y
presupuesto por review) y el detalle pinta el recorrido del hilo. Los comandos
(cancelar, reintentar, reanudar) son botones que solo ve el rol `operador`; el actor
auditado lo pone el servidor desde la sesion, no el navegador. La consola de una
pausa de fix sigue el SSE del panel.

Sin plantillas ni CDN (018 §3.5): el HTML, el CSS y el JavaScript van embebidos y
autocontenidos. Los datos que llegan del PR son de terceros, asi que **todo** lo que
se interpola pasa antes por `html.escape`.
"""

from __future__ import annotations

import html
import json
from collections.abc import Iterable
from datetime import datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from agent_review.domain.trace import ReviewTrace
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
    """Detalle de una review: recorrido y, si es pausa de fix, consola en vivo (OP15)."""
    trace = model.trace(review_key)
    if trace is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Sin recorrido para {review_key}")
    return HTMLResponse(_detail_page(trace, _paused_item(model.state(), review_key), session))


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
        body=body,
        session=session,
        script=_COMMAND_SCRIPT,
    )


def _detail_page(trace: ReviewTrace, paused: PausedItem | None, session: Session) -> str:
    body = "".join(
        (
            '<p class="breadcrumb"><a href="/">&larr; Volver al panel</a></p>',
            _trace_section(trace),
            _detail_commands(trace, session),
            _console_section(trace, paused),
        )
    )
    return _page(
        title=f"Review {trace.review_key}",
        meta=f"Estado {trace.state} · actualizado el {_fmt_dt(trace.updated_at)}",
        body=body,
        session=session,
        script=_COMMAND_SCRIPT + _CONSOLE_SCRIPT,
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
    text = f"{_esc(node.node)} desde {_fmt_dt(node.started_at)}"
    if node.branches > 1:
        text += f" ({_esc(node.branches)} ramas)"
    return text


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
