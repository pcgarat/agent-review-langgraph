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

from agent_review.domain.models import FindingAction, PendingDecision, Severity
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


def _pending(finding_id: str = "f1", **overrides: object) -> PendingDecision:
    base = PendingDecision(
        finding_id=finding_id,
        proposed_action=FindingAction.ESCALATE,
        reason="r",
        matched_rule="rule",
        policy_version="1",
        title="Falta validar el DNI",
        file="src/App.php",
        line=10,
        severity=Severity.P1,
        why_it_matters="un DNI mal formado llega a la base de datos",
    )
    return base.model_copy(update=overrides)


class FakeReadModel:
    """Read model en memoria: aisla la UI de Redis y Postgres."""

    def __init__(self, state: PanelState, traces: dict[str, ReviewTrace] | None = None) -> None:
        self.panel_state = state
        self.traces = traces or {}
        self.pins: list[tuple[str, int]] = []

    def state(self) -> PanelState:
        return self.panel_state

    def trace(self, review_key: str) -> ReviewTrace | None:
        return self.traces.get(review_key)

    def dial_pins(self) -> list[tuple[str, int]]:
        return self.pins


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
                    pending=[_pending("f1")],
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


def test_el_operador_ve_los_comandos_de_la_vista_global(panel: Panel) -> None:
    body = panel.client.get("/").text
    assert 'data-command="cancel"' in body
    assert 'data-command="retry"' in body
    # 022 Q1: la vista global no ofrece «Reanudar» (decidir es la accion).
    assert 'data-command="resume"' not in body


def test_la_review_completada_no_ofrece_comandos(panel: Panel) -> None:
    body = panel.client.get(f"/review/{_REVIEW_KEY}").text
    assert "data-command" not in body


def test_la_pausa_humana_ofrece_decidir_y_cancelar_sin_reanudar(panel: Panel) -> None:
    panel.model.traces[_HUMAN_KEY] = _trace(
        state="paused", pause_kind="human", review_key=_HUMAN_KEY
    )
    body = panel.client.get(f"/review/{_HUMAN_KEY}").text
    assert "Cancelar la review" in body
    assert "Cancelar lo pendiente" not in body
    # 022 Q1: decidir es la accion; `expired` es del sistema.
    assert 'id="decide"' in body
    assert "Reanudar" not in body
    assert 'data-command="resume"' not in body


def test_el_formulario_de_decision_pinta_cada_pendiente(panel: Panel) -> None:
    panel.model.traces[_HUMAN_KEY] = _trace(
        state="paused", pause_kind="human", review_key=_HUMAN_KEY
    )
    body = panel.client.get(f"/review/{_HUMAN_KEY}").text
    assert "Falta validar el DNI" in body
    assert "un DNI mal formado llega a la base de datos" in body
    assert 'data-finding="f1"' in body
    for outcome in ("approved", "rejected", "noted"):
        assert f'value="{outcome}"' in body
    assert f'data-endpoint="/api/reviews/{_HUMAN_KEY}/decision"' in body
    assert "ADR-0009" in body, "el aviso de que aprobar solo autoriza a intentarlo"


def test_el_formulario_de_decision_escapa_el_texto_del_pr(panel: Panel) -> None:
    panel.model.state().paused.human[0].pending = [_pending("f1", title=_XSS, why_it_matters=_XSS)]
    panel.model.traces[_HUMAN_KEY] = _trace(
        state="paused", pause_kind="human", review_key=_HUMAN_KEY
    )
    body = panel.client.get(f"/review/{_HUMAN_KEY}").text
    assert _XSS not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_el_lector_no_ve_el_formulario_de_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    panel_state = _state()
    monkeypatch.setattr(get_settings(), "panel_session_secret", "ana:lector")
    app = FastAPI()
    app.include_router(router)
    traces = {_HUMAN_KEY: _trace(state="paused", pause_kind="human", review_key=_HUMAN_KEY)}
    monkeypatch.setattr(app.state, "read_model", FakeReadModel(panel_state, traces), raising=False)
    client = TestClient(app)

    body = client.get(f"/review/{_HUMAN_KEY}", headers={"X-Panel-Session": "ana:lector"}).text
    assert 'id="decide"' not in body
    assert "Solo el rol operador decide" in body


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


