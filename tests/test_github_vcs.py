from __future__ import annotations

import base64

import httpx
import pytest

from agent_review.adapters.github_vcs import (
    GithubError,
    GithubUnavailable,
    GithubVcsAdapter,
)
from agent_review.ports.vcs import VcsPort

_PR_PAYLOAD = {
    "number": 417,
    "title": "Feature",
    "body": "desc",
    "state": "open",
    "user": {"login": "john"},
    "head": {"ref": "feature/x", "sha": "7bf898f54e14"},
    "base": {"ref": "main"},
    "html_url": "https://github.com/cqm/emotivoo-v2/pull/417",
}

_DIFF = (
    "diff --git a/src/Foo.php b/src/Foo.php\n"
    "--- a/src/Foo.php\n"
    "+++ b/src/Foo.php\n"
    "@@ -1,2 +1,3 @@\n"
    " <?php\n"
    "+$balance->frozen = true;\n"
    " echo 1;\n"
)


def _adapter(handler, *, token: str = "ghp_x") -> GithubVcsAdapter:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return GithubVcsAdapter(
        owner="cqm",
        repo="emotivoo-v2",
        token=token,
        api_url="https://api.github.com",
        client=client,
    )


def test_cumple_el_puerto_vcs() -> None:
    assert isinstance(_adapter(lambda r: httpx.Response(200, json={})), VcsPort)


def test_get_pull_request_mapea_campos_realistas() -> None:
    adapter = _adapter(lambda r: httpx.Response(200, json=_PR_PAYLOAD))
    pr = adapter.get_pull_request("", 417)
    assert pr.repo == "cqm/emotivoo-v2"
    assert pr.author == "john"
    assert pr.source_branch == "feature/x"
    assert pr.destination_branch == "main"
    assert pr.head_sha == "7bf898f54e14"
    assert pr.state == "OPEN"
    assert pr.url.endswith("/417")


def test_get_diff_pide_el_media_type_de_diff() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["accept"] = request.headers.get("accept")
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, text=_DIFF)

    adapter = _adapter(handler)
    assert adapter.get_diff("", 417) == _DIFF
    assert captured["accept"] == "application/vnd.github.v3.diff"
    assert captured["auth"] == "Bearer ghp_x"


def test_get_diff_se_cachea_por_pr() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=_DIFF)

    adapter = _adapter(handler)
    adapter.get_diff("", 417)
    adapter.get_diff("", 417)
    assert calls["n"] == 1


def test_get_diffstat_suma_additions_y_deletions() -> None:
    payload = [
        {"filename": "src/Foo.php", "additions": 3, "deletions": 1},
        {"filename": "src/Bar.php", "additions": 0, "deletions": 5},
    ]
    adapter = _adapter(lambda r: httpx.Response(200, json=payload))
    stats = adapter.get_diffstat("", 417)
    assert ("src/Foo.php", 3, 1) in stats
    assert ("src/Bar.php", 0, 5) in stats


def test_get_diffstat_pagina_hasta_llenar() -> None:
    full = [{"filename": f"f{i}.php", "additions": 1, "deletions": 0} for i in range(100)]

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        return httpx.Response(200, json=full if page == "1" else [full[0]])

    adapter = _adapter(handler)
    assert len(adapter.get_diffstat("", 417)) == 101


def test_get_file_decodifica_base64() -> None:
    content = base64.b64encode(b"<?php echo 1;\n").decode()
    payload = {"content": content, "encoding": "base64"}
    adapter = _adapter(lambda r: httpx.Response(200, json=payload))
    assert adapter.get_file("", "src/Foo.php", ref="abc") == "<?php echo 1;\n"


def test_get_file_binario_falla_permanente() -> None:
    adapter = _adapter(lambda r: httpx.Response(200, json={"content": None}))
    with pytest.raises(GithubError, match="texto"):
        adapter.get_file("", "logo.png", ref="abc")


def test_resolve_inline_anchor_calcula_sobre_el_diff() -> None:
    adapter = _adapter(lambda r: httpx.Response(200, text=_DIFF))
    anchor = adapter.resolve_inline_anchor("", 417, path="src/Foo.php", text="$balance->frozen")
    assert anchor is not None
    assert anchor.line == 2
    assert anchor.path == "src/Foo.php"


def test_resolve_inline_anchor_sin_coincidencia() -> None:
    adapter = _adapter(lambda r: httpx.Response(200, text=_DIFF))
    assert adapter.resolve_inline_anchor("", 417, path="src/Foo.php", text="zzz") is None
    assert adapter.resolve_inline_anchor("", 417, path="otro.php", text="x") is None


def test_post_inline_comment_usa_side_right() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = httpx.Response(200, content=request.content).json()
        return httpx.Response(201, json={"id": 1})

    adapter = _adapter(handler)
    adapter.post_inline_comment("", 417, path="src/Foo.php", line=2, body="hola")
    assert captured["url"].endswith("/pulls/417/comments")
    assert captured["body"]["side"] == "RIGHT"
    assert captured["body"]["line"] == 2
    assert "start_line" not in captured["body"]


def test_post_inline_comment_con_rango_usa_start_line() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = httpx.Response(200, content=request.content).json()
        return httpx.Response(201, json={"id": 1})

    _adapter(handler).post_inline_comment(
        "", 417, path="src/Foo.php", line=5, body="hola", from_line=3
    )
    assert captured["body"]["start_line"] == 3
    assert captured["body"]["start_side"] == "RIGHT"


def test_post_comment_va_a_issues() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(201, json={"id": 1})

    _adapter(handler).post_comment("", 417, "resumen")
    assert captured["url"].endswith("/issues/417/comments")


def test_list_review_markers_mira_generales_e_inline() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/pulls/417/comments" in str(request.url):
            return httpx.Response(200, json=[{"id": 3, "body": "inline <!-- reviews:abc -->"}])
        return httpx.Response(200, json=[{"id": 1, "body": "humano"}, {"id": 2, "body": None}])

    markers = _adapter(handler).list_review_markers("", 417)
    assert [m.id for m in markers] == ["1", "2", "3"]
    assert "reviews:abc" in markers[2].body
    assert markers[1].body == ""


def test_post_task_es_noop() -> None:
    adapter = _adapter(lambda r: httpx.Response(200, json={}))
    assert adapter.post_task("", 417, "tarea") is None


def test_5xx_es_transitorio() -> None:
    adapter = _adapter(lambda r: httpx.Response(503, json={"message": "unavailable"}))
    with pytest.raises(GithubUnavailable):
        adapter.get_pull_request("", 417)


def test_404_es_permanente_con_mensaje() -> None:
    adapter = _adapter(lambda r: httpx.Response(404, json={"message": "Not Found"}))
    with pytest.raises(GithubError, match="Not Found"):
        adapter.get_pull_request("", 417)


def test_fallo_de_red_es_transitorio() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(GithubUnavailable):
        _adapter(handler).get_pull_request("", 417)
