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
import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse

from agent_review.config_store import ConfigReadError, ConfigStore, Publication
from agent_review.domain.history import ReviewSummary
from agent_review.domain.models import PendingDecision
from agent_review.domain.trace import ReviewTrace
from agent_review.history_store import ReviewHistory
from agent_review.panel_config import SECTIONS, catalog, initial_values, lock_names
from agent_review.settings import Settings, get_settings
from agent_review.web.auth import Session, require_command_session, require_session
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

logger = logging.getLogger(__name__)

_CANCEL = "cancel"
_RETRY = "retry"
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


@router.get("/config", response_class=HTMLResponse)
def config_page(
    session: Annotated[Session, Depends(require_session)],
    request: Request,
    model: Annotated[PanelReadModel, Depends(get_read_model)],
) -> HTMLResponse:
    """Diales que el operador puede publicar (026)."""
    settings = get_settings()
    try:
        publication = _config_store(request, settings).read()
    except ConfigReadError as exc:
        # El panel es observabilidad: sin Postgres, decirlo; no un 500 con traza.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return HTMLResponse(_config_page(settings, publication, session, pins=_dial_pins(model)))


@router.get("/api/config")
def config_document(
    session: Annotated[Session, Depends(require_session)],
    request: Request,
) -> dict[str, object]:
    """El documento vigente en JSON, para un cliente que no pinta HTML (026).

    La página es para una persona; esta lectura permite a un `curl` o al CLI saber
    qué hay publicado sin parsear el formulario.
    """
    del session  # cualquier rol lee: el documento no es secreto
    settings = get_settings()
    store = _config_store(request, settings)
    try:
        publication = store.read()
    except ConfigReadError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return {
        "revision": publication.revision,
        "values": publication.overrides,
        "initial": initial_values(settings),
        "locked": sorted(lock_names(settings)),
        "actor": publication.actor,
        "updated_at": publication.updated_at.isoformat() if publication.updated_at else None,
    }


@router.post("/config")
async def save_config(
    request: Request,
    session: Annotated[Session, Depends(require_command_session)],
) -> dict[str, int]:
    """Publica el formulario entero. Una revisión vieja no escribe."""
    from agent_review.panel_config import (
        ConfigError,
        RevisionConflict,
        StaleForm,
        document_for,
    )

    revision, values, painted = await _config_form(request)
    settings = get_settings()
    try:
        document = document_for(
            settings,
            values,
            autonomy_ceiling=lambda: _autonomy_ceiling(settings),
            painted=painted,
        )
        published = _config_store(request, settings).publish(
            expected=revision, overrides=document, actor=session.user
        )
    except (RevisionConflict, StaleForm) as exc:
        # Las dos se resuelven recargando: la revisión la movió otro y el formulario
        # se pintó con otros valores iniciales. El cuerpo lleva cuál es la vigente.
        detail: dict[str, object] = {"detail": str(exc)}
        if isinstance(exc, RevisionConflict):
            detail["revision"] = exc.current
        else:
            detail["stale"] = True
        raise HTTPException(status.HTTP_409_CONFLICT, detail) from exc
    except ConfigError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, {"detail": str(exc), "field": exc.field}
        ) from exc
    except ConfigReadError as exc:
        # La escritura no llegó a confirmarse: es la infraestructura, no el
        # formulario. Un 400 culparía al operador de un Postgres caído.
        logger.warning("No se pudo publicar la configuración: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "No se pudo guardar la configuración"
        ) from exc
    return {"revision": published.revision}


async def _config_form(request: Request) -> tuple[int, dict[str, object], dict[str, object] | None]:
    """Cuerpo del formulario, con `400` propio.

    FastAPI tiparía el cuerpo como objeto y un array daría `422` con el detalle de
    pydantic; el resto de comandos del panel responde `400` para un cuerpo que no
    encaja, así que aquí se parsea igual.
    """
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El cuerpo no es JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "el cuerpo debe traer revision y values")
    revision = body.get("revision")
    values = body.get("values")
    if not isinstance(revision, int) or isinstance(revision, bool) or not isinstance(values, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "el cuerpo debe traer revision y values")
    painted = body.get("initial")
    if painted is not None and not isinstance(painted, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "initial debe ser un objeto")
    return revision, values, painted


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


def _config_store(request: Request, settings: Settings) -> ConfigStore:
    store: ConfigStore | None = getattr(request.app.state, "config_store", None)
    if store is None:
        from agent_review.config_store import build_config_store

        store = build_config_store(settings)
        request.app.state.config_store = store
    return store


