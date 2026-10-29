"""L7-L8: workspace de lectura del runner (016 W5-W7, ADR-0018).

Se ejercita contra un repositorio git **local** (sin red): el clon *bare*, las
operaciones `git show`/`git grep`/`git ls-tree`, el veto de rutas, la idempotencia,
el TTL y el tope de workspaces abiertos. Al final, los endpoints `/workspaces` con
`TestClient`.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from agent_review.ports.context import ToolError
from agent_review.sandbox_runner.read_workspace import (
    PathDenied,
    ReadWorkspace,
    ReadWorkspaceError,
    ReadWorkspaceManager,
    ReadWorkspaceUnavailable,
)

GIT = "git"
APP = "src/app.py"
README = "README.md"
ENV = "config/.env"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        [GIT, *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=30
    )
    return result.stdout.strip()


@pytest.fixture(scope="module")
def remote(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """Repositorio *bare* con dos commits; devuelve las rutas y los sha."""
    work = tmp_path_factory.mktemp("work")
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@example.test")
    _git(work, "config", "user.name", "Tests")
    (work / "src").mkdir()
    (work / "config").mkdir()
    (work / APP).write_text("import os\n\nneedle = 1\n", encoding="utf-8")
    (work / README).write_text("# repo\n", encoding="utf-8")
    (work / ENV).write_text("SECRET=1\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "base")
    base = _git(work, "rev-parse", "HEAD")

    (work / APP).write_text("import os\n\nneedle = 1\nprint(needle)\n", encoding="utf-8")
    (work / "src/util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "head")
    head = _git(work, "rev-parse", "HEAD")

    bare = tmp_path_factory.mktemp("bare") / "origin.git"
    _git(work, "clone", "-q", "--bare", str(work), str(bare))
    return {"repo": str(bare), "head": head, "base": base, "unreachable": "0" * 40}


@pytest.fixture
def manager(remote: dict[str, str]) -> ReadWorkspaceManager:
    manager = ReadWorkspaceManager(
        git_bin=GIT,
        ttl_s=1800.0,
        max_workspaces=4,
        denylist=(".env*",),
        url_template="{repo}",
    )
    yield manager
    manager.close()


@pytest.fixture
def workspace(manager: ReadWorkspaceManager, remote: dict[str, str]) -> ReadWorkspace:
    info = manager.open(repo=remote["repo"], head_sha=remote["head"], base_sha=remote["base"])
    return manager.get(info.workspace_id)


# --- `ReadWorkspace`: operaciones de git sobre el clon *bare* ---------------------


def _text(workspace: ReadWorkspace, path: str, **kwargs) -> str:
    reading = workspace.show_file(path, **kwargs)
    assert reading is not None
    return reading.content


def test_show_file_devuelve_el_contenido(workspace: ReadWorkspace) -> None:
    assert _text(workspace, APP) == "import os\n\nneedle = 1\nprint(needle)\n"


def test_show_file_declara_el_tamano_real_antes_de_recortar(workspace: ReadWorkspace) -> None:
    expected = "import os\n\nneedle = 1\nprint(needle)\n"
    reading = workspace.show_file(APP)
    assert reading is not None
    assert reading.total_bytes == len(expected.encode("utf-8"))
    assert reading.truncated is False
    assert reading.served_range is None


def test_show_file_con_rango(workspace: ReadWorkspace) -> None:
    reading = workspace.show_file(APP, start=3, end=3)
    assert reading is not None
    assert reading.content == "needle = 1"
    assert reading.served_range == (3, 3)
    assert reading.total_bytes == len(b"import os\n\nneedle = 1\nprint(needle)\n")


def test_show_file_inexistente_devuelve_none(workspace: ReadWorkspace) -> None:
    assert workspace.show_file("src/no-existe.py") is None


def test_show_file_lee_el_lado_base(workspace: ReadWorkspace) -> None:
    assert _text(workspace, APP, ref="base") == "import os\n\nneedle = 1\n"


def test_show_file_rechaza_un_rango_imposible(workspace: ReadWorkspace) -> None:
    with pytest.raises(ValueError):
        workspace.show_file(APP, start=0)


def test_show_file_en_outline_da_rangos_y_el_rango_se_lee(workspace: ReadWorkspace) -> None:
    index = workspace.show_file("src/util.py", mode="outline")
    assert index is not None
    assert "1-2  def helper()" in index.content
    assert index.total_bytes == len(b"def helper():\n    return 1\n")
    assert index.truncated is False
    assert _text(workspace, "src/util.py", start=1, end=2) == "def helper():\n    return 1"


def test_show_file_outline_de_un_fichero_sin_indexador_es_error(
    workspace: ReadWorkspace,
) -> None:
    with pytest.raises(ToolError, match="indexar"):
        workspace.show_file(README, mode="outline")
    with pytest.raises(ValueError):
        workspace.show_file(APP, start=5, end=2)


def test_show_file_de_una_ruta_vetada_lanza_path_denied(workspace: ReadWorkspace) -> None:
    with pytest.raises(PathDenied):
        workspace.show_file(ENV)


def test_grep_devuelve_path_linea_texto_sin_el_sha(workspace: ReadWorkspace) -> None:
    found = workspace.grep("needle")
    assert found.items == (
        f"{APP}:3: needle = 1",
        f"{APP}:4: print(needle)",
    )
    assert found.total == 2
    assert found.truncated is False


def test_grep_no_sale_del_arbol_ni_devuelve_vetadas(workspace: ReadWorkspace) -> None:
    assert workspace.grep("SECRET").items == ()


def test_grep_acota_por_glob(workspace: ReadWorkspace) -> None:
    assert workspace.grep("needle", glob="**/*.py").items == (
        f"{APP}:3: needle = 1",
        f"{APP}:4: print(needle)",
    )
    # `*` no cruza `/`: el glob se mide desde la raiz y no como un pathspec de git.
    assert workspace.grep("needle", glob="*.py").items == ()
    assert workspace.grep("needle", glob="*.md").items == ()


def test_grep_glob_se_mide_desde_path(workspace: ReadWorkspace) -> None:
    assert workspace.grep("needle", path="src", glob="*.py").items == (
        f"{APP}:3: needle = 1",
        f"{APP}:4: print(needle)",
    )
    assert workspace.grep("needle", path="src", glob="**/*.py").items == ()
    assert workspace.grep("needle", path="src", glob="*.md").items == ()


def test_grep_en_regex_usa_el_dialecto_portable(workspace: ReadWorkspace) -> None:
    assert workspace.grep(r"needle = \d", mode="regex").items == (f"{APP}:3: needle = 1",)
    # `\s` no acaba en `[ \t…]`: GNU grep leeria `\t` como la letra `t`.
    assert workspace.grep(r"print\(needle\)", mode="regex").items == (f"{APP}:4: print(needle)",)


@pytest.mark.parametrize(
    "pattern",
    [r"[[:alpha:]]+", r"\bneedle\b", r"(?=needle)", r"(needle)\1", "needle("],
)
def test_grep_rechaza_un_patron_no_portable(workspace: ReadWorkspace, pattern: str) -> None:
    with pytest.raises(ToolError):
        workspace.grep(pattern, mode="regex")


def test_grep_avisa_si_git_no_responde(workspace: ReadWorkspace, monkeypatch) -> None:
    monkeypatch.setattr(workspace, "_run", lambda args: None)
    with pytest.raises(ToolError, match="tiempo"):
        workspace.grep("needle")


def test_grep_avisa_si_git_falla(workspace: ReadWorkspace, monkeypatch) -> None:
    failed = subprocess.CompletedProcess(args=[], returncode=2, stdout="", stderr="boom")
    monkeypatch.setattr(workspace, "_run", lambda args: failed)
    with pytest.raises(ToolError, match="boom"):
        workspace.grep("needle")


def test_grep_con_patron_vacio_no_devuelve_nada(workspace: ReadWorkspace) -> None:
    assert workspace.grep("").items == ()


def test_grep_sin_coincidencias_devuelve_lista_vacia(workspace: ReadWorkspace) -> None:
    assert workspace.grep("no-aparece").items == ()


def test_grep_respeta_el_tope_de_coincidencias(remote: dict[str, str], tmp_path: Path) -> None:
    info = ReadWorkspaceManager(git_bin=GIT, denylist=(), url_template="{repo}", timeout_s=30.0)
    try:
        opened = info.open(repo=remote["repo"], head_sha=remote["head"])
        small = info.get(opened.workspace_id)
        small._max_matches = 1
        found = small.grep("needle")
        assert len(found.items) == 1
        assert found.total == 2, "el total cuenta antes del tope"
        assert found.truncated is True
    finally:
        info.close()


def test_tree_lista_nombres_directos(workspace: ReadWorkspace) -> None:
    assert workspace.tree("").items == ("README.md", "config", "src")
    listing = workspace.tree("src")
    assert listing.items == ("app.py", "util.py")
    assert listing.total == 2


def test_tree_de_un_directorio_inexistente_es_un_error(workspace: ReadWorkspace) -> None:
    # Vacio e inexistente no son lo mismo: el bucle tiene que poder decir cual (L1).
    with pytest.raises(ToolError, match="no se pudo listar"):
        workspace.tree("no/existe")


def test_tree_de_un_fichero_sugiere_read_file(workspace: ReadWorkspace) -> None:
    with pytest.raises(ToolError, match="es un fichero"):
        workspace.tree(APP)


def test_tree_acota_con_depth_y_glob(workspace: ReadWorkspace) -> None:
    assert workspace.tree("", glob="*.md").items == ("README.md",)
    assert workspace.tree("src", glob="*.py").items == ("app.py", "util.py")
    assert workspace.tree("", depth=2).items == (
        "README.md",
        "config",
        "src",
        "src/app.py",
        "src/util.py",
    )


def test_tree_no_ofrece_rutas_vetadas_en_ningun_nivel(workspace: ReadWorkspace) -> None:
    assert "config/.env" not in workspace.tree("", depth=2).items
    assert workspace.tree("config").items == ()


def test_la_base_ausente_deja_el_lado_base_sin_servir(manager, remote) -> None:
    info = manager.open(repo=remote["repo"], head_sha=remote["head"], base_sha="f" * 40)
    workspace = manager.get(info.workspace_id)
    assert workspace.show_file(APP, ref="base") is None
    assert _text(workspace, APP) == "import os\n\nneedle = 1\nprint(needle)\n"


def test_el_commit_obligatorio_ausente_es_un_fallo_recuperable(manager, remote) -> None:
    with pytest.raises(ReadWorkspaceUnavailable):
        manager.open(repo=remote["repo"], head_sha="f" * 40)


# --- `ReadWorkspaceManager`: idempotencia, TTL y tope -----------------------------


def test_abrir_dos_veces_el_mismo_par_devuelve_el_mismo_workspace(manager, remote) -> None:
    first = manager.open(repo=remote["repo"], head_sha=remote["head"])
    second = manager.open(repo=remote["repo"], head_sha=remote["head"])
    assert first.workspace_id == second.workspace_id


def test_reabrir_extiende_el_ttl(manager, remote) -> None:
    now = {"t": 1000.0}
    manager._clock = lambda: now["t"]
    first = manager.open(repo=remote["repo"], head_sha=remote["head"])
    now["t"] += 100.0
    second = manager.open(repo=remote["repo"], head_sha=remote["head"])
    assert second.expires_at > first.expires_at


def test_un_workspace_caducado_lo_borra_el_reaper(manager, remote) -> None:
    now = {"t": 1000.0}
    manager._clock = lambda: now["t"]
    info = manager.open(repo=remote["repo"], head_sha=remote["head"])
    now["t"] += 1801.0
    with pytest.raises(ReadWorkspaceError):
        manager.get(info.workspace_id)


def test_el_tope_de_workspaces_desaloja_el_mas_viejo(remote) -> None:
    manager = ReadWorkspaceManager(
        git_bin=GIT, max_workspaces=1, url_template="{repo}", timeout_s=30.0
    )
    try:
        first = manager.open(repo=remote["repo"], head_sha=remote["head"])
        manager.open(repo=remote["repo"], head_sha=remote["base"])
        with pytest.raises(ReadWorkspaceError):
            manager.get(first.workspace_id)
    finally:
        manager.close()


class _GatedManager(ReadWorkspaceManager):
    """Gestor cuyo clon espera a una señal: simula un `git fetch` lento."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.gate = threading.Event()
        self.cloning = threading.Event()
        self.clones = 0

    def _clone(self, *, repo: str, head_sha: str, base_sha: str) -> ReadWorkspace:
        self.clones += 1
        self.cloning.set()
        assert self.gate.wait(timeout=10), "el test no abrio la compuerta"
        return super()._clone(repo=repo, head_sha=head_sha, base_sha=base_sha)


