"""Panel de operacion: servicio `web` (iniciativa 019, Bloque 3).

Cubre los cuatro frentes del bloque: el read model (con `fakeredis`, sin Redis real),
los endpoints `GET /api/state` y `GET /api/trace/{key}` con un fake en memoria (sin
Redis ni Postgres, criterio del contrato), la sesion con rol (401/403) y el contrato
fail-closed del secreto del panel.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated

import fakeredis
import httpx
import pytest
import redis
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from agent_review.checkpointer import build_serde
from agent_review.domain.models import (
    ApprovalRequest,
    FindingAction,
    FixWaitRequest,
    PendingDecision,
    ReviewRequest,
    Severity,
    VerifyJob,
)
from agent_review.domain.paused import PausedReview, PauseKind
from agent_review.domain.trace import ReviewTrace, TraceNode
from agent_review.settings import (
    Settings,
    get_settings,
    insecure_secrets,
    panel_insecure_secrets,
    require_panel_secrets,
)
from agent_review.web import read_model
from agent_review.web.auth import Role, Session, parse_session, require_operador, verify_session
from agent_review.web.main import app
from agent_review.web.read_model import (
    ActiveNode,
    ActiveReview,
    BudgetItem,
    CompositePanelReadModel,
    DeadLetterItem,
    PanelReadModel,
    PanelState,
    PausedGroups,
    PausedItem,
    QueueItem,
    RedisPanelReadModel,
    build_panel_read_model,
)
from agent_review.web.runner_client import RunnerConsoleClient

client = TestClient(app)

_MOMENT = datetime(2026, 10, 16, 10, 0, tzinfo=UTC)
_REVIEW_KEY = "cqm/emotivoo-v2:42:abc:7"


def _pending(finding_id: str) -> PendingDecision:
    return PendingDecision(
        finding_id=finding_id,
        proposed_action=FindingAction.ESCALATE,
        reason="r",
        matched_rule="rule",
        policy_version="1",
        title=f"Hallazgo {finding_id}",
        file="src/App.php",
        line=10,
        severity=Severity.P1,
        why_it_matters="porque importa",
    )


class FakeReadModel:
    """Read model en memoria: aisla los endpoints de Redis y Postgres."""

    def __init__(self, state: PanelState, traces: dict[str, ReviewTrace] | None = None) -> None:
        self._state = state
        self._traces = traces or {}

    def state(self) -> PanelState:
        return self._state

    def trace(self, review_key: str) -> ReviewTrace | None:
        return self._traces.get(review_key)


def _fake_state() -> PanelState:
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
                    review_key="cqm/emotivoo-v2:40:aaa:5",
                    kind="human",
                    created_at=_MOMENT,
                    state="PAUSED",
                    pending=[_pending("f1")],
                    command_hint="agent_review decide …",
                )
            ],
            fix=[
                PausedItem(
                    review_key="cqm/emotivoo-v2:39:bbb:4",
                    kind="fix",
                    created_at=_MOMENT,
                    state="VERIFYING",
                    jobs=[{"run_key": "r:f2:1", "finding_id": "f2"}],
                )
            ],
        ),
        dead=[
            DeadLetterItem(
                review_key="cqm/emotivoo-v2:38:ccc:3",
                attempts=3,
                reason="twg caido",
                discarded_at=_MOMENT,
            )
        ],
        budget=[
            BudgetItem(
                review_key=_REVIEW_KEY, committed=1200, reserved=100, total=5000, remaining=3700
            )
        ],
    )


def _fake_trace() -> ReviewTrace:
    return ReviewTrace(
        review_key=_REVIEW_KEY,
        thread_id=_REVIEW_KEY,
        state="completed",
        nodes=[
            TraceNode(name="scope", index=0, started_at=_MOMENT),
            TraceNode(name="persona", index=1, started_at=_MOMENT),
        ],
        next=[],
        pause_kind=None,
        updated_at=_MOMENT,
    )


@pytest.fixture(autouse=True)
def _read_model(monkeypatch: pytest.MonkeyPatch) -> FakeReadModel:
    """Inyecta el fake en `app.state`: los endpoints no tocan Redis ni Postgres."""
    model = FakeReadModel(_fake_state(), traces={_REVIEW_KEY: _fake_trace()})
    monkeypatch.setattr(app.state, "read_model", model, raising=False)
    return model


# --- Endpoints de lectura (OP13/OP14) --------------------------------------------


def test_healthz_no_exige_sesion() -> None:
    assert client.get("/healthz").status_code == 200


def test_state_devuelve_las_secciones_del_panel() -> None:
    body = client.get("/api/state").json()
    assert body["queue"][0]["review_key"] == _REVIEW_KEY
    assert body["queue"][0]["attempts"] == 1
    active_node = body["active"][0]["nodes"][0]
    assert active_node["node"] == "persona"
    assert active_node["branches"] == 3
    assert str(active_node["started_at"]).startswith("2026-10-16T10:00:00")
    assert body["paused"]["human"][0]["pending"][0]["finding_id"] == "f1"
    assert body["paused"]["human"][0]["state"] == "PAUSED"
    assert body["paused"]["fix"][0]["jobs"][0]["run_key"] == "r:f2:1"
    assert body["dead"][0]["reason"] == "twg caido"
    assert body["budget"][0]["remaining"] == 3700


def test_state_sin_secreto_se_relaja() -> None:
    """Dev/test sin `PANEL_SESSION_SECRET`: se relaja, como el bearer de decision."""
    assert client.get("/api/state").status_code == 200


def test_trace_devuelve_el_recorrido() -> None:
    response = client.get(f"/api/trace/{_REVIEW_KEY}")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "completed"
    assert [node["name"] for node in body["nodes"]] == ["scope", "persona"]


def test_trace_de_una_clave_inexistente_es_404() -> None:
    assert client.get("/api/trace/no:existe").status_code == 404


# --- Sesion con rol (OP12/OP17) --------------------------------------------------


def test_sin_cabecera_valida_da_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "panel_session_secret", "ana:operador")
    assert client.get("/api/state").status_code == 401
    assert client.get(f"/api/trace/{_REVIEW_KEY}").status_code == 401
    # Una cabecera que no coincide con el secreto tampoco entra.
    assert client.get("/api/state", headers={"X-Panel-Session": "ana:lector"}).status_code == 401


def test_con_sesion_valida_se_lee(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "panel_session_secret", "ana:operador")
    ok = client.get("/api/state", headers={"X-Panel-Session": "ana:operador"})
    assert ok.status_code == 200


def test_parse_session() -> None:
    assert parse_session("ana:lector") == Session(user="ana", role=Role.LECTOR)
    assert parse_session("ana") is None
    assert parse_session(":lector") is None
    assert parse_session("ana:jefe") is None


def test_verify_session_relajada_sin_cabecera() -> None:
    assert verify_session(get_settings(), None).role is Role.OPERADOR


def test_verify_session_cabecera_mal_formada_da_401() -> None:
    with pytest.raises(HTTPException) as exc:
        verify_session(get_settings(), "ana:jefe")
    assert exc.value.status_code == 401


def _command_app() -> FastAPI:
    """App minima con un comando de operador, como los del bloque 5."""
    command_app = FastAPI()

    @command_app.post("/command")
    def command(session: Annotated[Session, Depends(require_operador)]) -> dict[str, str]:
        return {"user": session.user}

    return command_app


def test_lector_no_ejecuta_comandos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "panel_session_secret", "ana:lector")
    cmd = TestClient(_command_app())

    assert cmd.post("/command").status_code == 401, "sin sesion no hay comando"
    assert cmd.post("/command", headers={"X-Panel-Session": "ana:lector"}).status_code == 403


def test_operador_si_ejecuta_comandos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "panel_session_secret", "token-opaco")
    monkeypatch.setattr(get_settings(), "panel_session_user", "bob")
    monkeypatch.setattr(get_settings(), "panel_session_role", "operador")
    cmd = TestClient(_command_app())

    response = cmd.post("/command", headers={"X-Panel-Session": "token-opaco"})
    assert response.status_code == 200
    assert response.json()["user"] == "bob"
    guessed = cmd.post("/command", headers={"X-Panel-Session": "bob:operador"})
    assert guessed.status_code == 401


def test_el_usuario_publicado_no_sale_del_secreto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "panel_session_secret", "bob:operador")
    cmd = TestClient(_command_app())

    response = cmd.post("/command", headers={"X-Panel-Session": "bob:operador"})
    assert response.status_code == 200
    assert response.json()["user"] == "panel"


# --- Contrato fail-closed del secreto (OP18) -------------------------------------


def test_el_secreto_del_panel_entra_en_el_contrato_si_el_panel_esta_desplegado() -> None:
    assert "PANEL_SESSION_SECRET" in insecure_secrets(
        Settings(panel_enabled=True, panel_session_secret="change-me")
    )
    assert "PANEL_SESSION_SECRET" not in insecure_secrets(
        Settings(panel_enabled=False, panel_session_secret="")
    )


def test_panel_insecure_secrets() -> None:
    assert panel_insecure_secrets(Settings(panel_session_secret="")) == ["PANEL_SESSION_SECRET"]
    assert panel_insecure_secrets(Settings(panel_session_secret="change-me")) == [
        "PANEL_SESSION_SECRET"
    ]
    assert panel_insecure_secrets(Settings(panel_session_secret="ana:operador")) == []


def test_require_panel_secrets_falla_en_produccion_sin_sesion() -> None:
    with pytest.raises(RuntimeError, match="PANEL_SESSION_SECRET"):
        require_panel_secrets(Settings(environment="production", panel_session_secret="change-me"))
    # Con una sesion real pasa, aunque el resto del contrato del worker no este.
    require_panel_secrets(Settings(environment="production", panel_session_secret="ana:operador"))
    # En desarrollo se relaja.
    require_panel_secrets(Settings(environment="development", panel_session_secret=""))


# --- Read model real sobre `fakeredis` (OP13) ------------------------------------


def _seed_redis() -> fakeredis.FakeRedis:
    client_ = fakeredis.FakeRedis(decode_responses=True)
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=42, head_sha="abc", pipeline_id="7")
    client_.rpush("reviews:jobs", request.model_dump_json())
    client_.hset(
        "reviews:active:r:1:abc:1",
        "persona",
        json.dumps({"node": "persona", "started_at": "2026-10-16T10:00:00+00:00", "branches": 2}),
    )
    client_.set("budget:r:1:abc:1", 1200)
    client_.set("budget:reserved:r:1:abc:1", 300)
    # El contador del fix (017 B11) no es la review: no debe aparecer en el panel.
    client_.set("budget:fix:r:1:abc:1", 99)

    human = PausedReview(
        review_key="r:1:abc:1",
        request=request,
        kind=PauseKind.HUMAN,
        approval=ApprovalRequest(
            review_key="r:1:abc:1",
            repo="cqm/emotivoo-v2",
            pr_id=42,
            head_sha="abc",
            command_hint="decide",
        ),
    )
    client_.set("reviews:paused:r:1:abc:1", human.model_dump_json())

    wave = FixWaitRequest(
        review_key="r:2:def:2",
        repo="cqm/emotivoo-v2",
        pr_id=42,
        head_sha="def",
        jobs=[
            VerifyJob(
                run_key="r:2:def:2:f1:1",
                finding_id="f1",
                attempt=1,
                repo="cqm/emotivoo-v2",
                head_sha="def",
                patch="",
            )
        ],
    )
    fix = PausedReview(
        review_key="r:2:def:2",
        request=ReviewRequest(repo="cqm/emotivoo-v2", pr_id=42, head_sha="def", pipeline_id="8"),
        kind=PauseKind.FIX,
        approval=wave,
    )
    client_.set("reviews:paused:r:2:def:2", fix.model_dump_json())

    client_.rpush(
        "reviews:jobs:dead",
        json.dumps(
            {
                "review_key": "r:3:c:3",
                "attempts": 3,
                "reason": "twg caido",
                "discarded_at": 1760620000.0,
            }
        ),
    )
    return client_


def test_read_model_lee_el_estado_de_redis() -> None:
    model = RedisPanelReadModel(
        url="redis://localhost:6379/0",
        progress_key="reviews:active",
        budget_total=5000,
        client=_seed_redis(),
    )
    state = model.state()

    assert [item.review_key for item in state.queue] == ["cqm/emotivoo-v2:42:abc:7"]
    assert state.queue[0].pr_id == 42
    assert [review.review_key for review in state.active] == ["r:1:abc:1"]
    assert state.active[0].nodes[0].node == "persona"
    assert state.active[0].nodes[0].branches == 2
    assert [item.review_key for item in state.paused.human] == ["r:1:abc:1"]
    assert [item.review_key for item in state.paused.fix] == ["r:2:def:2"]
    assert state.paused.fix[0].jobs[0]["run_key"] == "r:2:def:2:f1:1"
    assert state.dead[0].reason == "twg caido"
    assert [item.review_key for item in state.budget] == ["r:1:abc:1"], "el fix no es la review"
    assert state.budget[0].committed == 1200
    assert state.budget[0].reserved == 300
    assert state.budget[0].remaining == 3500


def test_read_model_no_rompe_si_redis_cae() -> None:
    """El panel es observabilidad: una seccion sin Redis vuelve vacia, no revienta."""

    class _BrokenClient:
        def lrange(self, *args: object, **kwargs: object) -> object:
            raise redis.exceptions.ConnectionError("caido")

        def scan_iter(self, *args: object, **kwargs: object) -> object:
            raise redis.exceptions.ConnectionError("caido")

    model = RedisPanelReadModel(
        url="redis://localhost:6379/0", budget_total=1000, client=_BrokenClient()
    )
    state = model.state()

    assert state.queue == []
    assert state.active == []
    assert state.paused == PausedGroups()
    assert state.dead == []
    assert state.budget == []


# --- Wiring del lector de recorrido y la composicion -----------------------------


def test_pg_trace_reader_degrada_si_postgres_no_responde(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("postgres caido")

    monkeypatch.setattr(read_model, "build_reader_checkpointer", _boom)
    reader = read_model.PgTraceReader(database_url="postgresql://x")
    assert reader.trace("r:1:abc:1") is None


def test_pg_trace_reader_delega_en_read_trace_y_cierra_el_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []

    class _Pool:
        def close(self) -> None:
            closed.append("close")

    saver = InMemorySaver(serde=build_serde())
    saver.conn = _Pool()  # type: ignore[attr-defined]
    monkeypatch.setattr(read_model, "build_reader_checkpointer", lambda *a, **k: saver)

    reader = read_model.PgTraceReader(database_url="postgresql://x")
    assert reader.trace("no:existe") is None, "un hilo sin checkpoints no es un recorrido"
    reader.close()
    assert closed == ["close"]


def test_build_panel_read_model_compone_las_dos_fuentes() -> None:
    model = build_panel_read_model(Settings())
    assert isinstance(model, CompositePanelReadModel)
    assert isinstance(model, PanelReadModel)
    model.close()


# --- OP24: proxy de la consola del runner ------------------------------------------

_FIX_KEY = "cqm/emotivoo-v2:39:bbb:4"
_SSE = 'event: line\ndata: {"text": "hola"}\n\nevent: end\ndata: {}\n\n'


def _console(handler: object) -> RunnerConsoleClient:
    return RunnerConsoleClient(
        base_url="http://runner",
        token="t0k",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


def test_la_consola_reenvia_el_sse_del_runner_con_el_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, text=_SSE, headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(app.state, "console", _console(handler), raising=False)
    response = client.get(f"/api/reviews/{_FIX_KEY}/console")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"text": "hola"' in response.text and "event: end" in response.text
    assert seen == {"url": "http://runner/verify/r:f2:1/console", "auth": "Bearer t0k"}


def test_la_consola_da_404_si_el_runner_no_tiene_el_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un `200` vacio haria reconectar al `EventSource` sin fin; un `404` lo para."""
    monkeypatch.setattr(
        app.state, "console", _console(lambda request: httpx.Response(404)), raising=False
    )
    assert client.get(f"/api/reviews/{_FIX_KEY}/console").status_code == 404


