"""OP29-OP34: comandos del panel (019, bloque 5).

Cubre la semantica por estado (spec §5.4/§5.5) en el `CommandService` y los
endpoints que la exponen:

- **Cancelar antes de publicar** (en cola, en vuelo o pausa humana): limpio, sin
  comentarios; deja la marca y encola el `request` marcado si estaba pausado.
- **Cancelar despues de publicar** (pausa de fix): solo lo pendiente (`CANCELLED_PENDING`),
  la wave se reanuda como no verificable y los comentarios del PR se quedan.
- **Reintentar**: reencola un descarte de la DLQ con `attempts=0`.
- **Reanudar**: reencola la reanudacion de una pausa humana (resuelve como `expired`).
- El rol `lector` no ejecuta comandos (`403`) y un comando que no aplica da `409`.
"""

from __future__ import annotations

import fakeredis
import pytest
from fastapi.testclient import TestClient

from agent_review.domain.models import (
    ApprovalRequest,
    FindingAction,
    FixWaitRequest,
    PendingDecision,
    ReviewRequest,
    Severity,
    VerifyJob,
)
from agent_review.domain.paused import PauseKind
from agent_review.paused_store import InMemoryPausedStore
from agent_review.queue import InMemoryReviewQueue
from agent_review.web import main as web_main
from agent_review.web.commands import (
    CommandEffect,
    CommandError,
    CommandService,
    build_command_service,
)
from agent_review.worker.cancel import RedisCancellation

_REVIEW_KEY = "cqm/emotivoo-v2:42:abc:7"


def _marker() -> RedisCancellation:
    return RedisCancellation(url="redis://localhost:6379/0", client=fakeredis.FakeRedis())


def _service() -> tuple[
    CommandService, InMemoryReviewQueue, InMemoryPausedStore, RedisCancellation
]:
    queue = InMemoryReviewQueue()
    paused = InMemoryPausedStore()
    marker = _marker()
    return (
        build_command_service(queue=queue, paused=paused, cancellation=marker),
        queue,
        paused,
        marker,
    )


def _request(**overrides: object) -> ReviewRequest:
    base = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=42, head_sha="abc", pipeline_id="7")
    return base.model_copy(update=overrides)


def _pause_human(paused: InMemoryPausedStore, request: ReviewRequest) -> None:
    approval = ApprovalRequest(
        review_key=request.review_key,
        repo=request.repo,
        pr_id=request.pr_id,
        head_sha=request.head_sha,
        pending=[
            PendingDecision(
                finding_id="f1",
                proposed_action=FindingAction.ESCALATE,
                reason="r",
                matched_rule="rule",
                policy_version="1",
                title="t",
                file="f.php",
                line=1,
                severity=Severity.P1,
            )
        ],
    )
    paused.pause(request, approval, kind=PauseKind.HUMAN)


def _pause_fix(paused: InMemoryPausedStore, request: ReviewRequest) -> None:
    wave = FixWaitRequest(
        review_key=request.review_key,
        repo=request.repo,
        pr_id=request.pr_id,
        head_sha=request.head_sha,
        jobs=[
            VerifyJob(
                run_key="r:f2:1",
                finding_id="f2",
                attempt=1,
                repo=request.repo,
                head_sha=request.head_sha,
                patch="",
            )
        ],
    )
    paused.pause(request, wave, kind=PauseKind.FIX)


# --- OP29/OP30: cancelar por estado ----------------------------------------------


def test_cancelar_una_review_en_cola_deja_la_marca_y_no_publica() -> None:
    service, queue, _paused, marker = _service()

    effect = service.cancel(_REVIEW_KEY, actor="ana")

    assert effect is CommandEffect.CANCELLED
    assert marker.is_cancelled(_REVIEW_KEY) is True
    assert queue.dequeue() is None, "no se encola nada: la frontera de nodo aborta"


def test_cancelar_una_pausa_humana_encola_el_request_marcado() -> None:
    """Antes de publicar: cancelacion limpia; el worker aborta al desencolar."""
    service, queue, paused, marker = _service()
    _pause_human(paused, _request())

    effect = service.cancel(_REVIEW_KEY, actor="ana")

    assert effect is CommandEffect.CANCELLED
    assert marker.is_cancelled(_REVIEW_KEY) is True
    queued = queue.dequeue()
    assert queued is not None and queued.cancelled is True


def test_cancelar_una_pausa_de_fix_solo_cancela_lo_pendiente() -> None:
    """Despues de publicar: los comentarios se quedan; la wave se descarta (W6/R2).

    Sin marca: el worker abortaria con ella el propio job que resuelve la wave.
    """
    service, queue, paused, marker = _service()
    _pause_fix(paused, _request())

    effect = service.cancel(_REVIEW_KEY, actor="ana")

    assert effect is CommandEffect.CANCELLED_PENDING
    assert marker.is_cancelled(_REVIEW_KEY) is False
    queued = queue.dequeue()
    assert queued is not None and queued.cancelled is False
    assert queued.fix_resolution is not None
    assert [item.outcome for item in queued.fix_resolution.items] == ["error"]