def _gated(remote: dict[str, str]) -> _GatedManager:
    return _GatedManager(git_bin=GIT, url_template="{repo}", timeout_s=30.0)


def test_un_clon_lento_no_bloquea_las_lecturas_de_otro_workspace(remote) -> None:
    # El clon corre fuera del lock: mientras uno clona, `get()` de un workspace ya
    # abierto responde sin esperar (si no, el cliente agota sus 30 s y degrada).
    manager = _gated(remote)
    try:
        manager.gate.set()
        ready = manager.open(repo=remote["repo"], head_sha=remote["base"])
        manager.gate.clear()
        manager.cloning.clear()
        slow = threading.Thread(
            target=manager.open, kwargs={"repo": remote["repo"], "head_sha": remote["head"]}
        )
        slow.start()
        assert manager.cloning.wait(timeout=10)
        got: list[ReadWorkspace] = []
        reader = threading.Thread(target=lambda: got.append(manager.get(ready.workspace_id)))
        reader.start()
        reader.join(timeout=2)
        assert got, "get() no debe esperar a un clon ajeno"
        manager.gate.set()
        slow.join(timeout=10)
    finally:
        manager.gate.set()
        manager.close()


def test_dos_aperturas_simultaneas_del_mismo_par_clonan_una_vez(remote) -> None:
    manager = _gated(remote)
    try:
        results: list[str] = []

        def open_it() -> None:
            info = manager.open(repo=remote["repo"], head_sha=remote["head"])
            results.append(info.workspace_id)

        first = threading.Thread(target=open_it)
        first.start()
        assert manager.cloning.wait(timeout=10)
        second = threading.Thread(target=open_it)
        second.start()
        manager.gate.set()
        first.join(timeout=10)
        second.join(timeout=10)
        assert manager.clones == 1, "la segunda apertura espera al clon en curso"
        assert len(results) == 2 and results[0] == results[1]
    finally:
        manager.gate.set()
        manager.close()


