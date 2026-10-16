from __future__ import annotations

import stat
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_review.ports.sandbox import VerifyRequest
from agent_review.sandbox_runner.console import ConsoleBuffer, ConsoleNotFound
from agent_review.sandbox_runner.runtime import (
    ContainerOutcome,
    ContainerSpec,
    ContainerTimeout,
    DockerCliRuntime,
    LineSink,
)
from agent_review.sandbox_runner.verifier import FixedProfileSource, Verifier

# --- Fakes locales: `tests/conftest.py` lo comparten otros streams, asi que las
# --- fixtures y utilidades de esta zona viven en este modulo (aislamiento).

_PROFILE = {
    "repo": "cqm/x",
    "ci_image": "registry.test/x/ci:latest",
    "ci_image_digest": "sha256:deadbeef",
    "setup": ["bin/console cache:clear -n"],
    "checks": {"typecheck": "make stan"},
    "verify": ["typecheck"],
    "sandbox": {"timeout_s": 30},
}

_REQUEST = VerifyRequest(
    run_key="cqm/x:430:abc:7:f1:1",
    repo="cqm/x",
    head_sha="abc",
    patch="diff --git a/src/Foo.php b/src/Foo.php\n",
    recipes=["typecheck"],
    targets=["tests/Foo/BarTest.php"],
)


class _Workspace:
    def __init__(self) -> None:
        self.prepared: list[str] = []
        self.cleaned: list[str] = []

    def prepare(self, *, repo: str, head_sha: str, patch: str) -> str:
        self.prepared.append(head_sha)
        return "/tmp/ws"

    def cleanup(self, workspace: str) -> None:
        self.cleaned.append(workspace)


class _Runtime:
    """Fake de Docker: emite lineas por `on_line` y devuelve un informe fijo."""

    def __init__(self, *, timeout: bool = False) -> None:
        self.timeout = timeout
        self.created: list[ContainerSpec] = []
        self.removed: list[str] = []
        self.emitted: list[tuple[str, str]] = []
        self._counter = 0

    def pull(self, reference: str, *, registry_user: str, registry_token: str) -> None:
        pass

    def create(self, spec: ContainerSpec) -> str:
        self.created.append(spec)
        self._counter += 1
        return f"c{self._counter}"

    def start(
        self, container_id: str, *, timeout_s: float, on_line: LineSink | None = None
    ) -> ContainerOutcome:
        if self.timeout:
            raise ContainerTimeout("se acabo el tiempo")
        if on_line is not None:
            self.emitted.append(("stdout", "ok"))
            self.emitted.append(("stderr", "boom"))
            on_line("stdout", "ok")
            on_line("stderr", "boom")
        return ContainerOutcome(exit_code=0, duration_s=0.5, stdout="ok", stderr="boom")

    def remove(self, container_id: str) -> None:
        self.removed.append(container_id)


def _verifier(runtime: _Runtime, console: ConsoleBuffer | None = None) -> Verifier:
    return Verifier(
        profile_source=FixedProfileSource(_PROFILE),
        workspace=_Workspace(),  # type: ignore[arg-type]
        runtime=runtime,  # type: ignore[arg-type]
        image_already_pulled=True,
        console=console,
    )


def _fake_docker(tmp_path: Path) -> Path:
    """Script que imita `docker start -a`: escupe una linea por flujo y sale con `0`.

    Imprime **antes** de dormir, y el retardo (para probar el timeout o el
    *streaming* en vivo) se lee de `FAKE_DOCKER_DELAY`.
    """
    script = tmp_path / "docker"
    script.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import os, sys, time",
                "print('linea-stdout', flush=True)",
                "print('linea-stderr', file=sys.stderr, flush=True)",
                "time.sleep(float(os.environ.get('FAKE_DOCKER_DELAY', '0')))",
                "sys.exit(0)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


# ---------------------------------------------------------------------------
# OP19/OP23: el `VerifyReport` no cambia con `on_line`; timeout y destruccion igual.
# ---------------------------------------------------------------------------


