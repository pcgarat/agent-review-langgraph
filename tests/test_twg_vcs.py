from __future__ import annotations

import json

from reviews.adapters.twg_vcs import TwgVcsAdapter


def test_run_usa_json_puro(monkeypatch) -> None:
    adapter = TwgVcsAdapter(workspace="cqm", repo="emotivoo-v2")
    captured: dict[str, list[str]] = {}

    def fake_subprocess(cmd, **kwargs):  # noqa: ANN001, ANN003
        captured["cmd"] = list(cmd)

        class Result:
            returncode = 0
            stdout = "{}"
            stderr = ""

        return Result()

    monkeypatch.setattr("reviews.adapters.twg_vcs.subprocess.run", fake_subprocess)
    adapter._run_json(["prs", "get", "1"])
    assert "--output" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--output") + 1] == "json"
    assert "--output-summary" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--output-summary") + 1] == "none"


def test_get_pull_request_mapea_campos_reales(monkeypatch) -> None:
    adapter = TwgVcsAdapter(workspace="cqm", repo="emotivoo-v2")
    payload = {
        "id": 417,
        "title": "Feature",
        "description": "desc",
        "state": "OPEN",
        "author": {"display_name": "John Astete"},
        "source": {"branch": {"name": "feature/x"}, "commit": {"hash": "7bf898f54e14"}},
        "destination": {"branch": {"name": "main"}},
        "links": {"html": {"href": "https://bitbucket.org/cqm/emotivoo-v2/pull-requests/417"}},
    }
    monkeypatch.setattr(adapter, "_run", lambda *a, **k: json.dumps(payload))
    pr = adapter.get_pull_request("cqm/emotivoo-v2", 417)
    assert pr.author == "John Astete"
    assert pr.source_branch == "feature/x"
    assert pr.destination_branch == "main"
    assert pr.head_sha == "7bf898f54e14"
    assert pr.url.endswith("/417")


def test_get_diffstat_usa_old_new_paths(monkeypatch) -> None:
    adapter = TwgVcsAdapter(workspace="cqm", repo="emotivoo-v2")
    payload = [
        {
            "lines_added": 1,
            "lines_removed": 0,
            "status": "modified",
            "old": {"path": "config/packages/messenger.yaml"},
            "new": {"path": "config/packages/messenger.yaml"},
        },
        {
            "lines_added": 0,
            "lines_removed": 76,
            "status": "removed",
            "old": {"path": "tests/Migrations/Version20261005110000Test.php"},
            "new": None,
        },
    ]
    monkeypatch.setattr(adapter, "_run", lambda *a, **k: json.dumps(payload))
    stats = adapter.get_diffstat("cqm/emotivoo-v2", 417)
    assert ("config/packages/messenger.yaml", 1, 0) in stats
    assert ("tests/Migrations/Version20261005110000Test.php", 0, 76) in stats


def test_resolve_inline_anchor_parsea_matches(monkeypatch) -> None:
    adapter = TwgVcsAdapter(workspace="cqm", repo="emotivoo-v2")
    payload = {
        "pullRequest": 417,
        "path": "config/packages/messenger.yaml",
        "matches": [
            {
                "path": "config/packages/messenger.yaml",
                "content": "App\\Message: async",
                "toLine": 12,
            }
        ],
    }
    monkeypatch.setattr(adapter, "_run", lambda *a, **k: json.dumps(payload))
    anchor = adapter.resolve_inline_anchor(
        "cqm/emotivoo-v2", 417, path="config/packages/messenger.yaml", text="async"
    )
    assert anchor is not None
    assert anchor.line == 12
    assert anchor.path == "config/packages/messenger.yaml"


def test_resolve_inline_anchor_sin_match(monkeypatch) -> None:
    adapter = TwgVcsAdapter(workspace="cqm", repo="emotivoo-v2")
    monkeypatch.setattr(adapter, "_run", lambda *a, **k: json.dumps({"matches": []}))
    assert adapter.resolve_inline_anchor("r", 1, path="a.php", text="zzz") is None


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
    adapter.post_inline_comment("r", 1, path="a.php", line=3, body="hola", from_line=2)
    assert seen
    assert "--from-line" in seen[0]
