"""E1: el harness carga un caso versionado y conserva su procedencia.

El caso de estos tests es un *fixture de juguete*: valida la forma del harness, no
la review del PR #3 (esa vive en `pr_3/`, E5). Sin red, sin grafo.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import harness
import pytest
import yaml
from harness import (
    Case,
    CaseVcs,
    FingerprintMismatch,
    IncompleteMeasurement,
    Label,
    Recording,
    RecordingLlm,
    ReplayLlm,
    Report,
    Verdict,
    case_fingerprint,
    compare,
    coverage_of,
    evaluate,
    load_case,
    match,
    measure,
    normalize,
    prompt_fingerprint,
    record_call,
    run_case,
    to_json,
)

from agent_review.domain.models import AutofixClass, Finding, Owner, Severity, SkipReason
from agent_review.policy.engine import PolicyEngine
from agent_review.ports.llm import LlmError, LlmReply


def _write_case(root: Path, labels: dict) -> Path:
    (root / "labels.yaml").write_text(yaml.safe_dump(labels), encoding="utf-8")
    (root / "diff.patch").write_text(_TOY_DIFF, encoding="utf-8")
    return root


_TOY_DIFF = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -0,0 +1 @@\n+amor\n"


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
        failure_scenario="escenario de prueba",
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


# --- ADR-0025: el matcher mide el defecto, no la redaccion ---


def test_dos_redacciones_del_mismo_defecto_casan_la_misma_etiqueta() -> None:
    # Ancla por fichero + linea + un fragmento discriminante: la redaccion puede
    # cambiar sin que el defecto se mueva (E0d de 023).
    case = _case(
        expected=[Label(id="A", match=("mark_done",), file="src/x.py", line=95)],
    )
    literal = _finding(fid="uno", title="mark_done corre antes del enqueue", line=95)
    parafraseado = _finding(
        fid="dos",
        title="La reanudacion se pierde",
        why="Se llama a mark_done antes de encolar la reanudacion",
        line=97,
    )

    primero = match(case, [literal])
    segundo = match(case, [parafraseado])

    assert primero.expected["A"] is literal
    assert segundo.expected["A"] is parafraseado


def test_dos_defectos_en_la_misma_linea_no_casan_la_misma_etiqueta() -> None:
    # Sin fragmento discriminante, file+line (±5) casaria los dos. Con el, cada
    # defecto tiene su etiqueta y no hay recall por la puerta de atras (R1b).
    case = _case(
        expected=[
            Label(id="fuga", match=("github_token",), file="docker-compose.yml", line=76),
            Label(id="arranque", match=("arranca",), file="docker-compose.yml", line=76),
        ],
    )
    fuga = _finding(
        fid="fuga",
        file="docker-compose.yml",
        title="env_file filtra github_token",
        line=76,
    )
    arranque = _finding(
        fid="arranque", file="docker-compose.yml", title="el fix-worker no arranca", line=78
    )

    result = match(case, [fuga, arranque])

    assert result.expected == {"fuga": fuga, "arranque": arranque}
    assert result.unmatched == ()


def test_el_defecto_equivocado_en_la_misma_linea_queda_sin_casar() -> None:
    case = _case(
        expected=[Label(id="fuga", match=("github_token",), file="docker-compose.yml", line=76)],
    )
    otro = _finding(
        fid="otro", file="docker-compose.yml", title="el healthcheck no cubre /tmp", line=76
    )

    result = match(case, [otro])

    assert result.expected == {}
    assert result.missing == ("fuga",)
    assert result.unmatched == (otro,)


def test_sin_linea_los_fragmentos_siguen_siendo_conjuntivos() -> None:
    # El fichero solo no ancla: `settings.py:230` casaba integracion-2,
    # integracion-3 y R6 a la vez con fragmentos disyuntivos (ADR-0025).
    case = _case(
        expected=[Label(id="A", match=("MODELS_CONFIG", "LLM_"), file="src/settings.py")],
    )
    parcial = _finding(
        fid="parcial", file="src/settings.py", title="MODELS_CONFIG cambia el default", line=230
    )
    completo = _finding(
        fid="completo",
        file="src/settings.py",
        title="MODELS_CONFIG cambia el default",
        why="la via LLM_ deja de usarse",
        line=230,
    )

    assert match(case, [parcial]).expected == {}
    assert match(case, [completo]).expected == {"A": completo}


def test_una_etiqueta_por_linea_basta_con_un_fragmento() -> None:
    case = _case(
        expected=[
            Label(id="A", match=("mark_done", "enqueue"), file="src/x.py", line=95),
        ],
    )
    finding = _finding(title="mark_done se adelanta", why="", line=95)

    assert match(case, [finding]).expected == {"A": finding}


def test_un_hallazgo_que_casa_expected_y_must_not_es_error() -> None:
    # Sin prioridad silenciosa: el mismo hallazgo no puede ser acierto y FP.
    case = _case(
        expected=[Label(id="A", match=("tope de intentos",), file="src/x.py", line=10)],
        must_not_report=[Label(id="R3", match=("distinto",), file="src/x.py", line=10)],
    )
    finding = _finding(title="tope de intentos distinto", line=10)

    with pytest.raises(ValueError, match="A.*R3|R3.*A"):
        match(case, [finding])


def test_cargar_etiquetas_indistinguibles_falla(tmp_path: Path) -> None:
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
            "expected": [{"id": "A", "file": "src/x.py", "line": 10, "match": ["boom"]}],
            "must_not_report": [
                {"id": "B", "file": "src/x.py", "line": 12, "match": ["boom"], "why": "inventado"}
            ],
        },
    )

    with pytest.raises(ValueError, match="indistinguibles"):
        load_case(tmp_path)


def test_cargar_linea_sin_fichero_falla(tmp_path: Path) -> None:
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
            "expected": [{"id": "A", "line": 10, "match": ["boom"]}],
        },
    )

    with pytest.raises(ValueError, match="'line' sin 'file'"):
        load_case(tmp_path)


def test_cargar_id_repetido_falla(tmp_path: Path) -> None:
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
            "expected": [{"id": "A", "match": ["uno"]}],
            "must_not_report": [{"id": "A", "match": ["dos"], "why": "w"}],
        },
    )

    with pytest.raises(ValueError, match="repetido"):
        load_case(tmp_path)


# --- E3: metricas y JSON determinista ---


def _audit_case() -> Case:
    """El caso de la auditoria: 7 reales (`expected`), H `acceptable`, 6 falsos (B–G).

    A, I e integracion-1..3 mas no-detectado-1..3 son los siete hallazgos reales
    que fijo la revision del codigo; B–G son los falsos publicados.
    """
    return _case(
        expected=[
            Label(id="A", match=("mark_done",), severity="P2", subset="correctness"),
            Label(id="I", match=("heartbeat",), severity="P3", subset="correctness"),
            Label(id="integracion-1", match=("secretos",), severity="P1", subset="integracion"),
            Label(id="integracion-2", match=("arranca",), severity="P1", subset="integracion"),
            Label(id="integracion-3", match=("pisa",), severity="P1", subset="integracion"),
            Label(id="no-detectado-1", match=("probe",), severity="P2", subset="no_detectado"),
            Label(id="no-detectado-2", match=("json_mode",), severity="P2", subset="no_detectado"),
            Label(id="no-detectado-3", match=("TTL",), severity="P2", subset="no_detectado"),
        ],
        must_not_report=[
            Label(id=fid, match=(f"falso {fid.lower()}",)) for fid in ("B", "C", "D", "E", "F", "G")
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
        findings.append(
            _finding(fid=fid, title=f"falso {fid.lower()}", severity=Severity.P2, line=10 + index)
        )
    return findings


def test_precision_y_recall_de_la_auditoria() -> None:
    report = measure(_audit_case(), _audit_published())

    assert report.counts["published"] == 9
    assert report.counts["tp"] == 2
    assert report.counts["fp"] == 6
    assert report.counts["acceptable"] == 1
    assert report.counts["fp_unknown"] == 0
    assert report.counts["fn"] == 6
    assert report.metrics["precision"] == 0.25
    assert report.metrics["recall"] == 0.25
    assert report.metrics["recall_by_subset"] == {
        "correctness": 1.0,
        "integracion": 0.0,
        "no_detectado": 0.0,
    }
    # A se publico como P1 pero es P2: casan 2 de severidad exacta (I tambien
    # discrepa, P1 vs P3) -> 0/2. La accuracy mide casados con la severidad del
    # `expected`, asi que con ambos mal da 0.0.
    assert report.metrics["severity_accuracy"] == 0.0


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


def test_el_informe_desglosa_el_gasto_y_las_operaciones() -> None:
    # 023 M1: el total de tokens no dice donde se paga ni si el modelo usa las
    # palancas; el informe lleva los tres conceptos y el conteo por herramienta.
    case = _case(expected=[Label(id="A", match=("mark_done",))])
    published = [_finding(fid="a", title="mark_done", line=1)]

    report = measure(
        case,
        published,
        tokens=1000,
        usage={"prompt": 700, "completion": 300, "reasoning": 120},
        operations={"read_file:outline": 2, "grep:regex": 1},
    )

    assert report.usage == {"prompt": 700, "completion": 300, "reasoning": 120}
    assert report.operations == {"grep:regex": 1, "read_file:outline": 2}
    # El desglose explica el total, no lo sustituye: `cost_per_tp` sigue saliendo
    # de `tokens`, que es la suma que ya usaba el presupuesto.
    assert report.metrics["cost_per_tp_tokens"] == 1000.0


def test_las_omisiones_se_cuentan_por_motivo() -> None:
    # Criterio 12: una omision por presupuesto cuesta recall y no puede quedar
    # escondida en un numero global de cobertura.
    case = _case(expected=[Label(id="A", match=("mark_done",))])

    report = measure(
        case,
        [],
        skipped={
            "a.py": SkipReason.BUDGET,
            "b.py": SkipReason.BUDGET,
            "c.py": SkipReason.TOO_LARGE,
            "d.py": SkipReason.IGNORED,
        },
    )

    assert report.skipped == {"budget": 2, "ignored": 1, "too_large": 1}


def test_sin_instrumentacion_la_medida_es_vacia_no_cero() -> None:
    # Un informe sin `usage` no declara coste 0: no declara nada.
    case = _case(expected=[Label(id="A", match=("mark_done",))])

    report = measure(case, [])

    assert report.usage == {}
    assert report.operations == {}
    assert report.skipped == {}
    assert report.cache == {"cached": None, "ratio": None}


def test_el_informe_declara_el_uso_del_cache() -> None:
    # 024 S4: una sola columna, `cached_tokens / prompt_tokens`, por review.
    case = _case(expected=[Label(id="A", match=("mark_done",))])

    report = measure(
        case,
        [],
        usage={"prompt": 100, "completion": 50, "reasoning": 10, "cached": 25},
    )

    assert report.usage["cached"] == 25
    assert report.cache == {"cached": 25, "ratio": 0.25}


def test_cache_no_reportado_es_no_medido_y_no_cero() -> None:
    # 024 E3: el proveedor puede no reportarlo; un 0 falso cerraria la pregunta.
    case = _case(expected=[Label(id="A", match=("mark_done",))])

    report = measure(case, [], usage={"prompt": 100, "completion": 50, "cached": None})

    assert report.cache == {"cached": None, "ratio": None}


def test_el_cache_se_desglosa_por_lote_y_un_turno_mudo_lo_deja_no_medido() -> None:
    # Criterio 7 de 024: por lote. Un turno sin el campo en un lote lo invalida:
    # parcial no es medido, y el lote sin `batch_index` no se agrupa.
    case = _case()
    recording = Recording(case=case.name, case_fingerprint=case_fingerprint(case), calls={})
    for prompt, cached, batch in (
        ("a", 5, 0),
        ("b", 5, 0),
        ("c", 2, 1),
        ("d", None, 1),
        ("e", 9, None),
    ):
        record_call(
            recording,
            role="correctness",
            system="S",
            prompt=prompt,
            max_tokens=4096,
            reply=LlmReply(text="{}", prompt_tokens=10, completion_tokens=1, cached_tokens=cached),
            batch_index=batch,
        )

    assert harness.cache_by_batch(recording) == {
        "0": {"cached": 10, "ratio": 0.5},
        "1": {"cached": None, "ratio": None},
    }


def test_una_grabacion_sin_orden_no_inventa_lotes() -> None:
    # Una grabacion heredada no dice a que lote pertenece cada llamada (023 E0e).
    case = _case()
    recording = Recording(
        case=case.name,
        case_fingerprint=case_fingerprint(case),
        calls={},
        order=None,
    )

    assert harness.cache_by_batch(recording) == {}
    assert harness.cache_by_batch(None) == {}


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


def test_fingerprint_mismatch_es_la_excepcion_de_produccion() -> None:
    # 023 T0: el harness la importa de `ports.llm`; no queda una copia en tests.
    from agent_review.ports.llm import FingerprintMismatch as ProductionFingerprintMismatch

    assert FingerprintMismatch is ProductionFingerprintMismatch


def test_replay_aborta_si_cambia_el_max_findings() -> None:
    case = _case()
    llm = ReplayLlm(_recording(case, role="correctness", max_tokens=4096), case, role="correctness")

    with pytest.raises(FingerprintMismatch):
        llm.complete(system="S", prompt="DIFF:\nx", max_tokens=2048)


def test_replay_aborta_si_cambia_el_esquema_de_salida() -> None:
    """E4: otro `response_schema` cambia lo que el modelo devolveria; no se reutiliza."""
    case = _case()
    recording = Recording(case=case.name, case_fingerprint=case_fingerprint(case), calls={})
    record_call(
        recording,
        role="correctness",
        system="S",
        prompt="DIFF:\nx",
        max_tokens=4096,
        reply=LlmReply(text='{"findings": []}'),
        json_mode="json_schema",
        response_schema={"type": "object", "properties": {"findings": {}}},
    )
    llm = ReplayLlm(recording, case, role="correctness")

    with pytest.raises(FingerprintMismatch):
        llm.complete(
            system="S",
            prompt="DIFF:\nx",
            max_tokens=4096,
            json_mode="json_schema",
            response_schema={"type": "object", "properties": {"hallazgos": {}}},
        )


def test_replay_aborta_si_cambia_el_modo_json() -> None:
    case = _case()
    llm = ReplayLlm(_recording(case, role="correctness"), case, role="correctness")

    with pytest.raises(FingerprintMismatch):
        llm.complete(system="S", prompt="DIFF:\nx", max_tokens=4096, json_mode="json_object")


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


def test_la_grabacion_guarda_el_cache_y_lo_devuelve(tmp_path: Path) -> None:
    # 024 S3: escribir sin leer dejaria el informe en «no medido» para siempre.
    case = _case()
    recording = Recording(case=case.name, case_fingerprint=case_fingerprint(case), calls={})
    record_call(
        recording,
        role="correctness",
        system="S",
        prompt="DIFF:\nx",
        max_tokens=4096,
        reply=LlmReply(
            text='{"findings": []}', prompt_tokens=10, completion_tokens=20, cached_tokens=7
        ),
        batch_index=0,
    )
    path = tmp_path / "recorded.json"
    path.write_text(recording.dump(), encoding="utf-8")

    reply = ReplayLlm(Recording.load(path), case, role="correctness").complete(
        system="S", prompt="DIFF:\nx", max_tokens=4096
    )

    assert reply.cached_tokens == 7


def test_una_grabacion_sin_el_campo_es_no_medido_y_sigue_casando(tmp_path: Path) -> None:
    # 024 S3/S5: las grabaciones anteriores no cambian de fingerprint y no se les
    # inventa un 0 en el cache.
    case = _case()
    payload = json.loads(_recording(case, role="correctness").dump())
    for call in payload["order"]:
        call.pop("cached_tokens", None)
    path = tmp_path / "recorded.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    llm = ReplayLlm(Recording.load(path), case, role="correctness")
    reply = llm.complete(system="S", prompt="DIFF:\nx", max_tokens=4096)

    assert reply.cached_tokens is None


def test_s_no_cambia_el_fingerprint_de_un_prompt_conocido() -> None:
    # 024 S5: S no toca `system`, `prompt`, `max_tokens` ni el contrato. Si este
    # sha cambia, la grabacion de `pr_3` deja de casar: es un bug de S.
    assert (
        prompt_fingerprint(role="correctness", system="S", prompt="DIFF:\nx", max_tokens=4096)
        == "sha256:8809aac4b2fbc0dc5378db67de91c6d4e92917fa8abd57daf46454597fe4b3f8"
    )


# --- Runner: graba el modelo real y reproduce en replay por el grafo real ---


class _FakePort:
    """Modelo falso: devuelve un hallazgo valido anclado al diff de juguete."""

    def __init__(self, payload: str) -> None:
        self._payload = payload
        self.calls = 0

    def complete(self, *, system, prompt, max_tokens, json_mode=None, response_schema=None):
        del system, prompt, max_tokens, json_mode, response_schema
        self.calls += 1
        return LlmReply(text=self._payload, prompt_tokens=100, completion_tokens=50)


def _toy_case(tmp_path: Path) -> Case:
    _write_case(
        tmp_path,
        {
            "case": "toy",
            "repo": "r",
            "pr_id": 1,
            "base_sha": "b",
            "head_sha": "h",
            "captured_at": "2026-10-16",
            "source": "s",
            "scope": {"max_files_per_pr": 10},
        },
    )
    return load_case(tmp_path)


def _pipeline_policy() -> PolicyEngine:
    return PolicyEngine({"version": 1, "max_autonomy": "report", "guards": {}})


def _finding_payload() -> str:
    return json.dumps(
        {
            "findings": [
                {
                    "title": "Amor no validado",
                    "severity": "P2",
                    "file": "x.py",
                    "line": 1,
                    "why_it_matters": "Se escribe sin comprobar",
                    "autofix_class": "advisory",
                    "owner": "human",
                    "failure_scenario": "escenario de prueba",
                    "confidence": 50,
                    "evidence": ["x.py:1 amor"],
                }
            ]
        }
    )


def test_runner_graba_y_reproduce_el_mismo_conjunto(tmp_path: Path) -> None:
    case = _toy_case(tmp_path)
    recording = Recording(case=case.name, case_fingerprint=case_fingerprint(case), calls={})

    vivo = RecordingLlm(_FakePort(_finding_payload()), recording, role="correctness")
    primera = run_case(case, llm=vivo, policy=_pipeline_policy(), vcs=CaseVcs(case))

    assert len(recording.calls) == 1, "la corrida con modelo real debe grabar su llamada"
    assert [finding.id for finding in primera.published] == ["correctness-x-py-1-amor-no-validado"]
    assert primera.tokens == 150

    replay = ReplayLlm(recording, case, role="correctness")
    segunda = run_case(case, llm=replay, policy=_pipeline_policy(), vcs=CaseVcs(case))

    assert [finding.id for finding in segunda.published] == [
        finding.id for finding in primera.published
    ]


# --- E0e (023): orden, unicidad y lote de cada llamada ---------------------------


def _recorded(case: Case, calls: int, *, texts: tuple[str, ...] = ()) -> Recording:
    recording = Recording(case=case.name, case_fingerprint=case_fingerprint(case), calls={})
    for index in range(calls):
        record_call(
            recording,
            role="correctness",
            system="S",
            prompt="DIFF:\nx",
            max_tokens=4096,
            reply=LlmReply(
                text=texts[index] if index < len(texts) else '{"findings": []}',
                prompt_tokens=10,
                completion_tokens=20,
            ),
            batch_index=0,
            turn=index,
        )
    return recording


def test_la_grabacion_conserva_el_orden_y_no_colapsa_prompts_iguales(tmp_path: Path) -> None:
    # Dos turnos con el mismo prompt antes se pisaban en el `dict` y el replay
    # fallaba con «ya se consumio» (E0e).
    case = _case()
    recording = _recorded(case, 2, texts=('{"turno": 1}', '{"turno": 2}'))
    path = tmp_path / "recorded.json"
    path.write_text(recording.dump(), encoding="utf-8")

    loaded = Recording.load(path)
    assert loaded.measurable_by_batch
    assert [call.seq for call in loaded.order or ()] == [1, 2]
    assert [call.turn for call in loaded.order or ()] == [0, 1]
    assert len({call.fingerprint for call in loaded.order or ()}) == 1, "mismo prompt"
    assert len({call.call_id for call in loaded.order or ()}) == 2, "dos llamadas"

    llm = ReplayLlm(loaded, case, role="correctness")
    primera = llm.complete(system="S", prompt="DIFF:\nx", max_tokens=4096)
    segunda = llm.complete(system="S", prompt="DIFF:\nx", max_tokens=4096)

    assert (primera.text, segunda.text) == ('{"turno": 1}', '{"turno": 2}')
    with pytest.raises(FingerprintMismatch, match="ya se consumio"):
        llm.complete(system="S", prompt="DIFF:\nx", max_tokens=4096)


def test_una_grabacion_sin_orden_se_reproduce_pero_no_se_mide_por_lote(
    tmp_path: Path,
) -> None:
    # Grabaciones anteriores a E0e: se reproducen por fingerprint, pero nadie les
    # inventa lote ni turno (Q de la spec).
    case = _case()
    fingerprint = prompt_fingerprint(
        role="correctness", system="S", prompt="DIFF:\nx", max_tokens=4096
    )
    path = tmp_path / "vieja.json"
    path.write_text(
        json.dumps(
            {
                "case": case.name,
                "case_fingerprint": case_fingerprint(case),
                "calls": {
                    fingerprint: {
                        "role": "correctness",
                        "text": '{"findings": []}',
                        "prompt_tokens": 10,
                        "completion_tokens": 20,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = Recording.load(path)

    assert not loaded.measurable_by_batch
    assert loaded.order is None
    llm = ReplayLlm(loaded, case, role="correctness")
    assert llm.complete(system="S", prompt="DIFF:\nx", max_tokens=4096).text == '{"findings": []}'
    with pytest.raises(FingerprintMismatch, match="ya se consumio"):
        llm.complete(system="S", prompt="DIFF:\nx", max_tokens=4096)


def test_el_tracker_cuenta_lotes_y_turnos_y_reinicia_en_el_reintento() -> None:
    from agent_review.ports.reasoning import ReasoningEvent

    tracker = harness.BatchTracker()
    start = ReasoningEvent(kind="start", role="correctness", batch=1, total=2)

    tracker.emit("r:1", start)
    assert [tracker.take() for _ in range(3)] == [(0, 0), (0, 1), (0, 2)]

    # Un reintento del mismo lote vuelve a empezar en el turno 0.
    tracker.emit("r:1", start)
    assert tracker.take() == (0, 0)

    tracker.emit("r:1", ReasoningEvent(kind="start", role="correctness", batch=2, total=2))
    assert tracker.take() == (1, 0)
    # Fuera del `start` no se etiqueta nada (p. ej. el verificador).
    assert harness.BatchTracker().take() == (None, None)


def test_el_bucle_con_herramientas_graba_su_turno(tmp_path: Path) -> None:
    # El turno no se deduce del prompt: lo cuenta el `start` que emite el revisor.
    from agent_review.ports.context import ContextLimits

    case = _case_con_fixtures(tmp_path)
    tools = json.dumps({"tools": [{"tool": "read_file", "path": "src/x.py"}]})
    recording = Recording(case=case.name, case_fingerprint=case_fingerprint(case), calls={})
    tracker = harness.BatchTracker()
    llm = RecordingLlm(
        _ScriptedPort(tools, _finding_payload()), recording, role="correctness", tracker=tracker
    )

    run_case(
        case,
        llm=llm,
        policy=_pipeline_policy(),
        vcs=CaseVcs(case),
        reader_factory=harness.fixture_reader_factory(case),
        tool_limits=ContextLimits(),
    )

    assert [(call.batch_index, call.turn) for call in recording.order or ()] == [(0, 0), (0, 1)]


# --- E10/E11: cobertura instrumentada ---


def _case_con_clases() -> Case:
    """Caso con un fichero de cada clase: code, config, test y docs."""
    diff = (
        "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
        "@@ -0,0 +1 @@\n+x = 1\n"
        "diff --git a/docker-compose.yml b/docker-compose.yml\n--- a/docker-compose.yml\n"
        "+++ b/docker-compose.yml\n@@ -0,0 +1 @@\n+services: {}\n"
        "diff --git a/tests/test_app.py b/tests/test_app.py\n--- a/tests/test_app.py\n"
        "+++ b/tests/test_app.py\n@@ -0,0 +1 @@\n+def test(): ...\n"
        "diff --git a/README.md b/README.md\n--- a/README.md\n"
        "+++ b/README.md\n@@ -0,0 +1 @@\n+# hola\n"
    )
    return Case(
        name="clases",
        repo="r",
        pr_id=1,
        base_sha="b",
        head_sha="h",
        captured_at="2026-10-16",
        source="s",
        scope={},
        diff=diff,
    )


def test_la_cobertura_clasifica_igual_que_la_review() -> None:
    # Una sola clasificacion (012): la metrica usa `scope.classify`, la misma con la
    # que la review prioriza y decide si es parcial.
    from agent_review.domain.models import FileClass
    from agent_review.scope import classify

    assert classify("src/app.py") is FileClass.CODE
    assert classify("docker-compose.yml") is FileClass.CONFIG
    assert classify("config/models.yaml") is FileClass.CONFIG
    assert classify(".env.example") is FileClass.CONFIG
    assert classify("tests/test_app.py") is FileClass.TEST
    assert classify("docs/adr/0001-x.md") is FileClass.DOCS


def test_cobertura_cuenta_solo_codigo_y_configuracion() -> None:
    case = _case_con_clases()

    # Vistos code + config de 2 relevantes; test y docs no cuentan en el denominador.
    assert coverage_of(case, ["src/app.py", "docker-compose.yml"]) == 1.0
    assert coverage_of(case, ["src/app.py"]) == 0.5
    # Los ficheros de test/docs vistos no inflan la cobertura.
    assert coverage_of(case, ["tests/test_app.py", "README.md"]) == 0.0


def test_cobertura_none_si_no_hay_codigo_ni_configuracion() -> None:
    diff = (
        "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -0,0 +1 @@\n+x\n"
    )
    case = _case_con_clases()
    solo_docs = Case(**{**case.__dict__, "diff": diff})

    assert coverage_of(solo_docs, []) is None


def test_el_runner_expoe_los_ficheros_revisados(tmp_path: Path) -> None:
    case = _toy_case(tmp_path)
    port = _FakePort(_finding_payload())
    run = run_case(case, llm=port, policy=_pipeline_policy(), vcs=CaseVcs(case))

    # El diff de juguete solo trae x.py; sale de la `ReviewCoverage` del grafo (012).
    assert run.reviewed == ("x.py",)


class _RotoPort:
    """Modelo que falla siempre con un error permanente (no se reintenta)."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system, prompt, max_tokens, json_mode=None, response_schema=None):
        del system, prompt, max_tokens, json_mode, response_schema
        self.calls += 1
        raise LlmError("respuesta irreparable")


