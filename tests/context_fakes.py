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
from agent_review.ports.context import DEFAULT_CONTEXT_DENYLIST, is_denied

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
) -> Callable[[httpx.Request], httpx.Response]:
    """Runner de lectura en memoria: sirve el mismo arbol que el resto de fakes.

    Aplica el `context_denylist` como el runner real (W7): una ruta vetada da `403`
    en `file` y no aparece en `grep` ni en `tree`.
    """
    files = TREE if tree is None else tree
    denied = tuple(path for path in files if is_denied(path, DEFAULT_CONTEXT_DENYLIST))
    denied += tuple(extra_denied)

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.path == "/workspaces" and request.method == "POST":
            return httpx.Response(200, json={"workspace_id": WORKSPACE_ID, "expires_at": 1.0})
        suffix = url.path.rsplit("/", 1)[-1]
        if suffix == "file":
            return _file(url, files, denied)
        if suffix == "grep":
            payload: Any = _json(request)
            return httpx.Response(200, json={"matches": _grep(files, payload, denied)})
        if suffix == "tree":
            entries = _names(files, url.params.get("path", ""), denied)
            return httpx.Response(200, json={"entries": entries})
        return httpx.Response(404, json={"detail": "endpoint desconocido"})

    return handler


def _file(url: httpx.URL, files: dict[str, str], denied: Sequence[str]) -> httpx.Response:
    path = url.params.get("path", "")
    if path in denied:
        return httpx.Response(403, json={"detail": f"la ruta {path} esta vetada"})
    if path not in files:
        return httpx.Response(404, json={"detail": f"no existe {path}"})
    content = _slice(files[path], _as_int(url.params.get("start")), _as_int(url.params.get("end")))
    return httpx.Response(
        200, json={"path": path, "ref": url.params.get("ref", "head"), "content": content}
    )


def _json(request: httpx.Request) -> dict[str, Any]:
    import json

    data = json.loads(request.content or b"{}")
    return data if isinstance(data, dict) else {}


def _grep(files: dict[str, str], payload: dict[str, Any], denied: Sequence[str]) -> list[str]:
    pattern = str(payload.get("pattern", ""))
    glob = str(payload.get("glob") or "")
    matches: list[str] = []
    for path, content in files.items():
        if path in denied or not _glob_ok(path, glob):
            continue
        for number, line in enumerate(content.splitlines(), start=1):
            if pattern and pattern in line:
                matches.append(f"{path}:{number}: {line.strip()}")
    return matches


def _glob_ok(path: str, glob: str) -> bool:
    if not glob:
        return True
    from agent_review.globs import path_matches

    return path_matches(path, glob)


def _names(files: dict[str, str], prefix: str, denied: Sequence[str]) -> list[str]:
    head = f"{prefix.rstrip('/')}/" if prefix else ""
    seen: set[str] = set()
    for path in files:
        if path in denied or (head and not path.startswith(head)):
            continue
        seen.add(path[len(head) :].split("/", 1)[0])
    return sorted(seen)


def _slice(text: str, start: int | None, end: int | None) -> str:
    if start is None and end is None:
        return text
    lines = text.splitlines()
    first = max(0, (start or 1) - 1)
    last = end if end is not None else len(lines)
    return "\n".join(lines[first:last])


def _as_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
