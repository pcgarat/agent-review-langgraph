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
    README,
    build_fixture_reader,
    build_vcs_reader,
    build_workspace_reader,
)

from agent_review.adapters.context_fixture import FixtureContextReader
from agent_review.ports.context import (
    DEFAULT_CONTEXT_DENYLIST,
    ContextReaderPort,
    ToolError,
    is_denied,
    portable_pattern,
    slice_lines,
    truncate_bytes,
)


def _text(reader: ContextReaderPort, path: str, **kwargs: object) -> str:
    """Contenido de una lectura del contrato; falla si el lector no la sirve."""
    reading = reader.read_file(path, **kwargs)  # type: ignore[arg-type]
    assert reading is not None
    return reading.content


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
    assert _text(reader, APP) == APP_TEXT


def test_read_file_declara_el_tamano_real(reader: ContextReaderPort) -> None:
    reading = reader.read_file(APP)
    assert reading is not None
    assert reading.total_bytes == len(APP_TEXT.encode("utf-8"))
    assert reading.truncated is False
    assert reading.served_range is None


def test_read_file_con_rango_devuelve_las_lineas_pedidas(reader: ContextReaderPort) -> None:
    reading = reader.read_file(APP, start=3, end=3)
    assert reading is not None
    assert reading.content == "needle = 1"
    assert reading.served_range == (3, 3)


@pytest.mark.parametrize(
    ("start", "end"),
    [(0, None), (None, 0), (-1, None), (5, 3)],
)
def test_read_file_rechaza_un_rango_imposible(
    reader: ContextReaderPort, start: int | None, end: int | None
) -> None:
    # Una peticion mal formada es error, no «sin coincidencias» ni el fichero entero.
    with pytest.raises(ValueError):
        reader.read_file(APP, start=start, end=end)


def test_read_file_de_una_ruta_ausente_devuelve_none(reader: ContextReaderPort) -> None:
    assert reader.read_file("src/no-existe.py") is None


def test_read_file_de_una_ruta_vetada_devuelve_none(reader: ContextReaderPort) -> None:
    assert reader.read_file(ENV) is None


def test_grep_es_literal_y_formatea_path_linea_texto(reader: ContextReaderPort) -> None:
    found = reader.grep("needle")
    assert found.items == (f"{APP}:3: needle = 1", f"{APP}:4: print(needle)")
    assert found.total == 2
    assert found.truncated is False


def test_grep_no_devuelve_las_rutas_vetadas(reader: ContextReaderPort) -> None:
    assert reader.grep("SECRET").items == ()


def test_grep_sin_coincidencias_devuelve_lista_vacia(reader: ContextReaderPort) -> None:
    assert reader.grep("no-aparece-en-ningun-sitio").items == ()


def test_list_dir_lista_nombres_ordenados(reader: ContextReaderPort) -> None:
    listing = reader.list_dir("src")
    assert listing.items == ("app.py",)
    assert listing.total == 1
    assert listing.truncated is False


def test_grep_acota_a_un_subarbol(reader: ContextReaderPort) -> None:
    assert reader.grep("needle", path="src").items == (
        f"{APP}:3: needle = 1",
        f"{APP}:4: print(needle)",
    )
    assert reader.grep("needle", path="otro").items == ()


def test_grep_files_only_devuelve_rutas(reader: ContextReaderPort) -> None:
    found = reader.grep("needle", files_only=True)
    assert found.items == (APP,)
    assert found.total == 1, "un fichero con dos coincidencias cuenta una vez"


def test_grep_en_regex_casa_el_patron_canonico(reader: ContextReaderPort) -> None:
    # El mismo patron canonico en las cuatro superficies (K1): la cadena que emite
    # el normalizador es valida en `re` y en `git grep -E`.
    found = reader.grep(r"needle = \d", mode="regex")
    assert found.items == (f"{APP}:3: needle = 1",)


@pytest.mark.parametrize(
    "pattern",
    [r"[[:alpha:]]+", r"\bneedle\b", r"(?=needle)", r"(needle)\1", r"[\t]"],
)
def test_grep_rechaza_un_patron_no_portable(reader: ContextReaderPort, pattern: str) -> None:
    # Cada motor lo lee distinto: un error visible es mejor que dos resultados.
    with pytest.raises(ToolError):
        reader.grep(pattern, mode="regex")


def test_grep_rechaza_un_patron_invalido(reader: ContextReaderPort) -> None:
    with pytest.raises(ToolError):
        reader.grep("needle(", mode="regex")


def test_grep_glob_se_mide_desde_path(reader: ContextReaderPort) -> None:
    # `glob` se mide contra la ruta relativa a `path` (ADR-0027): `path="src"` +
    # `glob="*.py"` acota a los `.py` de `src`, no a los de la raiz.
    assert reader.grep("needle", path="src", glob="*.py").items != ()
    assert reader.grep("needle", path="src", glob="*.md").items == ()
    assert reader.grep("needle", glob="**/*.py").items != ()
    assert reader.grep("needle", glob="*.py").items == ()


