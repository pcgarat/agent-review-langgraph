"""L1-L2: el contrato del `ContextReaderPort` y el lector de fixtures.

El puerto es lo unico que conocen los consumidores (009 y 014), asi que las mismas
aserciones se ejecutan contra los tres adaptadores: `FixtureContextReader`,
`VcsContextReader` y `WorkspaceContextReader`. Un adaptador nuevo —o una regresion de
uno actual— que no cumpla el contrato cae aqui. Las particularidades de cada
adaptador viven en sus suites (`test_context_vcs.py`, `test_context_workspace.py`,
`test_read_workspace.py`).

El runner de los fakes aplica el veto de rutas **como el de verdad** (W7), asi que el
`WorkspaceContextReader` se construye sin `denylist` local: el contrato prueba que el
veto vive en el runner.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from context_fakes import (
    APP,
    APP_TEXT,
    ENV,
    build_fixture_reader,
    build_vcs_reader,
    build_workspace_reader,
)

from agent_review.adapters.context_fixture import FixtureContextReader
from agent_review.ports.context import (
    DEFAULT_CONTEXT_DENYLIST,
    ContextReaderPort,
    is_denied,
)


@pytest.fixture(params=["fixture", "vcs", "workspace"])
def reader(request: pytest.FixtureRequest, tmp_path: Path) -> ContextReaderPort:
    if request.param == "fixture":
        return build_fixture_reader(tmp_path)
    if request.param == "vcs":
        # El modo degradado solo ve lo servido: se siembra con los ficheros del diff
        # para que el contrato comun (leer/grep/listar) tenga el mismo arbol.
        reader = build_vcs_reader()
        reader.seed()
        return reader
    return build_workspace_reader()


# --- Contrato compartido ----------------------------------------------------------


def test_los_tres_adaptadores_cumplen_el_protocolo(reader: ContextReaderPort) -> None:
    assert isinstance(reader, ContextReaderPort)


def test_read_file_devuelve_el_contenido(reader: ContextReaderPort) -> None:
    assert reader.read_file(APP) == APP_TEXT


def test_read_file_con_rango_devuelve_las_lineas_pedidas(reader: ContextReaderPort) -> None:
    assert reader.read_file(APP, start=3, end=3) == "needle = 1"


def test_read_file_de_una_ruta_ausente_devuelve_none(reader: ContextReaderPort) -> None:
    assert reader.read_file("src/no-existe.py") is None


def test_read_file_de_una_ruta_vetada_devuelve_none(reader: ContextReaderPort) -> None:
    assert reader.read_file(ENV) is None


def test_grep_es_literal_y_formatea_path_linea_texto(reader: ContextReaderPort) -> None:
    assert reader.grep("needle") == [f"{APP}:3: needle = 1", f"{APP}:4: print(needle)"]


def test_grep_no_devuelve_las_rutas_vetadas(reader: ContextReaderPort) -> None:
    assert reader.grep("SECRET") == []


def test_grep_sin_coincidencias_devuelve_lista_vacia(reader: ContextReaderPort) -> None:
    assert reader.grep("no-aparece-en-ningun-sitio") == []


def test_list_dir_lista_nombres_ordenados(reader: ContextReaderPort) -> None:
    assert reader.list_dir("src") == ["app.py"]


def test_is_denied_casa_por_ruta_y_por_nombre() -> None:
    # `.env*` por si solo no casaria `config/.env` (el glob no cruza `/`): el veto
    # mira tambien el basename, que es lo que hace util el patron del perfil.
    assert is_denied(ENV, DEFAULT_CONTEXT_DENYLIST)
    assert is_denied("config/secrets/db.yaml", DEFAULT_CONTEXT_DENYLIST)
    assert is_denied("keys/server.pem", DEFAULT_CONTEXT_DENYLIST)
    assert not is_denied(APP, DEFAULT_CONTEXT_DENYLIST)


# --- `FixtureContextReader`: arbol local, determinista (ADR-0017) ----------------


def test_el_fixture_no_sale_de_la_raiz(tmp_path: Path) -> None:
    reader = build_fixture_reader(tmp_path)
    assert reader.read_file("../../etc/passwd") is None
    assert reader.read_file("src/../../../etc/passwd") is None


def test_el_fixture_sirve_los_mismos_bytes_para_el_replay(tmp_path: Path) -> None:
    first = build_fixture_reader(tmp_path)
    second = build_fixture_reader(tmp_path)
    assert first.read_file(APP) == second.read_file(APP)
    assert first.grep("needle") == second.grep("needle")


def test_el_fixture_acota_los_bytes_de_un_fichero(tmp_path: Path) -> None:
    build_fixture_reader(tmp_path)
    (tmp_path / "big.txt").write_text("x" * 300, encoding="utf-8")
    reader = FixtureContextReader(tmp_path, max_file_bytes=100)
    assert (reader.read_file("big.txt") or "") == "x" * 100


def test_el_fixture_respeta_el_rango_al_recortar(tmp_path: Path) -> None:
    build_fixture_reader(tmp_path)
    reader = build_fixture_reader(tmp_path)
    assert reader.read_file(APP, start=1, end=2) == "import os\n"


def test_el_fixture_ignora_la_ref(tmp_path: Path) -> None:
    # No hay dos lados: el mismo arbol sirve `head` y `base` (Q2 de 016).
    reader = build_fixture_reader(tmp_path)
    assert reader.read_file(APP, ref="base") == reader.read_file(APP, ref="head")


def test_el_fixture_lista_la_raiz(tmp_path: Path) -> None:
    reader = build_fixture_reader(tmp_path)
    assert reader.list_dir("") == ["README.md", "config", "src"]


def test_el_fixture_grep_acota_por_glob(tmp_path: Path) -> None:
    reader = build_fixture_reader(tmp_path)
    assert reader.grep("needle", glob="**/*.py") == [
        f"{APP}:3: needle = 1",
        f"{APP}:4: print(needle)",
    ]
    assert reader.grep("needle", glob="**/*.md") == []


def test_el_fixture_grep_no_entra_en_las_vetadas(tmp_path: Path) -> None:
    reader = build_fixture_reader(tmp_path)
    assert reader.grep("SECRET") == []