def test_replay_con_un_lote_fallido_aborta_en_vez_de_medir(tmp_path: Path) -> None:
    # R2c (023 T0c): «no se reviso» no es «no hay hallazgos». En replay, publicar
    # el informe convertiria un fallo de la grabacion en un recall bajo y verde.
    case = _toy_case(tmp_path)

    with pytest.raises(IncompleteMeasurement, match="x.py"):
        evaluate(case, llm=_RotoPort(), policy=_pipeline_policy(), mode="replay")


def test_en_vivo_un_lote_fallido_sigue_dando_informe(tmp_path: Path) -> None:
    # La misma corrida en vivo es exploratoria: un lote caido no invalida el
    # experimento, solo lo deja parcial (y la cobertura lo declara).
    case = _toy_case(tmp_path)

    report = evaluate(case, llm=_RotoPort(), policy=_pipeline_policy(), mode="live")

    assert report.mode == "live"
    assert report.counts["tp"] == 0


# --- 014 RH10: fixtures del caso y camino con herramientas ------------------------


def _case_con_fixtures(tmp_path: Path) -> Case:
    """Caso con `fixtures/head` y `fixtures/base`: el arbol que sirve el lector."""
    _write_case(
        tmp_path,
        {
            "case": "c",
            "repo": "r",
            "pr_id": 1,
            "base_sha": "B",
            "head_sha": "H",
            "captured_at": "2026-10-16",
            "source": "s",
        },
    )
    (tmp_path / "fixtures" / "head" / "src").mkdir(parents=True)
    (tmp_path / "fixtures" / "base" / "src").mkdir(parents=True)
    (tmp_path / "fixtures" / "head" / "src" / "x.py").write_text("amor = 2\n", encoding="utf-8")
    (tmp_path / "fixtures" / "base" / "src" / "x.py").write_text("amor = 1\n", encoding="utf-8")
    return load_case(tmp_path)


