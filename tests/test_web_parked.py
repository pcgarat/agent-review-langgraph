"""Los jobs aparcados en el estado del panel (iniciativa 030, bloque L).

`reviews:claude:parked` guarda los jobs que el worker apartó porque no podía
ejecutarlos. Hasta la 030 no aparecían en ninguna parte del panel: un lanzamiento
sin sesión de Claude devolvía `202` y desaparecía. Aquí se prueba que el estado
los lee **con su motivo**, que es lo único que dice qué los desatasca.
"""

from __future__ import annotations

import json

import fakeredis
import pytest

from agent_review.domain.models import ReviewRequest
from agent_review.queue import RedisReviewQueue, parse_parked
from agent_review.web.read_model import RedisPanelReadModel


def _request(pr_id: int, *, pipeline: str = "panel") -> ReviewRequest:
    return ReviewRequest(
        repo="pcgarat/agent-review-langgraph",
        pr_id=pr_id,
        head_sha="abc123",
        pipeline_id=pipeline,
    )


def _parked_payload(request: ReviewRequest, reason: str) -> str:
    return json.dumps({"reason": reason, "request": request.model_dump(mode="json")})


def _read_model(client: fakeredis.FakeRedis) -> RedisPanelReadModel:
    return RedisPanelReadModel(url="", client=client)  # type: ignore[arg-type]


def test_el_estado_trae_los_aparcados_con_su_motivo() -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    # Un lanzamiento desde el panel: no trae commit, lo resuelve el worker
    # (ADR-0029), y `launch` es lo que lo distingue de un job del CI.
    launched = ReviewRequest(
        repo="pcgarat/agent-review-langgraph",
        pr_id=19,
        head_sha="",
        pipeline_id="panel",
        launch=True,
    )
    client.rpush("reviews:claude:parked", _parked_payload(launched, "session"))
    client.rpush("reviews:claude:parked", _parked_payload(_request(20), "ceiling"))

    parked = _read_model(client).state().parked

    assert [(item.pr_id, item.reason) for item in parked] == [(19, "session"), (20, "ceiling")]
    assert parked[0].repo == "pcgarat/agent-review-langgraph"
    assert parked[0].pipeline_id == "panel"
    assert parked[0].launch is True


def test_los_aparcados_respetan_el_orden_en_que_llegaron() -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    for pr_id in (1, 2, 3):
        client.rpush("reviews:claude:parked", _parked_payload(_request(pr_id), "session"))

    assert [item.pr_id for item in _read_model(client).state().parked] == [1, 2, 3]


def test_sin_parking_la_lista_es_vacia() -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    assert _read_model(client).state().parked == []


def test_un_payload_ilegible_no_llega_a_la_pantalla() -> None:
    """Una entrada rota se descarta: pintarla obligaria a inventar el motivo."""
    client = fakeredis.FakeRedis(decode_responses=True)
    client.rpush("reviews:claude:parked", "no es json")
    client.rpush("reviews:claude:parked", json.dumps({"reason": "inventado", "request": {}}))
    client.rpush("reviews:claude:parked", _parked_payload(_request(7), "session"))

    parked = _read_model(client).state().parked

    assert [item.pr_id for item in parked] == [7]


def test_redis_caido_deja_los_aparcados_vacios_sin_tumbar_el_estado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El panel es observabilidad: una fuente caída vacía su sección, no el estado."""
    client = fakeredis.FakeRedis(decode_responses=True)
    client.rpush("reviews:claude:parked", _parked_payload(_request(19), "session"))
    model = _read_model(client)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise __import__("redis").exceptions.ConnectionError("servidor reiniciando")

    monkeypatch.setattr(client, "lrange", _boom)
    state = model.state()

    assert state.parked == []
    # El resto del estado sigue su camino: la sección es la que falla, no el panel.
    assert state.queue == []


def test_la_cola_expone_el_motivo_sin_cambiar_parked() -> None:
    """`parked()` sigue devolviendo requests; el motivo va por `parked_entries()`."""
    queue = RedisReviewQueue(url="redis://localhost:6379/0")
    queue._client = fakeredis.FakeRedis(decode_responses=True)  # type: ignore[assignment]
    queue.park(_request(1), reason="session")
    queue.park(_request(2), reason="quota")

    assert [item.pr_id for item in queue.parked()] == [1, 2]
    assert queue.parked_entries() == [("session", queue.parked()[0]), ("quota", queue.parked()[1])]


def test_parse_parked_es_el_mismo_parseo_para_el_worker_y_el_panel() -> None:
    request = _request(19)
    entries = parse_parked([_parked_payload(request, "quota-wait"), "roto"])

    assert len(entries) == 1
    reason, parsed = entries[0]
    assert reason == "quota-wait"
    assert parsed.pr_id == 19
