from __future__ import annotations

from reviews.scope import (
    DEFAULT_IGNORE_PATTERNS,
    changed_files,
    filter_diff,
    is_ignored,
    limit_files,
    merge_ignore_patterns,
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


def test_filter_diff_con_allowed_recorta_a_ficheros_revisados() -> None:
    filtered = filter_diff(DIFF, list(DEFAULT_IGNORE_PATTERNS), allowed=["src/App.php"])
    assert "src/App.php" in filtered
    assert "composer.lock" not in filtered


def test_limit_files_ordena_por_churn_y_trunca() -> None:
    files = [("a.php", 1, 0), ("b.php", 50, 10), ("c.php", 5, 5)]
    kept, truncated = limit_files(files, 2)
    assert kept == ["b.php", "c.php"]
    assert truncated is True


def test_limit_files_sin_tope_no_trunca() -> None:
    files = [("a.php", 1, 0), ("b.php", 2, 0)]
    kept, truncated = limit_files(files, 0)
    assert set(kept) == {"a.php", "b.php"}
    assert truncated is False


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
