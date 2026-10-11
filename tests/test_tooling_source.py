from __future__ import annotations

from pathlib import Path

import pytest

from reviews.adapters.tooling_source import (
    FileToolingOutputSource,
    StaticToolingOutputSource,
    safe_segment,
)
from reviews.domain.models import ToolingSourceStatus


def test_file_source_renderiza_la_plantilla_por_review(tmp_path: Path) -> None:
    source = FileToolingOutputSource(
        template="{slug}/{pr_id}/{head_sha}/{tool}.json", root=tmp_path
    )
    target = source.path_for("phpstan", repo="cqm/emotivoo-v2", pr_id=12, head_sha="abc123")

    assert target == tmp_path / "cqm_emotivoo-v2" / "12" / "abc123" / "phpstan.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"files": {}}', encoding="utf-8")

    status, payload = source.read("phpstan", repo="cqm/emotivoo-v2", pr_id=12, head_sha="abc123")
    assert status is ToolingSourceStatus.PRESENT
    assert payload == '{"files": {}}'


def test_file_source_sin_artefacto_reporta_missing(tmp_path: Path) -> None:
    source = FileToolingOutputSource(template="{tool}.json", root=tmp_path)
    assert source.read("phpstan", repo="r", pr_id=1, head_sha="s") == (
        ToolingSourceStatus.MISSING,
        None,
    )


def test_file_source_rechaza_plantilla_con_placeholder_desconocido() -> None:
    with pytest.raises(ValueError):
        FileToolingOutputSource(template="{tool}/{desconocido}.json")


def test_file_source_rechaza_plantilla_absoluta() -> None:
    with pytest.raises(ValueError):
        FileToolingOutputSource(template="/etc/{tool}.json")


def test_file_source_rechaza_plantilla_que_escapa_del_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        FileToolingOutputSource(template="../../{tool}.json", root=tmp_path)


def test_file_source_rechaza_format_spec_incompatible() -> None:
    # `pr_id` siempre llega como int: un spec de string debe fallar en construccion.
    with pytest.raises(ValueError):
        FileToolingOutputSource(template="{pr_id:s}/{tool}.json")


def test_file_source_head_sha_vacio_falla_ruidoso(tmp_path: Path) -> None:
    source = FileToolingOutputSource(template="{pr_id}/{head_sha}/{tool}.json", root=tmp_path)
    with pytest.raises(ValueError):
        source.path_for("phpstan", repo="r", pr_id=1, head_sha="")
    with pytest.raises(ValueError):
        source.read("phpstan", repo="r", pr_id=1, head_sha="")


def test_file_source_contenido_no_utf8_reporta_unreadable(tmp_path: Path) -> None:
    source = FileToolingOutputSource(template="{tool}.json", root=tmp_path)
    (tmp_path / "phpstan.json").write_bytes(b"\xff\xfe\x00\x01 not utf8")
    assert source.read("phpstan", repo="r", pr_id=1, head_sha="s") == (
        ToolingSourceStatus.UNREADABLE,
        None,
    )


def test_slug_neutraliza_separadores_y_traversal() -> None:
    slug = safe_segment("../../etc/passwd")
    assert "/" not in slug
    assert ".." not in slug
    assert slug == "etc_passwd"


def test_static_source_devuelve_el_payload() -> None:
    source = StaticToolingOutputSource("contenido")
    assert source.read("phpstan", repo="r", pr_id=1, head_sha="s") == (
        ToolingSourceStatus.PRESENT,
        "contenido",
    )


def test_static_source_puede_simular_ausencia() -> None:
    source = StaticToolingOutputSource("", status=ToolingSourceStatus.MISSING)
    assert source.read("phpstan", repo="r", pr_id=1, head_sha="s") == (
        ToolingSourceStatus.MISSING,
        None,
    )
