"""Regresion en CI de la review del PR #3 (E8 de la iniciativa 006).

Corre el caso en *replay* dentro de `pytest`: sin red ni credenciales. El informe
se compara con `expected_report.json`, el baseline congelado, de modo que una
deriva de contrato, merge o politica se ve como un fallo con el nombre del
hallazgo que se movio.

El caso tiene **dos** configuraciones con baseline propio (023 E12b): sin
herramientas (`recorded.json` / `expected_report.json`) y con herramientas
(`recorded_tools.json` / `expected_report_tools.json`). No se comparan entre si:
miden escenarios distintos y el segundo se compara contra su congelado.

La corrida contra el modelo real *no* vive aqui: es `make eval-live` (marca `live`
en `test_live_eval.py`), fuera de CI.
"""

from __future__ import annotations

import json
from pathlib import Path

import harness
import pytest
from harness import ReplayLlm, case_fingerprint, compare, to_json

from agent_review.policy.engine import PolicyEngine
from agent_review.ports.context import ContextLimits
from agent_review.settings import Settings

CASE_DIR = Path(__file__).parent / "pr_3"
BASELINE = CASE_DIR / "expected_report.json"
TOOLS_RECORDING = CASE_DIR / "recorded_tools.json"
TOOLS_BASELINE = CASE_DIR / "expected_report_tools.json"

# La variante con herramientas necesita una grabacion *live* hecha con el *prompt*
# de capacidades de 023 F1 (C0c). Mientras no exista, el baseline no se puede
# congelar; el test se declara omitido en vez de fingir que mide.
_sin_baseline_con_herramientas = pytest.mark.skipif(
    not TOOLS_BASELINE.exists(),
    reason="C0c: falta el baseline con herramientas (regrabacion *live* de 023)",
)


def _policy(case: harness.Case) -> PolicyEngine:
    """Politica del caso: la versionada. Desde 012 el `scope` no recorta por numero."""
    engine = PolicyEngine.from_file(Path(__file__).parents[2] / "config" / "autonomy.yaml")
    return engine


def _replay_report(case: harness.Case) -> harness.Report:
    recording = harness.Recording.load(CASE_DIR / "recorded.json")
    llm = ReplayLlm(recording, case, role="correctness")
    return harness.evaluate(case, llm=llm, policy=_policy(case), mode="replay", recording=recording)


def _tools_report(case: harness.Case) -> harness.Report:
    """Variante con herramientas: el rol `correctness` abre el lector (014 RH10)."""
    settings = Settings()
    recording = harness.Recording.load(TOOLS_RECORDING)
    llm = ReplayLlm(recording, case, role="correctness")
    return harness.evaluate(
        case,
        llm=llm,
        policy=_policy(case),
        mode="replay",
        recording=recording,
        reader_factory=harness.fixture_reader_factory(case),
        tool_limits=ContextLimits(
            max_turns=settings.review_tools_max_turns,
            max_calls=settings.review_tools_max_calls,
            max_bytes=settings.review_tools_max_bytes,
        ),
    )


def _baseline(path: Path) -> harness.Report:
    data = json.loads(path.read_text(encoding="utf-8"))
    return harness.Report(
        case=data["case"],
        mode=data["mode"],
        fingerprints=data.get("fingerprints", {}),
        counts=data.get("counts", {}),
        metrics=data.get("metrics", {}),
        usage=data.get("usage", {}),
        operations=data.get("operations", {}),
        skipped=data.get("skipped", {}),
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
    # Regrabacion del 2026-10-17 (014 RH10): deepseek-v4-flash con razonamiento alto y
    # el bucle de lectura de herramientas sobre el arbol `fixtures/head`. La auditoria
    # original (9 publicados, 2 TP, precision 0.25, cobertura 0.2) vive en la spec de
    # 006 §3.1. Con L1 completo y el lector el modelo resuelve el PR entero (cobertura
    # 1.0) y reconoce tres reales, pero fabrica siete FP de orquestacion (R1-R6) que
    # ve por leer el worker completo; el detalle del veredicto esta en `labels.yaml`.
    case = harness.load_case(CASE_DIR)
    report = _replay_report(case)

    assert report.counts["fp_unknown"] == 0, "todo hallazgo publicado debe estar etiquetado"
    assert report.metrics["coverage"] == 1.0
    # Tres reales: integracion-1 (fuga del fix-worker), integracion-3 (MODELS_CONFIG)
    # y no-detectado-1 (clasificacion por substring del mensaje de probe).
    assert report.counts["published"] == 10
    assert report.counts["tp"] == 3
    assert report.counts["tp_inverted"] == 0
    assert report.counts["fp_labeled"] == 7
    assert report.counts["acceptable"] == 0
    assert report.counts["fn"] == 5
    assert report.metrics["precision"] == 0.3
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

    assert compare(_replay_report(case), _baseline(BASELINE)) is harness.Verdict.NO_CONCLUYENTE


def test_el_baseline_fija_el_gasto_por_concepto() -> None:
    # 023 M1: el informe separa *prompt*, *completion* y razonamiento para poder
    # comparar configuraciones; el total de antes no decia donde se pagaba.
    report = _replay_report(harness.load_case(CASE_DIR))

    assert report.usage == {"prompt": 145442, "completion": 328318, "reasoning": 295600}
    assert report.usage["reasoning"] > 0, "el gold set tiene razonamiento: debe contarse"
    # Sin herramientas no hay peticiones servidas ni ficheros omitidos: el caso
    # entra entero (cobertura 1.0), asi que un `skipped` no vacio seria una fuga.
    assert report.operations == {}
    assert report.skipped == {}


@_sin_baseline_con_herramientas
def test_replay_con_herramientas_reproduce_su_baseline_byte_a_byte() -> None:
    report = _tools_report(harness.load_case(CASE_DIR))

    assert to_json(report) == TOOLS_BASELINE.read_text(encoding="utf-8"), (
        "el replay con herramientas no reproduce su baseline; revisa el diff para ver "
        "que hallazgo o que lectura se movio"
    )


@_sin_baseline_con_herramientas
def test_el_baseline_con_herramientas_declara_gasto_y_lecturas() -> None:
    # 023 M1: la tabla por configuracion sale de aqui. Un informe con herramientas
    # sin peticiones servidas seria una medida que no midio el bucle.
    report = _tools_report(harness.load_case(CASE_DIR))

    assert report.usage["prompt"] > 0
    assert report.usage["completion"] > 0
    assert report.operations, "la variante con herramientas debe contar lecturas"
    assert all(key.startswith(("read_file:", "grep:", "list_dir")) for key in report.operations), (
        f"operacion sin herramienta reconocible: {sorted(report.operations)}"
    )
    # El baseline con herramientas **no** sustituye al de 008: son escenarios
    # distintos (E12b) y cada informe apunta a su congelado.
    assert report.mode == "replay"
    assert compare(report, _baseline(TOOLS_BASELINE)) is harness.Verdict.NO_CONCLUYENTE
