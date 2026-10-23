"""Lanzar una review desde el panel con la URL del pull request."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_review.queue import InMemoryReviewQueue
from agent_review.web import main as web_main
from agent_review.web.launch import LaunchError, LaunchService

_URL = "https://github.com/cqm/emotivoo-v2/pull/42"


@pytest.fixture
def client() -> TestClient:
    return TestClient(web_main.app)


def _service(expected: str = "cqm/emotivoo-v2") -> tuple[LaunchService, InMemoryReviewQueue]:
    queue = InMemoryReviewQueue()
    return LaunchService(queue, expected_repo=expected), queue


def test_encola_el_lanzamiento_sin_sha_y_audita_al_actor(client: TestClient) -> None:
    service, queue = _service()
    with client as c:
        web_main.app.state.launcher = service
        response = c.post(
            "/api/launch",
            headers={"X-Panel-Session": "ana:operador"},
            json={"url": _URL},
        )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "encolada"
    assert body["repo"] == "cqm/emotivoo-v2"
    assert body["pr_id"] == 42
    assert body["pipeline_id"] == "panel"
    assert body["actor"] == "ana"
    assert "review_key" not in body
    queued = queue.dequeue()
    assert queued is not None
    assert queued.launch is True
    assert queued.head_sha == ""
    assert queued.review_key == "cqm/emotivoo-v2:42::panel"


def test_un_repo_distinto_no_se_encola(client: TestClient) -> None:
    service, queue = _service()
    with client as c:
        web_main.app.state.launcher = service
        response = c.post(
            "/api/launch",
            headers={"X-Panel-Session": "ana:operador"},
            json={"url": "https://github.com/otra/cosa/pull/1"},
        )
    assert response.status_code == 400
    assert "no admitido" in response.text
    assert queue.dequeue() is None


def test_una_url_irreconocible_es_400(client: TestClient) -> None:
    service, _queue = _service()
    with client as c:
        web_main.app.state.launcher = service
        response = c.post(
            "/api/launch",
            headers={"X-Panel-Session": "ana:operador"},
            json={"url": "https://example.com/no"},
        )
    assert response.status_code == 400


def test_sin_url_es_400(client: TestClient) -> None:
    service, _queue = _service()
    with client as c:
        web_main.app.state.launcher = service
        response = c.post(
            "/api/launch",
            headers={"X-Panel-Session": "ana:operador"},
            json={},
        )
    assert response.status_code == 400


def test_un_formulario_de_otra_web_no_lanza(client: TestClient) -> None:
    service, queue = _service()
    with client as c:
        web_main.app.state.launcher = service
        c.cookies.set("panel_session", "ana:operador")
        response = c.post(
            "/api/launch",
            headers={"Content-Type": "text/plain", "Origin": "https://evil.example"},
            content=_URL.encode(),
        )
    assert response.status_code == 403
    assert queue.dequeue() is None


def test_el_lector_no_lanza(client: TestClient) -> None:
    service, queue = _service()
    with client as c:
        web_main.app.state.launcher = service
        response = c.post(
            "/api/launch",
            headers={"X-Panel-Session": "ana:lector"},
            json={"url": _URL},
        )
    assert response.status_code == 403
    assert queue.dequeue() is None


def test_acepta_el_mismo_repo_con_otra_capitalizacion() -> None:
    service, queue = _service("cqm/emotivoo-v2")
    launched = service.launch("https://github.com/CQM/Emotivoo-V2/pull/7", actor="ana")
    assert launched.repo == "cqm/emotivoo-v2"
    assert queue.dequeue() is not None


def test_sin_repo_configurado_conserva_el_de_la_url() -> None:
    service, _queue = _service("")
    launched = service.launch(_URL, actor="ana")
    assert launched.repo == "cqm/emotivoo-v2"


def test_el_error_de_parseo_es_launch_error() -> None:
    service, _queue = _service()
    with pytest.raises(LaunchError):
        service.launch("no", actor="ana")
