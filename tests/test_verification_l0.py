from __future__ import annotations

from agent_review.adapters.diff_parser import FileDiff, parse_unified_diff
from agent_review.verification.l0 import L0Disposition, evaluate_l0, is_pre_existing

# Caso F: el modelo afirma una regresion sobre codigo que ya estaba (contexto).
_DIFF_F = (
    "diff --git a/src/agent_review/graph/build.py b/src/agent_review/graph/build.py\n"
    "--- a/src/agent_review/graph/build.py\n"
    "+++ b/src/agent_review/graph/build.py\n"
    "@@ -100,6 +100,7 @@\n"
    " def build_fix_wait():\n"
    "     targets = []\n"
    "     return targets\n"
    "+    # nuevo comentario\n"
    " def other():\n"
    "     pass\n"
)


def _foo_diff() -> FileDiff:
    return parse_unified_diff(
        "diff --git a/src/Foo.php b/src/Foo.php\n"
        "--- a/src/Foo.php\n"
        "+++ b/src/Foo.php\n"
        "@@ -10,4 +10,5 @@\n"
        " unchanged();\n"
        "-old_call();\n"
        " keep();\n"
        "+new_call();\n"
        " trailer();\n"
    )["src/Foo.php"]


def test_cita_en_anadida_se_conserva() -> None:
    result = evaluate_l0(_foo_diff(), hint=99, cite="new_call();")
    assert result.disposition is L0Disposition.KEEP
    assert result.line == 12
    assert result.pre_existing is False


def test_cita_en_contexto_va_al_resumen_y_es_preexistente() -> None:
    # Criterio 1 / caso F: no recolocar a una anadida cercana.
    result = evaluate_l0(_foo_diff(), hint=12, cite="keep();")
    assert result.disposition is L0Disposition.SUMMARY
    assert result.line == 11
    assert result.pre_existing is True


def test_cita_inexistente_se_descarta() -> None:
    result = evaluate_l0(_foo_diff(), hint=12, cite="invented_function();")
    assert result.disposition is L0Disposition.DROP
    assert result.line is None


def test_hint_en_contexto_sin_cita_no_recoloca() -> None:
    result = evaluate_l0(_foo_diff(), hint=11, cite=None)
    assert result.disposition is L0Disposition.SUMMARY
    assert result.line == 11
    assert result.pre_existing is True


def test_hint_huérfano_se_descarta_sin_best_line() -> None:
    result = evaluate_l0(_foo_diff(), hint=999, cite=None)
    assert result.disposition is L0Disposition.DROP


def test_pre_existing_por_base_content_cuando_no_esta_en_el_diff() -> None:
    # Paso (b): la cita no esta en contexto/borrada del diff, pero si en la base.
    file_diff = FileDiff(path="a.py", added={1: "brand_new();"}, context={}, removed={})
    assert is_pre_existing("legacy();", file_diff) is False
    assert (
        is_pre_existing(
            "legacy();",
            file_diff,
            base_content="def x():\n    legacy();\n",
        )
        is True
    )


def test_caso_f_targets_vacios_en_contexto() -> None:
    files = parse_unified_diff(_DIFF_F)
    build = files["src/agent_review/graph/build.py"]
    result = evaluate_l0(build, hint=101, cite="targets = []")
    assert result.disposition is L0Disposition.SUMMARY
    assert result.pre_existing is True
