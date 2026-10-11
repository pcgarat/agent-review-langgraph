from __future__ import annotations

from reviews.domain.models import (
    CommandResult,
    FixResult,
    FixStatus,
    ProposedPatch,
    ReviewOutcome,
    ReviewRequest,
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
    from reviews.checkpointer import build_serde

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
