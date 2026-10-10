from __future__ import annotations

from pathlib import Path

from reviews.adapters.diff_parser import best_line, file_key, parse_unified_diff
from reviews.adapters.tooling_reviewer import ToolingReviewer
from reviews.adapters.tooling_source import StaticToolingOutputSource

FIXTURES = Path(__file__).parent / "fixtures"
DIFF = (FIXTURES / "diff_emotivoo.diff").read_text(encoding="utf-8")
PHPSTAN = (FIXTURES / "phpstan" / "analysis.json").read_text(encoding="utf-8")


def test_parse_diff_indexa_solo_lineas_anadidas() -> None:
    files = parse_unified_diff(DIFF)
    transfer = files["src/BackOffice/Application/Wallet/Transfer.php"]
    assert set(transfer.added) == {13, 14, 15}
    assert "$balance->frozen" in transfer.added[13]
    assert "InsufficientBalance" in transfer.added[14]
    assert "tests/BackOffice/Wallet/TransferTest.php" in files


def test_file_key_casa_ruta_absoluta_y_relativa() -> None:
    files = parse_unified_diff(DIFF)
    assert (
        file_key(files, "/app/src/BackOffice/Application/Wallet/Transfer.php")
        == "src/BackOffice/Application/Wallet/Transfer.php"
    )
    assert file_key(files, "src/BackOffice/Application/Wallet/Nope.php") is None


def test_best_line_prefiere_needle_y_cae_al_hint() -> None:
    added = {10: "$a = 1;", 12: "$b = 2;"}
    assert best_line(added, hint=11, needle="$b = 2;") == 12
    assert best_line(added, hint=11, needle=None) == 10
    assert best_line({}, hint=1, needle="x") is None


def test_persona_filtra_lo_fuera_del_diff_y_ancla_al_diff() -> None:
    reviewer = ToolingReviewer(phpstan_json=PHPSTAN)
    findings = reviewer.review(repo="cqm/emotivoo-v2", pr_id=1, diff=DIFF)

    files = parse_unified_diff(DIFF)
    # Legacy.php no esta en el diff: se descarta.
    assert all("Legacy" not in finding.file for finding in findings)
    assert len(findings) == 2

    transfer = next(f for f in findings if f.file.endswith("Transfer.php"))
    assert transfer.line in files[transfer.file].added
    assert transfer.confidence == 100
    assert transfer.title.startswith("Cannot call method withdraw")

    # El segundo hallazgo del fixture es de test y de severidad baja.
    test_finding = next(f for f in findings if f.file.endswith("TransferTest.php"))
    assert test_finding.severity.value == "P3"


def test_persona_marca_tema_protegido() -> None:
    payload = """
    {"files": {"/app/src/Auth.php": {"messages": [
      {"message": "Unsafe password comparison", "line": 2}
    ]}}}
    """
    diff = (
        "diff --git a/src/Auth.php b/src/Auth.php\n"
        "--- a/src/Auth.php\n+++ b/src/Auth.php\n"
        "@@ -1,2 +1,3 @@\n"
        " <?php\n"
        "+if ($password === 'root') { login(); }\n"
        "+$ok = true;\n"
    )
    findings = ToolingReviewer(phpstan_json=payload).review(repo="r", pr_id=1, diff=diff)
    assert len(findings) == 1
    assert findings[0].protected_subject is not None
    assert findings[0].autofix_class.value == "manual"


def test_persona_sin_salida_no_inventa_hallazgos() -> None:
    assert ToolingReviewer(phpstan_json="").review(repo="r", pr_id=1, diff=DIFF) == []
    assert ToolingReviewer(phpstan_json="{no json").review(repo="r", pr_id=1, diff=DIFF) == []


class _ExplodingSource:
    def read(self, tool: str, *, repo: str, pr_id: int, head_sha: str) -> str | None:
        del tool, repo, pr_id, head_sha
        raise AssertionError("el reviewer no debe leer si la herramienta no esta permitida")


def test_persona_con_source_externo_lee_la_salida() -> None:
    reviewer = ToolingReviewer(source=StaticToolingOutputSource(PHPSTAN))
    findings = reviewer.review(repo="cqm/emotivoo-v2", pr_id=1, diff=DIFF, head_sha="abc")
    assert len(findings) == 2


def test_persona_source_sin_salida_no_inventa() -> None:
    reviewer = ToolingReviewer(source=StaticToolingOutputSource(""))
    assert reviewer.review(repo="r", pr_id=1, diff=DIFF) == []


def test_persona_no_lee_herramientas_no_permitidas() -> None:
    reviewer = ToolingReviewer(source=_ExplodingSource(), allowed_tools=("php-cs-fixer",))
    assert reviewer.review(repo="r", pr_id=1, diff=DIFF) == []
