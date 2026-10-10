from __future__ import annotations

from pathlib import Path

import pytest

from reviews.adapters.tooling_source import (
    FileToolingOutputSource,
    StaticToolingOutputSource,
    safe_segment,
)


def test_file_source_renderiza_la_plantilla_por_review(tmp_path: Path) -> None:
    source = FileToolingOutputSource(
        template="{slug}/{pr_id}/{head_sha}/{tool}.json", root=tmp_path
    )
    target = source.path_for("phpstan", repo="cqm/emotivoo-v2", pr_id=12, head_sha="abc123")

    assert target == tmp_path / "cqm_emotivoo-v2" / "12" / "abc123" / "phpstan.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"files": {}}', encoding="utf-8")

    assert source.read("phpstan", repo="cqm/emotivoo-v2", pr_id=12, head_sha="abc123") == (
        '{"files": {}}'
    )


def test_file_source_sin_artefacto_devuelve_none(tmp_path: Path) -> None:
    source = FileToolingOutputSource(template="{tool}.json", root=tmp_path)
    assert source.read("phpstan", repo="r", pr_id=1, head_sha="s") is None


def test_file_source_rechaza_plantilla_con_placeholder_desconocido() -> None:
    with pytest.raises(ValueError):
        FileToolingOutputSource(template="{tool}/{desconocido}.json")


def test_slug_neutraliza_separadores_y_traversal() -> None:
    slug = safe_segment("../../etc/passwd")
    assert "/" not in slug
    assert ".." not in slug
    assert slug == "etc_passwd"


def test_static_source_devuelve_el_payload() -> None:
    source = StaticToolingOutputSource("contenido")
    assert source.read("phpstan", repo="r", pr_id=1, head_sha="s") == "contenido"