# --- OP31: reintentar ------------------------------------------------------------


def test_reintentar_un_descarte_lo_reencola_con_attempts_cero() -> None:
    service, queue, _paused, _marker = _service()
    queue.dead_letter(_request(attempts=3), reason="intentos agotados")

    effect = service.retry(_REVIEW_KEY, actor="ana")

    assert effect is CommandEffect.RETRIED
    queued = queue.dequeue()
    assert queued is not None and queued.attempts == 0


def test_reintentar_retira_la_marca_de_una_review_cancelada() -> None:
    """Sin retirarla, el worker abortaria el reintento al desencolarlo."""
    service, queue, _paused, marker = _service()
    marker.mark(_REVIEW_KEY)
    queue.dead_letter(_request(attempts=3), reason="intentos agotados")

    service.retry(_REVIEW_KEY, actor="ana")

    assert marker.is_cancelled(_REVIEW_KEY) is False


def test_reintentar_algo_que_no_esta_en_la_dlq_falla() -> None:
    service, _queue, _paused, _marker = _service()

    with pytest.raises(CommandError, match="DLQ"):
        service.retry(_REVIEW_KEY, actor="ana")


# --- OP32: reanudar --------------------------------------------------------------


def test_reanudar_una_pausa_humana_encola_la_reanudacion() -> None:
    service, queue, paused, _marker = _service()
    _pause_human(paused, _request())

    effect = service.resume(_REVIEW_KEY, actor="ana")

    assert effect is CommandEffect.RESUMED
    queued = queue.dequeue()
    assert queued is not None and queued.decision is not None
    assert queued.decision.actor == "ana"
    assert all(item.outcome.value == "expired" for item in queued.decision.items)


def test_reanudar_una_pausa_de_fix_falla() -> None:
    service, _queue, paused, _marker = _service()
    _pause_fix(paused, _request())

    with pytest.raises(CommandError, match="humana"):
        service.resume(_REVIEW_KEY, actor="ana")


# --- OP34: endpoints -------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    test_client = TestClient(web_main.app)
    return test_client


def test_el_rol_lector_no_ejecuta_comandos(client: TestClient) -> None:
    with client as _c:
        response = _c.post(
            f"/api/reviews/{_REVIEW_KEY}/cancel",
            headers={"X-Panel-Session": "ana:lector"},
            json={"actor": "ana"},
        )
    assert response.status_code == 403


def test_cancelar_por_http_devuelve_el_efecto_y_audita_al_actor(client: TestClient) -> None:
    service, _queue, _paused, marker = _service()
    with client as _c:
        web_main.app.state.commands = service
        response = _c.post(
            f"/api/reviews/{_REVIEW_KEY}/cancel",
            headers={"X-Panel-Session": "ana:operador"},
            json={"actor": "ana"},
        )
    assert response.status_code == 200
    assert response.json()["effect"] == "cancelled"
    assert response.json()["actor"] == "ana"
    assert marker.is_cancelled(_REVIEW_KEY) is True


def test_un_comando_que_no_aplica_responde_409(client: TestClient) -> None:
    service, _queue, _paused, _marker = _service()
    with client as _c:
        web_main.app.state.commands = service
        response = _c.post(
            f"/api/reviews/{_REVIEW_KEY}/retry",
            headers={"X-Panel-Session": "ana:operador"},
            json={"actor": "ana"},
        )
    assert response.status_code == 409


# --- CSRF: los comandos por cookie exigen JSON del mismo origen --------------------


def _cookie_post(client: TestClient, *, headers: dict[str, str], **kwargs: object) -> int:
    service, _queue, _paused, _marker = _service()
    with client as _c:
        web_main.app.state.commands = service
        _c.cookies.set("panel_session", "ana:operador")
        response = _c.post(f"/api/reviews/{_REVIEW_KEY}/cancel", headers=headers, **kwargs)
    return response.status_code


def test_un_formulario_de_otra_web_no_ejecuta_comandos(client: TestClient) -> None:
    status_code = _cookie_post(
        client,
        headers={"Content-Type": "text/plain", "Origin": "https://evil.example"},
        content=b'{"actor": "x"}',
    )
    assert status_code == 403


def test_un_fetch_json_de_otro_origen_no_ejecuta_comandos(client: TestClient) -> None:
    status_code = _cookie_post(
        client, headers={"Origin": "https://evil.example"}, json={"actor": "ana"}
    )
    assert status_code == 403


def test_un_comando_por_cookie_sin_json_se_rechaza(client: TestClient) -> None:
    assert _cookie_post(client, headers={}, data={"actor": "ana"}) == 403


def test_la_ui_del_mismo_origen_ejecuta_comandos_por_cookie(client: TestClient) -> None:
    status_code = _cookie_post(
        client,
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
        json={"actor": "ana"},
    )
    assert status_code == 200
