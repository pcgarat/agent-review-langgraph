"""Proceso del CLI de Claude (025 R1–R4).

El puerto devuelve stdout. El worker lo llama antes del grafo y no lo repite
si el artefacto de ese `head_sha` ya es válido. El subproceso no hereda
credenciales. El servicio no es el clon bare. El subproceso no recibe el token de VCS.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
import pytest
import yaml

from agent_review.adapters.claude_artifact import ClaudeArtifactStore
from agent_review.adapters.claude_cli import SubprocessClaudeRunner, claude_argv, redact_text
from agent_review.adapters.claude_client import HttpClaudeRunner
from agent_review.claude_activity import ClaudeActivity, read_activity, transcript_events
from agent_review.claude_runner.checkout import (
    CheckoutError,
    _auth_url,
    checkout_directory,
    drop_push_remotes,
    prepare_checkout,
)
from agent_review.claude_runner.main import (
    ensure_bundled_skill,
    require_claude_binary,
    require_sandbox_binaries,
    write_runner_settings,
)
from agent_review.domain.models import ReviewRequest
from agent_review.errors import PermanentError, TransientError
from agent_review.ports.claude import (
    ClaudeCeiling,
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


def _agente_completo() -> dict[str, object]:
    return {
        "status": "complete",
        "findings": [
            {
                "#": 1,
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
        ],
        "pre_existing_findings": [],
    }


class _Runner:
    """Doble: cuenta llamadas y el `head_sha` de cada una."""

    def __init__(self, stdout: str = "[]", error: BaseException | None = None) -> None:
        self.calls: list[str] = []
        self.bases: list[str] = []
        self.stdout = stdout
        self.error = error

    def run(
        self,
        *,
        repo: str,
        pr_id: int,
        head_sha: str,
        base_sha: str,
        max_turns: int | None = None,
        timeout_s: float | None = None,
    ) -> str:
        del repo, pr_id, max_turns, timeout_s
        self.calls.append(head_sha)
        self.bases.append(base_sha)
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
    assert issubclass(ClaudeCeiling, PermanentError)
    assert not issubclass(ClaudeQuotaExceeded, TransientError)
    assert not issubclass(ClaudeCeiling, TransientError)


def test_el_argv_pide_la_skill_y_no_abre_un_shell_libre() -> None:
    argv = claude_argv([sys.executable, "claude"], max_turns=7, base_sha="abc123")
    assert argv[argv.index("--max-turns") + 1] == "7"
    assert argv[argv.index("--setting-sources") + 1] == "user"
    allowed = set(argv[argv.index("--allowedTools") + 1].split(","))
    denied = set(argv[argv.index("--disallowedTools") + 1].split(","))
    assert {"Read", "Grep", "Glob", "Bash"} <= allowed
    assert "Bash" not in denied
    assert {"Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch"} <= denied
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[-1].startswith("/ce-code-review mode:agent base:abc123")
    assert "peer externo" in argv[-1]
    assert "Revisa el árbol de trabajo" not in " ".join(argv)
    with pytest.raises(ValueError, match="base_sha"):
        claude_argv([sys.executable, "claude"], max_turns=7, base_sha=" ")


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

    stdout = runner.run(repo="cqm/demo", pr_id=1, head_sha="abc", base_sha="base")

    visto = json.loads(stdout)
    assert "--max-turns" in visto["argv"]
    assert "4" in visto["argv"]
    allowed = visto["argv"][visto["argv"].index("--allowedTools") + 1].split(",")
    denied = visto["argv"][visto["argv"].index("--disallowedTools") + 1].split(",")
    assert {"Read", "Grep", "Glob", "Bash"} <= set(allowed)
    assert "Bash" not in denied
    assert {"Edit", "Write", "WebFetch", "WebSearch"} <= set(denied)
    assert visto["argv"][visto["argv"].index("--setting-sources") + 1] == "user"
    prompt = visto["argv"][-1]
    assert prompt.startswith("/ce-code-review mode:agent base:base")
    assert "peer externo" in prompt
    assert "Revisa el árbol de trabajo" not in prompt
    assert "dontAsk" in visto["argv"]
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
    stdout = runner.run(repo="r", pr_id=1, head_sha="abc", base_sha="base")
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
    tope = tmp_path / "tope.py"
    tope.write_text(
        'import sys\nprint(\'{"type":"result","subtype":"error_max_turns"}\')\nsys.exit(1)\n',
        encoding="utf-8",
    )

    def lanzar(script: Path, timeout: float) -> None:
        SubprocessClaudeRunner(
            argv_prefix=[sys.executable, str(script)],
            max_turns=1,
            timeout_s=timeout,
            environ={"PATH": "/usr/bin"},
        ).run(repo="r", pr_id=1, head_sha="abc", base_sha="base")

    with pytest.raises(ClaudeProcessCrashed):
        lanzar(caido, 5)
    with pytest.raises(ClaudeQuotaExceeded):
        lanzar(cupo, 5)
    with pytest.raises(ClaudeCeiling):
        lanzar(lento, 0.2)
    with pytest.raises(ClaudeCeiling, match="tope de turnos"):
        lanzar(tope, 5)


def test_un_informe_que_cita_el_marcador_no_es_un_techo(tmp_path: Path) -> None:
    """El techo se lee del `subtype`, no de la prosa.

    Una review de este repo cita `error_max_turns` al describir este mismo
    adaptador. Con la comprobación por substring, el CLI terminaba con
    `subtype: success` y el job se parkeaba como `ceiling`.
    """
    informe = "claude_cli.py:241 _hit_turn_ceiling busca 'error_max_turns' en el texto"
    script = tmp_path / "cita.py"
    script.write_text(
        "import json\n"
        f"hallazgos = {json.dumps(informe)}\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success',"
        " 'is_error': False, 'result': hallazgos}))\n",
        encoding="utf-8",
    )
    stdout = SubprocessClaudeRunner(
        argv_prefix=[sys.executable, str(script)],
        max_turns=200,
        timeout_s=5,
        environ={"PATH": "/usr/bin"},
    ).run(repo="r", pr_id=1, head_sha="abc", base_sha="base")
    assert stdout == informe


def test_el_techo_se_detecta_aunque_el_cli_salga_con_cero(tmp_path: Path) -> None:
    """El `subtype` basta: no se depende del código de salida ni de stderr."""
    script = tmp_path / "techo_cero.py"
    script.write_text(
        "import json\n"
        "print(json.dumps({'type': 'result', 'subtype': 'error_max_turns',"
        " 'is_error': True}))\n",
        encoding="utf-8",
    )
    with pytest.raises(ClaudeCeiling, match="tope de turnos"):
        SubprocessClaudeRunner(
            argv_prefix=[sys.executable, str(script)],
            max_turns=1,
            timeout_s=5,
            environ={"PATH": "/usr/bin"},
        ).run(repo="r", pr_id=1, head_sha="abc", base_sha="base")


def test_un_desenlace_estructurado_manda_sobre_la_prosa_del_cupo(tmp_path: Path) -> None:
    """Un fallo con `subtype` de error no se reetiqueta como cupo por citar
    'quota' en el texto: eso publicaría un cupo agotado que no existe."""
    script = tmp_path / "cita_cupo.py"
    script.write_text(
        "import sys, json\n"
        "print(json.dumps({'type': 'result', 'subtype': 'error_invalid_request',"
        " 'is_error': True}))\n"
        "print('el informe menciona quota y rate limit sin agotarlos', file=sys.stderr)\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    with pytest.raises(ClaudeProcessCrashed):
        SubprocessClaudeRunner(
            argv_prefix=[sys.executable, str(script)],
            max_turns=1,
            timeout_s=5,
            environ={"PATH": "/usr/bin"},
        ).run(repo="r", pr_id=1, head_sha="abc", base_sha="base")


def test_mientras_corre_el_cli_la_review_esta_en_vuelo(tmp_path: Path) -> None:
    class _Progress:
        def __init__(self) -> None:
            self.events: list[tuple[str, str]] = []
            self.open: set[str] = set()

        def start_node(self, review_key: str, node: str, *, at: object = None) -> None:
            del review_key, at
            self.events.append(("start", node))
            self.open.add(node)

        def finish_node(self, review_key: str, node: str) -> None:
            del review_key
            self.events.append(("finish", node))
            self.open.discard(node)

        def clear(self, review_key: str) -> None:
            del review_key
            self.events.append(("clear", ""))
            self.open.clear()

    class _Watch(_Runner):
        def __init__(self, progress: _Progress, stdout: str) -> None:
            super().__init__(stdout=stdout)
            self.progress = progress

        def run(
            self,
            *,
            repo: str,
            pr_id: int,
            head_sha: str,
            base_sha: str,
            max_turns: int | None = None,
            timeout_s: float | None = None,
        ) -> str:
            assert "claude" in self.progress.open
            return super().run(
                repo=repo,
                pr_id=pr_id,
                head_sha=head_sha,
                base_sha=base_sha,
                max_turns=max_turns,
                timeout_s=timeout_s,
            )

    progress = _Progress()
    store = ClaudeArtifactStore(root=tmp_path)
    runner = _Watch(progress, json.dumps(_agente_completo()))
    request = _request()

    _consume(
        InMemoryReviewQueue(),
        _Graph(),
        request,
        max_attempts=3,
        claude_runner=runner,
        claude_store=store,
        progress=progress,
    )

    assert progress.events.index(("start", "claude")) < progress.events.index(("finish", "claude"))
    assert "claude" not in progress.open


def test_con_artefacto_valido_no_se_repite_y_el_grafo_arranca(tmp_path: Path) -> None:
    store = ClaudeArtifactStore(root=tmp_path)
    runner = _Runner(stdout=json.dumps(_agente_completo()))
    graph = _Graph()
    queue = InMemoryReviewQueue()
    request = _request().model_copy(update={"base_sha": "base123"})

    _consume(queue, graph, request, max_attempts=3, claude_runner=runner, claude_store=store)
    _consume(queue, graph, request, max_attempts=3, claude_runner=runner, claude_store=store)
    otro = _request("def456")
    _consume(queue, graph, otro, max_attempts=3, claude_runner=runner, claude_store=store)

    assert runner.calls == ["abc123", "def456"]
    assert runner.bases == ["base123", ""]
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


def test_stdout_invalido_avisa_una_vez_y_no_en_el_reintento(tmp_path: Path) -> None:
    class _Chat:
        def __init__(self) -> None:
            self.avisos: list[dict[str, object]] = []

        def notify(self, **kwargs: object) -> None:
            self.avisos.append(kwargs)

    store = ClaudeArtifactStore(root=tmp_path)
    runner = _Runner(stdout="esto no es una lista")
    chat = _Chat()
    queue = InMemoryReviewQueue()
    request = _request()

    _consume(
        queue,
        _Graph(),
        request,
        max_attempts=3,
        claude_runner=runner,
        claude_store=store,
        chat=chat,
    )
    retried = queue.dequeue()
    assert retried is not None
    _consume(
        queue,
        _Graph(),
        retried,
        max_attempts=3,
        claude_runner=runner,
        claude_store=store,
        chat=chat,
    )

    assert len(chat.avisos) == 1
    assert "No se ha publicado" in str(chat.avisos[0]["body"])


def test_el_descarte_guarda_el_log_de_los_intentos(tmp_path: Path) -> None:
    store = ClaudeArtifactStore(root=tmp_path)
    runner = _Runner(error=ClaudeProcessCrashed("no se encontro el ejecutable de claude"))
    queue = InMemoryReviewQueue()
    request = _request()

    _consume(queue, _Graph(), request, max_attempts=2, claude_runner=runner, claude_store=store)
    retried = queue.dequeue()
    assert retried is not None
    _consume(queue, _Graph(), retried, max_attempts=2, claude_runner=runner, claude_store=store)

    dead = queue.dead_letters()
    assert len(dead) == 1
    error = str(dead[0]["error"])
    assert "intento 1" in error
    assert "intento 2" in error
    assert "no se encontro el ejecutable de claude" in error
    assert queue.failure_log(request.review_key) == ""


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
        runner.run(repo="r", pr_id=1, head_sha="abc", base_sha="base")

    def ok(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["base_sha"] == "base1"
        assert body["head_sha"] == "abc"
        return httpx.Response(200, json={"stdout": "no es json"})

    runner = HttpClaudeRunner(
        url="http://claude-runner",
        token="",
        timeout_s=1,
        client=httpx.Client(transport=httpx.MockTransport(ok)),
    )
    assert runner.run(repo="r", pr_id=1, head_sha="abc", base_sha="base1") == "no es json"

    def roto(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, text="no es json")

    runner = HttpClaudeRunner(
        url="http://claude-runner",
        token="",
        timeout_s=1,
        client=httpx.Client(transport=httpx.MockTransport(roto)),
    )
    with pytest.raises(ClaudeProcessCrashed, match="JSON"):
        runner.run(repo="r", pr_id=1, head_sha="abc", base_sha="base")

    def caido(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(502, json={"detail": "Invalid bearer token"})

    runner = HttpClaudeRunner(
        url="http://claude-runner",
        token="",
        timeout_s=1,
        client=httpx.Client(transport=httpx.MockTransport(caido)),
    )
    with pytest.raises(ClaudeProcessCrashed, match="Invalid bearer token"):
        runner.run(repo="r", pr_id=1, head_sha="abc", base_sha="base")

    def caido(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(502, json={"detail": "no se encontro el ejecutable de claude"})

    runner = HttpClaudeRunner(
        url="http://claude-runner",
        token="",
        timeout_s=1,
        client=httpx.Client(transport=httpx.MockTransport(caido)),
    )
    with pytest.raises(ClaudeProcessCrashed, match="no se encontro el ejecutable de claude"):
        runner.run(repo="r", pr_id=1, head_sha="abc", base_sha="base")

    def techo(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(504, json={"detail": "claude llego al techo"})

    runner = HttpClaudeRunner(
        url="http://claude-runner",
        token="",
        timeout_s=1,
        client=httpx.Client(transport=httpx.MockTransport(techo)),
    )
    with pytest.raises(ClaudeCeiling, match="techo"):
        runner.run(repo="r", pr_id=1, head_sha="abc", base_sha="base")


def test_la_url_de_clonado_sigue_al_proveedor() -> None:
    assert _auth_url("acme/app", "ghp", provider="github") == (
        "https://x-access-token:ghp@github.com/acme/app.git"
    )
    assert _auth_url("acme/app", "tok") == "https://x-token-auth:tok@bitbucket.org/acme/app.git"


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


def _git_falso(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "git"
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)
    return script


def test_prepare_checkout_redacta_el_token_si_el_fetch_falla(tmp_path: Path) -> None:
    url_file = tmp_path / "url"
    git_bin = _git_falso(
        tmp_path,
        "#!/bin/sh\n"
        'if [ "$1" = "-C" ]; then shift 2; fi\n'
        f'if [ "$1" = "remote" ] && [ "$2" = "add" ]; then\n'
        f'  printf "%s" "$4" > "{url_file}"\n'
        "  exit 0\n"
        "fi\n"
        f'if [ "$1" = "fetch" ]; then echo "fatal: $(cat "{url_file}")" >&2; exit 1; fi\n'
        "exit 0\n",
    )
    token = "secreto-de-clonado"
    with pytest.raises(CheckoutError) as exc:
        prepare_checkout(
            tmp_path / "arbol",
            repo="cqm/demo",
            head_sha="abc",
            base_sha="def",
            git_token=token,
            git_bin=str(git_bin),
            timeout_s=5,
        )
    assert token not in str(exc.value)
    assert token in url_file.read_text(encoding="utf-8")


def test_si_el_remoto_no_se_quita_el_checkout_no_queda_listo(tmp_path: Path) -> None:
    git_bin = _git_falso(
        tmp_path,
        "#!/bin/sh\n"
        'if [ "$1" = "-C" ]; then shift 2; fi\n'
        'if [ "$1" = "remote" ] && [ "$2" = "remove" ]; then exit 1; fi\n'
        'if [ "$1" = "remote" ]; then echo origin; exit 0; fi\n'
        "exit 0\n",
    )
    with pytest.raises(CheckoutError, match="remoto"):
        prepare_checkout(
            tmp_path / "arbol",
            repo="cqm/demo",
            head_sha="abc",
            base_sha="def",
            git_token="secreto-de-clonado",
            git_bin=str(git_bin),
            timeout_s=5,
        )


def test_prepare_checkout_deja_el_arbol_en_el_sha_sin_remotos(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@b.c"}
    env["GIT_COMMITTER_NAME"] = "a"
    env["GIT_COMMITTER_EMAIL"] = "a@b.c"
    subprocess.run(["git", "init", "-q", str(origin)], check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "-C", str(origin), "config", "uploadpack.allowReachableSHA1InWant", "true"],
        check=True,
        capture_output=True,
        env=env,
    )
    (origin / "f.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(origin), "add", "f.txt"],
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(origin), "commit", "-q", "-m", "uno"],
        check=True,
        capture_output=True,
        env=env,
    )
    base = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()
    (origin / "f.txt").write_text("head\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(origin), "commit", "-q", "-am", "dos"],
        check=True,
        capture_output=True,
        env=env,
    )
    sha = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()
    dest = tmp_path / "checkout"
    token = "secreto-de-clonado"
    prepare_checkout(
        dest,
        repo="cqm/demo",
        head_sha=sha,
        base_sha=base,
        git_token=token,
        remote_url=origin.as_uri(),
        timeout_s=20,
    )
    assert (dest / "f.txt").read_text(encoding="utf-8") == "head\n"
    head_ref = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "review"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    base_ref = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "base"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    diff = subprocess.run(
        ["git", "-C", str(dest), "diff", "base", "--", "f.txt"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert head_ref == sha
    assert base_ref == base
    assert "head" in diff
    listed = subprocess.run(
        ["git", "-C", str(dest), "remote"],
        check=True,
        capture_output=True,
        text=True,
    )
    config = (dest / ".git" / "config").read_text(encoding="utf-8")
    symlinks = subprocess.run(
        ["git", "-C", str(dest), "config", "--get", "core.symlinks"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert listed.stdout.strip() == ""
    assert token not in config
    assert symlinks.stdout.strip() == "false"
    prepare_checkout(
        dest,
        repo="cqm/demo",
        head_sha=sha,
        base_sha=base,
        git_token=token,
        remote_url=origin.as_uri(),
        timeout_s=20,
    )
    again = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert again.stdout.strip() == "review"
    assert (dest / "f.txt").read_text(encoding="utf-8") == "head\n"


def test_sin_base_sha_el_checkout_no_arranca(tmp_path: Path) -> None:
    dest = tmp_path / "checkout"
    with pytest.raises(CheckoutError, match="base_sha"):
        prepare_checkout(
            dest,
            repo="cqm/demo",
            head_sha="abc",
            base_sha="  ",
            git_token="secreto-de-clonado",
            timeout_s=5,
        )
    assert not (dest / ".git").exists()


def test_si_la_base_no_se_trae_el_token_no_queda(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@b.c"}
    env["GIT_COMMITTER_NAME"] = "a"
    env["GIT_COMMITTER_EMAIL"] = "a@b.c"
    subprocess.run(["git", "init", "-q", str(origin)], check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "-C", str(origin), "config", "uploadpack.allowReachableSHA1InWant", "true"],
        check=True,
        capture_output=True,
        env=env,
    )
    (origin / "f.txt").write_text("hola\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(origin), "add", "f.txt"],
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(origin), "commit", "-q", "-m", "uno"],
        check=True,
        capture_output=True,
        env=env,
    )
    sha = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()
    dest = tmp_path / "checkout"
    token = "secreto-de-clonado"
    with pytest.raises(CheckoutError, match="base"):
        prepare_checkout(
            dest,
            repo="cqm/demo",
            head_sha=sha,
            base_sha="f" * 40,
            git_token=token,
            remote_url=origin.as_uri(),
            timeout_s=20,
        )
    config = (dest / ".git" / "config").read_text(encoding="utf-8")
    assert token not in config
    listed = subprocess.run(
        ["git", "-C", str(dest), "remote"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert listed.stdout.strip() == ""


def test_el_compose_no_pasa_el_token_de_publicacion_al_runner() -> None:
    services = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"]
    service = services["claude-runner"]
    assert service.get("profiles") == ["claude"]
    assert "env_file" not in service
    env = service["environment"]
    for secreto in (
        "TWG_TOKEN",
        "TEAMS_BOT_TOKEN",
        "TEAMS_BOT_SHARED_SECRET",
        "LLM_API_KEY",
        "REVIEWS_DECISION_TOKEN",
    ):
        assert secreto not in env, secreto
    assert "GITHUB_TOKEN" in env
    assert "TWG_BBC_TOKEN" in env
    assert "VCS_PROVIDER" in env
    assert "CLAUDE_GIT_TOKEN" in env
    assert "CLAUDE_RUNNER_TOKEN" in env
    assert "CLAUDE_MAX_TURNS" in env
    volumenes = [str(item) for item in service["volumes"]]
    assert any("claude-checkout" in item for item in volumenes)
    sandbox = [str(item) for item in services["sandbox-runner"].get("volumes", [])]
    assert not any("claude-checkout" in item or "read-ws" in item for item in sandbox)
    assert not any("claude-checkout" in item for item in volumenes if "docker.sock" in item)
    assert services["worker"]["environment"]["CLAUDE_GIT_TOKEN"] == ""
    assert set(service["security_opt"]) == {"apparmor=unconfined", "seccomp=unconfined"}
    sin_perfil = [name for name, item in services.items() if not item.get("profiles")]
    assert "claude-runner" not in sin_perfil


def test_el_worker_no_exige_la_credencial_de_clonado() -> None:
    settings = Settings(_env_file=None, environment="production", review_engine="claude")
    worker = secrets_for(settings, "worker")
    assert "CLAUDE_RUNNER_URL" in worker
    assert "CLAUDE_RUNNER_TOKEN" in worker
    assert "CLAUDE_GIT_TOKEN" not in worker
    runner = secrets_for(settings, "claude-runner")
    assert "CLAUDE_GIT_TOKEN" not in runner
    assert "TWG_BBC_TOKEN" in runner
    assert "CLAUDE_RUNNER_TOKEN" in runner
    assert "GITHUB_TOKEN" not in runner
    con_github = Settings(
        _env_file=None,
        environment="production",
        review_engine="claude",
        vcs_provider="github",
        github_token="ghp-real",
    )
    runner_github = secrets_for(con_github, "claude-runner")
    assert "GITHUB_TOKEN" not in runner_github
    assert "CLAUDE_GIT_TOKEN" not in runner_github


def test_solo_la_imagen_del_runner_instala_el_cli() -> None:
    services = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"]
    build = services["claude-runner"]["build"]
    assert build["target"] == "claude-runner"
    assert "INSTALL_CLAUDE" not in build.get("args", {})
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM app AS claude-runner" in dockerfile
    assert "INSTALL_CLAUDE" not in dockerfile
    for name, service in services.items():
        if name == "claude-runner":
            continue
        other = service.get("build")
        target = other.get("target") if isinstance(other, dict) else None
        assert target != "claude-runner"


def test_los_techos_por_defecto_cubren_el_fan_out() -> None:
    assert Settings.model_fields["claude_max_turns"].default == 200
    assert Settings.model_fields["claude_timeout_s"].default == 3600.0
    compose = _COMPOSE.read_text(encoding="utf-8")
    assert "CLAUDE_MAX_TURNS:-200" in compose
    assert "CLAUDE_TIMEOUT_S:-3600" in compose


def test_el_cliente_manda_el_techo_publicado_y_calla_si_no_hay() -> None:
    seen: dict[str, object] = {}

    def captura(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"stdout": "[]"})

    runner = HttpClaudeRunner(
        url="http://claude-runner",
        token="",
        timeout_s=3600,
        client=httpx.Client(transport=httpx.MockTransport(captura)),
    )
    runner.run(repo="r", pr_id=1, head_sha="abc", base_sha="base", max_turns=400, timeout_s=7200)
    assert seen["body"] == {
        "repo": "r",
        "pr_id": 1,
        "head_sha": "abc",
        "base_sha": "base",
        "max_turns": 400,
        "timeout_s": 7200,
    }
    runner.run(repo="r", pr_id=1, head_sha="abc", base_sha="base")
    assert "max_turns" not in seen["body"]
    assert "timeout_s" not in seen["body"]


def test_sin_cuerpo_el_runner_conserva_su_entorno() -> None:
    from agent_review.claude_runner.main import resolve_ceilings

    settings = Settings(_env_file=None, claude_max_turns=200, claude_timeout_s=3600)
    assert resolve_ceilings(settings, max_turns=None, timeout_s=None) == (200, 3600.0)
    assert resolve_ceilings(settings, max_turns=400, timeout_s=7200) == (400, 7200.0)


def test_el_settings_del_runner_exige_el_sandbox(tmp_path: Path) -> None:
    workspace = tmp_path / "checkouts"
    config = tmp_path / "config"
    path = write_runner_settings(config, workspace_root=workspace)
    data = json.loads(path.read_text(encoding="utf-8"))
    sandbox = data["sandbox"]
    assert sandbox["enabled"] is True
    assert sandbox["failIfUnavailable"] is True
    assert sandbox["allowUnsandboxedCommands"] is False
    assert sandbox["enableWeakerNestedSandbox"] is True
    assert sandbox["network"]["allowedDomains"] == ["api.anthropic.com"]
    assert sandbox["network"]["strictAllowlist"] is True
    assert "bitbucket.org" not in sandbox["network"]["allowedDomains"]
    assert "github.com" not in sandbox["network"]["allowedDomains"]
    assert "/tmp" in sandbox["filesystem"]["allowWrite"]
    assert str(workspace.resolve()) in sandbox["filesystem"]["denyWrite"]
    # El entorno de arranque del runner es un secreto: la jaula no lo lee.
    assert "/proc/1/environ" in sandbox["filesystem"]["denyRead"]


def test_el_hijo_no_corre_como_root(tmp_path: Path) -> None:
    """El vector del P0: siendo root, `/proc/1/environ` deja los tokens.

    Un `Read` del modelo no basta con denegarlo por el argv: el CLI tiene que
    ejecutarse con un uid que el kernel no deje leer ese fichero.
    """
    argv = tmp_path / "quien.py"
    argv.write_text("import os\nprint('uid=' + str(os.getuid()))\n", encoding="utf-8")
    if os.geteuid() != 0:
        pytest.skip("solo tiene sentido como root: sin privilegios ya no aplica")
    stdout = SubprocessClaudeRunner(
        argv_prefix=[sys.executable, str(argv)],
        max_turns=1,
        timeout_s=10,
        environ={"PATH": "/usr/bin"},
        user="nobody",
    ).run(repo="r", pr_id=1, head_sha="abc", base_sha="base")
    assert "uid=0" not in stdout, "el hijo corrio como root"


def test_sin_root_el_hijo_no_baja_privilegios(monkeypatch: pytest.MonkeyPatch) -> None:
    """En un portátil o en un test no hay a quién bajar: no se inventa el drop."""
    from agent_review.adapters import claude_cli

    monkeypatch.setattr(claude_cli.os, "geteuid", lambda: 1000)
    assert claude_cli._demote() is None


def test_sin_usuario_el_runner_no_arranca(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fallar al arrancar, no en la primera review con el CLI ya como root."""
    from agent_review.claude_runner import main as runner_main

    monkeypatch.setattr(runner_main.os, "geteuid", lambda: 0)

    def sin_usuario(name: str) -> object:
        raise KeyError(name)

    monkeypatch.setattr(runner_main.pwd, "getpwnam", sin_usuario)
    with pytest.raises(RuntimeError, match="no existe"):
        runner_main.require_child_user()


