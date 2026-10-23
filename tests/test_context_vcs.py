"""L6: `VcsContextReader`, el modo degradado sobre `VcsPort` (016 W1, W8).

El contrato comun vive en `tests/test_context_ports.py`; aqui se fijan las reglas
propias del modo sin arbol: cachear por `(path, ref)`, sembrar lo servido y no
tumbar la review cuando el VCS falla.
"""

from __future__ import annotations

import logging

import pytest
from context_fakes import APP, BASE, HEAD, README, FakeVcs

from agent_review.adapters.context_vcs import VcsContextReader
from agent_review.domain.models import AutofixClass, Finding, Owner, Severity
from agent_review.ports.context import ToolError


def _text(reader: VcsContextReader, path: str, **kwargs: object) -> str:
    reading = reader.read_file(path, **kwargs)  # type: ignore[arg-type]
    assert reading is not None
    return reading.content


def _reader(vcs: FakeVcs | None = None, **kwargs) -> VcsContextReader:
    return VcsContextReader(
        vcs=vcs or FakeVcs(),
        repo="cqm/x",
        head_sha=HEAD,
        base_sha=BASE,
        diff=kwargs.pop(
            "diff",
            ("diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"),
        ),
        **kwargs,
    )


def test_sin_diff_no_puede_listar_nada() -> None:
    reader = _reader(diff="")
    assert reader.list_dir("").items == ()
    with pytest.raises(ToolError):
        reader.list_dir("src")


def test_list_dir_no_afirma_que_un_directorio_inexistente_este_vacio() -> None:
    reader = _reader()
    with pytest.raises(ToolError, match="no aparece en los ficheros del cambio"):
        reader.list_dir("docs")


def test_list_dir_de_una_ruta_del_diff_que_es_fichero_sugiere_read_file() -> None:
    reader = _reader()
    with pytest.raises(ToolError, match="es un fichero"):
        reader.list_dir(APP)


def test_list_dir_acota_con_depth_y_glob() -> None:
    reader = _reader()
    assert reader.list_dir("src", depth=2).items == ("app.py",)
    assert reader.list_dir("src", glob="*.py").items == ("app.py",)
    assert reader.list_dir("src", glob="*.md").items == ()


def test_list_dir_es_estable_entre_llamadas() -> None:
    reader = _reader(diff="diff --git a/src/b.py b/src/b.py\ndiff --git a/src/a.py b/src/a.py\n")
    listing = reader.list_dir("src")
    assert listing.items == ("a.py", "b.py"), "ordenado y determinista (ADR-0017)"
    assert listing.total == 2


def test_seed_precarga_los_ficheros_del_diff() -> None:
    vcs = FakeVcs()
    reader = _reader(vcs)
    reader.seed()
    assert (APP, HEAD) in {(path, ref) for _, path, ref in vcs.calls}
    assert reader.grep("needle").items == (f"{APP}:3: needle = 1", f"{APP}:4: print(needle)")


def test_seed_con_rutas_explicitas_solo_precarga_esas() -> None:
    reader = _reader()
    reader.seed([README])
    assert reader.grep("repo").items == (f"{README}:1: # repo",)
    assert reader.grep("needle").items == ()


def test_seed_findings_precarga_los_ficheros_de_los_hallazgos() -> None:
    reader = _reader()
    finding = Finding(
        id="x-1",
        title="t",
        severity=Severity.P2,
        file=README,
        line=1,
        why_it_matters="w",
        autofix_class=AutofixClass.ADVISORY,
        owner=Owner.HUMAN,
        failure_scenario="escenario de prueba",
        confidence=75,
        evidence=[f"{README}:1 # repo"],
    )
    reader.seed_findings([finding])
    assert reader.grep("repo").items == (f"{README}:1: # repo",)


def test_grep_respeta_el_glob() -> None:
    reader = _reader()
    reader.seed()
    assert reader.grep("needle", glob="**/*.py").items == (
        f"{APP}:3: needle = 1",
        f"{APP}:4: print(needle)",
    )
    assert reader.grep("needle", glob="**/*.md").items == ()


def test_grep_con_patron_vacio_no_devuelve_nada() -> None:
    reader = _reader()
    reader.seed()
    assert reader.grep("").items == ()


def test_las_rutas_vetadas_no_se_sirven_ni_aparecen_en_grep(caplog) -> None:
    vcs = FakeVcs(files={("config/.env", HEAD): "SECRET=1\n"})
    reader = _reader(vcs, denylist=(".env*",))
    with caplog.at_level(logging.DEBUG):
        assert reader.read_file("config/.env") is None
    assert vcs.calls == [], "una ruta vetada no llega al VCS"
    assert reader.grep("SECRET").items == ()


def test_el_cache_clave_por_ref_no_mezcla_head_y_base() -> None:
    vcs = FakeVcs()
    reader = _reader(vcs)
    assert _text(reader, APP) == "import os\n\nneedle = 1\nprint(needle)\n"
    assert _text(reader, APP, ref="base") == "import os\n"
    assert len(vcs.calls) == 2, "head y base son lecturas distintas"
    reader.read_file(APP)
    assert len(vcs.calls) == 2, "repetir la misma ref sale de cache"


def test_una_ref_desconocida_devuelve_none() -> None:
    reader = _reader()
    assert reader.read_file("src/otro.py") is None
    assert _text(reader, APP, ref="base") == "import os\n"


def test_una_excepcion_del_vcs_no_tumba_la_review(caplog) -> None:
    class Boom(FakeVcs):
        def get_file(self, repo: str, path: str, *, ref: str) -> str:
            raise RuntimeError("twg no responde")

    reader = _reader(Boom())
    with caplog.at_level(logging.WARNING):
        assert reader.read_file(APP) is None
    assert "no se pudo leer" in caplog.text


def test_un_ref_base_ausente_devuelve_none_sin_excepcion() -> None:
    reader = _reader(FakeVcs(files={(APP, HEAD): "x\n"}), diff="")
    assert reader.read_file(APP, ref="base") is None


def test_el_tope_de_coincidencias_corta_el_grep() -> None:
    content = "".join(f"linea needle {index}\n" for index in range(20))
    reader = _reader(FakeVcs(files={("a.py", HEAD): content}), diff="", max_grep_matches=5)
    reader.read_file("a.py")
    found = reader.grep("needle")
    assert len(found.items) == 5
    assert found.total == 20, "el total cuenta todas, no solo las servidas"
    assert found.truncated is True


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [(None, None, "uno\ndos\ntres"), (1, 1, "uno"), (2, 3, "dos\ntres"), (2, None, "dos\ntres")],
)
def test_el_rango_de_read_file_es_inclusivo(start, end, expected) -> None:
    reader = _reader(FakeVcs(files={("a.txt", HEAD): "uno\ndos\ntres"}), diff="")
    assert _text(reader, "a.txt", start=start, end=end) == expected
