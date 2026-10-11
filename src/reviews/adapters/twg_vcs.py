from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence

from reviews.ports.vcs import InlineAnchor, PullRequestInfo, ReviewMarker


class TwgError(RuntimeError):
    pass


def check_twg(twg_bin: str = "twg", *, required: bool = True) -> str | None:
    """Comprueba que el binario de `twg` existe y responde, en el arranque.

    El contenedor se construye copiando `bin/twg` *si existe*: sin este chequeo,
    un `twg` ausente solo se descubre al procesar la primera review, y esa review
    falla como si fuera un problema del PR. Falla ruidoso (A2) en vez de eso.
    Devuelve el identificador de version, o `None` si `required=False` y no esta.
    """
    try:
        completed = subprocess.run(
            [twg_bin, "--version"], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        if required:
            raise TwgError(
                f"No se encontro el binario '{twg_bin}'. Copia twg en bin/ (ver bin/README.md) "
                "o instalalo de forma reproducible antes de arrancar."
            ) from None
        return None
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        if required:
            raise TwgError(f"'{twg_bin} --version' fallo ({completed.returncode}): {message}")
        return None
    return (completed.stdout or completed.stderr).strip() or "twg"


class TwgVcsAdapter:
    """Adaptador de Bitbucket via el CLI twg (>= 1.3).

    En 1.3 el flag `-o json` emite un envoltorio YAML con el payload en un fichero
    aparte. Para obtener JSON puro se usa `--output json --output-summary none`.
    """

    def __init__(self, *, workspace: str, repo: str, twg_bin: str = "twg") -> None:
        self._workspace = workspace
        self._repo = repo
        self._twg = twg_bin

    def _run(self, args: Sequence[str], *, json_output: bool = True) -> str:
        cmd = [self._twg, "bb", *args, "-w", self._workspace, "-r", self._repo]
        if json_output:
            cmd += ["--output", "json", "--output-summary", "none"]
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
        item = data if isinstance(data, dict) else {}
        author = item.get("author") or {}
        source = item.get("source") or {}
        destination = item.get("destination") or {}
        links = item.get("links") or {}
        return PullRequestInfo(
            repo=f"{self._workspace}/{self._repo}",
            pr_id=pr_id,
            title=str(item.get("title", "")),
            description=str(item.get("description", "") or ""),
            source_branch=str((source.get("branch") or {}).get("name", "")),
            destination_branch=str((destination.get("branch") or {}).get("name", "")),
            author=str(author.get("display_name", "") if isinstance(author, dict) else ""),
            head_sha=str((source.get("commit") or {}).get("hash", "")),
            state=str(item.get("state", "OPEN")),
            url=str(((links.get("html") or {}) if isinstance(links, dict) else {}).get("href", "")),
        )

    def get_diff(self, repo: str, pr_id: int) -> str:
        del repo
        return self._run(["prs", "diff", str(pr_id)], json_output=False)

    def get_diffstat(self, repo: str, pr_id: int) -> list[tuple[str, int, int]]:
        del repo
        entries = self._run_json(["prs", "diffstat", str(pr_id)])
        if not isinstance(entries, list):
            return []
        result: list[tuple[str, int, int]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            new = entry.get("new") or {}
            old = entry.get("old") or {}
            path = str((new or {}).get("path") or (old or {}).get("path") or "")
            if not path:
                continue
            result.append(
                (
                    path,
                    int(entry.get("lines_added", 0) or 0),
                    int(entry.get("lines_removed", 0) or 0),
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
        matches = data.get("matches", []) if isinstance(data, dict) else []
        if not isinstance(matches, list) or not matches:
            return None
        first = matches[0]
        if not isinstance(first, dict):
            return None
        to_line = first.get("toLine") or first.get("line")
        if to_line is None:
            return None
        from_line_raw = first.get("fromLine")
        return InlineAnchor(
            path=str(first.get("path", path)),
            line=int(to_line),
            from_line=int(from_line_raw) if from_line_raw is not None else None,
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

    def list_review_markers(self, repo: str, pr_id: int) -> list[ReviewMarker]:
        """Comentarios que el agente ya publico (general + inline) en el PR.

        `content.raw` trae el texto markdown; ahi va el token de dedupe. Se
        devuelven todos para que el publish filtre por su token.
        """
        del repo
        data = self._run_json(["prs", "comment", "query", str(pr_id), "-n", "200"])
        if not isinstance(data, list):
            return []
        markers: list[ReviewMarker] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            body = ""
            if isinstance(content, dict):
                body = str(content.get("raw", "") or "")
            elif isinstance(content, str):
                body = content
            markers.append(ReviewMarker(id=str(item.get("id", "")), body=body))
        return markers

    def post_task(self, repo: str, pr_id: int, body: str) -> None:
        del repo
        self._run(["prs", "task", "create", "--pull-request", str(pr_id), "--text", body])