def test_la_consola_da_404_si_el_runner_no_responde(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("runner caido", request=request)

    monkeypatch.setattr(app.state, "console", _console(handler), raising=False)
    assert client.get(f"/api/reviews/{_FIX_KEY}/console").status_code == 404


def test_la_consola_da_404_sin_verificacion_en_curso(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app.state, "console", _console(lambda request: httpx.Response(200)), raising=False
    )
    assert client.get(f"/api/reviews/{_REVIEW_KEY}/console").status_code == 404


def test_la_consola_da_404_sin_runner_configurado(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app.state, "console", RunnerConsoleClient(base_url=""), raising=False)
    assert client.get(f"/api/reviews/{_FIX_KEY}/console").status_code == 404


# --- Razonamiento en vivo (021 RZ8) ---


def _reasoning_sink():
    from agent_review.ports.reasoning import InMemoryReasoningSink, ReasoningEvent

    sink = InMemoryReasoningSink()

    def ev(kind, batch, text="", **meta):
        sink.emit(_REVIEW_KEY, ReasoningEvent(kind, "correctness", batch, 2, text, meta))

    ev("start", 1, files=["a.py"])
    ev("reasoning", 1, "miro a.py")
    ev("end", 1, findings=0)
    ev("start", 2, files=["b.py"])
    ev("error", 2, "sin presupuesto")
    return sink


def _sse_events(body: str) -> list[tuple[str, dict]]:
    out = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_el_razonamiento_se_sirve_como_sse_y_se_cierra_al_terminar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.state, "reasoning", _reasoning_sink(), raising=False)
    response = client.get(f"/api/reviews/{_REVIEW_KEY}/reasoning")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(response.text)
    assert [kind for kind, _ in events] == ["start", "reasoning", "end", "start", "fallo", "fin"]
    assert events[4][1]["kind"] == "error"
    assert events[1][1]["text"] == "miro a.py"
    assert events[0][1]["meta"] == {"files": ["a.py"]}


