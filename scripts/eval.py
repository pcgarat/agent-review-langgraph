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
from agent_review.ports.context import ContextLimits  # noqa: E402
from agent_review.settings import Settings  # noqa: E402

CASE_DIR = ROOT / "tests" / "eval" / "pr_3"
ROLE = "correctness"


def _tool_limits(settings: Settings) -> ContextLimits:
    return ContextLimits(
        max_turns=settings.review_tools_max_turns,
        max_calls=settings.review_tools_max_calls,
        max_bytes=settings.review_tools_max_bytes,
    )


def _reader_factory(case: harness.Case, settings: Settings) -> harness.ReviewerReaderFactory | None:
    """Fabrica del caso si el rol esta activado y hay fixtures (014 RH10/RH11).

    `REVIEW_TOOLS_ROLES=correctness` activa el mismo opt-in que el worker; sin
    fixtures versionados o con el rol fuera de la lista, el caso corre por el
    camino de 008 (una llamada por lote), que es el default de la medida.
    """
    if ROLE not in settings.review_tools_roles_list:
        return None
    if case.fixtures is None:
        raise SystemExit(
            f"el caso no trae `fixtures/head`: no se puede medir {ROLE} con herramientas; "
            "versiona los ficheros del caso (Q4 de 014) o quita el rol de REVIEW_TOOLS_ROLES"
        )
    return harness.fixture_reader_factory(case)


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
    parser.add_argument(
        "--recording",
        default="recorded.json",
        help=(
            "fichero de grabacion del caso (por defecto recorded.json). La variante "
            "con herramientas graba aparte (recorded_tools.json) para no pisar el "
            "baseline de 008 (014 RH11)"
        ),
    )
    args = parser.parse_args(argv)

    case = harness.load_case(args.case)
    case_dir = Path(args.case)
    settings = Settings()
    reader_factory = _reader_factory(case, settings)
    tool_limits = _tool_limits(settings) if reader_factory is not None else None
    recording_path = case_dir / args.recording

    if args.mode == "record":
        return _record(
            case,
            recording_path,
            reader_factory=reader_factory,
            tool_limits=tool_limits,
        )
    if args.mode == "live":
        return _report(
            case,
            llm=_live_llm(recording=None),
            mode="live",
            compare=args.compare,
            reader_factory=reader_factory,
            tool_limits=tool_limits,
        )
    recording = harness.Recording.load(recording_path)
    llm = harness.ReplayLlm(recording, case, role=ROLE)
    return _report(
        case,
        llm=llm,
        mode="replay",
        compare=args.compare,
        recording=recording,
        reader_factory=reader_factory,
        tool_limits=tool_limits,
    )


def _record(
    case: harness.Case,
    recording_path: Path,
    *,
    reader_factory: harness.ReviewerReaderFactory | None,
    tool_limits: ContextLimits | None,
) -> int:
    recording = harness.Recording(
        case=case.name,
        case_fingerprint=harness.case_fingerprint(case),
        calls={},
    )
    report = _evaluate(
        case,
        llm=_ProgressLlm(_live_llm(recording=recording), role=ROLE),
        mode="live",
        recording=recording,
        reader_factory=reader_factory,
        tool_limits=tool_limits,
    )
    recording_path.write_text(recording.dump(), encoding="utf-8")
    print(
        f"Grabadas {len(recording.calls)} llamada(s) en {recording_path} "
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
    reader_factory: harness.ReviewerReaderFactory | None = None,
    tool_limits: ContextLimits | None = None,
) -> int:
    report = _evaluate(
        case,
        llm=llm,
        mode=mode,
        recording=recording,
        reader_factory=reader_factory,
        tool_limits=tool_limits,
    )
    print(harness.to_json(report), end="")
    if compare:
        baseline = _load_report(Path(compare))
        print(f"\nVeredicto frente a {compare}: {harness.compare(report, baseline).value}")
    return 0


def _evaluate(
    case: harness.Case,
    *,
    llm,
    mode: str,
    recording: harness.Recording | None = None,
    reader_factory: harness.ReviewerReaderFactory | None = None,
    tool_limits: ContextLimits | None = None,
):
    return harness.evaluate(
        case,
        llm=llm,
        policy=_case_policy(case),
        mode=mode,
        recording=recording,
        reader_factory=reader_factory,
        tool_limits=tool_limits,
    )


def _case_policy(case: harness.Case) -> PolicyEngine:
    """Politica del caso: la versionada. Desde 012 el `scope` no recorta por numero."""
    return PolicyEngine.from_file(ROOT / "config" / "autonomy.yaml")


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


class _ProgressLlm:
    """Envuelve el puerto de grabacion y deja una linea por llamada en stderr.

    Una regrabacion del PR #3 tarda ~20 min sin ninguna senal intermedia; esto
    dice por donde va (llamadas y tokens) sin tocar la grabacion ni el informe.
    """

    def __init__(self, inner, *, role: str) -> None:
        self._inner = inner
        self._role = role
        self._calls = 0

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def complete(self, **kwargs):
        reply = self._inner.complete(**kwargs)
        self._calls += 1
        print(
            f"  [{self._calls}] {self._role}: {reply.total_tokens} tokens "
            f"(finish={reply.finish_reason or '?'})",
            file=sys.stderr,
            flush=True,
        )
        return reply


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