def test_si_el_clon_falla_otra_apertura_lo_reintenta(remote) -> None:
    manager = ReadWorkspaceManager(git_bin=GIT, url_template="{repo}", timeout_s=30.0)
    try:
        with pytest.raises(ReadWorkspaceUnavailable):
            manager.open(repo=remote["repo"], head_sha=remote["unreachable"])
        assert manager._cloning == {}, "un clon fallido no deja la marca colgada"
        info = manager.open(repo=remote["repo"], head_sha=remote["head"])
        assert manager.get(info.workspace_id)
    finally:
        manager.close()


def test_borrar_un_workspace_lo_quita_del_registro(manager, remote) -> None:
    info = manager.open(repo=remote["repo"], head_sha=remote["head"])
    assert manager.delete(info.workspace_id) is True
    assert manager.delete(info.workspace_id) is False
    with pytest.raises(ReadWorkspaceError):
        manager.get(info.workspace_id)


def test_borrar_permite_volver_a_abrir_el_mismo_par(manager, remote) -> None:
    first = manager.open(repo=remote["repo"], head_sha=remote["head"])
    manager.delete(first.workspace_id)
    second = manager.open(repo=remote["repo"], head_sha=remote["head"])
    assert second.workspace_id != first.workspace_id


def test_cleanup_borra_el_directorio_del_clon(manager, remote, tmp_path) -> None:
    info = manager.open(repo=remote["repo"], head_sha=remote["head"])
    directory = Path(manager.get(info.workspace_id).directory)
    assert directory.is_dir()
    manager.delete(info.workspace_id)
    assert not directory.exists()