def test_on_line_no_altera_el_verify_report() -> None:
    sin_consola = _Runtime()
    con_consola = _Runtime()
    console = ConsoleBuffer()

    report_sin = _verifier(sin_consola).verify(_REQUEST)
    report_con = _verifier(con_consola, console).verify(_REQUEST)

    assert report_con == report_sin, "la consola no debe cambiar el informe"


def test_on_line_emite_cada_linea_y_cierra_el_buffer() -> None:
    console = ConsoleBuffer()
    _verifier(_Runtime(), console).verify(_REQUEST)

    lines, closed = console.wait_for_lines(_REQUEST.run_key, 0, timeout_s=0.1)
    # `setup` + un check: dos comandos, dos lineas por comando (stdout y stderr).
    assert [(line.stream, line.text) for line in lines] == [
        ("stdout", "ok"),
        ("stderr", "boom"),
        ("stdout", "ok"),
        ("stderr", "boom"),
    ]
    assert closed, "el buffer se cierra al terminar la verificacion"


def test_el_timeout_y_la_destruccion_no_cambian_con_on_line() -> None:
    console = ConsoleBuffer()
    runtime = _Runtime(timeout=True)
    report = _verifier(runtime, console).verify(_REQUEST)

    assert report.results[0].exit_code == 124
    assert len(runtime.removed) == len(runtime.created), "se destruye pese al timeout"
    _, closed = console.wait_for_lines(_REQUEST.run_key, 0, timeout_s=0.1)
    assert closed


def test_una_entrega_repetida_no_reemite_consola() -> None:
    console = ConsoleBuffer()
    verifier = _verifier(_Runtime(), console)
    verifier.verify(_REQUEST)

    lines_primero, closed_primero = console.wait_for_lines(_REQUEST.run_key, 0, timeout_s=0.1)
    assert closed_primero
    verifier.verify(_REQUEST)  # idempotente: no reabre la consola ni reemite
    lines_segundo, _ = console.wait_for_lines(_REQUEST.run_key, 0, timeout_s=0.1)
    assert len(lines_segundo) == len(lines_primero), "la reentrega no reemite ni duplica"


# ---------------------------------------------------------------------------
# OP20: `DockerCliRuntime` con `Popen` y un hilo lector por flujo.
# ---------------------------------------------------------------------------


def test_docker_cli_runtime_separa_stdout_y_stderr(tmp_path: Path) -> None:
    runtime = DockerCliRuntime(docker_bin=str(_fake_docker(tmp_path)))
    emitted: list[tuple[str, str]] = []
    outcome = runtime.start("c1", timeout_s=10.0, on_line=lambda s, t: emitted.append((s, t)))

    assert outcome.exit_code == 0
    assert outcome.stdout == "linea-stdout\n", "stdout no se mezcla con stderr"
    assert outcome.stderr == "linea-stderr\n"
    assert ("stdout", "linea-stdout") in emitted
    assert ("stderr", "linea-stderr") in emitted


