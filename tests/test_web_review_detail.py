"""API de la ficha de una review (iniciativa 029, bloque A: A1-A3).

Cubre el recurso agregado `GET /api/reviews/{review_key}` con sus tres causas de
`404` y su caso contrario, la ausencia de streams (solo se informa de la
disponibilidad) y las revisiones fijadas en `GET /api/config`. Los fakes se
inyectan en `app.state`, como en `test_web.py`: ni Redis ni Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_review.claude_activity import ClaudeActivity
from agent_review.config_store import MemoryConfigStore
from agent_review.domain.models import FindingAction, PendingDecision, Severity
from agent_review.domain.trace import ReviewTrace, TraceNode
from agent_review.ports.reasoning import InMemoryReasoningSink
from agent_review.settings import get_settings
from agent_review.web.main import app
from agent_review.web.read_model import (
    DeadLetterItem,
    PanelState,
    PausedGroups,
    PausedItem,
)
from agent_review.web.runner_client import RunnerConsoleClient

client = TestClient(app)

_MOMENT = datetime(2026, 10, 16, 10, 0, tzinfo=UTC)
_REVIEW_KEY = "cqm/emotivoo-v2:42:abc:7"
_FIX_KEY = "cqm/emotivoo-v2:39:bbb:4"
_DEAD_KEY = "cqm/emotivoo-v2:38:ccc:3"


class FakeReadModel:
    """Read model en memoria con trazas, log y revisiones fijadas configurables."""

    def __init__(self) -> None:
        self.panel_state = PanelState(generated_at=_MOMENT)
        self.traces: dict[str, ReviewTrace] = {}
        self.extra_notes: dict[str, list[str]] = {}
        self.journal_lines: dict[str, list[str]] = {}
        self.pins: list[tuple[str, int]] | None = []

    def state(self) -> PanelState:
        return self.panel_state

    def trace(self, review_key: str) -> ReviewTrace | None:
        return self.traces.get(review_key)

    def notes(self, review_key: str) -> list[str]:
        return self.extra_notes.get(review_key, [])

    def journal(self, review_key: str) -> list[str]:
        return self.journal_lines.get(review_key, [])

    def dial_pins(self) -> list[tuple[str, int]] | None:
        return self.pins


class _ConsoleProhibida(RunnerConsoleClient):
    """Cliente que falla si alguien abre el stream: la ficha solo pregunta si existe."""

    def __init__(self) -> None:
        super().__init__(base_url="http://runner", token="t0k")

    def open(self, run_key: str):  # type: ignore[no-untyped-def]
        raise AssertionError(f"la ficha no debe abrir el SSE de {run_key}")


def _pending(finding_id: str = "f1") -> PendingDecision:
    return PendingDecision(
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


def _trace(review_key: str = _REVIEW_KEY) -> ReviewTrace:
    return ReviewTrace(
        review_key=review_key,
        thread_id=review_key,
        state="completed",
        nodes=[
            TraceNode(name="scope", index=0, started_at=_MOMENT, writes=["scope"]),
            TraceNode(name="persona", index=1, started_at=_MOMENT),
        ],
        next=[],
        pause_kind=None,
        updated_at=_MOMENT,
    )


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> FakeReadModel:
    fake = FakeReadModel()
    monkeypatch.setattr(app.state, "read_model", fake, raising=False)
    monkeypatch.setattr(app.state, "console", _ConsoleProhibida(), raising=False)
    monkeypatch.setattr(app.state, "reasoning", None, raising=False)
    # El motor por defecto de los tests es `llm`: sin CLI no hay actividad que enseñar.
    monkeypatch.setattr(get_settings(), "review_engine", "llm")
    return fake


# --- A1: el recurso agregado -----------------------------------------------------


def test_la_ficha_devuelve_todos_los_campos(model: FakeReadModel) -> None:
    model.traces[_REVIEW_KEY] = _trace()
    model.extra_notes[_REVIEW_KEY] = ["Fuente estática standards: missing."]
    model.journal_lines[_REVIEW_KEY] = ["empieza el nodo scope"]
    model.panel_state.paused = PausedGroups(
        human=[
            PausedItem(
                review_key=_REVIEW_KEY,
                kind="human",
                created_at=_MOMENT,
                state="PAUSED",
                pending=[_pending()],
                command_hint="agent_review decide …",
            )
        ]
    )

    response = client.get(f"/api/reviews/{_REVIEW_KEY}")

    assert response.status_code == 200
    body = response.json()
    assert body["review_key"] == _REVIEW_KEY
    assert body["trace"]["state"] == "completed"
    assert [node["name"] for node in body["trace"]["nodes"]] == ["scope", "persona"]
    assert body["paused"]["kind"] == "human"
    assert body["paused"]["pending"][0]["finding_id"] == "f1"
    assert body["notes"] == ["Fuente estática standards: missing."]
    assert body["journal"] == ["empieza el nodo scope"]
    # Sin pausa de fix no hay run_key: la consola no está disponible aunque el
    # cliente del runner sí lo esté.
    assert body["console_available"] is False
    assert body["reasoning_available"] is False
    assert body["claude"] is None


def test_la_ficha_con_todo_poblado_incluye_canales_y_claude(
    model: FakeReadModel, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """El caso completo de A1: traza, pausa, log, consola, razonamiento y CLI."""
    from agent_review.ports.reasoning import InMemoryReasoningSink, ReasoningEvent

    monkeypatch.setattr(get_settings(), "review_engine", "claude")
    session = tmp_path / "session.json"
    monkeypatch.setattr(get_settings(), "claude_session_path", str(session))
    sha = "d" * 40
    key = f"acme/app:9:{sha}"
    model.traces[key] = _trace(key)
    model.extra_notes[key] = ["Fuente estática standards: missing."]
    model.journal_lines[key] = ["empieza el CLI"]
    model.panel_state.paused = PausedGroups(
        fix=[
            PausedItem(
                review_key=key,
                kind="fix",
                created_at=_MOMENT,
                state="VERIFYING",
                jobs=[{"run_key": "r:f2:1", "finding_id": "f2"}],
            )
        ]
    )
    sink = InMemoryReasoningSink()
    sink.emit(key, ReasoningEvent("start", "correctness", 1, 1))
    monkeypatch.setattr(app.state, "reasoning", sink, raising=False)
    activity = ClaudeActivity(session, repo="acme/app", pr_id=9, head_sha=sha)
    activity.start()

    body = client.get(f"/api/reviews/{key}").json()

    assert set(body) == {
        "review_key",
        "trace",
        "paused",
        "notes",
        "journal",
        "console_available",
        "reasoning_available",
        "claude",
    }
    assert body["paused"]["kind"] == "fix"
    assert body["console_available"] is True
    assert body["reasoning_available"] is True
    assert body["claude"]["available"] is True
    assert body["claude"]["events_available"] is True
    # `start()` deja creado el fichero de líneas aunque aún no tenga texto.
    assert body["claude"]["download_available"] is True


def test_la_ficha_sin_traza_pero_con_log_es_200(model: FakeReadModel) -> None:
    model.extra_notes[_REVIEW_KEY] = ["Claude: empieza el CLI para abc."]

    response = client.get(f"/api/reviews/{_REVIEW_KEY}")

    assert response.status_code == 200
    body = response.json()
    assert body["trace"] is None
    assert body["paused"] is None
    assert body["notes"] == ["Claude: empieza el CLI para abc."]


def test_la_consola_esta_disponible_con_un_run_key_de_fix(model: FakeReadModel) -> None:
    model.traces[_FIX_KEY] = _trace(_FIX_KEY)
    model.panel_state.paused = PausedGroups(
        fix=[
            PausedItem(
                review_key=_FIX_KEY,
                kind="fix",
                created_at=_MOMENT,
                state="VERIFYING",
                jobs=[{"run_key": "r:f2:1", "finding_id": "f2"}],
            )
        ]
    )

    body = client.get(f"/api/reviews/{_FIX_KEY}").json()

    assert body["console_available"] is True
    assert body["paused"]["kind"] == "fix"


def test_el_razonamiento_se_informa_si_hay_lector_y_eventos(
    model: FakeReadModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_review.ports.reasoning import ReasoningEvent

    model.traces[_REVIEW_KEY] = _trace()
    sink = InMemoryReasoningSink()
    sink.emit(_REVIEW_KEY, ReasoningEvent("start", "correctness", 1, 1))
    monkeypatch.setattr(app.state, "reasoning", sink, raising=False)

    assert client.get(f"/api/reviews/{_REVIEW_KEY}").json()["reasoning_available"] is True


def test_el_claude_de_la_ficha_sale_de_la_actividad_del_cli(
    model: FakeReadModel, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(get_settings(), "review_engine", "claude")
    session = tmp_path / "session.json"
    monkeypatch.setattr(get_settings(), "claude_session_path", str(session))
    sha = "c" * 40
    key = f"acme/app:9:{sha}"
    model.extra_notes[key] = ["Claude: empieza el CLI."]
    activity = ClaudeActivity(session, repo="acme/app", pr_id=9, head_sha=sha)
    activity.start()
    activity.append('{"type":"system"}')

    body = client.get(f"/api/reviews/{key}").json()

    assert body["claude"] == {
        "available": True,
        "events_available": True,
        "download_available": True,
    }


def test_el_claude_no_aplica_fuera_del_motor_claude(model: FakeReadModel) -> None:
    model.traces[_REVIEW_KEY] = _trace()

    assert client.get(f"/api/reviews/{_REVIEW_KEY}").json()["claude"] is None


# --- A2: 404 y sus casos contrarios ----------------------------------------------


def test_sin_traza_ni_log_ni_descarte_es_404(model: FakeReadModel) -> None:
    response = client.get("/api/reviews/no:existe")

    assert response.status_code == 404
    assert "Sin recorrido" in response.json()["detail"]


def test_un_log_en_blanco_no_cuenta_como_contenido(model: FakeReadModel) -> None:
    model.extra_notes[_REVIEW_KEY] = ["   "]
    model.journal_lines[_REVIEW_KEY] = [""]

    assert client.get(f"/api/reviews/{_REVIEW_KEY}").status_code == 404


def test_una_traza_sola_es_200(model: FakeReadModel) -> None:
    model.traces[_REVIEW_KEY] = _trace()

    response = client.get(f"/api/reviews/{_REVIEW_KEY}")

    assert response.status_code == 200
    assert response.json()["trace"]["review_key"] == _REVIEW_KEY


def test_un_descarte_sin_traza_es_200_con_trace_nulo(model: FakeReadModel) -> None:
    model.panel_state.dead = [
        DeadLetterItem(
            review_key=_DEAD_KEY,
            attempts=3,
            reason="twg caido",
            discarded_at=_MOMENT,
            error="RuntimeError: twg caido",
        )
    ]

    response = client.get(f"/api/reviews/{_DEAD_KEY}")

    assert response.status_code == 200
    body = response.json()
    assert body["trace"] is None
    assert body["notes"] == [] and body["journal"] == []


def test_la_ficha_exige_sesion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "panel_session_secret", "ana:operador")

    assert client.get(f"/api/reviews/{_REVIEW_KEY}").status_code == 401


def test_la_ficha_no_cambia_el_404_de_una_ruta_inexistente() -> None:
    assert client.get("/api/inexistente").status_code == 404


# --- A3: revisiones fijadas en la configuración ----------------------------------


def _config(model: FakeReadModel, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    monkeypatch.setattr(app.state, "config_store", MemoryConfigStore(), raising=False)
    response = client.get("/api/config")
    assert response.status_code == 200
    return response.json()


def test_config_lleva_las_revisiones_fijadas(
    model: FakeReadModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    model.pins = [(_REVIEW_KEY, 4), (_FIX_KEY, 2)]

    body = _config(model, monkeypatch)

    assert body["pins"] == [
        {"review_key": _REVIEW_KEY, "revision": 4},
        {"review_key": _FIX_KEY, "revision": 2},
    ]


def test_config_con_pins_vacio_publica_la_lista(
    model: FakeReadModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    model.pins = []

    body = _config(model, monkeypatch)

    assert body["pins"] == [], "«ninguna review fijó revisión» sí es una respuesta"


def test_config_omite_pins_si_no_se_pudieron_leer(
    model: FakeReadModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    model.pins = None

    body = _config(model, monkeypatch)

    assert "pins" not in body, "un fallo de lectura no puede parecer una lista vacía"
    assert body["revision"] == 0