def test_read_file_outline_da_un_indice(reader: ContextReaderPort) -> None:
    reading = reader.read_file(APP, mode="outline")
    assert reading is not None
    assert reading.content.startswith("# indice")
    assert reading.total_bytes == len(APP_TEXT.encode("utf-8"))


def test_read_file_outline_no_sirve_el_cuerpo_si_no_puede_indexar(
    reader: ContextReaderPort,
) -> None:
    # `README.md` no tiene indexador: error con salida, nunca el fichero entero.
    with pytest.raises(ToolError):
        reader.read_file(README, mode="outline")


def test_list_dir_rechaza_una_profundidad_invalida(reader: ContextReaderPort) -> None:
    with pytest.raises(ToolError):
        reader.list_dir("src", depth=0)


def test_list_dir_distingue_vacio_de_inexistente(reader: ContextReaderPort) -> None:
    # Antes ambos daban `[]` y el bucle decia «vacio o inexistente»: el modelo no
    # sabia si corregir la ruta o cambiar de herramienta (L1).
    with pytest.raises(ToolError):
        reader.list_dir("src/no-existe")


def test_list_dir_acota_por_glob_y_profundidad(reader: ContextReaderPort) -> None:
    listing = reader.list_dir("", depth=2)
    assert {"src", "src/app.py"} <= set(listing.items)
    assert "config/.env" not in listing.items, "el veto se aplica en todos los niveles"
    assert reader.list_dir("src", glob="*.py").items == ("app.py",)
    assert reader.list_dir("src", glob="*.md").items == ()


def test_is_denied_casa_por_ruta_y_por_nombre() -> None:
    # `.env*` por si solo no casaria `config/.env` (el glob no cruza `/`): el veto
    # mira tambien el basename, que es lo que hace util el patron del perfil.
    assert is_denied(ENV, DEFAULT_CONTEXT_DENYLIST)
    assert is_denied("config/secrets/db.yaml", DEFAULT_CONTEXT_DENYLIST)
    assert is_denied("keys/server.pem", DEFAULT_CONTEXT_DENYLIST)
    assert not is_denied(APP, DEFAULT_CONTEXT_DENYLIST)


@pytest.mark.parametrize(
    "path",
    [".env", "./.env", "/.env", ".env.production", "secrets/db.yaml", "./secrets/db.yaml"],
)
def test_is_denied_veta_las_rutas_de_la_raiz(path: str) -> None:
    # Normalizar con `lstrip("./")` convertia `.env` en `env`, y `**/secrets/**`
    # exigia un directorio antes de `secrets`: la raiz quedaba sin vetar (W7).
    assert is_denied(path, DEFAULT_CONTEXT_DENYLIST)


def test_is_denied_no_veta_por_parecido() -> None:
    assert not is_denied("env/config.py", DEFAULT_CONTEXT_DENYLIST)
    assert not is_denied("src/secrets.py", DEFAULT_CONTEXT_DENYLIST)


def test_un_env_en_la_raiz_no_se_lee_ni_sale_en_un_grep(tmp_path: Path) -> None:
    reader = build_fixture_reader(tmp_path)
    (tmp_path / ".env").write_text("ROOT_SECRET=1\n", encoding="utf-8")
    assert reader.read_file(".env") is None
    assert reader.grep("ROOT_SECRET").items == ()


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
    reading = reader.read_file("big.txt")
    assert reading is not None
    assert reading.content == "x" * 100
    assert reading.truncated is True
    assert reading.total_bytes == 300, "el tamaño es el real, no el ya recortado"


def test_el_fixture_respeta_el_rango_al_recortar(tmp_path: Path) -> None:
    build_fixture_reader(tmp_path)
    reader = build_fixture_reader(tmp_path)
    assert _text(reader, APP, start=1, end=2) == "import os\n"


def test_el_fixture_ignora_la_ref(tmp_path: Path) -> None:
    # No hay dos lados: el mismo arbol sirve `head` y `base` (Q2 de 016).
    reader = build_fixture_reader(tmp_path)
    assert reader.read_file(APP, ref="base") == reader.read_file(APP, ref="head")


def test_el_fixture_lista_la_raiz(tmp_path: Path) -> None:
    reader = build_fixture_reader(tmp_path)
    assert reader.list_dir("").items == ("README.md", "config", "src")


def test_el_fixture_grep_acota_por_glob(tmp_path: Path) -> None:
    reader = build_fixture_reader(tmp_path)
    assert reader.grep("needle", glob="**/*.py").items == (
        f"{APP}:3: needle = 1",
        f"{APP}:4: print(needle)",
    )
    assert reader.grep("needle", glob="**/*.md").items == ()