def test_docker_cli_runtime_emite_lineas_antes_de_terminar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El callback corre **mientras** el proceso vive, no al final (lo esencial de 019)."""
    monkeypatch.setenv("FAKE_DOCKER_DELAY", "1")
    runtime = DockerCliRuntime(docker_bin=str(_fake_docker(tmp_path)))
    visto = threading.Event()
    resultado: list[ContainerOutcome] = []

    worker = threading.Thread(
        target=lambda: resultado.append(
            runtime.start("c1", timeout_s=10.0, on_line=lambda _s, _t: visto.set())
        )
    )
    worker.start()
    # Con el proceso aun durmiendo, la linea ya ha llegado al callback.
    assert visto.wait(timeout=5.0), "la linea se emitio al vuelo, no al terminar"
    assert worker.is_alive(), "el proceso sigue vivo cuando llego la linea"
    worker.join(timeout=10.0)
    assert resultado[0].exit_code == 0


def test_docker_cli_runtime_sin_on_line_sigue_acumulando(tmp_path: Path) -> None:
    runtime = DockerCliRuntime(docker_bin=str(_fake_docker(tmp_path)))
    outcome = runtime.start("c1", timeout_s=10.0)
    assert outcome.stdout == "linea-stdout\n"
    assert outcome.stderr == "linea-stderr\n"


def test_docker_cli_runtime_timeout_mata_el_proceso(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DOCKER_DELAY", "5")
    runtime = DockerCliRuntime(docker_bin=str(_fake_docker(tmp_path)))
    with pytest.raises(ContainerTimeout):
        runtime.start("c1", timeout_s=0.2)


# ---------------------------------------------------------------------------
# OP21: buffer circular en memoria, con alta/baja de suscriptores.
# ---------------------------------------------------------------------------


def test_buffer_descarta_las_lineas_mas_viejas() -> None:
    console = ConsoleBuffer(max_lines=2)
    console.open("k")
    for i in range(5):
        console.append("k", "stdout", f"l{i}")
    lines, _ = console.wait_for_lines("k", 0, timeout_s=0.1)
    assert [line.text for line in lines] == ["l3", "l4"], "el buffer es circular"


def test_sin_run_key_el_buffer_es_404() -> None:
    console = ConsoleBuffer()
    assert not console.has("ausente")
    with pytest.raises(ConsoleNotFound):
        console.wait_for_lines("ausente", 0, timeout_s=0.1)


def test_un_reinicio_pierde_el_buffer(tmp_path: Path) -> None:
    """Un proceso nuevo (buffer recien creado) no conoce al `run_key` previo."""
    antes = ConsoleBuffer()
    antes.open("k")
    antes.append("k", "stdout", "hola")
    antes.close("k")
    assert antes.has("k")

    reiniciado = ConsoleBuffer()  # un reinicio = buffer vacio
    assert not reiniciado.has("k")


def test_los_suscriptores_impiden_desalojar_un_run_terminado() -> None:
    console = ConsoleBuffer(max_runs=1)
    console.open("a")
    console.close("a")
    assert console.subscribe("a")
    console.open("b")  # desaloja... salvo que "a" tenga un lector vivo
    assert console.has("a"), "no se tira una consola que alguien esta leyendo"
    console.unsubscribe("a")
    console.open("c")
    assert not console.has("a"), "sin lector, la mas antigua cerrada se desaloja"


# ---------------------------------------------------------------------------
# OP22: endpoint SSE `GET /verify/{run_key}/console`.
# ---------------------------------------------------------------------------


@pytest.fixture
def runner_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    from fastapi.testclient import TestClient

    from agent_review.sandbox_runner import main as runner_main

    settings = runner_main.get_settings()
    monkeypatch.setattr(settings, "sandbox_runner_token", "change-me")
    runner_main._console = None
    yield TestClient(runner_main.app)
    runner_main._console = None


def test_endpoint_console_emite_lineas_y_cierra(runner_client: object) -> None:
    from agent_review.sandbox_runner import main as runner_main

    console = runner_main.build_console()
    console.open(_REQUEST.run_key)
    console.append(_REQUEST.run_key, "stdout", "hola")
    console.close(_REQUEST.run_key)

    with runner_client.stream("GET", f"/verify/{_REQUEST.run_key}/console") as response:  # type: ignore[attr-defined]
        assert response.status_code == 200
        body = "".join(response.iter_text())
    assert "event: line" in body
    assert "hola" in body
    assert "event: end" in body


def test_endpoint_console_404_si_no_esta_en_el_buffer(runner_client: object) -> None:
    response = runner_client.get("/verify/desconocido/console")  # type: ignore[attr-defined]
    assert response.status_code == 404


def test_endpoint_console_exige_token(
    runner_client: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_review.sandbox_runner import main as runner_main

    monkeypatch.setattr(runner_main.get_settings(), "sandbox_runner_token", "secreto-real")
    response = runner_client.get("/verify/k/console")  # type: ignore[attr-defined]
    assert response.status_code == 401


def test_un_reinicio_del_runner_da_404_y_no_toca_el_informe(runner_client: object) -> None:
    from agent_review.sandbox_runner import main as runner_main

    runner_main.build_console().open(_REQUEST.run_key)
    # Reinicio: el proceso pierde la memoria, un buffer nuevo no conoce el run_key.
    runner_main._console = None
    assert runner_main.build_console().has(_REQUEST.run_key) is False
    response = runner_client.get(f"/verify/{_REQUEST.run_key}/console")  # type: ignore[attr-defined]
    assert response.status_code == 404
