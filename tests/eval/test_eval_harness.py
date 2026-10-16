"""E1: el harness carga un caso versionado y conserva su procedencia.

El caso de estos tests es un *fixture de juguete*: valida la forma del harness, no
la review del PR #3 (esa vive en `pr_3/`, E5). Sin red, sin grafo.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest
import yaml
from harness import (
    Case,
    FingerprintMismatch,
    Label,
    Recording,
    ReplayLlm,
    Report,
    Verdict,
    case_fingerprint,
    compare,
    load_case,
    match,
    measure,
    normalize,
    record_call,
    to_json,
)

from agent_review.domain.models import AutofixClass, Finding, Owner, Severity
from agent_review.ports.llm import LlmReply


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


# --- E3: metricas y JSON determinista ---


def _audit_case() -> Case:
    """El caso de la auditoria: A e I `expected`, H `acceptable`, 6 falsos (B–G)."""
    return _case(
        expected=[
            Label(id="A", match=("mark_done",), severity="P1", subset="correctness"),
            Label(id="I", match=("heartbeat",), severity="P1", subset="correctness"),
            Label(id="integracion-1", match=("secretos",), severity="P1", subset="integracion"),
            Label(id="integracion-2", match=("arranca",), severity="P1", subset="integracion"),
            Label(id="integracion-3", match=("pisa",), severity="P1", subset="integracion"),
        ],
        must_not_report=[
            Label(id=fid, match=(fid.lower(),)) for fid in ("B", "C", "D", "E", "F", "G")
        ],
        acceptable=[Label(id="H", match=("documentada",))],
    )


def _audit_published() -> list[Finding]:
    findings = [
        _finding(fid="a", title="mark_done", severity=Severity.P1, line=1),
        _finding(fid="i", title="heartbeat", severity=Severity.P1, line=2),
        _finding(fid="h", title="documentada", severity=Severity.P3, line=3),
    ]
    for index, fid in enumerate(("B", "C", "D", "E", "F", "G")):
        findings.append(_finding(fid=fid, title=fid.lower(), severity=Severity.P2, line=10 + index))
    return findings


def test_precision_y_recall_de_la_auditoria() -> None:
    report = measure(_audit_case(), _audit_published())

    assert report.counts["published"] == 9
    assert report.counts["tp"] == 2
    assert report.counts["fp"] == 6
    assert report.counts["acceptable"] == 1
    assert report.counts["fp_unknown"] == 0
    assert report.counts["fn"] == 3
    assert report.metrics["precision"] == 0.25
    assert report.metrics["recall"] == 0.4
    assert report.metrics["recall_by_subset"] == {"correctness": 1.0, "integracion": 0.0}
    assert report.metrics["severity_accuracy"] == 1.0


def test_hallazgo_sin_etiqueta_es_falso_positivo() -> None:
    case = _case(expected=[Label(id="A", match=("mark_done",))])
    stray = _finding(fid="z", title="ruido", line=3)

    report = measure(case, [_finding(fid="a", title="mark_done", line=1), stray])

    assert report.counts["fp"] == 1
    assert report.counts["fp_unknown"] == 1
    assert report.counts["fp_labeled"] == 0


def test_tooling_queda_fuera_de_precision_y_recall_globales() -> None:
    case = _case(
        expected=[
            Label(id="A", match=("mark_done",), subset="correctness"),
            Label(id="T", match=("phpstan",), subset="tooling"),
        ]
    )
    published = [
        _finding(fid="a", title="mark_done", line=1),
        _finding(fid="t", title="phpstan", line=2),
        _finding(fid="z", title="ruido", line=3),
    ]

    report = measure(case, published)

    # 1 tp de correctness de 1 esperado, 1 fp: el tp de tooling no infla el numero.
    assert report.metrics["precision"] == 0.5
    assert report.metrics["recall"] == 1.0
    assert report.metrics["recall_by_subset"]["tooling"] == 1.0


def test_falsos_p0_p1_y_tp_inverted() -> None:
    case = _case(
        expected=[
            Label(id="A", match=("mark_done",), severity="P1", must_mention=("antes",)),
        ],
        must_not_report=[Label(id="B", match=("inventado",))],
    )
    published = [
        _finding(
            fid="a",
            title="mark_done se ordena mal",
            why="corre despues",
            severity=Severity.P1,
        ),
        _finding(fid="B", title="dato inventado", severity=Severity.P0, line=2),
    ]

    report = measure(case, published)

    assert report.counts["fp_p0_p1"] == 1
    assert report.inverted == ("A",)
    assert report.counts["tp_inverted"] == 1
    # El invertido sigue siendo tp: no mueve precision ni recall (E16).
    assert report.counts["tp"] == 1
    assert report.metrics["precision"] == 0.5
    assert report.metrics["recall"] == 1.0


def test_coste_y_cobertura() -> None:
    case = _case(expected=[Label(id="A", match=("mark_done",))])
    published = [_finding(fid="a", title="mark_done", line=1)]

    report = measure(case, published, tokens=1000, coverage=0.1)

    assert report.metrics["cost_per_tp_tokens"] == 1000.0
    assert report.metrics["coverage"] == 0.1


def test_coste_none_sin_tp_y_cobertura_none_no_medida() -> None:
    case = _case(expected=[Label(id="A", match=("mark_done",))])

    report = measure(case, [], tokens=1000)

    assert report.metrics["cost_per_tp_tokens"] is None
    assert report.metrics["coverage"] is None


def test_json_estable_entre_serializaciones() -> None:
    report = measure(_audit_case(), _audit_published())

    first = to_json(report)
    second = to_json(measure(_audit_case(), _audit_published()))

    assert first == second
    # Claves ordenadas: `case` antes que `counts` antes que `metrics`.
    assert first.index('"case"') < first.index('"counts"') < first.index('"metrics"')


# --- E4: harness.compare (dominancia, tres veredictos) ---


def _report(*, metrics: dict, fp_p0_p1: int = 0) -> Report:
    return Report(case="c", mode="replay", counts={"fp_p0_p1": fp_p0_p1}, metrics=metrics)


def test_compare_mejora_si_nada_baja_y_algo_sube() -> None:
    baseline = _report(metrics={"precision": 0.25, "recall": 0.4, "coverage": 0.1}, fp_p0_p1=6)
    current = _report(metrics={"precision": 0.25, "recall": 0.6, "coverage": 0.1}, fp_p0_p1=6)

    assert compare(current, baseline) is Verdict.MEJORA


def test_compare_mejora_bajando_los_p0_p1() -> None:
    baseline = _report(metrics={"precision": 0.25, "recall": 0.4, "coverage": 0.1}, fp_p0_p1=6)
    current = _report(metrics={"precision": 0.25, "recall": 0.4, "coverage": 0.1}, fp_p0_p1=2)

    assert compare(current, baseline) is Verdict.MEJORA


def test_compare_empeora_si_nada_mejora_y_algo_baja() -> None:
    baseline = _report(metrics={"precision": 0.25, "recall": 0.4, "coverage": 0.1}, fp_p0_p1=6)
    current = _report(metrics={"precision": 0.2, "recall": 0.4, "coverage": 0.1}, fp_p0_p1=6)

    assert compare(current, baseline) is Verdict.EMPEORA


def test_compare_empeora_si_suben_los_p0_p1() -> None:
    baseline = _report(metrics={"precision": 0.25, "recall": 0.4, "coverage": 0.1}, fp_p0_p1=6)
    current = _report(metrics={"precision": 0.25, "recall": 0.4, "coverage": 0.1}, fp_p0_p1=8)

    assert compare(current, baseline) is Verdict.EMPEORA


def test_compare_no_concluyente_si_sube_recall_bajan_p0_p1_y_cae_precision() -> None:
    baseline = _report(metrics={"precision": 0.25, "recall": 0.4, "coverage": 0.1}, fp_p0_p1=6)
    current = _report(metrics={"precision": 0.2, "recall": 0.6, "coverage": 0.1}, fp_p0_p1=2)

    assert compare(current, baseline) is Verdict.NO_CONCLUYENTE


def test_compare_iguales_es_no_concluyente() -> None:
    baseline = _report(metrics={"precision": 0.25, "recall": 0.4, "coverage": 0.1}, fp_p0_p1=6)
    current = _report(metrics={"precision": 0.25, "recall": 0.4, "coverage": 0.1}, fp_p0_p1=6)

    assert compare(current, baseline) is Verdict.NO_CONCLUYENTE


def test_compare_coverage_none_no_cuenta_como_caida() -> None:
    baseline = _report(metrics={"precision": 0.25, "recall": 0.4, "coverage": None}, fp_p0_p1=6)
    current = _report(metrics={"precision": 0.25, "recall": 0.5, "coverage": None}, fp_p0_p1=6)

    assert compare(current, baseline) is Verdict.MEJORA


# --- E6: LlmPort de replay con fingerprint ---


def _recording(case: Case, *, role: str, prompt: str = "DIFF:\nx", max_tokens: int = 4096):
    recording = Recording(case=case.name, case_fingerprint=case_fingerprint(case), calls={})
    record_call(
        recording,
        role=role,
        system="S",
        prompt=prompt,
        max_tokens=max_tokens,
        reply=LlmReply(text='{"findings": []}', prompt_tokens=10, completion_tokens=20),
    )
    return recording


def test_replay_devuelve_la_respuesta_grabada() -> None:
    case = _case()
    llm = ReplayLlm(_recording(case, role="correctness"), case, role="correctness")

    reply = llm.complete(system="S", prompt="DIFF:\nx", max_tokens=4096)

    assert reply.text == '{"findings": []}'
    assert reply.prompt_tokens == 10
    assert reply.completion_tokens == 20


def test_replay_aborta_si_cambia_el_prompt() -> None:
    case = _case()
    llm = ReplayLlm(_recording(case, role="correctness"), case, role="correctness")

    with pytest.raises(FingerprintMismatch, match="fingerprint"):
        llm.complete(system="S", prompt="DIFF:\nOTRO", max_tokens=4096)


def test_replay_aborta_si_cambia_el_max_findings() -> None:
    case = _case()
    llm = ReplayLlm(_recording(case, role="correctness", max_tokens=4096), case, role="correctness")

    with pytest.raises(FingerprintMismatch):
        llm.complete(system="S", prompt="DIFF:\nx", max_tokens=2048)


def test_replay_aborta_si_el_caso_es_distinto() -> None:
    case = _case()
    other = Case(
        name="otro",
        repo="r",
        pr_id=1,
        base_sha="b",
        head_sha="h",
        captured_at="2026-10-16",
        source="s",
        scope={},
        diff="otro diff",
    )

    with pytest.raises(FingerprintMismatch, match="caso"):
        ReplayLlm(_recording(case, role="correctness"), other, role="correctness")


def test_replay_el_rol_forma_parte_del_fingerprint() -> None:
    case = _case()
    recording = _recording(case, role="correctness")
    llm = ReplayLlm(recording, case, role="verifier")

    with pytest.raises(FingerprintMismatch):
        llm.complete(system="S", prompt="DIFF:\nx", max_tokens=4096)


def test_replay_aborta_si_se_consume_dos_veces_el_mismo_fingerprint() -> None:
    case = _case()
    llm = ReplayLlm(_recording(case, role="correctness"), case, role="correctness")

    llm.complete(system="S", prompt="DIFF:\nx", max_tokens=4096)
    with pytest.raises(FingerprintMismatch, match="ya se consumio"):
        llm.complete(system="S", prompt="DIFF:\nx", max_tokens=4096)


def test_recording_round_trip(tmp_path: Path) -> None:
    case = _case()
    recording = _recording(case, role="correctness")
    path = tmp_path / "recorded.json"
    path.write_text(recording.dump(), encoding="utf-8")

    reloaded = Recording.load(path)
    llm = ReplayLlm(reloaded, case, role="correctness")

    assert reloaded.case_fingerprint == recording.case_fingerprint
    assert llm.complete(system="S", prompt="DIFF:\nx", max_tokens=4096).text == '{"findings": []}'
