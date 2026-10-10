from __future__ import annotations

import json

from reviews.adapters.twg_vcs import TwgVcsAdapter


def test_resolve_inline_anchor_parsea_json(monkeypatch) -> None:
    adapter = TwgVcsAdapter(workspace="cqm", repo="emotivoo-v2", twg_bin="twg")
    payload = {"path": "src/Foo.php", "line": 42, "from_line": 40}

    def fake_run(args, *, json_output=True):  # noqa: ANN001
        assert "diff-line" in args
        assert "--path" in args
        assert "--text" in args
        assert json_output is True
        return json.dumps(payload)

    monkeypatch.setattr(adapter, "_run", fake_run)
    anchor = adapter.resolve_inline_anchor(
        "cqm/emotivoo-v2", 7, path="src/Foo.php", text="validate"
    )
    assert anchor is not None
    assert anchor.path == "src/Foo.php"
    assert anchor.line == 42
    assert anchor.from_line == 40


def test_resolve_inline_anchor_sin_match(monkeypatch) -> None:
    adapter = TwgVcsAdapter(workspace="cqm", repo="emotivoo-v2")

    monkeypatch.setattr(adapter, "_run", lambda *a, **k: "{}")
    assert (
        adapter.resolve_inline_anchor("r", 1, path="a.php", text="zzz") is None
    )


def test_post_inline_comment_sin_pending(monkeypatch) -> None:
    adapter = TwgVcsAdapter(workspace="cqm", repo="emotivoo-v2")
    seen: list[list[str]] = []

    def fake_run(args, *, json_output=True):  # noqa: ANN001
        seen.append(list(args))
        assert json_output is False
        assert "--pending" not in args
        assert args[args.index("--path") + 1] == "a.php"
        assert args[args.index("--line") + 1] == "3"
        return ""

    monkeypatch.setattr(adapter, "_run", fake_run)
    adapter.post_inline_comment(
        "r", 1, path="a.php", line=3, body="hola", from_line=2
    )
    assert seen
    assert "--from-line" in seen[0]