def test_sin_bwrap_el_proceso_no_arranca(monkeypatch: pytest.MonkeyPatch) -> None:
    def which(name: str) -> str | None:
        if name == "bwrap":
            return None
        return "/usr/bin/" + name

    monkeypatch.setattr("agent_review.claude_runner.main.shutil.which", which)
    with pytest.raises(RuntimeError, match="bwrap"):
        require_sandbox_binaries()


def test_la_imagen_trae_el_sandbox_y_la_skill() -> None:
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "bubblewrap" in dockerfile
    assert "socat" in dockerfile
    skill = _ROOT / "image" / "claude" / "skills" / "ce-code-review" / "SKILL.md"
    assert skill.is_file()
    assert "SKILL.md" in dockerfile


def test_el_runner_vuelca_la_skill_si_falta(tmp_path: Path) -> None:
    source = tmp_path / "origen" / "ce-code-review"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("name: ce-code-review\n", encoding="utf-8")
    config = tmp_path / "config"
    ensure_bundled_skill(config, source)
    assert (config / "skills" / "ce-code-review" / "SKILL.md").is_file()
    (source / "SKILL.md").write_text("cambiada\n", encoding="utf-8")
    ensure_bundled_skill(config, source)
    assert (config / "skills" / "ce-code-review" / "SKILL.md").read_text(encoding="utf-8") == (
        "name: ce-code-review\n"
    )