def build_config_store_for_panel(settings: Settings) -> ConfigStore:
    """Almacén del panel, validado en el arranque (026, ADR-0029).

    El candado se resuelve aquí y no en la primera petición: un nombre fuera del
    catálogo tiene que abortar el arranque, como en el worker. Construir también el
    almacén deja el fallo de Postgres en el mismo sitio.
    """
    from agent_review.config_store import build_config_store
    from agent_review.panel_config import lock_names

    lock_names(settings)
    return build_config_store(settings)


def _autonomy_ceiling(settings: Settings) -> str:
    """Techo de `max_autonomy` declarado en el YAML, leído con el motor de política.

    Se resuelve solo al validar un override: el resto de guardados no depende de
    que este proceso pueda leer `config/autonomy.yaml`.
    """
    from agent_review.panel_config import ConfigError
    from agent_review.policy.engine import PolicyEngine, PolicyError

    try:
        return str(PolicyEngine.from_file(settings.autonomy_config).raw()["max_autonomy"])
    except OSError as exc:
        raise ConfigError(
            "max_autonomy_override", f"no se pudo leer {settings.autonomy_config}"
        ) from exc
    except (PolicyError, ValueError, TypeError) as exc:
        raise ConfigError("max_autonomy_override", f"autonomy.yaml inválido: {exc}") from exc


def _dial_pins(model: object) -> list[tuple[str, int]] | None:
    """Fotos fijadas por las reviews en curso; `None` si no se pudieron leer."""
    reader = getattr(model, "dial_pins", None)
    if not callable(reader):
        return []
    found = reader()
    if found is None:
        return None
    if not isinstance(found, list):
        return []
    pins: list[tuple[str, int]] = []
    for item in found:
        if (
            isinstance(item, tuple)
            and len(item) == 2
            and isinstance(item[0], str)
            and isinstance(item[1], int)
            and not isinstance(item[1], bool)
        ):
            pins.append((item[0], item[1]))
    return pins


def _config_page(
    settings: Settings,
    publication: Publication,
    session: Session,
    *,
    pins: list[tuple[str, int]] | None = None,
) -> str:
    initial = initial_values(settings)
    locked = lock_names(settings)
    by_section: dict[str, list[str]] = {section: [] for section in SECTIONS}
    for field in catalog():
        if field.name in locked:
            # Un dial bloqueado se pinta con su valor inicial, no con el override
            # publicado que `apply_document` descarta: enseñar un número que la
            # review no usa es peor que enseñar el del despliegue.
            shown, blocked = initial[field.name], True
        else:
            shown, blocked = publication.overrides.get(field.name, initial[field.name]), False
        by_section[field.section].append(_dial_control(field, shown, initial[field.name], blocked))
    sections = "".join(
        _section(section, "".join(controls)) for section, controls in by_section.items() if controls
    )
    save = ""
    if session.can_command():
        save = '<button id="save-config" type="button">Guardar</button>'
    inflight = "".join(
        f"<li>{_esc(key)} fijó la revisión {revision}.</li>" for key, revision in pins or []
    )
    if pins is None:
        inflight_html = (
            '<p class="meta">No se pudo leer la foto de las reviews en curso '
            "(el checkpointer no respondió).</p>"
        )
    else:
        inflight_html = f"<ul>{inflight}</ul>" if inflight else ""
    note = f"<p>Revisión publicada: {publication.revision}.</p>{inflight_html}"
    # Los valores iniciales viajan con el formulario: el servidor compara contra
    # lo que se pinto, no contra los defaults que tenga en ese momento.
    painted = json.dumps(initial, ensure_ascii=False, default=str)
    body = (
        '<p class="breadcrumb"><a href="/">&larr; Volver al panel</a></p>'
        f"{note}"
        f'<form id="config" data-revision="{publication.revision}" '
        f'data-initial="{_esc(painted)}">{sections}{save}</form>'
    )
    return _page(
        title="Configuración",
        meta="Cada control vuelve a su valor inicial con Restaurar. Guardar publica la revisión.",
        body=body,
        session=session,
        script=_CONFIG_SCRIPT,
    )


def _dial_control(field: object, shown: object, initial: object, locked: bool) -> str:
    from agent_review.panel_config import DialField

    if not isinstance(field, DialField):
        raise TypeError("field")
    kind = "bool" if isinstance(initial, bool) else "int" if isinstance(initial, int) else "text"
    disabled = " disabled" if locked else ""
    control_id = f"dial-{field.name}"
    same = shown == initial
    restore_disabled = " disabled" if locked or same else ""
    if kind == "bool":
        checked = " checked" if shown else ""
        control = (
            f'<input id="{control_id}" type="checkbox" data-kind="bool" '
            f'data-name="{_esc(field.name)}"'
            f' data-initial="{"true" if initial else "false"}"{checked}{disabled}>'
        )
    else:
        control = (
            f'<input id="{control_id}" data-kind="{kind}" data-name="{_esc(field.name)}"'
            f' data-initial="{_esc(initial)}" value="{_esc(shown)}"{disabled}>'
        )
    reason = " Lo fija el despliegue." if locked else ""
    return (
        f'<p><label for="{control_id}">{_esc(field.name)}</label> {control} '
        f'<button type="button" class="restore" data-for="{control_id}"{restore_disabled}>'
        f'Restaurar</button><br><span class="meta">{_esc(field.help)}{reason}</span></p>'
    )


