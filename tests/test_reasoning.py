"""Razonamiento de los revisores (021): el sink Redis, su lector y el cableado.

El adaptador corre sobre `fakeredis` con streams de verdad; el revisor se prueba en
`test_llm_reviewer.py` contra el doble en memoria.
"""

from __future__ import annotations

import fakeredis
import pytest

from agent_review.adapters.reasoning_redis import (
    FLUSH_BYTES,
    RedisReasoningReader,
    RedisReasoningSink,
)
from agent_review.ports.reasoning import ReasoningEvent, reasoning_key


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _ev(kind: str, text: str = "", batch: int = 1, **meta) -> ReasoningEvent:
    return ReasoningEvent(kind=kind, role="correctness", batch=batch, total=2, text=text, meta=meta)  # type: ignore[arg-type]


@pytest.fixture
def server() -> fakeredis.FakeServer:
    return fakeredis.FakeServer()


def _client(server: fakeredis.FakeServer) -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(server=server, decode_responses=True)


def test_agrupa_los_trozos_y_cierra_el_lote_en_orden(server) -> None:
    clock = _Clock()
    sink = RedisReasoningSink(url="", client=_client(server), clock=clock)
    sink.emit("rk", _ev("start", files=["a.py"]))
    for piece in ("mi", "ro ", "el ", "lote"):
        sink.emit("rk", _ev("reasoning", piece))
    sink.emit("rk", _ev("end", prompt_tokens=10))

    events = [event for _, event in RedisReasoningReader(url="", client=_client(server)).read("rk")]
    # Cuatro trozos seguidos sin pasar el intervalo: una sola entrada de texto.
    assert [(e.kind, e.text) for e in events] == [
        ("start", ""),
        ("reasoning", "miro el lote"),
        ("end", ""),
    ]
    assert events[0].meta == {"files": ["a.py"]}
    assert events[2].meta == {"prompt_tokens": 10}
    assert (events[0].role, events[0].batch, events[0].total) == ("correctness", 1, 2)


def test_vacia_por_tamano_y_por_tiempo(server) -> None:
    clock = _Clock()
    sink = RedisReasoningSink(url="", client=_client(server), clock=clock)
    sink.emit("rk", _ev("reasoning", "x" * FLUSH_BYTES))  # por tamaño
    sink.emit("rk", _ev("reasoning", "a"))
    clock.now = 1.0
    sink.emit("rk", _ev("reasoning", "b"))  # por tiempo
    texts = [e.text for _, e in RedisReasoningReader(url="", client=_client(server)).read("rk")]
    assert texts == ["x" * FLUSH_BYTES, "ab"]


def test_cada_escritura_renueva_el_ttl_y_el_tope_acota_el_stream(server) -> None:
    redis = _client(server)
    sink = RedisReasoningSink(url="", client=redis, ttl_s=864000, max_bytes=FLUSH_BYTES * 3)
    for batch in range(1000):
        sink.emit("rk", _ev("start", batch=batch))
    assert 0 < redis.ttl(reasoning_key("rk")) <= 864000
    # `MAXLEN ~` recorta por nodos enteros (~100 entradas): acota, no es exacto. Con
    # el tope real (10 MB / 4 KB = 2 560 entradas) el error es despreciable.
    assert redis.xlen(reasoning_key("rk")) <= 100


def test_lotes_paralelos_no_mezclan_sus_trozos(server) -> None:
    sink = RedisReasoningSink(url="", client=_client(server), clock=_Clock())
    sink.emit("rk", _ev("reasoning", "uno-", batch=1))
    sink.emit("rk", _ev("reasoning", "dos-", batch=2))
    sink.emit("rk", _ev("reasoning", "uno", batch=1))
    sink.emit("rk", _ev("end", batch=2))
    sink.emit("rk", _ev("end", batch=1))
    events = [e for _, e in RedisReasoningReader(url="", client=_client(server)).read("rk")]
    assert [(e.batch, e.kind, e.text) for e in events] == [
        (2, "reasoning", "dos-"),
        (2, "end", ""),
        (1, "reasoning", "uno-uno"),
        (1, "end", ""),
    ]


def test_un_redis_caido_no_lanza(server) -> None:
    redis = _client(server)
    server.connected = False
    sink = RedisReasoningSink(url="", client=redis)
    sink.emit("rk", _ev("start"))  # no lanza
    reader = RedisReasoningReader(url="", client=redis)
    assert reader.read("rk") == []
    assert reader.exists("rk") is False


def test_el_lector_sigue_desde_un_id(server) -> None:
    sink = RedisReasoningSink(url="", client=_client(server))
    reader = RedisReasoningReader(url="", client=_client(server))
    sink.emit("rk", _ev("start"))
    [(first_id, _)] = reader.read("rk")
    sink.emit("rk", _ev("error", "sin presupuesto"))
    later = reader.read("rk", after=first_id)
    assert [(e.kind, e.text) for _, e in later] == [("error", "sin presupuesto")]
    assert reader.read("rk", after=later[-1][0], block_ms=10) == []
    assert reader.exists("rk") and not reader.exists("otra")


def test_el_worker_solo_crea_el_sink_si_se_pide() -> None:
    from agent_review.settings import Settings
    from agent_review.worker.main import build_reasoning_sink

    assert build_reasoning_sink(Settings(reasoning_stream_enabled=False)) is None
    assert build_reasoning_sink(Settings(reasoning_stream_enabled=True, redis_url="")) is None
    sink = build_reasoning_sink(
        Settings(reasoning_stream_enabled=True, redis_url="redis://localhost:6399/0")
    )
    assert isinstance(sink, RedisReasoningSink)
