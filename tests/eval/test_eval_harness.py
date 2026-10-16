"""E1: el harness carga un caso versionado y conserva su procedencia.

El caso de estos tests es un *fixture de juguete*: valida la forma del harness, no
la review del PR #3 (esa vive en `pr_3/`, E5). Sin red, sin grafo.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest
import yaml
from harness import Case, Label, load_case, match, normalize

from agent_review.domain.models import AutofixClass, Finding, Owner, Severity


def _write_case(root: Path, labels: dict) -> Path:
    (root / "labels.yaml").write_text(yaml.safe_dump(labels), encoding="utf-8")
    (root / "diff.patch").write_text("diff --git a/x.py b/x.py\n+amor\n", encoding="utf-8")
    return root


def _case(
    *,
    expected: Iterable[Label] = (),
    must_not_report: Iterable[Label] = (),
    acceptable: Iterable[Label] = (),
) -> Case:
    return Case(
        name="c",
        repo="r",
        pr_id=1,
        base_sha="b",
        head_sha="h",
        captured_at="2026-10-16",
        source="s",
        scope={},
        diff="",
        expected=tuple(expected),
        must_not_report=tuple(must_not_report),
        acceptable=tuple(acceptable),
    )


def _finding(
    *,
    title: str,
    why: str = "",
    fid: str = "f",
    severity: Severity = Severity.P2,
    file: str = "src/x.py",
    line: int = 1,
) -> Finding:
    return Finding(
        id=fid,
        title=title,
        severity=severity,
        file=file,
        line=line,
        why_it_matters=why or title,
        autofix_class=AutofixClass.ADVISORY,
        owner=Owner.HUMAN,
        requires_verification=False,
        confidence=50,
        evidence=[f"{file}:{line} algo"],
    )


def test_normaliza_espacios_y_mayusculas() -> None:
    assert normalize("  El  Merge   Dedupe ") == "el merge dedupe"


def test_no_quita_tildes_para_no_ensanchar_el_matcher() -> None:
    # Quitar tildes casaria "validacion" con "validación": ensancharia el matcher
    # sin querer y decidiria el numero por accidente (riesgo de la spec §6).
    assert normalize("validación") == "validación"
    assert normalize("validación") != normalize("validacion")


def test_carga_caso_con_procedencia_y_etiquetas(tmp_path: Path) -> None:
    _write_case(
        tmp_path,
        {
            "case": "pr-3-agent-review",
            "repo": "pcgarat/agent-review-langgraph",
            "pr_id": 3,
            "base_sha": "a8ead73",
            "head_sha": "008da57",
            "captured_at": "2026-10-16",
            "source": "auditoria manual",
            "scope": {"max_files_per_pr": 10, "scope_ignore": ""},
            "expected": [
                {
                    "id": "A",
                    "severity": "P1",
                    "subset": "correctness",
                    "match": ["mark_done", "enqueue"],
                    "must_mention": ["antes"],
                },
                {
                    "id": "integracion-1",
                    "severity": "P1",
                    "subset": "integracion",
                    "match": ["secretos", "entorno"],
                },
            ],
            "must_not_report": [
                {"id": "B", "match": ["requeue_or_drop"], "why": "semantica inventada"},
            ],
            "acceptable": [
                {"id": "H", "match": ["env_for", "Settings"], "why": "decision documentada"},
            ],
        },
    )

    case = load_case(tmp_path)

    assert isinstance(case, Case)
    assert case.name == "pr-3-agent-review"
    assert case.head_sha == "008da57"
    assert case.scope["max_files_per_pr"] == 10
    assert [label.id for label in case.expected] == ["A", "integracion-1"]
    assert [label.id for label in case.must_not_report] == ["B"]
    assert [label.id for label in case.acceptable] == ["H"]
    assert case.expected[0].must_mention == ("antes",)
    assert case.expected[1].subset == "integracion"
    assert case.must_not_report[0].why == "semantica inventada"


def test_labels_agrupa_las_tres_listas(tmp_path: Path) -> None:
    _write_case(
        tmp_path,
        {
            "case": "c",
            "repo": "r",
            "pr_id": 1,
            "base_sha": "b",
            "head_sha": "h",
            "captured_at": "2026-10-16",
            "source": "s",
            "scope": {},
            "expected": [{"id": "A", "match": ["a"]}],
            "must_not_report": [{"id": "B", "match": ["b"], "why": "w"}],
            "acceptable": [{"id": "H", "match": ["h"], "why": "w"}],
        },
    )

    case = load_case(tmp_path)

    assert [label.id for label in case.labels()] == ["A", "B", "H"]


def test_diff_ausente_falla(tmp_path: Path) -> None:
    (tmp_path / "labels.yaml").write_text("case: c\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        load_case(tmp_path)


# --- E2: matcher 1:1 determinista ---


def test_match_conjuntivo_sobre_titulo_y_why() -> None:
    case = _case(expected=[Label(id="A", match=("mark_done", "enqueue"))])
    finding = _finding(title="mark_done se ordena mal", why="enqueue corre antes de tiempo")

    result = match(case, [finding])

    assert result.expected["A"] is finding
    assert result.missing == ()


def test_exige_todas_las_frases() -> None:
    case = _case(expected=[Label(id="A", match=("mark_done", "enqueue"))])
    finding = _finding(title="mark_done se ordena mal", why="nada mas")

    result = match(case, [finding])

    assert result.expected == {}
    assert result.missing == ("A",)
    assert result.unmatched == (finding,)


def test_dos_etiquetas_no_consumen_el_mismo_hallazgo() -> None:
    # Dos etiquetas que casarian el mismo hallazgo se reparten: la segunda busca
    # el siguiente no consumido, no lo cuenta dos veces.
    case = _case(expected=[Label(id="A", match=("enqueue",)), Label(id="B", match=("enqueue",))])
    first = _finding(fid="uno", title="enqueue falla", line=1)
    second = _finding(fid="dos", title="enqueue otra vez", line=2)

    result = match(case, [first, second])

    assert result.expected == {"A": first, "B": second}
    assert result.unmatched == ()


def test_hallazgo_sin_etiqueta_queda_unmatched() -> None:
    case = _case(expected=[Label(id="A", match=("mark_done",))])
    matched = _finding(fid="a", title="mark_done mal", line=1)
    stray = _finding(fid="z", title="ruido irrelevante", line=2)

    result = match(case, [matched, stray])

    assert result.expected == {"A": matched}
    assert result.unmatched == (stray,)


def test_file_y_line_con_tolerancia_de_cinco() -> None:
    case = _case(expected=[Label(id="A", match=("bug",), file="src/x.py", line=10)])
    assert match(case, [_finding(title="bug aqui", line=15)]).expected != {}
    assert match(case, [_finding(title="bug aqui", line=16)]).expected == {}
    assert match(case, [_finding(title="bug aqui", file="src/otro.py", line=10)]).expected == {}


def test_no_depende_del_orden_de_entrada() -> None:
    case = _case(expected=[Label(id="A", match=("alta",)), Label(id="B", match=("baja",))])
    alta = _finding(fid="alta", title="alta", severity=Severity.P0, line=9)
    baja = _finding(fid="baja", title="baja", severity=Severity.P3, line=1)

    directo = match(case, [alta, baja])
    invertido = match(case, [baja, alta])

    assert directo.expected == invertido.expected == {"A": alta, "B": baja}
    assert directo.unmatched == invertido.unmatched == ()


def test_must_not_report_y_acceptable_tambien_se_atribuyen() -> None:
    case = _case(
        must_not_report=[Label(id="B", match=("inventado",))],
        acceptable=[Label(id="H", match=("documentada",))],
    )
    b = _finding(fid="b", title="dato inventado")
    h = _finding(fid="h", title="decision documentada", line=2)

    result = match(case, [b, h])

    assert result.must_not["B"] is b
    assert result.acceptable["H"] is h
    assert result.unmatched == ()


def test_expected_sin_match_falla() -> None:
    case = _case(expected=[Label(id="A", match=())])
    with pytest.raises(ValueError, match="no declara 'match'"):
        match(case, [])
