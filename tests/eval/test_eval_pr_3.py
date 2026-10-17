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
    """Politica del caso: la versionada. Desde 012 el `scope` no recorta por numero."""
    engine = PolicyEngine.from_file(Path(__file__).parents[2] / "config" / "autonomy.yaml")
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


def test_el_baseline_recoge_los_numeros_de_la_regrabacion() -> None:
    # Regrabacion del 2026-10-17 (021): deepseek-v4-flash con razonamiento alto,
    # lotes de 20 000, diff numerado y troceo por hunks, grabada en una review real en
    # el stack. La auditoria original (9 publicados, 2 TP, precision 0.25, cobertura
    # 0.2) vive en la spec de 006 §3.1; sin razonamiento (012) eran 0 TP.
    case = harness.load_case(CASE_DIR)
    report = _replay_report(case)

    assert report.counts["fp_unknown"] == 0, "todo hallazgo publicado debe estar etiquetado"
    assert report.metrics["coverage"] == 1.0
    # Tres reales: A (en la direccion correcta), integracion-1 y no-detectado-1.
    assert report.counts["published"] == 10
    assert report.counts["tp"] == 3
    assert report.counts["tp_inverted"] == 0
    assert report.counts["fp_labeled"] == 5
    assert report.counts["acceptable"] == 2
    assert report.counts["fn"] == 5
    assert report.metrics["precision"] == 0.375
    assert report.metrics["recall"] == 0.375
    assert report.inverted == ()


def test_el_pr_3_entero_llega_al_revisor() -> None:
    # 012 criterio 1: sin `max_files_per_pr` ni el corte a 60000 chars, la
    # configuracion que dejo fuera la review auditada entra en la review.
    case = harness.load_case(CASE_DIR)
    recording = harness.Recording.load(CASE_DIR / "recorded.json")
    llm = ReplayLlm(recording, case, role="correctness")
    run = harness.run_case(case, llm=llm, policy=_policy(case), vcs=harness.CaseVcs(case))

    reviewed = set(run.reviewed)
    for path in (
        "docker-compose.yml",
        "src/agent_review/settings.py",
        ".env.example",
        "src/agent_review/worker/main.py",
    ):
        assert path in reviewed, f"{path} no llego al revisor"


def test_sin_deriva_frente_al_baseline() -> None:
    case = harness.load_case(CASE_DIR)

    assert compare(_replay_report(case), _baseline()) is harness.Verdict.NO_CONCLUYENTE