def test_la_pagina_de_configuracion_pinta_cada_dial_y_restaurar(
    panel: Panel, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_review.config_store import MemoryConfigStore
    from agent_review.panel_config import catalog

    monkeypatch.setattr(panel.client.app.state, "config_store", MemoryConfigStore(), raising=False)
    body = panel.client.get("/config").text
    fields = catalog()
    assert body.count(">Restaurar<") == len(fields)
    for field in fields:
        assert field.name in body
        assert field.help in body
    for section in ("Presupuesto y lotes", "Verificador", "Bucle de lectura", "Política"):
        assert section in body
    assert "Guardar" in body
    assert 'href="/config"' in panel.client.get("/").text


def test_la_pagina_nombra_la_revision_que_fijo_una_review_en_curso(
    panel: Panel, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_review.config_store import MemoryConfigStore

    monkeypatch.setattr(panel.client.app.state, "config_store", MemoryConfigStore(), raising=False)
    panel.model.pins = [("acme/app:7:abc", 4)]
    body = panel.client.get("/config").text
    assert "acme/app:7:abc fijó la revisión 4." in body


def test_guardar_publica_solo_la_clave_distinta_y_una_revision_vieja_no_escribe(
    panel: Panel, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_review.config_store import MemoryConfigStore
    from agent_review.panel_config import initial_values

    store = MemoryConfigStore()
    monkeypatch.setattr(panel.client.app.state, "config_store", store, raising=False)
    monkeypatch.setattr(get_settings(), "panel_config_lock", "")
    values = initial_values(get_settings())
    current = values["llm_max_findings"]
    chosen = 0 if current != 0 else 1
    values["llm_max_findings"] = chosen
    saved = panel.client.post("/config", json={"revision": 0, "values": values})
    assert saved.status_code == 200
    assert store.read().overrides == {"llm_max_findings": chosen}
    stale = panel.client.post("/config", json={"revision": 0, "values": values})
    assert stale.status_code == 409
    assert store.read().revision == 1
    values["llm_batch_retries"] = -1
    rejected = panel.client.post("/config", json={"revision": 1, "values": values})
    assert rejected.status_code == 400
    assert "llm_batch_retries" in rejected.text
    assert store.read().overrides == {"llm_max_findings": chosen}


def test_el_lector_no_puede_guardar(panel: Panel, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_review.config_store import MemoryConfigStore

    monkeypatch.setattr(panel.client.app.state, "config_store", MemoryConfigStore(), raising=False)
    monkeypatch.setattr(get_settings(), "panel_session_secret", "ana:lector")
    body = panel.client.get("/config", headers={"X-Panel-Session": "ana:lector"}).text
    assert 'id="save-config"' not in body
    assert "Restaurar" in body
    rejected = panel.client.post(
        "/config",
        json={"revision": 0, "values": {}},
        headers={"X-Panel-Session": "ana:lector"},
    )
    assert rejected.status_code == 403


def test_el_lector_de_trazas_saca_la_revision_de_la_foto() -> None:
    from agent_review.web.read_model import PgTraceReader

    class _Tuple:
        checkpoint = {"channel_values": {"dial_snapshot": {"revision": 4, "overrides": {}}}}

    class _Saver:
        def get_tuple(self, config: dict[str, object]) -> _Tuple | None:
            configurable = config["configurable"]
            assert isinstance(configurable, dict)
            if str(configurable["thread_id"]).endswith(":partial"):
                return None
            return _Tuple()

    reader = PgTraceReader(database_url="postgresql://unused")
    reader._resolved = True
    reader._saver = _Saver()  # type: ignore[assignment]
    assert reader.dial_revision("acme/app:1") == 4