def test_el_caso_carga_sus_fixtures(tmp_path: Path) -> None:
    case = _case_con_fixtures(tmp_path)
    assert case.fixtures is not None and case.fixtures.name == "head"
    assert case.base_fixtures is not None

    otro = tmp_path / "otro"
    otro.mkdir()
    sin_fixtures = _toy_case(otro)
    assert sin_fixtures.fixtures is None


def test_case_vcs_sirve_los_ficheros_del_caso(tmp_path: Path) -> None:
    # L1 (008) y el lector de herramientas ven el mismo arbol: el VCS del caso
    # responde con el fichero versionado, no con "".
    case = _case_con_fixtures(tmp_path)
    vcs = CaseVcs(case)

    assert vcs.get_file("r", "src/x.py", ref="H") == "amor = 2\n"
    assert vcs.get_file("r", "src/x.py", ref="B") == "amor = 1\n"
    assert vcs.get_file("r", "src/no-existe.py", ref="H") == ""
    assert vcs.get_file("r", ".env", ref="H") == ""


def test_la_fabrica_de_fixtures_sirve_el_arbol(tmp_path: Path) -> None:
    case = _case_con_fixtures(tmp_path)
    factory = harness.fixture_reader_factory(case)
    assert factory is not None
    reader = factory(case.repo, case.head_sha, case.base_sha, case.diff)

    head_read = reader.read_file("src/x.py")
    base_read = reader.read_file("src/x.py", ref="base")
    assert head_read is not None and head_read.content == "amor = 2\n"
    assert base_read is not None and base_read.content == "amor = 1\n"