_CONFIG_SCRIPT = """<script>
(() => {
  const form = document.getElementById("config");
  if (!form) return;
  const error = document.getElementById("error");
  const controls = [...form.querySelectorAll("[data-kind]")];
  const current = (control) => (
    control.dataset.kind === "bool" ? String(control.checked) : control.value
  );
  const sync = (control) => {
    const button = form.querySelector(`button.restore[data-for="${control.id}"]`);
    if (!button || control.disabled) return;
    button.disabled = current(control) === control.dataset.initial;
  };
  controls.forEach((control) => {
    control.addEventListener("input", () => sync(control));
    const button = form.querySelector(`button.restore[data-for="${control.id}"]`);
    if (!button) return;
    button.addEventListener("click", () => {
      if (control.dataset.kind === "bool") control.checked = control.dataset.initial === "true";
      else control.value = control.dataset.initial;
      sync(control);
    });
  });
  const save = document.getElementById("save-config");
  if (!save) return;
  save.addEventListener("click", async () => {
    save.disabled = true;
    error.hidden = true;
    const values = {};
    controls.forEach((control) => {
      // Un control bloqueado no se publica: su valor lo fija el despliegue y el
      // servidor lo descartaria. Enviarlo solo confunde el diff.
      if (control.disabled) return;
      const name = control.dataset.name;
      if (control.dataset.kind === "bool") values[name] = control.checked;
      // Un entero vacio no es 0: publicarlo apagaria el presupuesto en silencio.
      // Se envia sin recortar y el servidor responde 400 nombrando el campo.
      else if (control.dataset.kind === "int") values[name] = control.value.trim();
      else values[name] = control.value;
    });
    try {
      const response = await fetch("/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          revision: Number(form.dataset.revision),
          values,
          initial: JSON.parse(form.dataset.initial || "{}"),
        }),
      });
      if (response.status === 409) {
        error.textContent =
          "Alguien guardó antes o la página es de otra versión. Recarga la página.";
        error.hidden = false;
        save.disabled = false;
        return;
      }
      if (!response.ok) {
        error.textContent = await response.text();
        error.hidden = false;
        save.disabled = false;
        return;
      }
      window.location.reload();
    } catch (cause) {
      error.textContent = String(cause);
      error.hidden = false;
      save.disabled = false;
    }
  });
})();
</script>"""


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
        body=(
            '<p class="breadcrumb"><a href="/history">Histórico de reviews &rarr;</a>'
            ' · <a href="/config">Configuración &rarr;</a></p>' + body
        ),
        session=session,
        script=_COMMAND_SCRIPT,
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


