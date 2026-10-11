from __future__ import annotations

import pytest

from agent_review.ports.sandbox import VerifyRequest
from agent_review.sandbox_runner.recipes import (
    InvalidTarget,
    RecipeError,
    expand_check,
    expand_setup,
    validate_targets,
)
from agent_review.sandbox_runner.runtime import ContainerOutcome, ContainerSpec, ContainerTimeout
from agent_review.sandbox_runner.verifier import FixedProfileSource, RunnerError, Verifier

_PROFILE = {
    "repo": "cqm/x",
    "ci_image": "registry.test/x/ci:latest",
    "ci_image_digest": "sha256:deadbeef",
    "env": {"APP_ENV": "test"},
    "setup_network": ["composer install --no-scripts"],
    "setup": ["bin/console cache:clear -n"],
    "checks": {
        "typecheck": "make stan",
        "targeted": "bin/phpunit {targets}",
        "tests": "make test",
    },
    "verify": ["targeted", "typecheck", "tests"],
    "sandbox": {"timeout_s": 30},
}

_REQUEST = VerifyRequest(
    run_key="k:f1:1",
    repo="cqm/x",
    head_sha="abc",
    patch="diff --git a/src/Foo.php b/src/Foo.php\n",
    recipes=["targeted", "typecheck"],
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
    """Fake de Docker: cuenta creaciones y destrucciones y simula exit codes."""

    def __init__(self, *, exit_codes: dict[str, int] | None = None) -> None:
        self.exit_codes = exit_codes or {}
        self.created: list[ContainerSpec] = []
        self.removed: list[str] = []
        self.pulled: list[str] = []
        self._counter = 0

    def pull(self, reference: str, *, registry_user: str, registry_token: str) -> None:
        self.pulled.append(reference)

    def create(self, spec: ContainerSpec) -> str:
        self.created.append(spec)
        self._counter += 1
        return f"c{self._counter}"

    def start(self, container_id: str, *, timeout_s: float) -> ContainerOutcome:
        # El resultado depende del argv, para poder fallar un check concreto.
        spec = self.created[int(container_id[1:]) - 1]
        code = self.exit_codes.get(" ".join(spec.argv), 0)
        return ContainerOutcome(exit_code=code, duration_s=0.5, stdout="ok", stderr="boom")

    def remove(self, container_id: str) -> None:
        self.removed.append(container_id)


def _verifier(runtime: _Runtime, workspace: _Workspace) -> Verifier:
    return Verifier(
        profile_source=FixedProfileSource(_PROFILE),
        workspace=workspace,  # type: ignore[arg-type]
        runtime=runtime,  # type: ignore[arg-type]
        image_already_pulled=True,
    )


def test_verificacion_ok_ejecuta_recipes_en_orden_y_limpia() -> None:
    runtime = _Runtime()
    workspace = _Workspace()
    report = _verifier(runtime, workspace).verify(_REQUEST)

    assert report.passed
    assert report.image_digest == "sha256:deadbeef"
    # `setup_network` (composer) primero, luego `setup` offline, luego los checks.
    assert [spec.argv[:2] for spec in runtime.created] == [
        ["composer", "install"],
        ["bin/console", "cache:clear"],
        ["bin/phpunit", "tests/Foo/BarTest.php"],
        ["make", "stan"],
    ]
    assert workspace.prepared == ["abc"]
    assert workspace.cleaned == ["/tmp/ws"], "el workspace se destruye siempre"


def test_el_check_dirigido_inyecta_los_targets_sin_shell() -> None:
    runtime = _Runtime()
    _verifier(runtime, _Workspace()).verify(_REQUEST)
    targeted = next(spec for spec in runtime.created if spec.argv[:1] == ["bin/phpunit"])
    assert targeted.argv == ["bin/phpunit", "tests/Foo/BarTest.php"]


def test_los_checks_no_tienen_red_y_el_setup_con_red_si() -> None:
    runtime = _Runtime()
    _verifier(runtime, _Workspace()).verify(_REQUEST)
    for spec in runtime.created:
        if spec.argv[:2] == ["composer", "install"]:
            assert spec.network == "bridge", "composer install necesita red"
        else:
            assert spec.network == "none", "ningun check se ejecuta con red"


def test_todos_los_contenedores_se_destruyen() -> None:
    runtime = _Runtime()
    _verifier(runtime, _Workspace()).verify(_REQUEST)
    assert len(runtime.removed) == len(runtime.created), "creacion y destruccion por igual"


def test_el_workspace_se_destruye_aunque_falle_un_check() -> None:
    runtime = _Runtime(exit_codes={"make stan": 1})
    workspace = _Workspace()
    report = _verifier(runtime, workspace).verify(_REQUEST)
    assert not report.passed
    assert workspace.cleaned == ["/tmp/ws"]


def test_para_al_primer_fallo() -> None:
    runtime = _Runtime(exit_codes={"make stan": 1})
    request = _REQUEST.model_copy(update={"recipes": ["typecheck", "tests"]})
    _verifier(runtime, _Workspace()).verify(request)
    # setup + typecheck; `tests` no se ejecuta tras el fallo.
    assert not any(spec.argv[:2] == ["make", "test"] for spec in runtime.created)


def test_timeout_mata_el_contenedor_y_lo_destruye() -> None:
    class _TimeoutRuntime(_Runtime):
        def start(self, container_id: str, *, timeout_s: float) -> ContainerOutcome:
            raise ContainerTimeout("se acabo el tiempo")

    runtime = _TimeoutRuntime()
    report = _verifier(runtime, _Workspace()).verify(_REQUEST)
    assert not report.passed
    assert report.results[0].exit_code == 124
    assert len(runtime.removed) == len(runtime.created)


def test_sin_ci_image_digest_no_verifica_ni_clona() -> None:
    profile = {**_PROFILE, "ci_image_digest": "latest"}
    runtime = _Runtime()
    workspace = _Workspace()
    verifier = Verifier(
        profile_source=FixedProfileSource(profile),
        workspace=workspace,  # type: ignore[arg-type]
        runtime=runtime,  # type: ignore[arg-type]
    )
    with pytest.raises(RunnerError, match="ci_image_digest"):
        verifier.verify(_REQUEST)
    assert workspace.prepared == [], "sin digest no se clona nada"
    assert runtime.pulled == [], "sin digest no se hace pull"


def test_run_key_idempotente_no_relanza_el_sandbox() -> None:
    runtime = _Runtime()
    workspace = _Workspace()
    verifier = _verifier(runtime, workspace)
    first = verifier.verify(_REQUEST)
    created_after_first = len(runtime.created)
    second = verifier.verify(_REQUEST)
    assert second == first
    assert len(runtime.created) == created_after_first, "un run_key visto no relanza"
    assert workspace.prepared == ["abc"], "no se clona por segunda vez"


def test_target_invalido_es_rechazado() -> None:
    request = _REQUEST.model_copy(update={"targets": ["../../etc/passwd"]})
    runtime = _Runtime()
    workspace = _Workspace()
    with pytest.raises(InvalidTarget):
        _verifier(runtime, workspace).verify(request)
    assert runtime.created == []


def test_recipe_dirigida_sin_targets_se_salta() -> None:
    assert expand_check("targeted", {"targeted": "bin/phpunit {targets}"}, []) is None
    assert expand_check("targeted", {"targeted": "bin/phpunit {targets}"}, ["tests/A.php"]) == [
        "bin/phpunit",
        "tests/A.php",
    ]


def test_recipe_no_declarada_es_error() -> None:
    with pytest.raises(RecipeError):
        expand_check("inexistente", {"typecheck": "make stan"}, [])


def test_setup_se_interpreta_sin_shell() -> None:
    assert expand_setup(["a && b"]) == [["a", "&&", "b"]]
    assert validate_targets(["tests/Foo/BarTest.php"]) == ["tests/Foo/BarTest.php"]


def test_endpoint_verify_rechaza_sin_digest_con_409(monkeypatch, tmp_path) -> None:
    from fastapi.testclient import TestClient

    from agent_review.sandbox_runner import main as runner_main

    profile = tmp_path / "p.yaml"
    profile.write_text("repo: r\nci_image: reg/x:latest\n", encoding="utf-8")
    settings = runner_main.get_settings()
    monkeypatch.setattr(settings, "repo_profile_config", str(profile))
    # El token de ejemplo relaja la auth, como en development.
    monkeypatch.setattr(settings, "sandbox_runner_token", "change-me")
    runner_main._verifier = None

    response = TestClient(runner_main.app).post("/verify", json=_REQUEST.model_dump(mode="json"))
    assert response.status_code == 409
    assert "ci_image_digest" in response.json()["detail"]


def test_endpoint_verify_exige_token_si_esta_configurado(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from agent_review.sandbox_runner import main as runner_main

    settings = runner_main.get_settings()
    monkeypatch.setattr(settings, "sandbox_runner_token", "secreto-real")
    runner_main._verifier = None

    response = TestClient(runner_main.app).post("/verify", json=_REQUEST.model_dump(mode="json"))
    assert response.status_code == 401