def test_sin_fixtures_no_hay_fabrica() -> None:
    assert harness.fixture_reader_factory(_case_vacia()) is None


def _case_vacia() -> Case:
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
    )


class _ScriptedPort:
    """Modelo falso con respuestas en orden (guion de herramientas → findings)."""

    def __init__(self, *payloads: str) -> None:
        self._payloads = list(payloads)
        self.calls = 0

    def complete(
        self, *, system, prompt, max_tokens, json_mode=None, response_schema=None, on_reasoning=None
    ):
        del system, prompt, max_tokens, json_mode, response_schema, on_reasoning
        index = min(self.calls, len(self._payloads) - 1)
        self.calls += 1
        return LlmReply(text=self._payloads[index], prompt_tokens=100, completion_tokens=50)


def test_el_runner_con_herramientas_publica_por_el_grafo(tmp_path: Path) -> None:
    # 014 RH4/RH10: con fabrica, el grafo corre el bucle; el hallazgo sigue anclado.
    from agent_review.ports.context import ContextLimits

    case = _case_con_fixtures(tmp_path)
    tools = json.dumps({"tools": [{"tool": "read_file", "path": "src/x.py"}]})
    port = _ScriptedPort(tools, _finding_payload())
    factory = harness.fixture_reader_factory(case)
    run = run_case(
        case,
        llm=port,
        policy=_pipeline_policy(),
        vcs=CaseVcs(case),
        reader_factory=factory,
        tool_limits=ContextLimits(),
    )

    assert port.calls == 2, "un turno de herramientas y el final"
    assert [finding.id for finding in run.published] == ["correctness-x-py-1-amor-no-validado"]
