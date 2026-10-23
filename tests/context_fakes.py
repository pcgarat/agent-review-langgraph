"""Fakes compartidos por las suites del lector de contexto (016).

Un unico **arbol logico** sirve a los tres adaptadores, de modo que el test de
contrato de `tests/test_context_ports.py` ejecute las mismas aserciones contra cada
uno. Sin red ni repositorio real: el VCS es un doble y el runner, un
`httpx.MockTransport`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx

from agent_review.adapters.context_fixture import FixtureContextReader
from agent_review.adapters.context_vcs import VcsContextReader
from agent_review.adapters.context_workspace import WorkspaceContextReader
from agent_review.outline import outline_text
from agent_review.ports.context import (
    DEFAULT_CONTEXT_DENYLIST,
    READER_CAPABILITIES,
    ToolError,
    is_denied,
    line_matcher,
    slice_lines,
    truncate_bytes,
)

REPO = "cqm/x"
HEAD = "head1"
BASE = "base1"
WORKSPACE_ID = "w1"

# Arbol logico: una ruta normal, una raiz y una vetada por el denylist.
APP = "src/app.py"
README = "README.md"
ENV = "config/.env"
TREE: dict[str, str] = {
    APP: "import os\n\nneedle = 1\nprint(needle)\n",
    README: "# repo\n",
    ENV: "SECRET=1\n",
}
# Contenido esperado de `APP` (el arbol de los fakes conserva el salto final).
APP_TEXT = TREE[APP]
# El diff solo toca `src/app.py`: es lo unico que el modo VCS puede listar.
DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,4 @@\n"
    "+import os\n"
    "+\n"
    "+needle = 1\n"
    "+print(needle)\n"
)


class FakeVcs:
    """`VcsPort.get_file` sobre un mapa `(path, sha) -> contenido`."""

    def __init__(self, files: dict[tuple[str, str], str] | None = None) -> None:
        self.files = (
            files
            if files is not None
            else {
                (APP, HEAD): TREE[APP],
                (README, HEAD): TREE[README],
                (APP, BASE): "import os\n",
            }
        )
        self.calls: list[tuple[str, str, str]] = []

    def get_file(self, repo: str, path: str, *, ref: str) -> str:
        self.calls.append((repo, path, ref))
        try:
            return self.files[(path, ref)]
        except KeyError:
            raise FileNotFoundError(f"{path}@{ref} no existe") from None


def write_tree(root: Path) -> Path:
    for relative, content in TREE.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def build_fixture_reader(root: Path) -> FixtureContextReader:
    write_tree(root)
    return FixtureContextReader(root, denylist=DEFAULT_CONTEXT_DENYLIST)


def build_vcs_reader(vcs: FakeVcs | None = None) -> VcsContextReader:
    return VcsContextReader(
        vcs=vcs or FakeVcs(),
        repo=REPO,
        head_sha=HEAD,
        base_sha=BASE,
        diff=DIFF,
    )


def build_workspace_reader(
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
    *,
    token: str = "",
) -> WorkspaceContextReader:
    client = httpx.Client(transport=httpx.MockTransport(handler or runner_handler()))
    return WorkspaceContextReader(
        base_url="http://runner:8099",
        repo=REPO,
        head_sha=HEAD,
        base_sha=BASE,
        token=token,
        client=client,
    )


def runner_handler(
    tree: dict[str, str] | None = None,
    *,
    extra_denied: Sequence[str] = (),
    capabilities: Sequence[str] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Runner de lectura en memoria: sirve el mismo arbol que el resto de fakes.

    Aplica el `context_denylist` como el runner real (W7): una ruta vetada da `403`
    en `file` y no aparece en `grep` ni en `tree`. `capabilities=None` anuncia las
    del runner actual; una lista vacia simula un runner anterior a 023.
    """
    files = TREE if tree is None else tree
    denied = tuple(path for path in files if is_denied(path, DEFAULT_CONTEXT_DENYLIST))
    denied += tuple(extra_denied)
    advertised = list(READER_CAPABILITIES if capabilities is None else capabilities)

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.path == "/workspaces" and request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "workspace_id": WORKSPACE_ID,
                    "expires_at": 1.0,
                    "capabilities": advertised,
                },
            )
        suffix = url.path.rsplit("/", 1)[-1]
        if suffix == "file":
            try:
                return _file(url, files, denied)
            except ToolError as exc:
                return httpx.Response(400, json={"detail": str(exc)})
        if suffix == "grep":
            payload: Any = _json(request)
            try:
                matches, total = _grep(files, payload, denied)
            except ToolError as exc:
                return httpx.Response(400, json={"detail": str(exc)})
            return httpx.Response(
                200,
                json={
                    "matches": matches,
                    "total": total,
                    "truncated": total > len(matches),
                },
            )
        if suffix == "tree":
            try:
                entries = _names(
                    files,
                    url.params.get("path", ""),
                    denied,
                    glob=url.params.get("glob"),
                    depth=_as_int(url.params.get("depth")) or 1,
                )
            except ToolError as exc:
                return httpx.Response(400, json={"detail": str(exc)})
            return httpx.Response(
                200,
                json={"entries": entries, "total": len(entries), "truncated": False},
            )
        return httpx.Response(404, json={"detail": "endpoint desconocido"})

    return handler


