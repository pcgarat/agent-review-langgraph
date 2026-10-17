"""L10: fabrica de lectores y degradacion al VCS (016 W8, H3 de 014).

La fabrica es lo unico que ve el consumidor: recibe `(repo, head_sha, base_sha)` y
devuelve un `ContextReaderPort`. Aqui se fija que, sin runner, devuelve el modo VCS
y que, con el runner caido, el lector degrada sin tumbar la review.

La degradacion se prueba sobre `_DegradingReader` con un primario que falla, sin
HTTP: es la pieza que decide el cambio de lector.
"""

from __future__ import annotations

from typing import Any

import pytest
from context_fakes import APP, APP_TEXT, HEAD, FakeVcs, build_vcs_reader

from agent_review.adapters.context_factory import _DegradingReader, build_reader_factory
from agent_review.adapters.context_vcs import VcsContextReader
from agent_review.adapters.context_workspace import ContextError, WorkspaceContextReader
from agent_review.ports.context import ReadResult, ToolResult
from agent_review.settings import Settings

DIFF = "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"sandbox_runner_url": "", "sandbox_runner_token": ""}
    base.update(overrides)
    return Settings(**base)


def _factory(vcs: FakeVcs | None = None, **overrides: Any):
    return build_reader_factory(
        _settings(**overrides), vcs=vcs or FakeVcs(), diff=DIFF, denylist=(".env*",)
    )


class ExplodingReader:
    """Primario que siempre falla, como un runner caido (W8)."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def read_file(self, path: str, **kwargs: Any) -> ReadResult | None:
        self.calls += 1
        raise self.exc

    def grep(self, pattern: str, *, glob: str | None = None) -> ToolResult:
        self.calls += 1
        raise self.exc

    def list_dir(self, path: str) -> ToolResult:
        self.calls += 1
        raise self.exc


# --- Eleccion del adaptador -------------------------------------------------------


def test_sin_runner_la_fabrica_devuelve_el_modo_vcs() -> None:
    assert isinstance(_factory()("cqm/x", HEAD, "base1"), VcsContextReader)


def test_sin_runner_url_de_espacios_se_usa_el_vcs() -> None:
    reader = _factory(sandbox_runner_url="   ")("cqm/x", HEAD, "base1")
    assert isinstance(reader, VcsContextReader)


def test_con_runner_la_fabrica_devuelve_un_lector_con_workspace() -> None:
    reader = _factory(sandbox_runner_url="http://runner:8099")("cqm/x", HEAD, "base1")
    assert isinstance(reader, _DegradingReader)
    assert isinstance(reader._active, WorkspaceContextReader)


def test_sin_runner_el_lector_sirve_lo_que_toca_el_diff() -> None:
    reader = _factory()("cqm/x", HEAD, "base1")
    reading = reader.read_file(APP)
    assert reading is not None
    assert reading.content == APP_TEXT
    assert reader.list_dir("src").items == ("app.py",)


def test_el_diff_del_lote_alimenta_el_modo_degradado() -> None:
    reader = build_reader_factory(_settings(), vcs=FakeVcs(), diff=DIFF, denylist=())(
        "cqm/x", HEAD, "base1"
    )
    assert reader.list_dir("").items == ("src",)


def test_cada_lote_recibe_su_propio_lector() -> None:
    factory = _factory(sandbox_runner_url="http://runner:8099")
    first = factory("cqm/x", HEAD, "base1")
    second = factory("cqm/y", "otro", "base2")
    assert first is not second


# --- Degradacion -------------------------------------------------------------------


def _degrading(exc: Exception) -> tuple[_DegradingReader, ExplodingReader]:
    primary = ExplodingReader(exc)
    fallback = build_vcs_reader()
    return _DegradingReader(primary=primary, fallback=fallback), primary


@pytest.mark.parametrize("exc", [ContextError("4xx"), RuntimeError("runner caido")])
def test_una_operacion_fallida_degrada_al_vcs(exc: Exception) -> None:
    reader, primary = _degrading(exc)
    if isinstance(exc, ContextError):
        reading = reader.read_file(APP)
        assert reading is not None
        assert reading.content == APP_TEXT, "sirve desde el VCS"
    else:
        # Un fallo que no es de contexto (bug del adaptador) sube: no se enmascara.
        with pytest.raises(RuntimeError):
            reader.read_file(APP)
        return
    assert reader.grep("needle").items == (f"{APP}:3: needle = 1", f"{APP}:4: print(needle)")
    assert reader.list_dir("src").items == ("app.py",)
    assert primary.calls == 1


def test_la_degradacion_es_pegajosa() -> None:
    reader, primary = _degrading(ContextError("4xx"))
    reader.read_file(APP)
    reader.read_file(APP)
    reader.grep("needle")
    assert primary.calls == 1, "solo se intenta el runner una vez"


def test_la_degradacion_siembra_el_modo_vcs() -> None:
    reader, _ = _degrading(ContextError("4xx"))
    reader.read_file(APP)
    # El fallback queda sembrado con lo que el diff declara: `list_dir` ya responde.
    assert reader.list_dir("").items == ("src",)


def test_un_fallo_del_fallback_sube_como_lectura_fallida() -> None:
    primary = ExplodingReader(ContextError("4xx"))
    fallback = build_vcs_reader(FakeVcs(files={}))
    reader = _DegradingReader(primary=primary, fallback=fallback)
    assert reader.read_file(APP) is None, "el modo VCS devuelve None, no revienta"


def test_una_ruta_ausente_en_el_primario_no_degrada() -> None:
    # `None` no es un fallo del runner: es "el fichero no esta". Degradar por eso
    # gastaria el VCS en cada lectura legitima de un fichero inexistente.
    class Missing:
        def read_file(self, path: str, **kwargs: Any) -> ReadResult | None:
            return None

        def grep(self, pattern: str, *, glob: str | None = None) -> ToolResult:
            return ToolResult()

        def list_dir(self, path: str) -> ToolResult:
            return ToolResult()

    fallback = build_vcs_reader()
    reader = _DegradingReader(primary=Missing(), fallback=fallback)
    assert reader.read_file(APP) is None, "no hay nada que el VCS pueda aportar aqui"