def test_close_borra_todos_los_clones(remote) -> None:
    manager = ReadWorkspaceManager(git_bin=GIT, url_template="{repo}", timeout_s=30.0)
    info = manager.open(repo=remote["repo"], head_sha=remote["head"])
    directory = Path(manager.get(info.workspace_id).directory)
    manager.close()
    assert not directory.exists()
    with pytest.raises(ReadWorkspaceError):
        manager.get(info.workspace_id)


def test_un_git_ausente_es_un_fallo_recuperable(tmp_path: Path) -> None:
    manager = ReadWorkspaceManager(git_bin="git-que-no-existe", url_template="{repo}")
    try:
        with pytest.raises(ReadWorkspaceUnavailable):
            manager.open(repo=str(tmp_path), head_sha="a" * 40)
    finally:
        manager.close()


# --- Endpoints `/workspaces` -------------------------------------------------------


@pytest.fixture
def runner_client(monkeypatch, remote):
    from fastapi.testclient import TestClient

    from agent_review.sandbox_runner import main as runner_main

    settings = runner_main.get_settings()
    # El token de ejemplo relaja la auth, como en development.
    monkeypatch.setattr(settings, "sandbox_runner_token", "change-me")
    manager = ReadWorkspaceManager(
        git_bin=GIT, denylist=(".env*",), url_template="{repo}", timeout_s=30.0
    )
    monkeypatch.setattr(runner_main, "_workspaces", manager)
    with TestClient(runner_main.app) as client:
        yield client, manager
    manager.close()


