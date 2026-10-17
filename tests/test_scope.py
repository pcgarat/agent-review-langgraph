from __future__ import annotations

from agent_review.domain.models import FileClass, SkipReason
from agent_review.scope import (
    DEFAULT_IGNORE_PATTERNS,
    changed_files,
    churn,
    filter_diff,
    is_ignored,
    merge_ignore_patterns,
    prioritize_diff,
    split_diff,
)

DIFF = (
    "diff --git a/composer.lock b/composer.lock\n"
    "--- a/composer.lock\n"
    "+++ b/composer.lock\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
    "diff --git a/src/App.php b/src/App.php\n"
    "--- a/src/App.php\n"
    "+++ b/src/App.php\n"
    "@@ -1 +1 @@\n"
    "-a\n"
    "+b\n"
    "diff --git a/var/cache/x.php b/var/cache/x.php\n"
    "--- a/var/cache/x.php\n"
    "+++ b/var/cache/x.php\n"
    "@@ -1 +1 @@\n"
    "-a\n"
    "+b\n"
)


def test_filter_diff_descarta_lockfiles_y_generados() -> None:
    filtered = filter_diff(DIFF, list(DEFAULT_IGNORE_PATTERNS))
    assert "composer.lock" not in filtered
    assert "var/cache/x.php" not in filtered
    assert "src/App.php" in filtered
    # Las cabeceras restantes siguen siendo bloques completos del diff.
    assert filtered.startswith("diff --git a/src/App.php")


def _block(path: str, added: int, removed: int = 0) -> str:
    body = "".join("+x\n" for _ in range(added)) + "".join("-y\n" for _ in range(removed))
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n{body}"


def test_churn_cuenta_lineas_sin_cabeceras() -> None:
    assert churn(_block("a.py", 3, 2)) == 5


def test_prioritize_no_recorta_y_ordena_por_clase_y_churn() -> None:
    # 012 C4/C8: nada se recorta por numero; config > code > test > docs y, dentro de
    # cada clase, mas churn primero.
    diff = "".join(
        [
            _block("docs/guia.md", 90),
            _block("tests/test_a.py", 80),
            _block("src/pequeno.py", 1),
            _block("src/grande.py", 70),
            _block("docker-compose.yml", 2),
            _block("composer.lock", 500),
        ]
    )
    ordered, coverage = prioritize_diff(diff, list(DEFAULT_IGNORE_PATTERNS))

    assert [path for path, _ in split_diff(ordered)] == [
        "docker-compose.yml",
        "src/grande.py",
        "src/pequeno.py",
        "tests/test_a.py",
        "docs/guia.md",
    ]
    assert coverage.files_total == 6
    assert coverage.skipped == {"composer.lock": SkipReason.IGNORED}
    assert coverage.reviewed == []
    assert coverage.file_class["docker-compose.yml"] is FileClass.CONFIG
    # Un ignorado no hace parcial la review.
    assert not coverage.partial


def test_split_diff_conserva_los_bloques_enteros() -> None:
    blocks = split_diff(DIFF)
    assert [path for path, _ in blocks] == ["composer.lock", "src/App.php", "var/cache/x.php"]
    assert "".join(block for _, block in blocks) == DIFF


def test_changed_files_deduplica_por_orden() -> None:
    assert changed_files(DIFF) == ["composer.lock", "src/App.php", "var/cache/x.php"]


def test_merge_ignore_patterns_anade_los_del_deploy() -> None:
    patterns = merge_ignore_patterns("docs/** , *.snap")
    assert "docs/**" in patterns
    assert "*.snap" in patterns
    assert "composer.lock" in patterns


def test_is_ignored_normaliza_el_prefijo_punto() -> None:
    assert is_ignored("./composer.lock", list(DEFAULT_IGNORE_PATTERNS))
    assert not is_ignored("src/App.php", list(DEFAULT_IGNORE_PATTERNS))


def test_classify_por_clase_con_orden_test_docs_config_code() -> None:
    from agent_review.domain.models import FileClass
    from agent_review.scope import classify

    cases = {
        "docker-compose.yml": FileClass.CONFIG,
        ".env.example": FileClass.CONFIG,
        "src/agent_review/settings.py": FileClass.CONFIG,
        "config/packages/doctrine.yaml": FileClass.CONFIG,
        "Dockerfile": FileClass.CONFIG,
        "pyproject.toml": FileClass.CONFIG,
        # `test` se comprueba antes que `config`: un YAML de tests es test.
        "tests/config/a.yaml": FileClass.TEST,
        "src/Wallet/TransferTest.php": FileClass.TEST,
        "tests/test_scope.py": FileClass.TEST,
        "docs/a.md": FileClass.DOCS,
        "README.md": FileClass.DOCS,
        "src/agent_review/worker/main.py": FileClass.CODE,
        "./src/App.php": FileClass.CODE,
    }
    assert {path: classify(path) for path in cases} == cases


def test_las_clases_del_perfil_se_suman_a_las_de_por_defecto() -> None:
    from agent_review.domain.models import FileClass
    from agent_review.scope import classify, merge_file_classes

    classes = merge_file_classes({"config": ["infra/**"]})
    assert classify("infra/main.tf", classes) is FileClass.CONFIG
    # Las de por defecto siguen ahi.
    assert classify("docker-compose.yml", classes) is FileClass.CONFIG


def test_is_ignored_respeta_los_ficheros_que_empiezan_por_punto() -> None:
    # `lstrip("./")` se comia el punto de `.github/...` y el patron no casaba.
    assert is_ignored(".github/workflows/ci.yml", [".github/**"])
    assert is_ignored("./vendor/a.php", ["vendor/**"])
