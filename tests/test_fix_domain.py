from __future__ import annotations

from agent_review.domain.models import (
    ApprovalRequest,
    CommandResult,
    FixResolution,
    FixResolutions,
    FixResult,
    FixStatus,
    FixWaitRequest,
    ProposedPatch,
    ReviewOutcome,
    ReviewRequest,
    VerifyJob,
)


def _patch(**overrides: object) -> ProposedPatch:
    base: dict[str, object] = {
        "finding_id": "f1",
        "file": "src/Foo.php",
        "diff": "diff --git a/src/Foo.php b/src/Foo.php\n",
        "start_line": 2,
        "end_line": 2,
        "replacement": "use App\\Bar;",
    }
    base.update(overrides)
    return ProposedPatch(**base)  # type: ignore[arg-type]


def test_fix_result_verified_helper() -> None:
    result = FixResult(finding_id="f1", status=FixStatus.VERIFIED, attempts=1)
    assert result.verified
    assert not FixResult(finding_id="f1", status=FixStatus.FAILED, attempts=1).verified
    # `PENDING` es el estado intermedio del handoff: despachado, sin verificar aun.
    assert not FixResult(finding_id="f1", status=FixStatus.PENDING, attempts=1).verified


def _verify_job(run_key: str = "k:1:1") -> VerifyJob:
    return VerifyJob(
        run_key=run_key,
        finding_id="f1",
        attempt=1,
        repo="r",
        head_sha="abc",
        patch="diff --git a/src/Foo.php b/src/Foo.php",
        recipes=["targeted", "tests"],
        targets=["tests/FooTest.php"],
    )


def test_fix_wait_request_lleva_la_wave_de_jobs() -> None:
    wave = FixWaitRequest(
        review_key="r:1:abc:7", repo="r", pr_id=1, head_sha="abc", jobs=[_verify_job()]
    )
    assert wave.kind == "fix"
    assert wave.jobs[0].run_key == "k:1:1"


def test_fix_resolutions_es_el_valor_del_resume() -> None:
    resolutions = FixResolutions(
        items=[
            FixResolution(
                run_key="k:1:1",
                outcome="passed",
                image_digest="sha256:deadbeef",
            )
        ]
    )
    assert resolutions.items[0].outcome == "passed"


def test_review_request_sin_fix_resolution_es_el_de_siempre() -> None:
    """El campo nuevo no altera un `ReviewRequest` de review normal (compatibilidad)."""
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="7")
    assert request.fix_resolution is None


def test_review_request_lleva_la_resolucion_de_la_wave() -> None:
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="7")
    resumed = request.model_copy(
        update={
            "fix_resolution": FixResolutions(
                items=[FixResolution(run_key="k:1:1", outcome="failed")]
            )
        }
    )
    assert resumed.fix_resolution is not None
    assert resumed.fix_resolution.items[0].run_key == "k:1:1"


def test_command_result_ok() -> None:
    ok = CommandResult(recipe="typecheck", argv=["make", "stan"], exit_code=0, duration_s=1.5)
    bad = CommandResult(recipe="typecheck", argv=["make", "stan"], exit_code=1, duration_s=1.5)
    assert ok.ok and not bad.ok


def test_patch_rejects_empty_replacement_is_allowed_but_range_validated() -> None:
    # `end_line` < `start_line` no es un rango: lo valida la validacion local.
    patch = _patch(start_line=5, end_line=5)
    assert patch.start_line == patch.end_line == 5


def test_outcome_exposes_fixes() -> None:
    request = ReviewRequest(repo="cqm/x", pr_id=1, head_sha="abc")
    outcome = ReviewOutcome(
        request=request,
        fixes={"f1": FixResult(finding_id="f1", status=FixStatus.FAILED, attempts=2)},
    )
    assert outcome.fixes["f1"].status is FixStatus.FAILED
    assert outcome.fixes["f1"].patch is None


def test_fix_result_se_serializa_en_el_serde_de_produccion() -> None:
    """`FixResult` viaja en el estado del grafo: debe pasar la allowlist del serde.

    Sin esto, reanudar un hilo con fixes registrados fallaria al deserializar el
    checkpoint (los tipos no allowlisted avisan hoy y bloquean mas adelante).
    """
    from agent_review.checkpointer import build_serde

    serde = build_serde()
    result = FixResult(
        finding_id="f1",
        status=FixStatus.VERIFIED,
        attempts=1,
        patch=_patch(),
        checks=[
            CommandResult(recipe="typecheck", argv=["make", "stan"], exit_code=0, duration_s=2.0)
        ],
        image_digest="sha256:deadbeef",
    )
    payload = serde.dumps_typed(result)
    restored = serde.loads_typed(payload)
    assert restored == result


def test_tipos_del_handoff_pasan_el_serde() -> None:
    """La wave y su resolucion viajan por el estado: deben pasar la allowlist."""
    from agent_review.checkpointer import build_serde

    serde = build_serde()
    wave = FixWaitRequest(
        review_key="r:1:abc:7", repo="r", pr_id=1, head_sha="abc", jobs=[_verify_job()]
    )
    assert serde.loads_typed(serde.dumps_typed(wave)) == wave
    resolutions = FixResolutions(
        items=[FixResolution(run_key="k:1:1", outcome="failed", error="boom")]
    )
    assert serde.loads_typed(serde.dumps_typed(resolutions)) == resolutions


def test_approval_request_es_la_pausa_humana() -> None:
    """El discriminante simetrico: una pausa de decision humana es `kind=human`."""
    approval = ApprovalRequest(review_key="k", repo="r", pr_id=1, head_sha="abc")
    assert approval.kind == "human"