def test_endpoint_abre_y_sirve_fichero(runner_client, remote) -> None:
    client, _ = runner_client
    opened = client.post(
        "/workspaces",
        json={"repo": remote["repo"], "head_sha": remote["head"], "base_sha": remote["base"]},
    )
    assert opened.status_code == 200
    workspace_id = opened.json()["workspace_id"]
    assert opened.json()["expires_at"] > 0

    response = client.get(f"/workspaces/{workspace_id}/file", params={"path": APP, "ref": "head"})
    assert response.status_code == 200
    body = response.json()
    assert "needle = 1" in body["content"]
    assert body["total_bytes"] == len(body["content"].encode("utf-8"))
    assert body["truncated"] is False
    assert body["served_range"] is None


def test_endpoint_de_fichero_transporta_el_rango_y_el_tamano(runner_client, remote) -> None:
    client, _ = runner_client
    workspace_id = client.post(
        "/workspaces", json={"repo": remote["repo"], "head_sha": remote["head"]}
    ).json()["workspace_id"]
    response = client.get(
        f"/workspaces/{workspace_id}/file", params={"path": APP, "start": 3, "end": 3}
    )
    body = response.json()
    assert response.status_code == 200
    assert body["content"] == "needle = 1"
    assert body["served_range"] == [3, 3]
    assert body["total_bytes"] == len(b"import os\n\nneedle = 1\nprint(needle)\n")


def test_endpoint_de_fichero_con_rango_imposible_da_400(runner_client, remote) -> None:
    client, _ = runner_client
    workspace_id = client.post(
        "/workspaces", json={"repo": remote["repo"], "head_sha": remote["head"]}
    ).json()["workspace_id"]
    response = client.get(f"/workspaces/{workspace_id}/file", params={"path": APP, "start": 0})
    assert response.status_code == 400


def test_endpoint_de_fichero_en_outline(runner_client, remote) -> None:
    client, _ = runner_client
    workspace_id = client.post(
        "/workspaces", json={"repo": remote["repo"], "head_sha": remote["head"]}
    ).json()["workspace_id"]
    index = client.get(
        f"/workspaces/{workspace_id}/file",
        params={"path": "src/util.py", "mode": "outline"},
    )
    assert index.status_code == 200
    body = index.json()
    assert "1-2  def helper()" in body["content"]
    assert body["total_bytes"] == len(b"def helper():\n    return 1\n")
    assert body["truncated"] is False

    # Sin indexador: error con salida, nunca el fichero entero.
    bad = client.get(
        f"/workspaces/{workspace_id}/file",
        params={"path": README, "mode": "outline"},
    )
    assert bad.status_code == 400
    assert "indexar" in bad.json()["detail"]


def test_endpoint_abrir_dos_veces_es_idempotente(runner_client, remote) -> None:
    client, _ = runner_client
    payload = {"repo": remote["repo"], "head_sha": remote["head"]}
    first = client.post("/workspaces", json=payload).json()["workspace_id"]
    second = client.post("/workspaces", json=payload).json()["workspace_id"]
    assert first == second


def test_endpoint_de_fichero_inexistente_da_404(runner_client, remote) -> None:
    client, _ = runner_client
    workspace_id = client.post(
        "/workspaces", json={"repo": remote["repo"], "head_sha": remote["head"]}
    ).json()["workspace_id"]
    response = client.get(f"/workspaces/{workspace_id}/file", params={"path": "src/no-existe.py"})
    assert response.status_code == 404


def test_endpoint_de_ruta_vetada_da_403(runner_client, remote) -> None:
    client, _ = runner_client
    workspace_id = client.post(
        "/workspaces", json={"repo": remote["repo"], "head_sha": remote["head"]}
    ).json()["workspace_id"]
    response = client.get(f"/workspaces/{workspace_id}/file", params={"path": ENV})
    assert response.status_code == 403


def test_endpoint_de_workspace_desconocido_da_410(runner_client) -> None:
    # `410` y no `404`: el lector distingue un workspace perdido de un fichero que
    # no existe, y solo el primero gasta su reapertura (W8).
    client, _ = runner_client
    response = client.get("/workspaces/no-existe/file", params={"path": APP})
    assert response.status_code == 410