def test_el_fixture_grep_no_entra_en_las_vetadas(tmp_path: Path) -> None:
    reader = build_fixture_reader(tmp_path)
    assert reader.grep("SECRET").items == ()


def test_el_fixture_grep_lee_la_base_si_la_hay(tmp_path: Path) -> None:
    head = tmp_path / "head"
    base = tmp_path / "base"
    (head / "src").mkdir(parents=True)
    (base / "src").mkdir(parents=True)
    (head / "src/app.py").write_text("nuevo = 2\n", encoding="utf-8")
    (base / "src/app.py").write_text("viejo = 1\n", encoding="utf-8")
    reader = FixtureContextReader(head, base_root=base)
    assert reader.grep("viejo").items == ()
    assert reader.grep("viejo", ref="base").items == ("src/app.py:1: viejo = 1",)


def test_el_fixture_lista_con_profundidad_y_glob(tmp_path: Path) -> None:
    reader = build_fixture_reader(tmp_path)
    assert reader.list_dir("src", depth=2).items == ("app.py",)
    assert reader.list_dir("", depth=2).items == (
        "README.md",
        "config",
        "src",
        "src/app.py",
    )
    assert reader.list_dir("", glob="*.md").items == ("README.md",)


def test_el_fixture_list_dir_no_ofrece_rutas_vetadas(tmp_path: Path) -> None:
    reader = build_fixture_reader(tmp_path)
    assert "config/.env" not in reader.list_dir("", depth=2).items


def test_el_fixture_declara_el_corte_de_grep(tmp_path: Path) -> None:
    build_fixture_reader(tmp_path)
    (tmp_path / "muchas.txt").write_text("needle\n" * 5, encoding="utf-8")
    reader = FixtureContextReader(tmp_path, max_grep_matches=2)
    found = reader.grep("needle")
    assert len(found.items) == 2
    assert found.total == 7, "cuenta las coincidencias de todo el árbol, no solo las servidas"
    assert found.truncated is True


# --- `slice_lines` / `truncate_bytes`: una sola implementacion -------------------


def test_slice_lines_sin_rango_devuelve_todo() -> None:
    assert slice_lines("a\nb", None, None) == ("a\nb", None)


def test_slice_lines_recorta_al_final() -> None:
    assert slice_lines("a\nb\nc", 2, 9) == ("b\nc", (2, 3))


@pytest.mark.parametrize(("start", "end"), [(0, None), (None, 0), (-1, None), (4, 2)])
def test_slice_lines_rechaza_limites_invalidos(start: int | None, end: int | None) -> None:
    with pytest.raises(ValueError):
        slice_lines("a\nb", start, end)


def test_truncate_bytes_no_parte_una_linea() -> None:
    text = "uno\ndos\ntres\n"
    cut, truncated = truncate_bytes(text, 9)
    assert truncated is True
    assert cut == "uno\ndos", "la línea incompleta se descarta, no se sirve a medias"


def test_truncate_bytes_cuenta_bytes_utf8() -> None:
    cut, truncated = truncate_bytes("ñ\nñ\n", 3)
    assert truncated is True
    assert cut == "ñ"


# --- Dialecto portable de `grep` (023 G1, ADR-0027) --------------------------------


def test_el_dialecto_expande_las_clases_que_cada_motor_escribe_distinto() -> None:
    assert portable_pattern(r"\d+") == "[0-9]+"
    assert portable_pattern(r"\w+") == "[A-Za-z0-9_]+"
    assert portable_pattern(r"\D") == "[^0-9]"
    # `\s` no se emite como `[ \t…]`: dentro de una clase, GNU grep lee `\t` como `t`.
    assert portable_pattern(r"\s") == "[ \t\r\f\v]"
    assert "\\" not in portable_pattern(r"\s")


def test_el_dialecto_es_idempotente() -> None:
    for pattern in (r"a\d+b", r"foo\(bar\)", r"[A-Za-z0-9_]+", r"a\sb"):
        once = portable_pattern(pattern)
        assert portable_pattern(once) == once


def test_el_dialecto_conserva_la_puntuacion_escapada() -> None:
    assert portable_pattern(r"needle\.") == r"needle\."
    assert portable_pattern(r"a\|b") == r"a\|b"


@pytest.mark.parametrize(
    ("pattern", "hint"),
    [
        (r"[[:alpha:]]", "POSIX"),
        (r"\bneedle\b", "frontera"),
        (r"\<needle", "palabra"),
        (r"(?=needle)", "lookaround"),
        (r"(?:needle)", "lookaround"),
        (r"(needle)\1", "retro-referencias"),
        (r"a\tb", "escape no portable"),
        (r"[a\-z]", "dentro de una clase"),
        (r"needle(", "invalido"),
    ],
)
def test_el_dialecto_rechaza_lo_que_no_es_portable(pattern: str, hint: str) -> None:
    with pytest.raises(ToolError, match=hint):
        portable_pattern(pattern)
