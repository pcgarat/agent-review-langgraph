from __future__ import annotations

from reviews.domain.models import (
    AutofixClass,
    Finding,
    FixResult,
    FixStatus,
    Owner,
    ProposedPatch,
    Severity,
)
from reviews.fix.patch import build_patch
from reviews.fix.validation import validate_patch

_PR_DIFF = (
    "diff --git a/src/Foo.php b/src/Foo.php\n"
    "--- a/src/Foo.php\n"
    "+++ b/src/Foo.php\n"
    "@@ -1,3 +1,4 @@\n"
    " <?php\n"
    "+use App\\Bar;\n"
    " function foo() {\n"
)
_SOURCE = "<?php\nuse App\\Baz;\nfunction foo() {\n"

# El hallazgo apunta a la linea anadida (2) del PR.
_FINDING = Finding(
    id="f1",
    title="Import sin usar",
    severity=Severity.P2,
    file="src/Foo.php",
    line=2,
    why_it_matters="Ruido",
    autofix_class=AutofixClass.GATED_AUTO,
    owner=Owner.HUMAN,
    requires_verification=True,
    confidence=75,
    evidence=["src/Foo.php:2 +use App\\Bar;"],
)


def _patch(*, start: int, end: int, replacement: str, path: str = "src/Foo.php") -> ProposedPatch:
    source = _SOURCE
    finding = _FINDING.model_copy(update={"file": path})
    return build_patch(
        finding, source=source, start_line=start, end_line=end, replacement=replacement
    )


def test_patch_valido_pasa() -> None:
    patch = _patch(start=2, end=2, replacement="use App\\Bar;")
    assert validate_patch(patch, finding=_FINDING, diff=_PR_DIFF, max_fix_lines=30) is None


def test_rango_fuera_de_las_lineas_anadidas_es_invalido() -> None:
    # La linea 3 existe en el fichero pero NO es una linea anadida del PR: una
    # sugerencia solo puede reemplazar lo que el diff muestra.
    patch = _patch(start=3, end=3, replacement="function bar() {")
    reason = validate_patch(patch, finding=_FINDING, diff=_PR_DIFF, max_fix_lines=30)
    assert reason is not None and "anadidas" in reason


def test_fichero_distinto_al_del_hallazgo_es_invalido() -> None:
    patch = _patch(start=2, end=2, replacement="x", path="src/Other.php")
    reason = validate_patch(patch, finding=_FINDING, diff=_PR_DIFF, max_fix_lines=30)
    assert reason is not None and "Other.php" in reason


def test_rango_demasiado_grande_es_invalido() -> None:
    # Un rango de 2 lineas con max_fix_lines=1 se rechaza por tamano.
    patch = _patch(start=2, end=3, replacement="a\nb")
    reason = validate_patch(patch, finding=_FINDING, diff=_PR_DIFF, max_fix_lines=1)
    assert reason is not None and "max_fix_lines" in reason


def test_diff_vacio_es_invalido() -> None:
    patch = _patch(start=2, end=2, replacement="x").model_copy(update={"diff": "  \n"})
    reason = validate_patch(patch, finding=_FINDING, diff=_PR_DIFF, max_fix_lines=30)
    assert reason is not None and "vacio" in reason


def test_fichero_del_hallazgo_ausente_del_pr_es_invalido() -> None:
    other_pr = _PR_DIFF.replace("src/Foo.php", "src/Solo.php")
    patch = _patch(start=2, end=2, replacement="use App\\Bar;")
    reason = validate_patch(patch, finding=_FINDING, diff=other_pr, max_fix_lines=30)
    assert reason is not None and "no esta en el diff" in reason


def test_varios_hunks_es_invalido() -> None:
    patch = _patch(start=2, end=2, replacement="use App\\Bar;")
    two_hunks = patch.diff + "@@ -10,1 +11,1 @@\n-x\n+y\n"
    patch = patch.model_copy(update={"diff": two_hunks})
    reason = validate_patch(patch, finding=_FINDING, diff=_PR_DIFF, max_fix_lines=30)
    assert reason is not None and "2 hunks" in reason


def test_resultado_de_fix_conserva_los_checks() -> None:
    result = FixResult(
        finding_id="f1",
        status=FixStatus.FAILED,
        attempts=2,
        checks=[],
        reason="typecheck fallo",
    )
    assert result.status is FixStatus.FAILED
