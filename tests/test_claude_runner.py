"""Proceso del CLI de Claude (025 R1–R4).

El puerto devuelve stdout. El worker lo llama antes del grafo y no lo repite
si el artefacto de ese `head_sha` ya es válido. El subproceso no hereda
credenciales. El servicio no es el clon bare ni recibe el token de publicación.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
import pytest
import yaml

from agent_review.adapters.claude_artifact import ClaudeArtifactStore
from agent_review.adapters.claude_cli import SubprocessClaudeRunner, claude_argv
from agent_review.adapters.claude_client import HttpClaudeRunner
from agent_review.claude_runner.checkout import checkout_directory, drop_push_remotes
from agent_review.domain.models import ReviewRequest
from agent_review.errors import PermanentError, TransientError
from agent_review.ports.claude import (
    ClaudeInvalidOutput,
    ClaudeProcessCrashed,
    ClaudeQuotaExceeded,
    ClaudeTimeout,
)
from agent_review.queue import InMemoryReviewQueue
from agent_review.sandbox_runner.read_workspace import BARE_CLONE_PREFIX
from agent_review.settings import Settings, secrets_for
from agent_review.worker.main import _consume

_ROOT = Path(__file__).resolve().parents[1]
_COMPOSE = _ROOT / "docker-compose.yml"
_SECRETOS = (
    "GITHUB_TOKEN",
    "TWG_BBC_TOKEN",
    "TWG_TOKEN",
    "TEAMS_BOT_TOKEN",
    "TEAMS_BOT_SHARED_SECRET",
    "CLAUDE_GIT_TOKEN",
)


def _hallazgo() -> dict[str, object]:
    return {
        "id": "claude-1",
        "title": "Saldo negativo no validado antes de retirar",
        "severity": "P2",
        "file": "src/Transfer.php",
        "line": 14,
        "why_it_matters": "Se lanza una excepcion de dominio sin proteger el estado",
        "autofix_class": "gated_auto",
        "owner": "downstream-resolver",
        "failure_scenario": "retirar sin saldo deja el estado a medias",
        "confidence": 75,
        "evidence": ["src/Transfer.php:14 throw new InsufficientBalance('no funds');"],
    }


class _Runner:
    """Doble: cuenta llamadas y el `head_sha` de cada una."""

    def __init__(self, stdout: str = "[]", error: BaseException | None = None) -> None:
        self.calls: list[str] = []
        self.stdout = stdout
        self.error = error

    def run(self, *, repo: str, pr_id: int, head_sha: str) -> str:
        del repo, pr_id
        self.calls.append(head_sha)
        if self.error is not None:
            raise self.error
        return self.stdout


class _Graph:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, state, config=None):  # noqa: ANN001
        del config
        self.calls += 1
        return {"request": state["request"], "findings": [], "merged": [], "decisions": []}


def _request(head_sha: str = "abc123") -> ReviewRequest:
    return ReviewRequest(repo="cqm/emotivoo-v2", pr_id=12, head_sha=head_sha, pipeline_id="7")


def test_los_errores_del_puerto_se_distinguen() -> None:
    assert issubclass(ClaudeTimeout, TransientError)
    assert issubclass(ClaudeProcessCrashed, TransientError)
    assert issubclass(ClaudeInvalidOutput, TransientError)
    assert issubclass(ClaudeQuotaExceeded, PermanentError)
    assert not issubclass(ClaudeQuotaExceeded, TransientError)


def test_el_argv_lleva_el_tope_de_turnos_y_no_nombra_escritura() -> None:
    argv = claude_argv([sys.executable, "claude"], max_turns=7)
    indice = argv.index("--max-turns")
    assert argv[indice + 1] == "7"
    texto = " ".join(argv)
    for herramienta in ("Bash", "Edit", "Write", "NotebookEdit"):
        assert herramienta not in texto


def test_el_subproceso_no_hereda_credenciales(tmp_path: Path) -> None:
    script = tmp_path / "claude.py"
    script.write_text(
        "import json, os, sys\nprint(json.dumps({'argv': sys.argv, 'env': dict(os.environ)}))\n",
        encoding="utf-8",
    )
    parent = {"PATH": "/usr/bin", "HOME": "/tmp", "LANG": "C"}
    parent.update({name: f"secreto-{name}" for name in _SECRETOS})
    runner = SubprocessClaudeRunner(
        argv_prefix=[sys.executable, str(script)],
        max_turns=4,
        timeout_s=5,
        environ=parent,
        cwd=tmp_path,
    )

    stdout = runner.run(repo="cqm/demo", pr_id=1, head_sha="abc")

    visto = json.loads(stdout)
    assert "--max-turns" in visto["argv"]
    assert "4" in visto["argv"]
    for herramienta in ("Bash", "Edit", "Write", "NotebookEdit"):
        assert herramienta not in " ".join(visto["argv"])
    for nombre in _SECRETOS:
        assert nombre not in visto["env"]
        assert f"secreto-{nombre}" not in stdout
    assert visto["env"]["PATH"] == "/usr/bin"


def test_un_stdout_que_no_es_hallazgos_lo_valida_quien_llama(tmp_path: Path) -> None:
    script = tmp_path / "claude.py"
    script.write_text("print('no es json')\n", encoding="utf-8")
    runner = SubprocessClaudeRunner(
        argv_prefix=[sys.executable, str(script)],
        max_turns=1,
        timeout_s=5,
        environ={"PATH": "/usr/bin"},
    )
    stdout = runner.run(repo="r", pr_id=1, head_sha="abc")
    store = ClaudeArtifactStore(root=tmp_path)
    assert store.save(stdout, repo="r", pr_id=1, head_sha="abc") is False


def test_el_proceso_caido_el_cupo_y_el_tiempo_son_errores_distintos(tmp_path: Path) -> None:
    caido = tmp_path / "cae.py"
    caido.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    cupo = tmp_path / "cupo.py"
    cupo.write_text(
        "import sys\nprint('rate limit exceeded', file=sys.stderr)\nsys.exit(1)\n",
        encoding="utf-8",
    )
    lento = tmp_path / "lento.py"
    lento.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")

    def lanzar(script: Path, timeout: float) -> None:
        SubprocessClaudeRunner(
            argv_prefix=[sys.executable, str(script)],
            max_turns=1,
            timeout_s=timeout,
            environ={"PATH": "/usr/bin"},
        ).run(repo="r", pr_id=1, head_sha="abc")

    with pytest.raises(ClaudeProcessCrashed):
        lanzar(caido, 5)
    with pytest.raises(ClaudeQuotaExceeded):
        lanzar(cupo, 5)
    with pytest.raises(ClaudeTimeout):
        lanzar(lento, 0.2)


def test_con_artefacto_valido_no_se_repite_y_el_grafo_arranca(tmp_path: Path) -> None:
    store = ClaudeArtifactStore(root=tmp_path)
    runner = _Runner(stdout=json.dumps([_hallazgo()]))
    graph = _Graph()
    queue = InMemoryReviewQueue()
    request = _request()

    _consume(queue, graph, request, max_attempts=3, claude_runner=runner, claude_store=store)
    _consume(queue, graph, request, max_attempts=3, claude_runner=runner, claude_store=store)
    otro = _request("def456")
    _consume(queue, graph, otro, max_attempts=3, claude_runner=runner, claude_store=store)

    assert runner.calls == ["abc123", "def456"]
    assert graph.calls == 3
    assert store.load(repo=request.repo, pr_id=request.pr_id, head_sha="abc123") is not None


def test_stdout_invalido_no_entra_al_grafo_y_el_job_se_reintenta(tmp_path: Path) -> None:
    store = ClaudeArtifactStore(root=tmp_path)
    runner = _Runner(stdout="esto no es una lista")
    graph = _Graph()
    queue = InMemoryReviewQueue()
    request = _request()

    _consume(queue, graph, request, max_attempts=3, claude_runner=runner, claude_store=store)

    assert graph.calls == 0
    assert store.load(repo=request.repo, pr_id=request.pr_id, head_sha=request.head_sha) is None
    retried = queue.dequeue()
    assert retried is not None
    assert retried.attempts == 1


def test_un_proceso_caido_no_invoca_el_grafo(tmp_path: Path) -> None:
    store = ClaudeArtifactStore(root=tmp_path)
    runner = _Runner(error=ClaudeProcessCrashed("murio"))
    graph = _Graph()
    queue = InMemoryReviewQueue()

    _consume(queue, graph, _request(), max_attempts=3, claude_runner=runner, claude_store=store)

    assert graph.calls == 0
    assert runner.calls == ["abc123"]
    assert queue.dequeue() is not None


def test_el_cupo_no_reintenta_ni_arranca_el_grafo(tmp_path: Path) -> None:
    store = ClaudeArtifactStore(root=tmp_path)
    runner = _Runner(error=ClaudeQuotaExceeded("cupo"))
    graph = _Graph()
    queue = InMemoryReviewQueue()

    _consume(queue, graph, _request(), max_attempts=3, claude_runner=runner, claude_store=store)

    assert graph.calls == 0
    assert queue.dequeue() is None
    assert queue.dead_letters() == []
    assert [item.head_sha for item in queue.parked()] == ["abc123"]


def test_el_cliente_http_traduce_el_cupo_y_devuelve_stdout_sin_validar() -> None:
    def cupo(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(429, text="quota")

    runner = HttpClaudeRunner(
        url="http://claude-runner",
        token="t",
        timeout_s=1,
        client=httpx.Client(transport=httpx.MockTransport(cupo)),
    )
    with pytest.raises(ClaudeQuotaExceeded):
        runner.run(repo="r", pr_id=1, head_sha="abc")

    def ok(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"stdout": "no es json"})

    runner = HttpClaudeRunner(
        url="http://claude-runner",
        token="",
        timeout_s=1,
        client=httpx.Client(transport=httpx.MockTransport(ok)),
    )
    assert runner.run(repo="r", pr_id=1, head_sha="abc") == "no es json"


def test_el_checkout_no_es_el_clon_bare(tmp_path: Path) -> None:
    bare = Path(tempfile.mkdtemp(prefix=BARE_CLONE_PREFIX))
    try:
        with pytest.raises(ValueError):
            checkout_directory(bare, repo="cqm/demo", head_sha="abc")
    finally:
        bare.rmdir()

    path = checkout_directory(tmp_path / "claude-checkout", repo="cqm/demo", head_sha="abc")
    assert BARE_CLONE_PREFIX not in path.parts
    assert path.is_relative_to(tmp_path / "claude-checkout")


def test_quitar_remotos_borra_la_url_con_token(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    token = "secreto-de-clonado"
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            f"https://x-token-auth:{token}@bitbucket.org/cqm/demo.git",
        ],
        check=True,
        capture_output=True,
    )
    drop_push_remotes(repo)
    listed = subprocess.run(
        ["git", "-C", str(repo), "remote"],
        check=True,
        capture_output=True,
        text=True,
    )
    config = (repo / ".git" / "config").read_text(encoding="utf-8")
    assert listed.stdout.strip() == ""
    assert token not in config


def test_el_compose_no_pasa_el_token_de_publicacion_al_runner() -> None:
    services = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"]
    service = services["claude-runner"]
    assert service.get("profiles") == ["claude"]
    assert "env_file" not in service
    env = service["environment"]
    for secreto in (
        "GITHUB_TOKEN",
        "TWG_BBC_TOKEN",
        "TWG_TOKEN",
        "TEAMS_BOT_TOKEN",
        "TEAMS_BOT_SHARED_SECRET",
        "LLM_API_KEY",
        "REVIEWS_DECISION_TOKEN",
    ):
        assert secreto not in env, secreto
    assert "CLAUDE_GIT_TOKEN" in env
    assert "CLAUDE_RUNNER_TOKEN" in env
    assert "CLAUDE_MAX_TURNS" in env
    volumenes = [str(item) for item in service["volumes"]]
    assert any("claude-checkout" in item for item in volumenes)
    sandbox = [str(item) for item in services["sandbox-runner"].get("volumes", [])]
    assert not any("claude-checkout" in item or "read-ws" in item for item in sandbox)
    assert not any("claude-checkout" in item for item in volumenes if "docker.sock" in item)
    assert services["worker"]["environment"]["CLAUDE_GIT_TOKEN"] == ""
    sin_perfil = [name for name, item in services.items() if not item.get("profiles")]
    assert "claude-runner" not in sin_perfil


def test_el_worker_no_exige_la_credencial_de_clonado() -> None:
    settings = Settings(_env_file=None, environment="production", review_engine="claude")
    worker = secrets_for(settings, "worker")
    assert "CLAUDE_RUNNER_URL" in worker
    assert "CLAUDE_RUNNER_TOKEN" in worker
    assert "CLAUDE_GIT_TOKEN" not in worker
    runner = secrets_for(settings, "claude-runner")
    assert "CLAUDE_GIT_TOKEN" in runner
    assert "CLAUDE_RUNNER_TOKEN" in runner
    assert "TWG_BBC_TOKEN" not in runner
    assert "GITHUB_TOKEN" not in runner