def test_el_runner_no_arranca_sin_el_ejecutable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_review.claude_runner.main.shutil.which", lambda _binary: None)
    with pytest.raises(RuntimeError, match="claude-code"):
        require_claude_binary("claude")


def test_la_transcripcion_guarda_la_linea_entera_sin_el_secreto(tmp_path: Path) -> None:
    secret = "secreto-de-clonado-largo"
    sha = "a" * 40
    session = tmp_path / "session.json"
    activity = ClaudeActivity(session, repo="acme/app", pr_id=3, head_sha=sha)
    activity.start()
    activity.append(redact_text("x" * 800 + secret, [secret]))
    activity.finish()

    body = read_activity(session, repo="acme/app", pr_id=3, head_sha=sha)
    assert secret not in str(body["text"])
    assert "x" * 800 in str(body["text"])
    otro = read_activity(session, repo="otra/app", pr_id=3, head_sha=sha)
    assert otro["text"] == ""
    assert otro["started_at"] is None


def test_el_sse_recibe_una_linea_antes_de_cerrar(tmp_path: Path) -> None:
    session = tmp_path / "session.json"
    sha = "b" * 40
    activity = ClaudeActivity(session, repo="acme/app", pr_id=3, head_sha=sha)
    activity.start()
    steps = {"n": 0}

    def dormir(_seconds: float) -> None:
        if steps["n"] == 0:
            activity.append("linea-a-mitad " + ("y" * 700))
        elif steps["n"] == 1:
            activity.finish()
        steps["n"] += 1
        if steps["n"] > 4:
            raise AssertionError("el stream no cerro")

    frames = list(transcript_events(session, repo="acme/app", pr_id=3, head_sha=sha, sleep=dormir))
    assert any("linea-a-mitad" in frame and "y" * 700 in frame for frame in frames)
    assert frames[-1].startswith("event: end")


