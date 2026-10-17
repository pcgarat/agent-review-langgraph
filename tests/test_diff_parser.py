from __future__ import annotations

from agent_review.adapters.diff_parser import parse_unified_diff

_DIFF = (
    "diff --git a/src/Foo.php b/src/Foo.php\n"
    "--- a/src/Foo.php\n"
    "+++ b/src/Foo.php\n"
    "@@ -10,4 +10,5 @@\n"
    " unchanged();\n"
    "-old_call();\n"
    " keep();\n"
    "+new_call();\n"
    " trailer();\n"
)


def test_parse_indexa_anadidas_contexto_y_borradas() -> None:
    # 008 CX2: L0 necesita el lado viejo; el anclaje publico sigue en `added`.
    files = parse_unified_diff(_DIFF)
    foo = files["src/Foo.php"]
    assert foo.added == {12: "new_call();"}
    assert foo.context == {
        10: "unchanged();",
        11: "keep();",
        13: "trailer();",
    }
    assert foo.removed == {11: "old_call();"}


def test_parse_sin_contexto_ni_borradas_deja_dicts_vacios() -> None:
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1,2 @@\n+one\n+two\n"
    files = parse_unified_diff(diff)
    assert files["a.py"].added == {1: "one", 2: "two"}
    assert files["a.py"].context == {}
    assert files["a.py"].removed == {}
