"""Regresion en CI de la review del PR #3 (E8 de la iniciativa 006).

Corre el caso en *replay* dentro de `pytest`: sin red ni credenciales. El informe
se compara con `expected_report.json`, el baseline congelado, de modo que una
deriva de contrato, merge o politica se ve como un fallo con el nombre del
hallazgo que se movio.

La corrida contra el modelo real *no* vive aqui: es `make eval-live` (marca `live`
en `test_live_eval.py`), fuera de CI.
"""

from __future__ import annotations

import json
from pathlib import Path

import harness
from harness import ReplayLlm, case_fingerprint, compare, to_json

from agent_review.policy.engine import PolicyEngine

CASE_DIR = Path(__file__).parent / "pr_3"
BASELINE = CASE_DIR / "expected_report.json"


def _policy(case: harness.Case) -> PolicyEngine:
    """Politica del caso: la versionada con el `scope` congelado (E10)."""
    engine = PolicyEngine.from_file(Path(__file__).parents[2] / "config" / "autonomy.yaml")
    max_files = case.scope.get("max_files_per_pr")
    if max_files is not None:
        engine.guards["max_files_per_pr"] = int(max_files)
    return engine


def _replay_report(case: harness.Case) -> harness.Report:
    recording = harness.Recording.load(CASE_DIR / "recorded.json")
    llm = ReplayLlm(recording, case, role="correctness")
    return harness.evaluate(case, llm=llm, policy=_policy(case), mode="replay", recording=recording)


def _baseline() -> harness.Report:
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    return harness.Report(
        case=data["case"],
        mode=data["mode"],
        fingerprints=data.get("fingerprints", {}),
        counts=data.get("counts", {}),
        metrics=data.get("metrics", {}),
        matched=tuple(data.get("matched", [])),
        unmatched=tuple(data.get("unmatched", [])),
        missing=tuple(data.get("missing", [])),
        acceptable=tuple(data.get("acceptable", [])),
        inverted=tuple(data.get("inverted", [])),
    )


def test_el_caso_carga_con_su_procedencia() -> None:
    case = harness.load_case(CASE_DIR)

    assert case.pr_id == 3
    assert case.head_sha == "008da57"
    assert case.scope["max_files_per_pr"] == 10


def test_la_grabacion_casa_el_caso() -> None:
    case = harness.load_case(CASE_DIR)
    recording = harness.Recording.load(CASE_DIR / "recorded.json")

    assert recording.case_fingerprint == case_fingerprint(case)


def test_replay_reproduce_el_baseline_byte_a_byte() -> None:
    case = harness.load_case(CASE_DIR)

    report = _replay_report(case)
    frozen = BASELINE.read_text(encoding="utf-8")

    assert to_json(report) == frozen, (
        "el replay no reproduce el baseline congelado; revisa el diff del informe "
        "para ver que hallazgo se movio"
    )


def test_replay_es_determinista() -> None:
    case = harness.load_case(CASE_DIR)

    assert to_json(_replay_report(case)) == to_json(_replay_report(case))


def test_el_baseline_recoge_los_numeros_de_la_auditoria() -> None:
    case = harness.load_case(CASE_DIR)
    report = _replay_report(case)

    assert report.counts["fp_unknown"] == 0, "todo hallazgo publicado debe estar etiquetado"
    assert report.counts["fp_labeled"] == 6
    assert report.counts["tp"] == 2
    # 7 hallazgos reales (A, I, integracion 1-3, no-detectado 1-3); solo 2 se
    # publicaron. El recall global y el de los dos subconjuntos faltantes caen a 0.
    assert report.counts["fn"] == 6
    assert report.metrics["precision"] == 0.25
    assert report.metrics["recall"] == 0.25
    assert report.metrics["recall_by_subset"]["integracion"] == 0.0
    assert report.metrics["recall_by_subset"]["no_detectado"] == 0.0
    # A se publico como P1 pero es P2 (el TTL recupera la review): severidad mal.
    assert report.metrics["severity_accuracy"] == 0.5
    assert report.inverted == ("A",)


def test_sin_deriva_frente_al_baseline() -> None:
    case = harness.load_case(CASE_DIR)

    assert compare(_replay_report(case), _baseline()) is harness.Verdict.NO_CONCLUYENTE
