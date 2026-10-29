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
from agent_review.ports.claude import (
    ClaudeCeiling,
    ClaudeClusterLimited,
    ClaudeProcessCrashed,
    ClaudeQuotaExceeded,
)
from agent_review.queue import InMemoryReviewQueue
from agent_review.settings import Settings
from agent_review.web.main import ClaudeLogin, app
from agent_review.worker.main import (
    _consume,
    claude_blocker,
    dispatch_job,
    release_cluster_wait,
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

    def run(
        self,
        *,
        repo: str,
        pr_id: int,
        head_sha: str,
        base_sha: str,
        max_turns: int | None = None,
        timeout_s: float | None = None,
    ) -> str:
        del repo, pr_id, base_sha, max_turns, timeout_s
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
        claude_session=lambda: "session",
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
        claude_session=lambda: "session",
        park_link=_LINK,
    )
    assert _notices(outbox) == []


def test_con_sesion_se_suelta_solo_lo_parado_por_ella() -> None:
    queue = InMemoryReviewQueue()
    queue.park(_request("sesion"), reason="session")
    queue.park(_request("cupo"), reason="quota")

    assert claude_blocker(queue, lambda: None) is None

    suelto = queue.dequeue()
    assert suelto is not None
    assert suelto.head_sha == "sesion"
    assert [item.head_sha for item in queue.parked()] == ["cupo"]


