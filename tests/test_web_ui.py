"""UI del panel de operacion (iniciativa 019, OP15/OP33).

Monta el `router` de la UI en una app de prueba con un read model en memoria (sin
Redis ni Postgres, como `test_web.py`). Cubre las cuatro cosas del contrato: la vista
global pinta cada seccion, el detalle pinta el recorrido, los botones dependen del
estado y del rol (`lector` no ve ninguno) y el HTML escapa los datos de terceros.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_review.domain.trace import ReviewTrace, TraceNode
from agent_review.settings import get_settings
from agent_review.web.read_model import (
    ActiveNode,
    ActiveReview,
    BudgetItem,
    DeadLetterItem,
    PanelState,
    PausedGroups,
    PausedItem,
    QueueItem,
)
from agent_review.web.ui import router

_MOMENT = datetime(2026, 10, 16, 10, 0, tzinfo=UTC)
_REVIEW_KEY = "cqm/emotivoo-v2:42:abc:7"
_HUMAN_KEY = "cqm/emotivoo-v2:40:aaa:5"
_FIX_KEY = "cqm/emotivoo-v2:39:bbb:4"
_DEAD_KEY = "cqm/emotivoo-v2:38:ccc:3"
# Carga maliciosa en un campo del PR (terceros): el HTML debe servirla escapada.
_XSS = "<script>alert(1)</script>"


class FakeReadModel:
    """Read model en memoria: aisla la UI de Redis y Postgres."""

    def __init__(self, state: PanelState, traces: dict[str, ReviewTrace] | None = None) -> None:
        self.panel_state = state
        self.traces = traces or {}

    def state(self) -> PanelState:
        return self.panel_state

    def trace(self, review_key: str) -> ReviewTrace | None:
        return self.traces.get(review_key)


@dataclass
class Panel:
    """App de prueba con la UI montada y su read model fake."""

    client: TestClient
    model: FakeReadModel


@pytest.fixture
def panel(monkeypatch: pytest.MonkeyPatch) -> Panel:
    app = FastAPI()
    app.include_router(router)
    model = FakeReadModel(_state(), {_REVIEW_KEY: _trace(state="completed")})
    monkeypatch.setattr(app.state, "read_model", model, raising=False)
    return Panel(TestClient(app), model)


def _state() -> PanelState:
    return PanelState(
        generated_at=_MOMENT,
        queue=[
            QueueItem(
                review_key=_REVIEW_KEY,
                repo="cqm/emotivoo-v2",
                pr_id=42,
                head_sha="abc",
                pipeline_id="7",
                attempts=1,
            )
        ],
        active=[
            ActiveReview(
                review_key="cqm/emotivoo-v2:41:def:6",
                nodes=[ActiveNode(node="persona", started_at=_MOMENT, branches=3)],
            )
        ],
        paused=PausedGroups(
            human=[
                PausedItem(
                    review_key=_HUMAN_KEY,
                    kind="human",
                    created_at=_MOMENT,
                    state="PAUSED",
                    pending=[{"finding_id": "f1"}],
                    command_hint="agent_review decide …",
                )
            ],
            fix=[
                PausedItem(
                    review_key=_FIX_KEY,
                    kind="fix",
                    created_at=_MOMENT,
                    state="VERIFYING",
                    jobs=[{"run_key": "r:f2:1", "finding_id": "f2"}],
                )
            ],
        ),
        dead=[
            DeadLetterItem(
                review_key=_DEAD_KEY,
                attempts=3,
                reason=_XSS,
                discarded_at=_MOMENT,
            )
        ],
        budget=[
            BudgetItem(
                review_key=_REVIEW_KEY, committed=1200, reserved=100, total=5000, remaining=3700
            )
        ],
    )


def _trace(state: str, pause_kind: str | None = None, review_key: str = _REVIEW_KEY) -> ReviewTrace:
    return ReviewTrace(
        review_key=review_key,
        thread_id=review_key,
        state=state,  # type: ignore[arg-type]
        nodes=[
            TraceNode(name="scope", index=0, started_at=_MOMENT, writes=["scope"]),
            TraceNode(name="persona", index=1, started_at=_MOMENT),
        ],
        next=["verify"] if state != "completed" else [],
        pause_kind=pause_kind,  # type: ignore[arg-type]
        updated_at=_MOMENT,
    )


# --- Vista global (OP15) ---------------------------------------------------------


def test_la_vista_global_pinta_las_secciones(panel: Panel) -> None:
    response = panel.client.get("/")
    assert response.status_code == 200
    body = response.text
    for title in (
        "Cola",
        "En vuelo",
        "Pausas humanas",
        "Pausas de fix",
        "Descartes",
        "Presupuesto",
    ):
        assert title in body, f"falta la seccion {title}"
    assert _REVIEW_KEY in body
    assert _HUMAN_KEY in body
    assert _FIX_KEY in body
    assert _DEAD_KEY in body
    assert "persona" in body, "el nodo en vuelo"


def test_la_vista_global_enlaza_al_detalle(panel: Panel) -> None:
    body = panel.client.get("/").text
    assert f'href="/review/{_REVIEW_KEY}"' in body


def test_el_panel_no_delega_el_actor_en_el_navegador(panel: Panel) -> None:
    """El actor auditado lo pone el servidor desde la sesion, no el HTML."""
    body = panel.client.get("/").text
    assert "data-actor" not in body
    assert "Sesión: dev (operador)" in body


# --- Detalle y recorrido (OP15) --------------------------------------------------


def test_el_detalle_pinta_el_recorrido(panel: Panel) -> None:
    panel.model.traces[_REVIEW_KEY] = _trace(state="in_progress")
    response = panel.client.get(f"/review/{_REVIEW_KEY}")
    assert response.status_code == 200
    body = response.text
    assert "Recorrido" in body
    assert "scope" in body
    assert "persona" in body
    assert "Siguiente" in body or "siguiente" in body


def test_el_detalle_sin_recorrido_es_404(panel: Panel) -> None:
    assert panel.client.get("/review/no:existe").status_code == 404


# --- Botones segun estado y rol (OP33) -------------------------------------------


def test_el_operador_ve_los_tres_comandos(panel: Panel) -> None:
    body = panel.client.get("/").text
    assert 'data-command="cancel"' in body
    assert 'data-command="retry"' in body
    assert 'data-command="resume"' in body


def test_la_review_completada_no_ofrece_comandos(panel: Panel) -> None:
    body = panel.client.get(f"/review/{_REVIEW_KEY}").text
    assert "data-command" not in body


def test_la_pausa_humana_ofrece_cancelar_la_review_y_reanudar(panel: Panel) -> None:
    panel.model.traces[_HUMAN_KEY] = _trace(
        state="paused", pause_kind="human", review_key=_HUMAN_KEY
    )
    body = panel.client.get(f"/review/{_HUMAN_KEY}").text
    assert "Cancelar la review" in body
    assert "Reanudar" in body
    assert "Cancelar lo pendiente" not in body


def test_la_pausa_de_fix_ofrece_cancelar_lo_pendiente_y_su_consola(panel: Panel) -> None:
    panel.model.traces[_FIX_KEY] = _trace(state="paused", pause_kind="fix", review_key=_FIX_KEY)
    body = panel.client.get(f"/review/{_FIX_KEY}").text
    assert "Cancelar lo pendiente" in body
    assert "Cancelar la review" not in body, "ya publico: los comentarios se quedan (W6/R2)"
    assert 'id="console"' in body
    assert f'data-endpoint="/api/reviews/{_FIX_KEY}/console"' in body


def test_el_lector_no_ve_botones(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "panel_session_secret", "ana:lector")
    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr(app.state, "read_model", FakeReadModel(_state()), raising=False)
    client = TestClient(app)

    assert client.get("/").status_code == 401, "sin sesion no hay panel"
    response = client.get("/", headers={"X-Panel-Session": "ana:lector"})
    assert response.status_code == 200
    assert "data-command" not in response.text


# --- Escapado de datos de terceros -----------------------------------------------


def test_el_html_escapa_un_script_inyectado_en_un_campo_del_pr(panel: Panel) -> None:
    body = panel.client.get("/").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert _XSS not in body


# --- Razonamiento (021 RZ9) ---


def test_el_detalle_lleva_la_pestana_de_razonamiento(panel: Panel) -> None:
    body = panel.client.get(f"/review/{_REVIEW_KEY}").text
    assert "<h2>Razonamiento</h2>" in body
    assert f'data-endpoint="/api/reviews/{_REVIEW_KEY}/reasoning"' in body
    assert "new EventSource(root.dataset.endpoint)" in body
    # Los eventos de lote fallido viajan como `fallo`, no como el `error` nativo.
    assert 'addEventListener("fallo"' in body


def test_el_texto_del_modelo_nunca_se_inserta_como_html(panel: Panel) -> None:
    # El razonamiento repite codigo del PR: se pinta con `textContent`, jamas con
    # `innerHTML`, para que un `<script>` en un diff no se ejecute en el panel.
    body = panel.client.get(f"/review/{_REVIEW_KEY}").text
    script = body[body.index('const root = document.getElementById("reasoning")') :]
    script = script[: script.index("</script>")]
    assert "innerHTML" not in script
    assert "textContent += ev.text" in script


# --- Histórico (021 RZ11) ---


def _history(*summaries):
    from agent_review.history_store import InMemoryReviewHistory

    history = InMemoryReviewHistory()
    for summary in summaries:
        history.record(summary)
    return history


def _summary(key: str, *, repo: str = "cqm/emotivoo-v2", pr_id: int = 42, hours_ago: int = 0, **kw):
    from datetime import UTC, datetime, timedelta

    from agent_review.domain.history import ReviewEnd, ReviewSummary

    base = {
        "review_key": key,
        "repo": repo,
        "pr_id": pr_id,
        "head_sha": "abcdef123456",
        "finished_at": datetime(2026, 10, 17, 12, tzinfo=UTC) - timedelta(hours=hours_ago),
        "end": ReviewEnd.COMPLETE,
    }
    return ReviewSummary(**(base | kw))


def test_el_historico_lista_las_reviews_y_enlaza_al_detalle(
    panel: Panel, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_review.domain.history import ReviewEnd

    history = _history(
        _summary(_REVIEW_KEY, hours_ago=1, findings=2, by_severity={"P1": 1, "P3": 1}),
        _summary("otra:1:x", pr_id=7, end=ReviewEnd.PARTIAL, reasoning=True),
    )
    monkeypatch.setattr(panel.client.app.state, "history", history, raising=False)

    body = panel.client.get("/history").text

    assert "Histórico de reviews" in body
    assert f'href="/review/{_REVIEW_KEY}"' in body
    assert "(P1: 1, P3: 1)" in body
    assert 'class="end end-parcial"' in body
    # La más reciente primero.
    assert body.index("otra:1:x") < body.index(_REVIEW_KEY)


def test_el_historico_filtra_y_escapa(panel: Panel, monkeypatch: pytest.MonkeyPatch) -> None:
    history = _history(
        _summary("a:1:x", repo="<b>malo</b>"),
        _summary("b:2:y", repo="otro"),
    )
    monkeypatch.setattr(panel.client.app.state, "history", history, raising=False)

    body = panel.client.get("/history", params={"repo": "<b>malo</b>"}).text
    assert "<b>malo</b>" not in body
    assert "&lt;b&gt;malo&lt;/b&gt;" in body
    assert "b:2:y" not in body


def test_sin_redis_el_historico_sale_vacio(panel: Panel, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(panel.client.app.state, "history", None, raising=False)
    response = panel.client.get("/history")
    assert response.status_code == 200
    assert "Sin entradas." in response.text


def test_la_vista_global_enlaza_al_historico(panel: Panel) -> None:
    assert 'href="/history"' in panel.client.get("/").text