def test_endpoint_grep_y_tree(runner_client, remote) -> None:
    client, _ = runner_client
    workspace_id = client.post(
        "/workspaces", json={"repo": remote["repo"], "head_sha": remote["head"]}
    ).json()["workspace_id"]

    grep = client.post(f"/workspaces/{workspace_id}/grep", json={"pattern": "needle"})
    assert grep.status_code == 200
    assert f"{APP}:4: print(needle)" in grep.json()["matches"]

    assert grep.json()["total"] == 2
    assert grep.json()["truncated"] is False

    tree = client.get(f"/workspaces/{workspace_id}/tree", params={"path": "src"})
    assert tree.json()["entries"] == ["app.py", "util.py"]
    assert tree.json()["total"] == 2


def test_endpoint_grep_acota_por_max_matches(runner_client, remote) -> None:
    client, _ = runner_client
    workspace_id = client.post(
        "/workspaces", json={"repo": remote["repo"], "head_sha": remote["head"]}
    ).json()["workspace_id"]
    response = client.post(
        f"/workspaces/{workspace_id}/grep",
        json={"pattern": "needle", "max_matches": 1},
    )
    body = response.json()
    assert len(body["matches"]) == 1
    assert body["total"] == 2, "el tope recorta lo servido, no lo contado"
    assert body["truncated"] is True


def test_endpoint_grep_en_regex_y_patron_no_portable(runner_client, remote) -> None:
    client, _ = runner_client
    opened = client.post(
        "/workspaces",
        json={"repo": remote["repo"], "head_sha": remote["head"]},
    ).json()
    assert "grep.mode" in opened["capabilities"]
    assert "read.mode" in opened["capabilities"]
    workspace_id = opened["workspace_id"]

    regex = client.post(
        f"/workspaces/{workspace_id}/grep",
        json={"pattern": r"needle = \d", "mode": "regex"},
    )
    assert regex.status_code == 200
    assert regex.json()["matches"] == [f"{APP}:3: needle = 1"]

    bad = client.post(
        f"/workspaces/{workspace_id}/grep",
        json={"pattern": r"\bneedle\b", "mode": "regex"},
    )
    assert bad.status_code == 400
    assert "frontera" in bad.json()["detail"]


def test_endpoint_delete_borra_el_workspace(runner_client, remote) -> None:
    client, _ = runner_client
    workspace_id = client.post(
        "/workspaces", json={"repo": remote["repo"], "head_sha": remote["head"]}
    ).json()["workspace_id"]
    assert client.delete(f"/workspaces/{workspace_id}").json() == {"deleted": True}
    assert client.get(f"/workspaces/{workspace_id}/tree").status_code == 410


def test_endpoints_exigen_token_cuando_esta_configurado(monkeypatch, remote) -> None:
    from fastapi.testclient import TestClient

    from agent_review.sandbox_runner import main as runner_main

    settings = runner_main.get_settings()
    monkeypatch.setattr(settings, "sandbox_runner_token", "secreto-real")
    with TestClient(runner_main.app) as client:
        response = client.post(
            "/workspaces", json={"repo": remote["repo"], "head_sha": remote["head"]}
        )
    assert response.status_code == 401


def test_la_base_es_la_bifurcacion_aunque_destino_haya_avanzado(
    tmp_path: Path,
) -> None:
    """`ref="base"` lee el fichero como estaba al abrir la rama, no la punta de destino.

    Con `--depth 1` no hay ancestro común; leer a la punta enseñaría como «antes del
    PR» lo que entró en destino después.
    """
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@example.test")
    _git(work, "config", "user.name", "Tests")
    (work / "src").mkdir()
    (work / APP).write_text("fork\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "fork")
    fork = _git(work, "rev-parse", "HEAD")
    (work / README).write_text("rama\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "rama")
    head = _git(work, "rev-parse", "HEAD")
    _git(work, "checkout", "-q", fork)
    for i in range(3):
        (work / APP).write_text(f"main {i}\n", encoding="utf-8")
        _git(work, "commit", "-qam", f"main {i}")
    tip = _git(work, "rev-parse", "HEAD")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    manager = ReadWorkspaceManager(git_bin=GIT, url_template="file://{repo}")
    try:
        info = manager.open(repo=str(bare), head_sha=head, base_sha=tip)
        workspace = manager.get(info.workspace_id)
        assert workspace.base_sha == fork
        assert _text(workspace, APP, ref="base") == "fork\n"
    finally:
        manager.close()
