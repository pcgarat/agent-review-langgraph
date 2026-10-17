"""Corrida real del caso de evaluacion (E11 de la iniciativa 006).

Es el paso previo a aprobar un cambio de prompt o de modelo: corre el caso contra
el modelo real, emite el informe y compara con el baseline. n=1 y a mano, sin
bloquear CI (el modelo no es determinista, E15): vive fuera de `make test` por la
marca `live` (`addopts = "-m 'not live'"`). El comando directo es `make eval-live`.

Comprueba que el caso corre de punta a punta contra el puerto real sin romper y que
el informe se puede comparar con el baseline. No afirma un numero: el modelo no es
determinista y n=1 (E15), de modo que nadie lea una sola corrida como una tendencia.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import harness
import pytest

from agent_review.models_registry import ModelRegistry
from agent_review.policy.engine import PolicyEngine
from agent_review.settings import get_settings

pytestmark = pytest.mark.live

ROOT = Path(__file__).parents[2]
CASE_DIR = ROOT / "tests" / "eval" / "pr_3"
BASELINE = CASE_DIR / "expected_report.json"
ROLE = "correctness"


def _policy(case: harness.Case) -> PolicyEngine:
    engine = PolicyEngine.from_file(ROOT / "config" / "autonomy.yaml")
    return engine


@pytest.mark.skipif(
    not os.environ.get("LLM_API_KEY"),
    reason="define LLM_API_KEY para la corrida real",
)
def test_la_corrida_real_emite_el_informe_y_compara() -> None:
    """Corre el caso contra el modelo real y calcula el informe con su delta (E15)."""
    case = harness.load_case(CASE_DIR)
    registry = ModelRegistry.from_settings(get_settings())
    port = registry.port_for(ROLE)
    assert port is not None, f"el rol {ROLE!r} no tiene modelo declarado"

    report = harness.evaluate(case, llm=port, policy=_policy(case), mode="live")

    assert report.case == case.name
    # `coverage` sale de la `ReviewCoverage` del grafo (012): lo que el modelo
    # llego a leer, independiente de lo que respondiera.
    assert report.metrics["coverage"] is not None
    assert harness.compare(report, _load_report(BASELINE)) in tuple(harness.Verdict)


def _load_report(path: Path) -> harness.Report:
    data = json.loads(path.read_text(encoding="utf-8"))
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
