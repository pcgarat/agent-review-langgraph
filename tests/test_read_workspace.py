"""L7-L8: workspace de lectura del runner (016 W5-W7, ADR-0018).

Se ejercita contra un repositorio git **local** (sin red): el clon *bare*, las
operaciones `git show`/`git grep`/`git ls-tree`, el veto de rutas, la idempotencia,
el TTL y el tope de workspaces abiertos. Al final, los endpoints `/workspaces` con
`TestClient`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

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


def test_show_file_devuelve_el_contenido(workspace: ReadWorkspace) -> None:
    assert workspace.show_file(APP) == "import os\n\nneedle = 1\nprint(needle)\n"


def test_show_file_con_rango(workspace: ReadWorkspace) -> None:
    assert workspace.show_file(APP, start=3, end=3) == "needle = 1"


def test_show_file_inexistente_devuelve_none(workspace: ReadWorkspace) -> None:
    assert workspace.show_file("src/no-existe.py") is None


def test_show_file_lee_el_lado_base(workspace: ReadWorkspace) -> None:
    assert workspace.show_file(APP, ref="base") == "import os\n\nneedle = 1\n"


def test_show_file_de_una_ruta_vetada_lanza_path_denied(workspace: ReadWorkspace) -> None:
    with pytest.raises(PathDenied):
        workspace.show_file(ENV)


def test_grep_devuelve_path_linea_texto_sin_el_sha(workspace: ReadWorkspace) -> None:
    assert workspace.grep("needle") == [
        f"{APP}:3: needle = 1",
        f"{APP}:4: print(needle)",
    ]


def test_grep_no_sale_del_arbol_ni_devuelve_vetadas(workspace: ReadWorkspace) -> None:
    assert workspace.grep("SECRET") == []


def test_grep_acota_por_glob(workspace: ReadWorkspace) -> None:
    assert workspace.grep("needle", glob="*.py") == [
        f"{APP}:3: needle = 1",
        f"{APP}:4: print(needle)",
    ]
    assert workspace.grep("needle", glob="*.md") == []


def test_grep_con_patron_vacio_no_devuelve_nada(workspace: ReadWorkspace) -> None:
    assert workspace.grep("") == []


def test_grep_sin_coincidencias_devuelve_lista_vacia(workspace: ReadWorkspace) -> None:
    assert workspace.grep("no-aparece") == []


def test_grep_respeta_el_tope_de_coincidencias(remote: dict[str, str], tmp_path: Path) -> None:
    info = ReadWorkspaceManager(git_bin=GIT, denylist=(), url_template="{repo}", timeout_s=30.0)
    try:
        opened = info.open(repo=remote["repo"], head_sha=remote["head"])
        small = info.get(opened.workspace_id)
        small._max_matches = 1
        assert len(small.grep("needle")) == 1
    finally:
        info.close()


def test_tree_lista_nombres_directos(workspace: ReadWorkspace) -> None:
    assert workspace.tree("") == ["README.md", "config", "src"]
    assert workspace.tree("src") == ["app.py", "util.py"]


def test_tree_de_un_directorio_inexistente_devuelve_vacio(workspace: ReadWorkspace) -> None:
    assert workspace.tree("no/existe") == []


def test_la_base_ausente_deja_el_lado_base_sin_servir(manager, remote) -> None:
    info = manager.open(repo=remote["repo"], head_sha=remote["head"], base_sha="f" * 40)
    workspace = manager.get(info.workspace_id)
    assert workspace.show_file(APP, ref="base") is None
    assert workspace.show_file(APP) == "import os\n\nneedle = 1\nprint(needle)\n"


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
    assert "needle = 1" in response.json()["content"]


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


def test_endpoint_de_workspace_desconocido_da_404(runner_client) -> None:
    client, _ = runner_client
    response = client.get("/workspaces/no-existe/file", params={"path": APP})
    assert response.status_code == 404


def test_endpoint_grep_y_tree(runner_client, remote) -> None:
    client, _ = runner_client
    workspace_id = client.post(
        "/workspaces", json={"repo": remote["repo"], "head_sha": remote["head"]}
    ).json()["workspace_id"]

    grep = client.post(f"/workspaces/{workspace_id}/grep", json={"pattern": "needle"})
    assert grep.status_code == 200
    assert f"{APP}:4: print(needle)" in grep.json()["matches"]

    tree = client.get(f"/workspaces/{workspace_id}/tree", params={"path": "src"})
    assert tree.json()["entries"] == ["app.py", "util.py"]


def test_endpoint_grep_acota_por_max_matches(runner_client, remote) -> None:
    client, _ = runner_client
    workspace_id = client.post(
        "/workspaces", json={"repo": remote["repo"], "head_sha": remote["head"]}
    ).json()["workspace_id"]
    response = client.post(
        f"/workspaces/{workspace_id}/grep",
        json={"pattern": "needle", "max_matches": 1},
    )
    assert len(response.json()["matches"]) == 1


def test_endpoint_delete_borra_el_workspace(runner_client, remote) -> None:
    client, _ = runner_client
    workspace_id = client.post(
        "/workspaces", json={"repo": remote["repo"], "head_sha": remote["head"]}
    ).json()["workspace_id"]
    assert client.delete(f"/workspaces/{workspace_id}").json() == {"deleted": True}
    assert client.get(f"/workspaces/{workspace_id}/tree").status_code == 404


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
