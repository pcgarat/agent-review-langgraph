"""Sesión y parking del motor claude (025 S1–S4).

Sin sesión el job sale de la cola principal y no hay `invoke`. Un job del motor
`llm` que iba detrás se consume. El aviso no lleva el token. El login del
operador suelta solo lo parqueado por la sesión; el cupo se queda.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from agent_review.adapters.claude_artifact import ClaudeArtifactStore
from agent_review.adapters.claude_client import HttpClaudeRunner
from agent_review.chat_outbox import InMemoryChatOutbox, OutboxChatAdapter
from agent_review.claude_runner.main import build_app
from agent_review.claude_session import write_session
from agent_review.domain.models import (
    DecisionOutcome,
    FindingDecision,
    HitlDecision,
    ReviewRequest,
)
from agent_review.ports.claude import ClaudeQuotaExceeded
from agent_review.queue import InMemoryReviewQueue
from agent_review.settings import Settings
from agent_review.web.main import ClaudeLogin, app
from agent_review.worker.main import (
    _consume,
    claude_session_ready,
    dispatch_job,
    release_quota_wait,
)

_ROOT = Path(__file__).resolve().parents[1]
_COMPOSE = _ROOT / "docker-compose.yml"
_TOKEN = "super-secreto-claude"
_LINK = "http://panel.test/claude"


def _request(head_sha: str) -> ReviewRequest:
    return ReviewRequest(repo="cqm/emotivoo-v2", pr_id=12, head_sha=head_sha, pipeline_id="7")


class _Runner:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls: list[str] = []

    def run(self, *, repo: str, pr_id: int, head_sha: str) -> str:
        del repo, pr_id
        self.calls.append(head_sha)
        raise self.error


class _Graph:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, state, config=None):  # noqa: ANN001
        del config
        self.calls += 1
        return {"request": state["request"], "findings": [], "merged": [], "decisions": []}


def _notices(outbox: InMemoryChatOutbox) -> list[str]:
    found: list[str] = []
    while True:
        notice = outbox.dequeue()
        if notice is None:
            return found
        found.append(notice.model_dump_json())


def test_sin_sesion_parkea_y_el_job_llm_se_consume() -> None:
    queue = InMemoryReviewQueue()
    queue.enqueue(_request("claude"))
    queue.enqueue(_request("llm"))
    graph = _Graph()
    outbox = InMemoryChatOutbox()
    chat = OutboxChatAdapter(outbox=outbox)

    primero = queue.dequeue()
    assert primero is not None
    dispatch_job(
        queue,
        graph,
        primero,
        max_attempts=3,
        chat=chat,
        claude_session=lambda: False,
        park_link=_LINK,
    )
    segundo = queue.dequeue()
    assert segundo is not None
    dispatch_job(queue, graph, segundo, max_attempts=3, chat=chat, claude_session=None)

    assert graph.calls == 1
    assert [item.head_sha for item in queue.parked()] == ["claude"]
    assert queue.dequeue() is None
    avisos = _notices(outbox)
    assert len(avisos) == 1
    assert _LINK in avisos[0]
    assert _TOKEN not in avisos[0]

    otro = _request("claude-2")
    queue.enqueue(otro)
    sacado = queue.dequeue()
    assert sacado is not None
    dispatch_job(
        queue,
        graph,
        sacado,
        max_attempts=3,
        chat=chat,
        claude_session=lambda: False,
        park_link=_LINK,
    )
    assert _notices(outbox) == []


def test_con_sesion_se_suelta_solo_lo_parado_por_ella() -> None:
    queue = InMemoryReviewQueue()
    queue.park(_request("sesion"), reason="session")
    queue.park(_request("cupo"), reason="quota")

    assert claude_session_ready(queue, lambda: True) is True

    suelto = queue.dequeue()
    assert suelto is not None
    assert suelto.head_sha == "sesion"
    assert [item.head_sha for item in queue.parked()] == ["cupo"]


def test_el_operador_guarda_sin_devolver_el_token(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    queue = InMemoryReviewQueue()
    queue.park(_request("sesion"), reason="session")
    queue.park(_request("cupo"), reason="quota")
    queue.claim_notice("session")
    login = ClaudeLogin(path=tmp_path / "session.json", queue=queue, ttl_s=60)
    caplog.set_level(logging.DEBUG)

    with TestClient(app) as client:
        app.state.claude_login = login
        lector = client.post(
            "/api/claude/session",
            headers={"X-Panel-Session": "ana:lector"},
            json={"token": _TOKEN},
        )
        client.cookies.set("panel_session", "ana:operador")
        formulario = client.post("/api/claude/session", data={"token": _TOKEN})
        pagina = client.get("/claude", headers={"X-Panel-Session": "ana:lector"})
        operador = client.post(
            "/api/claude/session",
            headers={"X-Panel-Session": "ana:operador"},
            json={"token": _TOKEN},
        )
        formulario_ok = client.get("/claude", headers={"X-Panel-Session": "ana:operador"})

    assert lector.status_code == 403
    assert formulario.status_code == 403
    assert pagina.status_code == 403
    assert operador.status_code == 200
    assert operador.json() == {"released": 1}
    assert _TOKEN not in operador.text
    assert _TOKEN not in caplog.text
    assert _TOKEN not in formulario_ok.text
    assert "https://claude.ai/settings/claude-code" in formulario_ok.text
    path = tmp_path / "session.json"
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600
    suelto = queue.dequeue()
    assert suelto is not None
    assert suelto.head_sha == "sesion"
    assert [item.head_sha for item in queue.parked()] == ["cupo"]
    assert queue.claim_notice("session") is True


def test_la_sesion_caducada_se_retira_y_el_aviso_se_repite(tmp_path: Path) -> None:
    queue = InMemoryReviewQueue()
    outbox = InMemoryChatOutbox()
    chat = OutboxChatAdapter(outbox=outbox)
    graph = _Graph()
    primero = _request("antes")
    dispatch_job(
        queue,
        graph,
        primero,
        max_attempts=3,
        chat=chat,
        claude_session=lambda: False,
        park_link=_LINK,
    )
    assert len(_notices(outbox)) == 1
    queue.clear_notice("session")

    path = tmp_path / "session.json"
    write_session(path, _TOKEN, ttl_s=10, now=0)
    settings = Settings(_env_file=None, claude_session_path=str(path), claude_runner_token="")
    with TestClient(build_app(settings)) as client:
        respuesta = client.get("/session")
    assert respuesta.json() == {"active": False}
    assert _TOKEN not in respuesta.text
    assert not path.exists()

    dispatch_job(
        queue,
        graph,
        _request("despues"),
        max_attempts=3,
        chat=chat,
        claude_session=lambda: False,
        park_link=_LINK,
    )
    avisos = _notices(outbox)
    assert len(avisos) == 1
    assert _TOKEN not in avisos[0]
    assert graph.calls == 0


def test_el_cupo_parkea_avisa_y_no_entra_al_grafo(tmp_path: Path) -> None:
    store = ClaudeArtifactStore(root=tmp_path)
    runner = _Runner(error=ClaudeQuotaExceeded("cupo"))
    graph = _Graph()
    queue = InMemoryReviewQueue()
    outbox = InMemoryChatOutbox()
    chat = OutboxChatAdapter(outbox=outbox)

    _consume(
        queue,
        graph,
        _request("uno"),
        max_attempts=3,
        chat=chat,
        claude_runner=runner,
        claude_store=store,
        park_link=_LINK,
    )
    _consume(
        queue,
        graph,
        _request("dos"),
        max_attempts=3,
        chat=chat,
        claude_runner=runner,
        claude_store=store,
        park_link=_LINK,
    )

    assert graph.calls == 0
    assert runner.calls == ["uno"]
    assert queue.dequeue() is None
    assert [item.head_sha for item in queue.parked()] == ["uno", "dos"]
    avisos = _notices(outbox)
    assert len(avisos) == 1
    assert "Cupo" in avisos[0]
    assert _TOKEN not in avisos[0]
    assert _LINK in avisos[0]


def test_get_session_es_un_booleano(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    write_session(path, _TOKEN, ttl_s=3600)
    settings = Settings(_env_file=None, claude_session_path=str(path), claude_runner_token="")
    with TestClient(build_app(settings)) as client:
        respuesta = client.get("/session")
    assert respuesta.json() == {"active": True}
    assert _TOKEN not in respuesta.text

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"active": True})

    cliente = HttpClaudeRunner(
        url="http://claude-runner",
        token="",
        timeout_s=1,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert cliente.active() is True


def test_el_worker_no_monta_el_fichero_de_sesion() -> None:
    services = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"]
    worker = [str(item) for item in services["worker"].get("volumes", [])]
    runner = [str(item) for item in services["claude-runner"]["volumes"]]
    web = [str(item) for item in services["web"]["volumes"]]
    assert not any("claude-session" in item for item in worker)
    assert "CLAUDE_SESSION_PATH" not in services["worker"]["environment"]
    assert any("claude-session" in item for item in runner)
    assert any("claude-session" in item for item in web)


def test_la_espera_de_cupo_se_suelta_al_enfriar(tmp_path: Path) -> None:
    store = ClaudeArtifactStore(root=tmp_path)
    runner = _Runner(error=ClaudeQuotaExceeded("cupo"))
    queue = InMemoryReviewQueue()
    _consume(
        queue,
        _Graph(),
        _request("uno"),
        max_attempts=3,
        claude_runner=runner,
        claude_store=store,
    )
    _consume(
        queue,
        _Graph(),
        _request("dos"),
        max_attempts=3,
        claude_runner=runner,
        claude_store=store,
    )
    crudo = list(queue._parked)
    assert '"quota-wait"' not in crudo[0]
    assert '"quota-wait"' in crudo[1]
    assert release_quota_wait(queue) == []
    queue.clear_notice("quota")
    sueltos = release_quota_wait(queue)
    assert [item.head_sha for item in sueltos] == ["dos"]
    assert [item.head_sha for item in queue.parked()] == ["uno"]


def test_el_enfriamiento_no_cubre_un_artefacto_ya_valido(tmp_path: Path) -> None:
    store = ClaudeArtifactStore(root=tmp_path)
    request = _request("ya")
    assert store.save("[]", repo=request.repo, pr_id=request.pr_id, head_sha=request.head_sha)
    queue = InMemoryReviewQueue()
    assert queue.claim_notice("quota", ttl_s=900)

    class _Ok:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def run(self, *, repo: str, pr_id: int, head_sha: str) -> str:
            del repo, pr_id
            self.calls.append(head_sha)
            return "[]"

    runner = _Ok()
    graph = _Graph()
    _consume(queue, graph, request, max_attempts=3, claude_runner=runner, claude_store=store)
    assert runner.calls == []
    assert graph.calls == 1
    assert queue.parked() == []


def test_una_decision_sin_sesion_no_se_parkea() -> None:
    queue = InMemoryReviewQueue()
    graph = _Graph()
    request = _request("dec")
    request = request.model_copy(
        update={
            "decision": HitlDecision(
                review_key=request.review_key,
                actor="ana",
                items=[FindingDecision(finding_id="f1", outcome=DecisionOutcome.APPROVED)],
            )
        }
    )
    dispatch_job(queue, graph, request, max_attempts=3, claude_session=lambda: False)
    assert graph.calls == 1
    assert queue.parked() == []