def test_el_operador_guarda_sin_devolver_el_token(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # El `dist` no se versiona: el armazón de la SPA se inyecta para que la prueba
    # no dependa de haber construido `frontend/` (029, R4).
    from agent_review.web import spa

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><div id=root></div>", encoding="utf-8")
    monkeypatch.setattr(spa, "spa_directory", lambda: dist)

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
        # La pagina es de la SPA (029): el armazon se sirve a cualquier sesion y es
        # el cliente quien no pinta el formulario al `lector`. Quien manda es la
        # API, que sigue devolviendo `403` al POST.
        pagina = client.get("/claude", headers={"X-Panel-Session": "ana:lector"})
        operador = client.post(
            "/api/claude/session",
            headers={"X-Panel-Session": "ana:operador"},
            json={"token": _TOKEN},
        )

    assert lector.status_code == 403
    assert formulario.status_code == 403
    assert pagina.status_code == 200
    assert operador.status_code == 200
    assert operador.json() == {"released": 1}
    assert _TOKEN not in operador.text
    assert _TOKEN not in caplog.text
    assert _TOKEN not in pagina.text
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
        claude_session=lambda: "session",
        park_link=_LINK,
    )
    assert len(_notices(outbox)) == 1
    queue.clear_notice("session")

    path = tmp_path / "session.json"
    write_session(path, _TOKEN, ttl_s=10, now=0)
    settings = Settings(_env_file=None, claude_session_path=str(path), claude_runner_token="")
    with TestClient(build_app(settings)) as client:
        respuesta = client.get("/session")
    assert respuesta.json() == {"active": False, "reason": "session"}
    assert _TOKEN not in respuesta.text
    assert not path.exists()

    dispatch_job(
        queue,
        graph,
        _request("despues"),
        max_attempts=3,
        chat=chat,
        claude_session=lambda: "session",
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


def test_el_techo_parkea_una_vez_y_el_login_no_lo_suelta(tmp_path: Path) -> None:
    store = ClaudeArtifactStore(root=tmp_path)
    runner = _Runner(error=ClaudeCeiling("techo"))
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
    assert queue.dequeue() is None
    assert [item.head_sha for item in queue.parked()] == ["uno", "dos"]
    assert len(_notices(outbox)) == 1
    assert claude_blocker(queue, lambda: None) is None
    assert [item.head_sha for item in queue.parked()] == ["uno", "dos"]


def test_una_caida_sigue_reintentandose(tmp_path: Path) -> None:
    store = ClaudeArtifactStore(root=tmp_path)
    runner = _Runner(error=ClaudeProcessCrashed("502"))
    graph = _Graph()
    queue = InMemoryReviewQueue()
    _consume(
        queue,
        graph,
        _request("uno"),
        max_attempts=3,
        claude_runner=runner,
        claude_store=store,
    )
    retried = queue.dequeue()
    assert graph.calls == 0
    assert retried is not None
    assert retried.attempts == 1
    assert queue.parked() == []


def test_un_429_del_cluster_se_reencola_y_no_parkea(tmp_path: Path) -> None:
    store = ClaudeArtifactStore(root=tmp_path)
    runner = _Runner(error=ClaudeProcessCrashed("el cluster limito la llamada"))
    queue = InMemoryReviewQueue()
    _consume(
        queue,
        _Graph(),
        _request("limite"),
        max_attempts=3,
        claude_runner=runner,
        claude_store=store,
    )
    retried = queue.dequeue()
    assert retried is not None
    assert retried.head_sha == "limite"
    assert queue.parked() == []
    assert queue.dead_letters() == []


def test_get_session_dice_si_y_por_que_no(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    write_session(path, _TOKEN, ttl_s=3600)
    settings = Settings(_env_file=None, claude_session_path=str(path), claude_runner_token="")
    with TestClient(build_app(settings)) as client:
        respuesta = client.get("/session")
    assert respuesta.json() == {"active": True, "reason": None}
    assert _TOKEN not in respuesta.text


@pytest.mark.parametrize(
    ("payload", "blocker"),
    [
        ({"active": True, "reason": None}, None),
        ({"active": False, "reason": "gateway"}, "gateway"),
        ({"active": False, "reason": "session"}, "session"),
        # Un runner anterior a ADR-0038 no manda `reason`: falta la sesión.
        ({"active": False}, "session"),
        ({"active": True}, None),
    ],
)
def test_el_cliente_traduce_el_motivo_de_session(
    payload: dict[str, object], blocker: str | None
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=payload)

    cliente = HttpClaudeRunner(
        url="http://claude-runner",
        token="",
        timeout_s=1,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert cliente.blocker() == blocker
    assert cliente.active() is (blocker is None)


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

        def run(
            self,
            *,
            repo: str,
            pr_id: int,
            head_sha: str,
            base_sha: str,
            max_turns: int | None = None,
            timeout_s: float | None = None,
        ) -> str:
            del repo, pr_id, base_sha, max_turns, timeout_s
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
    dispatch_job(queue, graph, request, max_attempts=3, claude_session=lambda: "session")
    assert graph.calls == 1
    assert queue.parked() == []


def test_el_aviso_de_parqueo_caduca_para_volver_a_avisar() -> None:
    """El #11: el aviso de `ceiling` se reclamaba sin fecha y no se limpiaba.

    `ceiling` no se desbloquea solo, así que sin caducidad el aviso de Teams no
    volvería a salir nunca, ni siquiera tras subir el techo y volver a chocar.
    """
    from agent_review.worker.main import park_claude_job

    queue = InMemoryReviewQueue()
    outbox = InMemoryChatOutbox()
    chat = OutboxChatAdapter(outbox=outbox)
    request = _request("ceiling-aviso")

    park_claude_job(queue, request, reason="ceiling", chat=chat, link=_LINK)
    assert len(_notices(outbox)) == 1
    # El aviso queda retenido, pero **con** fecha: no es eterno.
    assert queue.notice_held("ceiling") is True
    assert queue.notice_ttl_s("ceiling") is not None

    # Un segundo parqueo no repite el aviso mientras siga vigente.
    park_claude_job(queue, request, reason="ceiling", chat=chat, link=_LINK)
    assert _notices(outbox) == []


def test_el_aviso_de_sesion_tambien_caduca() -> None:
    """`session` se suelta al entrar, pero si nadie entra el aviso no es eterno."""
    from agent_review.worker.main import park_claude_job

    queue = InMemoryReviewQueue()
    chat = OutboxChatAdapter(outbox=InMemoryChatOutbox())
    park_claude_job(queue, _request("sesion-aviso"), reason="session", chat=chat, link=_LINK)
    assert queue.notice_ttl_s("session") is not None


def test_el_enfriamiento_del_cupo_sigue_siendo_el_corto() -> None:
    """El cupo sí caduca a los 900 s: no debe heredar el plazo largo del aviso."""
    from agent_review.worker.main import _QUOTA_COOLDOWN_S, park_claude_job

    queue = InMemoryReviewQueue()
    chat = OutboxChatAdapter(outbox=InMemoryChatOutbox())
    park_claude_job(queue, _request("cupo"), reason="quota", chat=chat, link=_LINK)
    ttl = queue.notice_ttl_s("quota")
    assert ttl is not None
    assert ttl <= _QUOTA_COOLDOWN_S + 1


def test_un_429_del_cluster_espera_sin_gastar_intentos(tmp_path: Path) -> None:
    """ADR-0038: ni reintento inmediato ni DLQ. Espera el enfriamiento y vuelve sola."""
    store = ClaudeArtifactStore(root=tmp_path)
    runner = _Runner(error=ClaudeClusterLimited("el cluster limito la llamada"))
    graph = _Graph()
    queue = InMemoryReviewQueue()
    outbox = InMemoryChatOutbox()
    chat = OutboxChatAdapter(outbox=outbox)
    for head_sha in ("uno", "dos"):
        _consume(
            queue,
            graph,
            _request(head_sha),
            max_attempts=3,
            chat=chat,
            claude_runner=runner,
            claude_store=store,
            park_link=_LINK,
        )

    # El segundo ni llama al CLI: el enfriamiento ya está puesto.
    assert runner.calls == ["uno"]
    assert graph.calls == 0
    assert queue.dequeue() is None
    assert queue.dead_letters() == []
    assert [reason for reason, _ in queue.parked_entries()] == ["cluster-wait", "cluster-wait"]
    assert all(item.attempts == 0 for item in queue.parked())
    avisos = _notices(outbox)
    assert len(avisos) == 1
    assert "cluster limita" in avisos[0]
    assert 0 < (queue.notice_ttl_s("cluster-wait") or 0) <= 300

    assert release_cluster_wait(queue) == []
    queue.clear_notice("cluster-wait")
    sueltos = release_cluster_wait(queue)
    assert [item.head_sha for item in sueltos] == ["uno", "dos"]
    assert queue.parked() == []


def test_con_el_gateway_caido_aparca_como_gateway_y_vuelve_solo() -> None:
    queue = InMemoryReviewQueue()
    graph = _Graph()
    outbox = InMemoryChatOutbox()
    chat = OutboxChatAdapter(outbox=outbox)
    for head_sha in ("uno", "dos"):
        dispatch_job(
            queue,
            graph,
            _request(head_sha),
            max_attempts=3,
            chat=chat,
            claude_session=lambda: "gateway",
            park_link=_LINK,
        )
    assert graph.calls == 0
    assert [reason for reason, _ in queue.parked_entries()] == ["gateway", "gateway"]
    avisos = _notices(outbox)
    assert len(avisos) == 1
    assert "Gateway" in avisos[0]
    assert "Inicia sesion" not in avisos[0]

    # Vuelve el health: los dos salen y la próxima caída avisa otra vez.
    assert claude_blocker(queue, lambda: None) is None
    assert queue.parked() == []
    assert not queue.notice_held("gateway")
