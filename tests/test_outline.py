"""R1: índice de símbolos de `read_file(mode="outline")` (023 E5).

El índice no sustituye al cuerpo: da rangos para pedir después el trozo. Donde hay
AST los rangos son exactos; donde no, la heurística lo declara en su cabecera. Un
fichero sin indexador devuelve `None` y el adaptador lo convierte en error con
salida (`start`/`end`), nunca en una lectura completa disfrazada.
"""

from __future__ import annotations

from agent_review.outline import MAX_OUTLINE_BYTES, outline_text

PYTHON = """\
import os


def foo(a, b=1):
    return a


class Bar:
    def baz(self):
        return 2

    @property
    def value(self):
        return 3


async def run():
    return None
"""

JAVASCRIPT = """\
const x = 1;

function sumar(a, b) {
  return a + b;
}

class Caja {
  abrir() {
    return 1;
  }
}
"""


def test_el_indice_de_python_da_rangos_exactos() -> None:
    index = outline_text("mod.py", PYTHON)
    assert index is not None
    assert index.splitlines()[0].startswith("# indice (AST)")
    assert "4-5  def foo(a, b)" in index
    assert "8-14  class Bar" in index
    assert "9-10    def baz(self)" in index
    assert "12-14    def value(self)" in index, "el decorador entra en el rango"
    assert "17-18  async def run()" in index


def test_el_rango_del_indice_devuelve_el_cuerpo(tmp_path) -> None:
    from agent_review.adapters.context_fixture import FixtureContextReader

    (tmp_path / "mod.py").write_text(PYTHON, encoding="utf-8")
    reader = FixtureContextReader(tmp_path)
    index = reader.read_file("mod.py", mode="outline")
    assert index is not None
    body = reader.read_file("mod.py", start=4, end=5)
    assert body is not None
    assert body.content == "def foo(a, b=1):\n    return a"


def test_el_indice_de_python_avisa_si_no_hay_simbolos() -> None:
    index = outline_text("script.py", "print('hola')\n")
    assert index is not None
    assert "sin funciones ni clases" in index


def test_un_fichero_que_no_compila_cae_a_la_heuristica() -> None:
    index = outline_text("roto.py", "def foo(:\n    pass\n")
    assert index is not None
    assert "heuristico" in index
    assert "1-2  foo" in index


def test_la_heuristica_declara_que_no_es_ast() -> None:
    index = outline_text("app.js", JAVASCRIPT)
    assert index is not None
    assert index.splitlines()[0].startswith("# indice heuristico")
    assert "3-5  sumar" in index, "el rango cierra con la llave del bloque"
    assert "7-11  Caja" in index


def test_la_heuristica_sin_declaraciones_lo_dice() -> None:
    index = outline_text("app.rs", "// solo un comentario\n")
    assert index is not None
    assert "sin declaraciones reconocibles" in index


def test_un_fichero_sin_indexador_no_se_inventa_un_indice() -> None:
    assert outline_text("README.md", "# titulo\n") is None
    assert outline_text("config.yaml", "a: 1\n") is None


def test_un_fichero_demasiado_grande_no_se_indexa() -> None:
    assert outline_text("mod.py", "x = 1\n" * (MAX_OUTLINE_BYTES // 4)) is None