def test_el_evento_stream_json_llega_entero(tmp_path: Path) -> None:
    largo = "z" * 600
    script = tmp_path / "claude.py"
    event = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": largo}]},
    }
    script.write_text(f"import json\nprint(json.dumps({event!r}))\n", encoding="utf-8")
    seen: list[str] = []
    SubprocessClaudeRunner(
        argv_prefix=[sys.executable, str(script)],
        max_turns=1,
        timeout_s=5,
        environ={"PATH": "/usr/bin"},
    ).run(repo="r", pr_id=1, head_sha="abc", base_sha="base", on_line=seen.append)
    assert seen
    assert largo in seen[0]


def test_el_sse_reanuda_desde_last_event_id(tmp_path: Path) -> None:
    """Un `Last-Event-ID` reanuda sin repetir lo ya recibido.

    Sin esto, cada reconexión del `EventSource` (o una recarga de la ficha)
    reemite el transcript entero y el panel duplica cada línea.
    """
    session = tmp_path / "session.json"
    sha = "c" * 40
    activity = ClaudeActivity(session, repo="acme/app", pr_id=3, head_sha=sha)
    activity.start()
    activity.append('{"primera": 1}')
    activity.append('{"segunda": 2}')
    activity.finish()

    frames = list(
        transcript_events(session, repo="acme/app", pr_id=3, head_sha=sha, sleep=lambda _s: None)
    )
    primera = next(frame for frame in frames if "primera" in frame)
    assert "id: " in primera, "sin id el navegador no puede reanudar"
    ident = primera.split("id: ", 1)[1].split("\n", 1)[0]

    reanudado = list(
        transcript_events(
            session,
            repo="acme/app",
            pr_id=3,
            head_sha=sha,
            sleep=lambda _s: None,
            start_offset=int(ident),
        )
    )
    texto = "".join(reanudado)
    assert "primera" in texto, "el byte del id es el inicio de su propia linea"
    assert "segunda" in texto
    assert reanudado[-1].startswith("event: end")


def test_un_last_event_id_que_no_es_un_byte_empieza_del_principio() -> None:
    """Otra version del contrato no puede romper el stream: se repite entero."""
    from agent_review.web.main import _resume_offset

    assert _resume_offset(None) == 0
    assert _resume_offset("  123 ") == 123
    assert _resume_offset("no-es-un-numero") == 0
    assert _resume_offset("-5") == 0