def _detail_page(trace: ReviewTrace, paused: PausedItem | None, session: Session) -> str:
    body = "".join(
        (
            '<p class="breadcrumb"><a href="/">&larr; Volver al panel</a></p>',
            _trace_section(trace),
            _detail_commands(trace, session),
            _decision_section(trace, paused, session),
            _console_section(trace, paused),
            _reasoning_section(trace),
        )
    )
    return _page(
        title=f"Review {trace.review_key}",
        meta=f"Estado {trace.state} · actualizado el {_fmt_dt(trace.updated_at)}",
        body=body,
        session=session,
        script=_COMMAND_SCRIPT + _DECISION_SCRIPT + _CONSOLE_SCRIPT + _REASONING_SCRIPT,
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


def _decision_section(trace: ReviewTrace, paused: PausedItem | None, session: Session) -> str:
    """Formulario de decision de una pausa humana (022 H4/§5.5).

    Un control por pendiente y una nota por hallazgo. La completitud la valida el
    servidor (`build_decision`); el navegador no la reimplementa. Solo el rol
    `operador` lo ve.
    """
    if not _trace_decision(trace, paused) or paused is None:
        return ""
    if not session.can_command():
        return (
            '<section><h2>Decisión</h2><p class="empty">Solo el rol operador decide.</p></section>'
        )
    endpoint = f"/api/reviews/{quote(trace.review_key, safe='/:')}/decision"
    rows = [
        "<tr>"
        f"<td><strong>{_esc(item.title)}</strong><br>"
        f'<span class="meta">{_esc(item.severity.value)} · '
        f"{_esc(item.proposed_action.value)} · {_esc(item.file)}:{_esc(item.line)}</span><br>"
        f"{_esc(item.why_it_matters)}</td>"
        f'<td><select name="outcome" data-finding="{_esc(item.finding_id)}">'
        f'<option value="approved">Aprobar</option>'
        f'<option value="rejected">Rechazar</option>'
        f'<option value="noted">Anotar</option></select></td>'
        f'<td><input name="note" data-finding="{_esc(item.finding_id)}" '
        f'placeholder="nota opcional"></td>'
        "</tr>"
        for item in paused.pending
    ]
    table = _table(["Hallazgo", "Resultado", "Nota"], rows)
    hint = (
        '<p class="meta">Aprobar solo autoriza a <em>intentar</em> el arreglo: los '
        "temas protegidos y las rutas nunca-auto siguen mandando (ADR-0009).</p>"
    )
    return (
        "<section><h2>Decisión</h2>"
        f"{hint}{table}"
        '<p><button type="button" class="decide" id="decide" '
        f'data-endpoint="{_esc(endpoint)}">Decidir</button></p>'
        "</section>"
    )


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
    """Comandos del detalle segun el estado del hilo (spec §5.4/§5.5).

    Una pausa humana no ofrece «Reanudar»: decidir es la accion (022 §5.5, Q1); el
    `expired` es del sistema (TTL).
    """
    if trace.state == "completed":
        return ()
    if trace.pause_kind == "fix":
        return ((_CANCEL, _CANCEL_LABEL_PENDING),)
    if trace.pause_kind == "human":
        return ((_CANCEL, "Cancelar la review"),)
    return ((_CANCEL, "Cancelar"),)


def _trace_decision(trace: ReviewTrace, paused: PausedItem | None) -> bool:
    """¿El detalle debe pintar el formulario de decision? (022)"""
    return trace.pause_kind == "human" and paused is not None and paused.kind == "human"


def _pause_commands(item: PausedItem) -> tuple[tuple[str, str], ...]:
    if item.kind == "fix":
        return ((_CANCEL, _CANCEL_LABEL_PENDING),)
    return ((_CANCEL, "Cancelar la review"),)


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
        parts.append(_pending_list(item.pending))
    if item.jobs:
        parts.append(_inline_list("Run keys", (_json(job.get("run_key", "")) for job in item.jobs)))
    if item.command_hint:
        parts.append(f"<code>{_esc(item.command_hint)}</code>")
    return "".join(parts) or "—"


def _pending_list(pending: list[PendingDecision]) -> str:
    """Pendientes de una pausa humana, legibles para decidir sin abrir el PR (022 H3).

    Muestra lo que `PendingDecision` ya trae; no se inventa contexto. El enlace a
    `code_url` solo aparece si el worker lo resolvio.
    """
    rows = []
    for item in pending:
        where = f"{_esc(item.file)}:{_esc(item.line)}"
        link = (
            f' <a href="{_esc(item.code_url)}" rel="noreferrer noopener">código</a>'
            if item.code_url
            else ""
        )
        meta = f"{_esc(item.severity.value)} · {_esc(item.proposed_action.value)} · {where}{link}"
        rows.append(
            f'<li data-finding="{_esc(item.finding_id)}">'
            f"<strong>{_esc(item.title)}</strong><br>"
            f'<span class="meta">{meta}</span><br>'
            f"{_esc(item.why_it_matters)}</li>"
        )
    return f'<span class="tag">{_esc("Pendientes")}</span><ul class="pending">{"".join(rows)}</ul>'


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
button.cmd, button.decide { font: inherit; padding: .25rem .6rem; border: 1px solid var(--line);
  border-radius: .35rem; cursor: pointer; }
button.cmd:disabled, button.decide:disabled { opacity: .5; cursor: default; }
ul.pending { margin: .25rem 0 .25rem 1rem; padding: 0; }
ul.pending li { margin: 0 0 .5rem; }
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

_DECISION_SCRIPT = """<script>
(() => {
  const button = document.getElementById("decide");
  if (!button) {
    return;
  }
  const error = document.getElementById("error");
  button.addEventListener("click", async () => {
    const items = [...document.querySelectorAll("select[data-finding]")].map((select) => {
      const sel = `input[data-finding="${CSS.escape(select.dataset.finding)}"]`;
      const note = document.querySelector(sel);
      return { finding_id: select.dataset.finding, outcome: select.value,
               note: note ? note.value : "" };
    });
    button.disabled = true;
    error.hidden = true;
    try {
      const response = await fetch(button.dataset.endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items }),
        credentials: "same-origin",
      });
      if (!response.ok) {
        throw new Error((await response.text()) || response.statusText);
      }
      button.textContent = "Decidido";
    } catch (cause) {
      error.textContent = String(cause);
      error.hidden = false;
      button.disabled = false;
    }
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