def _file(url: httpx.URL, files: dict[str, str], denied: Sequence[str]) -> httpx.Response:
    path = url.params.get("path", "")
    if path in denied:
        return httpx.Response(403, json={"detail": f"la ruta {path} esta vetada"})
    if path not in files:
        return httpx.Response(404, json={"detail": f"no existe {path}"})
    text = files[path]
    if url.params.get("mode") == "outline":
        index = outline_text(path, text)
        if index is None:
            raise ToolError(f"no se puede indexar {path}; pide `start`/`end` tras un `grep`")
        return httpx.Response(
            200,
            json={
                "path": path,
                "ref": url.params.get("ref", "head"),
                "content": index,
                "total_bytes": len(text.encode("utf-8")),
                "truncated": False,
                "served_range": None,
            },
        )
    content, served_range = slice_lines(
        text, _as_int(url.params.get("start")), _as_int(url.params.get("end"))
    )
    content, truncated = truncate_bytes(content, 200_000)
    return httpx.Response(
        200,
        json={
            "path": path,
            "ref": url.params.get("ref", "head"),
            "content": content,
            "total_bytes": len(text.encode("utf-8")),
            "truncated": truncated,
            "served_range": list(served_range) if served_range else None,
        },
    )


def _json(request: httpx.Request) -> dict[str, Any]:
    import json

    data = json.loads(request.content or b"{}")
    return data if isinstance(data, dict) else {}


def _grep(
    files: dict[str, str], payload: dict[str, Any], denied: Sequence[str]
) -> tuple[list[str], int]:
    pattern = str(payload.get("pattern", ""))
    glob = str(payload.get("glob") or "")
    prefix = str(payload.get("path") or "").strip("/")
    files_only = payload.get("files_only") is True
    matches_line = line_matcher(pattern, str(payload.get("mode") or "literal"))  # type: ignore[arg-type]
    matches: list[str] = []
    seen: set[str] = set()
    total = 0
    for path, content in files.items():
        if path in denied or not _glob_ok(_under(path, prefix), glob):
            continue
        if prefix and path != prefix and not path.startswith(f"{prefix}/"):
            continue
        for number, line in enumerate(content.splitlines(), start=1):
            if pattern and matches_line(line):
                if files_only:
                    if path not in seen:
                        seen.add(path)
                        matches.append(path)
                    continue
                total += 1
                matches.append(f"{path}:{number}: {line.strip()}")
    return matches, len(seen) if files_only else total


def _glob_ok(path: str, glob: str) -> bool:
    if not glob:
        return True
    from agent_review.globs import path_matches

    return path_matches(path, glob)


def _under(path: str, prefix: str) -> str:
    """Ruta vista desde `prefix`; el runner mide el glob ahi (ADR-0027)."""
    return path[len(prefix) + 1 :] if prefix else path


def _names(
    files: dict[str, str],
    prefix: str,
    denied: Sequence[str],
    *,
    glob: str | None = None,
    depth: int = 1,
) -> list[str]:
    head = f"{prefix.rstrip('/')}/" if prefix else ""
    if prefix and prefix in files:
        raise ToolError(f"`{prefix}` es un fichero, no un directorio; usa `read_file`")
    if prefix and not any(path.startswith(head) for path in files):
        raise ToolError(f"no existe el directorio `{prefix}`")
    seen: set[str] = set()
    for path in files:
        if path in denied or (head and not path.startswith(head)):
            continue
        rest = path[len(head) :]
        parts = rest.split("/")
        for level in range(1, min(depth, len(parts)) + 1):
            name = "/".join(parts[:level])
            if glob and not _glob_ok(name, glob):
                continue
            seen.add(name)
    return sorted(seen)


def _as_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
