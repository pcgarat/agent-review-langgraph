#!/usr/bin/env python3
"""CLI del harness de evaluacion de la review (iniciativa 006).

Tres modos, mapeados a los comandos `make`:

- `report` (`make eval`): corre el caso en *replay* (sin red) e imprime el
  informe. Con `EVAL_COMPARE=expected_report.json` anade el delta por dominancia.
- `record` (`make eval-record`): corre el caso contra el modelo real y regraba
  `recorded.json`. Es el unico camino que produce grabaciones.
- `live`   (`make eval-live`): corre el caso contra el modelo real y compara con
  el baseline, sin escribir ni bloquear (paso previo a aprobar un prompt).

El harness vive en `tests/eval/harness.py` (sin `__init__.py`); este script anade
esa carpeta a `sys.path` para importarlo como modulo hermano (spec §5.1).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "eval"))

import harness  # noqa: E402  (depende del sys.path de arriba)

from agent_review.models_registry import ModelRegistry  # noqa: E402
from agent_review.policy.engine import PolicyEngine  # noqa: E402
from agent_review.settings import Settings  # noqa: E402

CASE_DIR = ROOT / "tests" / "eval" / "pr_3"
ROLE = "correctness"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluacion de la review (006)")
    parser.add_argument(
        "mode",
        nargs="?",
        default="report",
        choices=("report", "record", "live"),
        help="report (replay), record (regraba) o live (modelo real)",
    )
    parser.add_argument("--case", default=str(CASE_DIR), help="directorio del caso")
    parser.add_argument(
        "--compare",
        default=None,
        help="ruta del baseline (expected_report.json) para el delta",
    )
    args = parser.parse_args(argv)

    case = harness.load_case(args.case)
    case_dir = Path(args.case)

    if args.mode == "record":
        return _record(case, case_dir)
    if args.mode == "live":
        return _report(case, llm=_live_llm(recording=None), mode="live", compare=args.compare)
    recording = harness.Recording.load(case_dir / "recorded.json")
    llm = harness.ReplayLlm(recording, case, role=ROLE)
    return _report(case, llm=llm, mode="replay", compare=args.compare, recording=recording)


def _record(case: harness.Case, case_dir: Path) -> int:
    recording = harness.Recording(
        case=case.name,
        case_fingerprint=harness.case_fingerprint(case),
        calls={},
    )
    report = _evaluate(case, llm=_live_llm(recording=recording), mode="live", recording=recording)
    (case_dir / "recorded.json").write_text(recording.dump(), encoding="utf-8")
    print(
        f"Grabadas {len(recording.calls)} llamada(s) en {case_dir / 'recorded.json'} "
        f"· {len(report.matched) + len(report.unmatched)} hallazgo(s) publicado(s)"
    )
    return 0


def _report(
    case: harness.Case,
    *,
    llm,
    mode: str,
    compare: str | None,
    recording: harness.Recording | None = None,
) -> int:
    report = _evaluate(case, llm=llm, mode=mode, recording=recording)
    print(harness.to_json(report), end="")
    if compare:
        baseline = _load_report(Path(compare))
        print(f"\nVeredicto frente a {compare}: {harness.compare(report, baseline).value}")
    return 0


def _evaluate(case: harness.Case, *, llm, mode: str, recording: harness.Recording | None = None):
    return harness.evaluate(
        case, llm=llm, policy=_case_policy(case), mode=mode, recording=recording
    )


def _case_policy(case: harness.Case) -> PolicyEngine:
    """Politica del caso: la versionada con los guards congelados en `scope` (E10).

    El caso corre con el `max_files_per_pr` con el que se audito; sin fijarlo, el
    *replay* miraria mas (o menos) ficheros que la review original.
    """
    engine = PolicyEngine.from_file(ROOT / "config" / "autonomy.yaml")
    max_files = case.scope.get("max_files_per_pr")
    if max_files is not None:
        engine.guards["max_files_per_pr"] = int(max_files)
    return engine


def _live_llm(*, recording: harness.Recording | None):
    settings = Settings()
    registry = ModelRegistry.from_settings(settings)
    port = registry.port_for(ROLE)
    if port is None:
        raise SystemExit(
            f"El rol {ROLE!r} no tiene modelo: declara LLM_* o MODELS_CONFIG con "
            "LLM_REVIEWER_ENABLED=true, o pasa credenciales para `make eval-record`."
        )
    if recording is None:
        return port
    return harness.RecordingLlm(port, recording, role=ROLE)


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


if __name__ == "__main__":
    raise SystemExit(main())
