from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence

from reviews.ports.vcs import InlineAnchor, PullRequestInfo


class TwgError(RuntimeError):
    pass


class TwgVcsAdapter:
    """Adaptador de Bitbucket via el CLI twg.

    Se invoca siempre con salida JSON cuando aplica y se parsea de forma
    tolerante: los contratos de twg pueden variar entre versiones.
    """

    def __init__(self, *, workspace: str, repo: str, twg_bin: str = "twg") -> None:
        self._workspace = workspace
        self._repo = repo
        self._twg = twg_bin

    def _run(self, args: Sequence[str], *, json_output: bool = True) -> str:
        cmd = [self._twg, "bb", *args, "-w", self._workspace, "-r", self._repo]
        if json_output:
            cmd += ["-o", "json"]
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise TwgError(f"twg fallo ({completed.returncode}): {completed.stderr.strip()}")
        return completed.stdout

    def _run_json(self, args: Sequence[str]) -> object:
        raw = self._run(args).strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}

    def get_pull_request(self, repo: str, pr_id: int) -> PullRequestInfo:
        del repo
        data = self._run_json(["prs", "get", str(pr_id)])
        item = data.get("data", data) if isinstance(data, dict) else {}
        if not isinstance(item, dict):
            item = {}
        return PullRequestInfo(
            repo=f"{self._workspace}/{self._repo}",
            pr_id=pr_id,
            title=str(item.get("title", "")),
            description=str(item.get("description", "") or ""),
            source_branch=str(item.get("source_branch", "")),
            destination_branch=str(item.get("destination_branch", "")),
            author=str(item.get("author", "")),
            head_sha=str(item.get("head_sha", "")),
            state=str(item.get("state", "OPEN")),
            url=str(item.get("url", "")),
        )

    def get_diff(self, repo: str, pr_id: int) -> str:
        del repo
        return self._run(["prs", "diff", str(pr_id)], json_output=False)

    def get_diffstat(self, repo: str, pr_id: int) -> list[tuple[str, int, int]]:
        del repo
        data = self._run_json(["prs", "diffstat", str(pr_id)])
        entries = data.get("data", []) if isinstance(data, dict) else data
        if not isinstance(entries, list):
            return []
        result: list[tuple[str, int, int]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            result.append(
                (
                    str(entry.get("path", entry.get("file", ""))),
                    int(entry.get("lines_added", entry.get("added", 0)) or 0),
                    int(entry.get("lines_removed", entry.get("removed", 0)) or 0),
                )
            )
        return result

    def resolve_inline_anchor(
        self, repo: str, pr_id: int, *, path: str, text: str
    ) -> InlineAnchor | None:
        del repo
        if not path or not text.strip():
            return None
        data = self._run_json(
            ["prs", "diff-line", str(pr_id), "--path", path, "--text", text.strip()]
        )
        item = data
        if isinstance(data, dict):
            nested = data.get("data", data)
            item = nested[0] if isinstance(nested, list) and nested else nested
        if not isinstance(item, dict):
            return None
        line = item.get("line") or item.get("new_line") or item.get("to")
        if line is None:
            return None
        from_line_raw = item.get("from_line") or item.get("old_line") or item.get("from")
        from_line = int(from_line_raw) if from_line_raw is not None else None
        return InlineAnchor(
            path=str(item.get("path", path)),
            line=int(line),
            from_line=from_line,
        )

    def post_inline_comment(
        self,
        repo: str,
        pr_id: int,
        *,
        path: str,
        line: int,
        body: str,
        from_line: int | None = None,
    ) -> None:
        del repo
        args = [
            "prs",
            "comment",
            "create",
            "--pull-request",
            str(pr_id),
            "--text",
            body,
            "--path",
            path,
            "--line",
            str(line),
        ]
        if from_line is not None:
            args += ["--from-line", str(from_line)]
        self._run(args, json_output=False)

    def post_comment(self, repo: str, pr_id: int, body: str) -> None:
        del repo
        self._run(["prs", "comment", "create", "--pull-request", str(pr_id), "--text", body])

    def post_task(self, repo: str, pr_id: int, body: str) -> None:
        del repo
        self._run(["prs", "task", "create", "--pull-request", str(pr_id), "--text", body])