def test_sin_razonamiento_es_404(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_review.ports.reasoning import InMemoryReasoningSink

    monkeypatch.setattr(app.state, "reasoning", InMemoryReasoningSink(), raising=False)
    assert client.get(f"/api/reviews/{_REVIEW_KEY}/reasoning").status_code == 404
    monkeypatch.setattr(app.state, "reasoning", None, raising=False)
    assert client.get(f"/api/reviews/{_REVIEW_KEY}/reasoning").status_code == 404


def test_un_lector_tambien_puede_ver_el_razonamiento(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "panel_session_secret", "ana:lector")
    monkeypatch.setattr(app.state, "reasoning", _reasoning_sink(), raising=False)
    response = client.get(
        f"/api/reviews/{_REVIEW_KEY}/reasoning", headers={"X-Panel-Session": "ana:lector"}
    )
    assert response.status_code == 200


def test_el_stream_sigue_en_vivo_hasta_cerrar_todos_los_lotes() -> None:
    from agent_review.ports.reasoning import InMemoryReasoningSink, ReasoningEvent
    from agent_review.web.reasoning import reasoning_stream

    sink = InMemoryReasoningSink()
    sink.emit("rk", ReasoningEvent("start", "correctness", 1, 1))

    class _Live:
        """Lector que entrega el cierre del lote en la primera espera."""

        def exists(self, review_key):
            return True

        def read(self, review_key, *, after="0-0", block_ms=None, count=500):
            if block_ms is not None and len(sink.events["rk"]) == 1:
                sink.emit("rk", ReasoningEvent("end", "correctness", 1, 1))
            return sink.read(review_key, after=after)

    chunks = list(reasoning_stream(_Live(), "rk", block_ms=1, idle_s=60))
    kinds = [c.split("\n")[1].removeprefix("event: ") for c in chunks if c.startswith("id:")]
    assert kinds == ["start", "end"]
    assert chunks[-1].startswith("event: fin")


def test_el_stream_se_cierra_por_inactividad() -> None:
    from agent_review.ports.reasoning import InMemoryReasoningSink, ReasoningEvent
    from agent_review.web.reasoning import reasoning_stream

    sink = InMemoryReasoningSink()
    sink.emit("rk", ReasoningEvent("start", "correctness", 1, 3))  # nunca se cierra
    ticks = iter([0.0, 1.0, 999.0])
    chunks = list(reasoning_stream(sink, "rk", block_ms=1, idle_s=10, clock=lambda: next(ticks)))
    assert chunks[-1].startswith("event: fin")


def test_api_history_devuelve_json_filtrado(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_review.domain.history import ReviewEnd, ReviewSummary
    from agent_review.history_store import InMemoryReviewHistory

    history = InMemoryReviewHistory()
    for key, pr in (("a", 1), ("b", 2)):
        history.record(
            ReviewSummary(
                review_key=key,
                repo="r",
                pr_id=pr,
                head_sha="x",
                finished_at=_MOMENT,
                end=ReviewEnd.COMPLETE,
            )
        )
    monkeypatch.setattr(app.state, "history", history, raising=False)

    data = client.get("/api/history", params={"pr_id": 2}).json()
    assert [item["review_key"] for item in data] == ["b"]
    assert data[0]["end"] == "completa"


def test_un_aviso_de_reintento_no_cierra_el_lote() -> None:
    # 021 R14: tras el aviso de reintento viene otro intento del mismo lote.
    from agent_review.ports.reasoning import InMemoryReasoningSink, ReasoningEvent
    from agent_review.web.reasoning import reasoning_stream

    sink = InMemoryReasoningSink()
    sink.emit("rk", ReasoningEvent("start", "correctness", 1, 1))
    sink.emit("rk", ReasoningEvent("error", "correctness", 1, 1, "corte", {"retry": 1}))
    ticks = iter([0.0, 1.0, 999.0])
    chunks = list(reasoning_stream(sink, "rk", block_ms=1, idle_s=10, clock=lambda: next(ticks)))
    # No se cerro por «todos los lotes cerrados» sino por inactividad: hubo keep-alive.
    assert any(chunk.startswith(": sigo aqui") for chunk in chunks)
