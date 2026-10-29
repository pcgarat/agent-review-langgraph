"""Harness offline de evaluacion: carga un caso, lo corre y mide la review.

Puro y sin red: no conoce el grafo ni el VCS; los recibe. Un *caso* es un
directorio versionado con `labels.yaml` (procedencia, `scope` y etiquetas),
`diff.patch` (el diff auditado) y, mas adelante, `recorded.json` (las respuestas
del modelo) y `expected_report.json` (el baseline congelado).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from agent_review.adapters.context_fixture import FixtureContextReader
from agent_review.adapters.diff_parser import parse_unified_diff, resolve_added_line
from agent_review.adapters.llm_reviewer import LlmReviewer, ReviewerReaderFactory
from agent_review.adapters.llm_verifier import LlmVerifier
from agent_review.domain.models import PARTIAL_CLASSES, Finding, SkipReason
from agent_review.graph.build import build_graph
from agent_review.policy.engine import PolicyEngine
from agent_review.ports.context import DEFAULT_CONTEXT_DENYLIST, ContextLimits, is_denied
from agent_review.ports.llm import (
    FingerprintMismatch,
    JsonMode,
    LlmError,
    LlmPort,
    LlmReply,
    LlmTransientError,
    LlmTruncatedError,
)
from agent_review.ports.reasoning import ReasoningEvent
from agent_review.ports.vcs import InlineAnchor, PullRequestInfo, ReviewMarker
from agent_review.ports.verifier import VerifierPort
from agent_review.scope import changed_files, classify

# Tres etiquetas (E9 de la spec): lo que debe salir, lo que no, y lo tolerable.
LBL_EXPECTED = "expected"
LBL_MUST_NOT = "must_not_report"
LBL_ACCEPTABLE = "acceptable"

_WHITESPACE = re.compile(r"\s+")

# Tolerancia de lineas al casar `file`/`line`: la misma que el merge entre
# personas (ADR-0014). Un ancla no tiene por que coincidir al caracter.
LINE_TOLERANCE = 5

# Orden canonico de atribucion: severidad, luego linea, luego id (spec §5.2).
_SEVERITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def normalize(text: str) -> str:
    """Forma canonica para casar contenido: minusculas y espacios colapsados.

    Un `match` es una frase distintiva, no un literal: el modelo cambia el
    espaciado y las mayusculas entre corridas. No se quitan tildes ni
    puntuacion a proposito: eso ensancharia el matcher y decidiria el numero
    por accidente (riesgo de la spec §6).
    """
    return _WHITESPACE.sub(" ", text.lower()).strip()


@dataclass(frozen=True)
class Label:
    """Una etiqueta del caso.

    `match` son frases **conjuntivas** (todas deben aparecer). `file`/`line` son
    opcionales: cuando estan, exigen fichero y ±5 lineas (ADR-0014); cuando no,
    casa por contenido. `severity`/`subset` solo los usan los `expected`;
    `must_mention` captura un fallo descrito al reves (solo el caso A).
    """

    id: str
    match: tuple[str, ...]
    why: str = ""
    severity: str | None = None
    subset: str = "correctness"
    file: str | None = None
    line: int | None = None
    must_mention: tuple[str, ...] = ()


@dataclass(frozen=True)
class Case:
    """Un caso del *gold set* con su procedencia y sus etiquetas."""

    name: str
    repo: str
    pr_id: int
    base_sha: str
    head_sha: str
    captured_at: str
    source: str
    scope: dict[str, Any]
    diff: str
    expected: tuple[Label, ...] = ()
    must_not_report: tuple[Label, ...] = ()
    acceptable: tuple[Label, ...] = ()
    # Arboles del caso (Q4 de 014): `fixtures/head` y `fixtures/base` a los `sha`
    # declarados arriba. Sin ellos, el caso no se puede medir con herramientas ni
    # ensamblar L1 fiel; el replay del caso de juguete no los necesita.
    fixtures: Path | None = None
    base_fixtures: Path | None = None

    def labels(self) -> tuple[Label, ...]:
        return (*self.expected, *self.must_not_report, *self.acceptable)


@dataclass(frozen=True)
class Report:
    """Informe de una corrida. Estable y serializable (claves ordenadas, 4 decimales).

    Los `id` de `matched`/`unmatched`/`missing` existen para que el fallo de CI
    diga **que** hallazgo se movio, no solo que un numero cambio. `usage` desglosa
    el gasto (023 M1): sin separar *prompt*, *completion* y razonamiento no se
    puede decidir que palanca del bucle se paga. `cache` es el uso de la cache del
    proveedor (`cached_tokens / prompt_tokens`, 024 §5.4) y `cache_by_batch` su
    desglose por lote; `None` es «no medido», nunca 0. `operations` cuenta las
    peticiones servidas por herramienta y modo, para saber si el modelo usa
    `outline` o `regex` en vez de suponerlo. `skipped` cuenta los ficheros que la
    review dejo fuera, por motivo: una omision por presupuesto tambien cuesta
    recall (criterio 12).
    """

    case: str
    mode: str
    fingerprints: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    metrics: dict[str, float | None] = field(default_factory=dict)
    usage: dict[str, int | None] = field(default_factory=dict)
    cache: dict[str, float | int | None] = field(default_factory=dict)
    cache_by_batch: dict[str, dict[str, float | int | None]] = field(default_factory=dict)
    operations: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    matched: tuple[str, ...] = ()
    unmatched: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    acceptable: tuple[str, ...] = ()
    inverted: tuple[str, ...] = ()


def load_case(directory: str | Path) -> Case:
    """Carga un caso versionado desde su directorio.

    El `diff.patch` es obligatorio (sin diff no hay review que medir). Las
    etiquetas se validan al cargar (ADR-0025): una etiqueta mal anclada o un
    solapamiento `expected`/`must_not_report` falla aqui, no al medir.
    """
    base = Path(directory)
    data = _read_yaml(base / "labels.yaml")
    diff = (base / "diff.patch").read_text(encoding="utf-8")
    scope = data.get("scope") or {}
    head_root = base / "fixtures" / "head"
    base_root = base / "fixtures" / "base"
    case = Case(
        name=str(data["case"]),
        repo=str(data["repo"]),
        pr_id=int(data["pr_id"]),
        base_sha=str(data["base_sha"]),
        head_sha=str(data["head_sha"]),
        captured_at=str(data["captured_at"]),
        source=str(data.get("source", "")),
        scope=dict(scope),
        diff=diff,
        expected=tuple(_label(item) for item in data.get(LBL_EXPECTED, [])),
        must_not_report=tuple(_label(item) for item in data.get(LBL_MUST_NOT, [])),
        acceptable=tuple(_label(item) for item in data.get(LBL_ACCEPTABLE, [])),
        fixtures=head_root if head_root.is_dir() else None,
        base_fixtures=base_root if base_root.is_dir() else None,
    )
    _validate_labels(case)
    return case


def _validate_labels(case: Case) -> None:
    """Valida el anclaje de las etiquetas (ADR-0025).

    Reglas que se comprueban **al cargar**: `id` unico, `line` solo con `file`,
    toda etiqueta con fragmentos y ninguna pareja `expected`/`must_not_report`
    indistinguible (mismo anclaje y mismos fragmentos), que seria un hallazgo
    contado a la vez como acierto y como falso positivo.
    """
    seen: set[str] = set()
    for label in case.labels():
        if label.id in seen:
            raise ValueError(f"id de etiqueta repetido en el caso: {label.id!r}")
        seen.add(label.id)
        if label.line is not None and label.file is None:
            raise ValueError(
                f"la etiqueta {label.id!r} declara 'line' sin 'file': una linea de otro "
                "fichero no ancla nada"
            )
        if not label.match:
            raise ValueError(
                f"la etiqueta {label.id!r} no declara 'match': sin un fragmento "
                "discriminante no se puede distinguir de otro defecto"
            )
    for expected in case.expected:
        for must_not in case.must_not_report:
            if _indistinguishable(expected, must_not):
                raise ValueError(
                    f"las etiquetas {expected.id!r} (expected) y {must_not.id!r} "
                    "(must_not_report) son indistinguibles: mismo anclaje y mismos "
                    "fragmentos; un hallazgo contaria a la vez como acierto y como FP"
                )


def _indistinguishable(left: Label, right: Label) -> bool:
    """¿Dos etiquetas de distinta clase no se pueden separar al medir?"""
    if left.file != right.file:
        return False
    if (left.line is None) != (right.line is None):
        return False
    if left.line is not None and right.line is not None:
        assert left.line is not None and right.line is not None
        if abs(left.line - right.line) > LINE_TOLERANCE:
            return False
    return {normalize(fragment) for fragment in left.match} == {
        normalize(fragment) for fragment in right.match
    }


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: se esperaba un mapa YAML")
    return data


def _label(item: dict[str, Any]) -> Label:
    return Label(
        id=str(item["id"]),
        match=tuple(str(value) for value in item.get("match", [])),
        why=str(item.get("why", "")),
        severity=item.get("severity"),
        subset=str(item.get("subset", "correctness")),
        file=item.get("file"),
        line=item.get("line"),
        must_mention=tuple(str(value) for value in item.get("must_mention", [])),
    )


@dataclass(frozen=True)
class MatchResult:
    """Atribucion de los hallazgos publicados contra las etiquetas del caso.

    Cada `expected`/`must_not_report` casa con **como mucho un** hallazgo: la
    atribucion es 1:1 para que un matcher laxo no cuente el mismo hallazgo dos
    veces. `acceptable` no participa en la atribucion (E9): se lista aparte.
    """

    expected: dict[str, Finding]
    must_not: dict[str, Finding]
    acceptable: dict[str, Finding]
    unmatched: tuple[Finding, ...]
    missing: tuple[str, ...]


def match(case: Case, published: Sequence[Finding]) -> MatchResult:
    """Atribuye `published` contra las etiquetas del caso (1:1 determinista, ADR-0025).

    El orden no depende de como llegaron los hallazgos: ambos lados se ordenan en
    forma canonica (severidad, linea, id) y los pares (etiqueta, hallazgo) se
    asignan por numero de fragmentos casados. El empate lo rompe el orden
    declarado de la etiqueta y, despues, el orden canonico del hallazgo.

    Un hallazgo que casa a la vez un `expected` y un `must_not_report` es un caso
    ambiguo: no puede ser acierto y falso positivo en la misma medida, asi que
    **falla** en vez de dejar que `expected` gane por orden de atribucion.
    """
    _validate_labels(case)
    findings = sorted(published, key=_finding_order)
    _reject_overlap(case, findings)
    used: set[int] = set()
    expected = _attribute(case.expected, findings, used)
    must_not = _attribute(case.must_not_report, findings, used)
    acceptable = _attribute(case.acceptable, findings, used)
    unmatched = tuple(finding for finding in findings if id(finding) not in used)
    missing = tuple(label.id for label in case.expected if label.id not in expected)
    return MatchResult(
        expected=expected,
        must_not=must_not,
        acceptable=acceptable,
        unmatched=unmatched,
        missing=missing,
    )


def _reject_overlap(case: Case, findings: Sequence[Finding]) -> None:
    """Un hallazgo que casa `expected` y `must_not_report` invalida la medida."""
    for finding in findings:
        hits_expected = [label.id for label in case.expected if _score(label, finding) is not None]
        hits_must_not = [
            label.id for label in case.must_not_report if _score(label, finding) is not None
        ]
        if hits_expected and hits_must_not:
            raise ValueError(
                f"el hallazgo {finding.id!r} casa a la vez las etiquetas "
                f"expected {hits_expected} y must_not_report {hits_must_not}: "
                "el caso es ambiguo (ADR-0025); separa el anclaje o los fragmentos"
            )


def _attribute(
    labels: tuple[Label, ...], findings: list[Finding], used: set[int]
) -> dict[str, Finding]:
    """Asigna cada etiqueta a su mejor hallazgo no consumido (ADR-0025).

    La puntuacion es el numero de fragmentos casados: la etiqueta mas especifica
    gana el hallazgo aunque este declarada despues. Un hallazgo se consume una
    sola vez para que un matcher laxo no lo cuente dos veces.
    """
    pairs: list[tuple[int, int, int]] = []
    for label_index, label in enumerate(labels):
        for finding_index, finding in enumerate(findings):
            score = _score(label, finding)
            if score is None or id(finding) in used:
                continue
            pairs.append((-score, label_index, finding_index))
    pairs.sort()
    result: dict[str, Finding] = {}
    for _, label_index, finding_index in pairs:
        label = labels[label_index]
        finding = findings[finding_index]
        if label.id in result or id(finding) in used:
            continue
        result[label.id] = finding
        used.add(id(finding))
    return result


def _score(label: Label, finding: Finding) -> int | None:
    """Fragmentos casados, o `None` si el hallazgo no casa la etiqueta (ADR-0025).

    `file`/`line` son el ancla primaria. Con `line` declarada basta **un**
    fragmento discriminante; sin linea (o sin fichero) el conjunto de fragmentos
    es la unica ancla y exige **todos**, como en 006.
    """
    if label.file is not None and finding.file != label.file:
        return None
    if label.line is not None and abs(finding.line - label.line) > LINE_TOLERANCE:
        return None
    text = normalize(f"{finding.title} {finding.why_it_matters}")
    hits = sum(1 for fragment in label.match if normalize(fragment) in text)
    if label.file is not None and label.line is not None:
        return hits or None
    return hits if hits == len(label.match) else None


def _finding_order(finding: Finding) -> tuple[int, int, str]:
    """Orden canonico de atribucion: severidad, linea, id (spec §5.2)."""
    return (_SEVERITY_RANK.get(str(finding.severity), 9), finding.line, finding.id)


# El subconjunto de tooling mide a la herramienta, no al LLM: queda fuera de la
# precision y el recall globales y se reporta aparte (E14 de la spec).
SUBSET_TOOLING = "tooling"


def measure(
    case: Case,
    published: Sequence[Finding],
    *,
    mode: str = "replay",
    tokens: int | None = None,
    coverage: float | None = None,
    fingerprints: Mapping[str, str] | None = None,
    usage: Mapping[str, int | None] | None = None,
    operations: Mapping[str, int] | None = None,
    skipped: Mapping[str, SkipReason] | None = None,
    cache_by_batch: Mapping[str, Mapping[str, float | int | None]] | None = None,
) -> Report:
    """Traduce una corrida en el informe de seis metricas (E3, spec §5.4).

    `tokens` es el gasto del run (del `usage` del proveedor, E13) y `coverage` la
    fraccion de ficheros vistos por el `LlmReviewer` (E11); ambos llegan de fuera
    porque el harness puro no corre el grafo. Hasta que exista la instrumentacion,
    `coverage=None` (nunca 0: «no medido» no es «no visto»). `usage`, `operations`
    y `skipped` son la medida de coste de 023 M1: no entran en precision ni en
    recall, solo explican de donde sale el numero. `cache` (y su desglose por lote)
    es el uso de la cache del proveedor de 024 §5.4, con `None` cuando no se mide.
    """
    result = match(case, published)
    inverted = _inverted(case.expected, result.expected)
    all_tp = result.expected
    tooling_tp = [label for label, _ in all_tp.items() if _subset(label, case) == SUBSET_TOOLING]
    tp_global = len(all_tp) - len(tooling_tp)
    fps = _fps(result, case)
    fp_global = [item for item in fps if item[1] != SUBSET_TOOLING]
    expected_global = [label for label in case.expected if label.subset != SUBSET_TOOLING]

    counts = {
        "published": len(published),
        "tp": len(all_tp),
        "fp": len(fps),
        "acceptable": len(result.acceptable),
        "fp_labeled": len(result.must_not),
        "fp_unknown": len(result.unmatched),
        "fn": len(result.missing),
        "fp_p0_p1": sum(1 for _, _, finding in fps if str(finding.severity) in ("P0", "P1")),
        "tp_inverted": len(inverted),
    }
    metrics: dict[str, float | None] = {
        "precision": _ratio(tp_global, tp_global + len(fp_global)),
        "recall": _ratio(tp_global, len(expected_global)),
        "recall_by_subset": _recall_by_subset(case, all_tp),
        "severity_accuracy": _severity_accuracy(case, all_tp, len(all_tp)),
        "cost_per_tp_tokens": _cost(tokens, len(all_tp)),
        "coverage": round(coverage, 4) if coverage is not None else None,
    }
    return Report(
        case=case.name,
        mode=mode,
        fingerprints=dict(sorted((fingerprints or {}).items())),
        counts=counts,
        metrics=metrics,
        usage=dict(sorted((usage or {}).items())),
        cache=_cache_report(int((usage or {}).get("prompt") or 0), (usage or {}).get("cached")),
        cache_by_batch={
            index: dict(sorted(entry.items()))
            for index, entry in sorted((cache_by_batch or {}).items())
        },
        operations=dict(sorted((operations or {}).items())),
        skipped=dict(sorted(_skipped_by_reason(skipped).items())),
        matched=tuple(sorted(all_tp)),
        unmatched=tuple(sorted(finding.id for finding in result.unmatched)),
        missing=tuple(sorted(result.missing)),
        acceptable=tuple(sorted(result.acceptable)),
        inverted=tuple(sorted(inverted)),
    )


def _skipped_by_reason(skipped: Mapping[str, SkipReason] | None) -> dict[str, int]:
    """Ficheros omitidos por motivo (023 M1): `budget`, `too_large`, `ignored`, `failed`.

    Se cuentan los ficheros, no los lotes: el presupuesto corta antes de agrupar,
    asi que una omision de `budget` es exactamente lo que el recall pierde.
    """
    counts: dict[str, int] = {}
    for reason in (skipped or {}).values():
        counts[str(reason)] = counts.get(str(reason), 0) + 1
    return counts


def _cache_report(prompt: int, cached: int | None) -> dict[str, float | int | None]:
    """Uso de la cache del proveedor: `cached_tokens / prompt_tokens` (024 §5.4).

    `ratio` es `None` cuando el proveedor no reporta (`cached` es `None`) **o**
    cuando no hubo prompt: «no medido» no es 0. El ratio se redondea a cuatro
    decimales para que el baseline sea comparable byte a byte.
    """
    ratio = None
    if cached is not None and prompt > 0:
        ratio = round(cached / prompt, 4)
    return {"cached": cached, "ratio": ratio}


def cache_by_batch(
    recording: Recording | None,
) -> dict[str, dict[str, float | int | None]]:
    """Uso de la cache por lote, leido de la grabacion (024 §5.4, criterio 7).

    Solo se puede agrupar con `order`: una grabacion heredada no dice a que lote
    pertenece cada llamada y no se le inventa (023 E0e). Los turnos sin lote (el
    verificador) no se agrupan. Un lote donde **algun** turno no reporta cache
    queda con `cached=None`: parcial no es medido.
    """
    if recording is None or recording.order is None:
        return {}
    prompts: dict[int, int] = {}
    cached: dict[int, int | None] = {}
    for call in recording.order:
        if call.batch_index is None:
            continue
        index = call.batch_index
        prompts[index] = prompts.get(index, 0) + call.prompt_tokens
        previous = cached.get(index, 0)
        if previous is None or call.cached_tokens is None:
            cached[index] = None
        else:
            cached[index] = previous + call.cached_tokens
    return {
        str(index): _cache_report(prompts[index], cached.get(index)) for index in sorted(prompts)
    }


def to_json(report: Report) -> str:
    """Serializa el informe de forma **determinista** (E6): claves ordenadas, 4 decimales.

    Dos corridas del mismo caso dan el mismo texto byte a byte, de modo que el
    baseline congelado en `expected_report.json` se puede comparar con un `==`.
    """
    return json.dumps(asdict(report), sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def _subset(label_id: str, case: Case) -> str:
    for label in case.expected:
        if label.id == label_id:
            return label.subset
    return SUBSET_TOOLING


def _fps(result: MatchResult, case: Case) -> list[tuple[str, str, Finding]]:
    """Falsos positivos: `(id, subset, hallazgo)`. Un no etiquetado es `fp` (E5).

    Los `must_not_report` traen su subset (por defecto `correctness`); un hallazgo
    sin etiqueta no puede declararse tooling, asi que cuenta como global.
    """
    subsets = {label.id: label.subset for label in case.must_not_report}
    labeled = [
        (label_id, subsets.get(label_id, "correctness"), finding)
        for label_id, finding in result.must_not.items()
    ]
    unknown = [(finding.id, "correctness", finding) for finding in result.unmatched]
    return labeled + unknown


def _inverted(expected: Sequence[Label], matched: Mapping[str, Finding]) -> list[str]:
    """`expected` casados cuyo `must_mention` no se cumple (E16).

    Cuenta el hecho «descrito al reves» sin castigarlo: sigue siendo un `tp` y no
    mueve precision ni recall.
    """
    by_id = {label.id: label for label in expected}
    inverted: list[str] = []
    for label_id, finding in matched.items():
        label = by_id.get(label_id)
        if label is None or not label.must_mention:
            continue
        text = normalize(f"{finding.title} {finding.why_it_matters}")
        if not all(normalize(fragment) in text for fragment in label.must_mention):
            inverted.append(label_id)
    return inverted


def _recall_by_subset(case: Case, matched: Mapping[str, Finding]) -> dict[str, float]:
    """Recall por `subset` (incluye `tooling`, que se reporta aparte; E14)."""
    totals: dict[str, int] = {}
    hits: dict[str, int] = {}
    for label in case.expected:
        totals[label.subset] = totals.get(label.subset, 0) + 1
        if label.id in matched:
            hits[label.subset] = hits.get(label.subset, 0) + 1
    return {subset: _ratio(hits.get(subset, 0), total) for subset, total in sorted(totals.items())}


def _severity_accuracy(case: Case, matched: Mapping[str, Finding], tp: int) -> float | None:
    """Casados con la severidad del `expected` / `tp` (spec §5.4)."""
    by_id = {label.id: label for label in case.expected}
    correct = sum(
        1
        for label_id, finding in matched.items()
        if by_id[label_id].severity is not None
        and str(finding.severity) == by_id[label_id].severity
    )
    return _ratio(correct, tp)


def _cost(tokens: int | None, tp: int) -> float | None:
    """Coste por hallazgo verificado en tokens: `None` si no hay `tp` o no hay gasto."""
    if tokens is None or tp == 0:
        return None
    return round(tokens / tp, 4)


def _ratio(numerator: int, denominator: int) -> float | None:
    """Cociente redondeado a 4 decimales; `None` si el denominador es 0."""
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


class Verdict(StrEnum):
    """Resultado de comparar una corrida con el baseline (E6 de la spec)."""

    MEJORA = "mejora"
    EMPEORA = "empeora"
    NO_CONCLUYENTE = "no concluyente"


class _Trend(StrEnum):
    """Sentido del cambio de una metrica entre baseline y corrida."""

    MEJOR = "mejor"
    IGUAL = "igual"
    PEOR = "peor"
    DESCONOCIDO = "desconocido"


# Metricas donde subir es mejorar (spec §5.4). `fp_p0_p1` va aparte: menor es mejor.
_METRICS_HIGHER_BETTER = ("precision", "recall", "coverage")


def compare(current: Report, baseline: Report) -> Verdict:
    """Dominancia entre una corrida y el baseline (E6), sin poder hacer trampa.

    **Mejora** solo si ninguna metrica empeora (precision, recall y cobertura no
    bajan; los falsos `P0`/`P1` no suben) y al menos una mejora estrictamente.
    **Empeora** solo si ninguna mejora y al menos una empeora. El resto es **no
    concluyente**: subir el recall publicando ruido cae aqui, no en «mejora».
    Una metrica sin medir (`None`, p. ej. `coverage`) no cuenta como caida (E11).
    """
    trends = [
        _trend(baseline.metrics.get(name), current.metrics.get(name), higher_better=True)
        for name in _METRICS_HIGHER_BETTER
    ]
    trends.append(
        _trend(
            baseline.counts.get("fp_p0_p1"),
            current.counts.get("fp_p0_p1"),
            higher_better=False,
        )
    )
    if all(trend is not _Trend.PEOR for trend in trends) and any(
        trend is _Trend.MEJOR for trend in trends
    ):
        return Verdict.MEJORA
    if all(trend is not _Trend.MEJOR for trend in trends) and any(
        trend is _Trend.PEOR for trend in trends
    ):
        return Verdict.EMPEORA
    return Verdict.NO_CONCLUYENTE


def _trend(baseline: float | None, current: float | None, *, higher_better: bool) -> _Trend:
    """Compara una metrica. Un valor ausente en cualquiera de los dos es desconocido."""
    if baseline is None or current is None:
        return _Trend.DESCONOCIDO
    if current == baseline:
        return _Trend.IGUAL
    improves = current > baseline if higher_better else current < baseline
    return _Trend.MEJOR if improves else _Trend.PEOR


# ---------------------------------------------------------------------------
# Replay: puerto LLM con respuestas grabadas y fingerprint (E6).
# ---------------------------------------------------------------------------

# Lo que un caso mide cambia si cambia el prompt (rol + tope de hallazgos), el
# contrato de salida estructurada (`json_mode`, `response_schema`), el diff o el
# scope. El fingerprint los cubre: una grabacion vieja con un prompt o un esquema
# nuevos aborta en vez de medir el pasado en silencio (E4 de la spec).
_FINGERPRINT_VERSION = 2


def prompt_fingerprint(
    *,
    role: str,
    system: str,
    prompt: str,
    max_tokens: int,
    json_mode: JsonMode | None = None,
    response_schema: dict[str, Any] | None = None,
) -> str:
    """Huella estable de una llamada de un rol: rol + system + prompt + `max_tokens`
    + el contrato de salida (`json_mode` y `response_schema` canonico).

    El rol forma parte del fingerprint porque dos roles con el mismo diff y tope
    de hallazgos generan el mismo prompt (E4 de la spec): sin el, regrabar un rol
    invalidaria los demas o pasaria por valido sin serlo. El esquema entra porque
    cambiarlo cambia lo que el modelo devolveria: replay de la respuesta vieja
    mediria el pasado.
    """
    schema = json.dumps(response_schema, sort_keys=True) if response_schema is not None else ""
    mode = json_mode or ""
    digest = hashlib.sha256()
    for part in (_FINGERPRINT_VERSION, role, system, prompt, max_tokens, mode, schema):
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\x00")
    return f"sha256:{digest.hexdigest()}"


def case_fingerprint(case: Case) -> str:
    """Huella del caso: diff + scope. Un caso distinto no casa la grabacion."""
    digest = hashlib.sha256()
    digest.update(case.name.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(case.diff.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(json.dumps(case.scope, sort_keys=True).encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"


def fixture_reader_factory(case: Case) -> ReviewerReaderFactory | None:
    """Fabrica de lectura determinista sobre los fixtures del caso (014 H10).

    Con `fixtures/head` (y, si el caso lo trae, `fixtures/base`) devuelve un lector
    que sirve exactamente esos bytes turno a turno: el *replay* reproduce el
    *fingerprint* de cada turno (ADR-0017). Sin fixtures, `None`: el caso solo se
    puede correr en el camino de 008 (una llamada por lote).
    """
    if case.fixtures is None:
        return None

    def factory(repo: str, head_sha: str, base_sha: str, diff: str = "") -> FixtureContextReader:
        del repo, head_sha, base_sha, diff
        return FixtureContextReader(
            case.fixtures,
            base_root=case.base_fixtures,
            denylist=DEFAULT_CONTEXT_DENYLIST,
        )

    return factory


def fixture_verifier(case: Case, llm: LlmPort) -> LlmVerifier:
    """Verificador L2 sobre los fixtures del caso (009 / 014 RH11).

    Es el escenario de **produccion**: con verificador, el nodo `verify` corre
    entre `merge` y `policy` y un hallazgo `REFUTED` sale `DROP` (no se publica).
    Usa el mismo lector determinista que el revisor, de modo que la comparacion
    con la base (`ref="base"`) y los ficheros del diff estan disponibles.
    """
    limits = ContextLimits(max_calls=8, max_bytes=60_000)

    def open_reader(repo: str, head_sha: str, base_sha: str, diff: str) -> FixtureContextReader:
        del repo, head_sha, base_sha, diff
        return FixtureContextReader(
            case.fixtures,
            base_root=case.base_fixtures,
            denylist=DEFAULT_CONTEXT_DENYLIST,
        )

    return LlmVerifier(llm, open_reader=open_reader, limits=limits)


@dataclass(frozen=True)
class RecordedCall:
    """Una llamada grabada con su sitio en la corrida (E0e de 023).

    El fingerprint no basta como clave: dos llamadas pueden compartir prompt
    (dos turnos con el mismo texto, un reintento) y antes se colapsaban en el
    `dict`. `call_id` las distingue y `seq` fija el orden global; `batch_index` y
    `turn` dicen a que lote y a que turno del bucle pertenece.
    """

    seq: int
    call_id: int
    fingerprint: str
    role: str
    batch_index: int | None
    turn: int | None
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int | None = None
    finish_reason: str = ""
    # Un intento que **fallo** tambien ocupa su sitio: sin el, el replay se salta
    # una llamada que en vivo ocurrio y aborta por fingerprint (T0c). Se guarda
    # el tipo y el mensaje, y `ReplayLlm` levanta el mismo error.
    error: str = ""

    def entry(self) -> dict[str, Any]:
        """La respuesta en el formato del indice heredado (`calls`)."""
        return {
            "role": self.role,
            "text": self.text,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
            "finish_reason": self.finish_reason,
            "error": self.error,
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "call_id": self.call_id,
            "fingerprint": self.fingerprint,
            "batch_index": self.batch_index,
            "turn": self.turn,
            **self.entry(),
        }


@dataclass
class Recording:
    """Respuestas grabadas de un caso, con su orden (E0e de 023).

    `order` es la verdad: la lista de llamadas en el orden en que ocurrieron.
    `calls` es un indice por fingerprint (la primera ocurrencia de cada prompt)
    que conserva a los consumidores que solo necesitan saber que se grabo y con
    que rol. Una grabacion anterior a E0e no trae `order`: se puede reproducir
    por fingerprint, pero **no** se puede medir por lote y no se le inventa uno.
    """

    case: str
    case_fingerprint: str
    calls: dict[str, dict[str, Any]]
    order: list[RecordedCall] | None = field(default_factory=list)

    @property
    def measurable_by_batch(self) -> bool:
        """¿La grabacion declara lote y turno de cada llamada?"""
        return self.order is not None

    @classmethod
    def load(cls, path: str | Path) -> Recording:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        raw_order = data.get("order")
        if raw_order is None:
            return cls(
                case=str(data["case"]),
                case_fingerprint=str(data["case_fingerprint"]),
                calls={str(key): dict(value) for key, value in data.get("calls", {}).items()},
                order=None,
            )
        recorded = [_recorded_call(raw) for raw in raw_order]
        calls: dict[str, dict[str, Any]] = {}
        for call in recorded:
            calls.setdefault(call.fingerprint, call.entry())
        return cls(
            case=str(data["case"]),
            case_fingerprint=str(data["case_fingerprint"]),
            calls=calls,
            order=recorded,
        )

    def dump(self) -> str:
        # `order` manda; `calls` se deriva de el para que el indice no discrepe.
        payload: dict[str, Any] = {
            "case": self.case,
            "case_fingerprint": self.case_fingerprint,
            "calls": self.calls,
        }
        if self.order is not None:
            index: dict[str, int] = {}
            for call in self.order:
                index.setdefault(call.fingerprint, call.call_id)
            payload["calls"] = index
            payload["order"] = [call.to_json() for call in self.order]
        return json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def _recorded_call(raw: Mapping[str, Any]) -> RecordedCall:
    return RecordedCall(
        seq=int(raw.get("seq", 0)),
        call_id=int(raw.get("call_id", 0)),
        fingerprint=str(raw.get("fingerprint", "")),
        role=str(raw.get("role", "")),
        batch_index=None if raw.get("batch_index") is None else int(raw["batch_index"]),
        turn=None if raw.get("turn") is None else int(raw["turn"]),
        text=str(raw.get("text", "")),
        prompt_tokens=int(raw.get("prompt_tokens", 0)),
        completion_tokens=int(raw.get("completion_tokens", 0)),
        reasoning_tokens=int(raw.get("reasoning_tokens", 0)),
        cached_tokens=(int(raw["cached_tokens"]) if raw.get("cached_tokens") is not None else None),
        finish_reason=str(raw.get("finish_reason", "")),
        error=str(raw.get("error", "")),
    )


@dataclass
class Usage:
    """Gasto real de una corrida, desglosado por concepto (023 M1, 024 E9).

    El proveedor reporta el `usage` por llamada y nadie lo suma: el informe solo
    tenia el total. `reasoning` va **dentro** de `completion` en el proveedor y se
    cuenta aparte porque es lo que se paga dos veces al reenviar contexto
    (espec 023 §1). `cached` es el prefijo que el proveedor sirvio de su cache y
    queda en `None` si **alguna** llamada no lo reporto: sumar las que si lo
    hicieron daria un numero incompleto con aspecto de medido (024 E3).
    """

    prompt: int = 0
    completion: int = 0
    reasoning: int = 0
    cached: int | None = None
    unmeasured_cache: bool = False

    @property
    def total(self) -> int:
        return self.prompt + self.completion

    def add(self, reply: LlmReply) -> None:
        self.prompt += reply.prompt_tokens
        self.completion += reply.completion_tokens
        self.reasoning += reply.reasoning_tokens
        if reply.cached_tokens is None:
            self.unmeasured_cache = True
        else:
            self.cached = (self.cached or 0) + reply.cached_tokens

    def as_dict(self) -> dict[str, int | None]:
        return {
            "prompt": self.prompt,
            "completion": self.completion,
            "reasoning": self.reasoning,
            "cached": None if self.unmeasured_cache else self.cached,
        }


class ReplayLlm:
    """`LlmPort` de *replay*: devuelve las respuestas grabadas y aborta si no casan.

    Es el mismo puerto que el modelo real (E7 de la spec): lo unico que cambia
    entre *replay* y real es esta clase. Se construye **para un rol** (el `name`
    de la persona), de modo que cada uno tiene sus propias respuestas grabadas y
    regrabar un rol no invalida los demas. Cada llamada se comprueba contra el
    fingerprint del prompt (que incluye el rol) y del caso; un prompt cambiado o
    un caso distinto lanza `FingerprintMismatch` en vez de devolver una respuesta
    ajena.
    """

    def __init__(self, recording: Recording, case: Case, *, role: str) -> None:
        self._recording = recording
        self._role = role
        self._used: set[str] = set()
        # Con `order`, cada llamada grabada se sirve una vez y en su orden: dos
        # turnos con el mismo prompt dejan de confundirse (E0e).
        self._pending: dict[str, deque[RecordedCall]] = {}
        for call in recording.order or ():
            if call.role == role:
                self._pending.setdefault(call.fingerprint, deque()).append(call)
        # Tokens de las respuestas servidas: es el gasto del run (E13). El `usage`
        # real lo reporta el proveedor y viaja en la grabacion; en replay se suma.
        self.total_tokens = 0
        self.usage = Usage()
        current = case_fingerprint(case)
        if recording.case_fingerprint != current:
            raise FingerprintMismatch(
                f"el caso no casa la grabacion: {recording.case_fingerprint} != {current}; "
                f"regraba con `make eval-record`"
            )

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        json_mode: JsonMode | None = None,
        response_schema: dict[str, Any] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> LlmReply:
        # Una respuesta grabada no trae razonamiento: no hay nada que escuchar.
        del on_reasoning
        fingerprint = prompt_fingerprint(
            role=self._role,
            system=system,
            prompt=prompt,
            max_tokens=max_tokens,
            json_mode=json_mode,
            response_schema=response_schema,
        )
        entry = self._next_entry(fingerprint)
        reproducido = str(entry.get("error") or "")
        if reproducido:
            # El intento fallo en vivo y la grabacion lo conserva: se levanta el
            # mismo tipo de error para que el reintento del lote ocurra igual.
            raise _recorded_error(reproducido)
        reply = LlmReply(
            text=str(entry["text"]),
            prompt_tokens=int(entry.get("prompt_tokens", 0)),
            completion_tokens=int(entry.get("completion_tokens", 0)),
            reasoning_tokens=int(entry.get("reasoning_tokens", 0)),
            cached_tokens=(
                int(entry["cached_tokens"]) if entry.get("cached_tokens") is not None else None
            ),
            finish_reason=str(entry.get("finish_reason", "")),
        )
        self.total_tokens += reply.total_tokens
        self.usage.add(reply)
        return reply

    def _next_entry(self, fingerprint: str) -> Mapping[str, Any]:
        """La siguiente respuesta para ese prompt, o aborto si la grabacion no llega."""
        if self._recording.order is not None:
            queue = self._pending.get(fingerprint)
            if not queue:
                if fingerprint in self._recording.calls:
                    raise FingerprintMismatch(
                        f"el fingerprint {fingerprint} ya se consumio: la grabacion no "
                        f"cubre estos turnos; regraba con `make eval-record`"
                    )
                raise FingerprintMismatch(
                    f"el prompt del rol {self._role!r} no casa la grabacion "
                    f"(fingerprint {fingerprint}); regraba con `make eval-record`"
                )
            return queue.popleft().entry()
        entry = self._recording.calls.get(fingerprint)
        if entry is None:
            raise FingerprintMismatch(
                f"el prompt del rol {self._role!r} no casa la grabacion "
                f"(fingerprint {fingerprint}); regraba con `make eval-record`"
            )
        if fingerprint in self._used:
            raise FingerprintMismatch(
                f"el fingerprint {fingerprint} ya se consumio: la grabacion no cubre "
                f"esta llamada; regraba con `make eval-record`"
            )
        self._used.add(fingerprint)
        return entry


def record_call(
    recording: Recording,
    *,
    role: str,
    system: str,
    prompt: str,
    max_tokens: int,
    reply: LlmReply | None,
    json_mode: JsonMode | None = None,
    response_schema: dict[str, Any] | None = None,
    batch_index: int | None = None,
    turn: int | None = None,
    error: BaseException | None = None,
) -> str:
    """Guarda una llamada real en `recording` con su sitio en la corrida.

    Devuelve la huella del prompt. Lo usa `make eval-record` (E7): graba cada
    llamada a medida que ocurre, por rol, para que regrabar un rol no invalide
    los demas. `batch_index`/`turn` los aporta el `BatchTracker`; sin el (p. ej.
    el verificador) quedan `None`, que es «no se», no un cero. Una grabacion
    cargada de un fichero sin `order` no se puede ampliar: su orden no existe.

    Un intento que **fallo** tambien se graba (`error`): si no, el *replay* se
    salta una llamada que en vivo consumio un turno y aborta por fingerprint, con
    lo que la corrida entera se vuelve irreproducible (023 T0c).
    """
    fingerprint = prompt_fingerprint(
        role=role,
        system=system,
        prompt=prompt,
        max_tokens=max_tokens,
        json_mode=json_mode,
        response_schema=response_schema,
    )
    registro = f"{type(error).__name__}: {error}" if error is not None else ""
    entry = {
        "role": role,
        "text": "" if reply is None else reply.text,
        "prompt_tokens": 0 if reply is None else reply.prompt_tokens,
        "completion_tokens": 0 if reply is None else reply.completion_tokens,
        "reasoning_tokens": 0 if reply is None else reply.reasoning_tokens,
        "cached_tokens": None if reply is None else reply.cached_tokens,
        "finish_reason": "" if reply is None else reply.finish_reason,
        "error": registro,
    }
    # El indice conserva la primera ocurrencia del prompt; las repeticiones viven
    # en `order`, que es la verdad (E0e).
    if recording.order is None:
        raise ValueError(
            "la grabacion se cargo de un fichero sin `order`: no se puede ampliar "
            "sin inventar el orden de lo ya grabado; empieza una grabacion nueva"
        )
    recording.calls.setdefault(fingerprint, entry)
    call_id = len(recording.order)
    recording.order.append(
        RecordedCall(
            seq=call_id + 1,
            call_id=call_id,
            fingerprint=fingerprint,
            role=role,
            batch_index=batch_index,
            turn=turn,
            text=entry["text"],
            prompt_tokens=entry["prompt_tokens"],
            completion_tokens=entry["completion_tokens"],
            reasoning_tokens=entry["reasoning_tokens"],
            cached_tokens=entry["cached_tokens"],
            finish_reason=entry["finish_reason"],
            error=registro,
        )
    )
    return fingerprint


_ERRORS: dict[str, type[LlmError]] = {
    "LlmError": LlmError,
    "LlmTransientError": LlmTransientError,
    "LlmTruncatedError": LlmTruncatedError,
    "FingerprintMismatch": FingerprintMismatch,
}


def _recorded_error(raw: str) -> LlmError:
    """El error de una llamada grabada, reconstruido por su tipo."""
    name, _, message = raw.partition(": ")
    return _ERRORS.get(name, LlmError)(message or raw)


def fingerprints_by_role(recording: Recording) -> dict[str, str]:
    """Huellas de los prompts grabados, por rol, para el informe.

    Un rol con varias llamadas las une con `,` (ordenadas): el informe declara de
    que grabacion salio cada respuesta, de modo que un cambio de prompt se ve como
    un diff en el baseline congelado.
    """
    result: dict[str, list[str]] = {}
    for fingerprint, entry in recording.calls.items():
        result.setdefault(str(entry.get("role", "")), []).append(fingerprint)
    return {role: ",".join(sorted(values)) for role, values in sorted(result.items())}


@dataclass
class BatchTracker:
    """Etiqueta cada llamada grabada con su lote y su turno (E0e de 023).

    Es un `ReasoningSink`: el revisor ya emite un `start` por lote antes de cada
    intento (el mismo evento que consume el panel), asi que el lote no se
    adivina ni se deduce del prompt. Los turnos del bucle se cuentan desde ese
    `start`; un reintento del lote vuelve a emitirlo y reinicia el turno.
    """

    batch_index: int | None = None
    turn: int = 0

    def emit(self, review_key: str, event: ReasoningEvent) -> None:
        del review_key
        if event.kind != "start":
            return
        self.batch_index = event.batch - 1
        self.turn = 0

    def take(self) -> tuple[int | None, int | None]:
        """Lote y turno de la llamada que empieza; avanza el turno."""
        if self.batch_index is None:
            return None, None
        turn = self.turn
        self.turn += 1
        return self.batch_index, turn


class RecordingLlm:
    """`LlmPort` que envuelve al modelo real y graba cada respuesta (E7).

    Es el puerto de `make eval-record`: delega en el modelo real y guarda la
    respuesta bajo el fingerprint del prompt, de modo que la corrida en *replay*
    reproduzca exactamente lo mismo. `total_tokens` acumula el gasto del run.
    """

    def __init__(
        self,
        inner: LlmPort,
        recording: Recording,
        *,
        role: str,
        tracker: BatchTracker | None = None,
    ) -> None:
        self._inner = inner
        self._recording = recording
        self._role = role
        # El mismo tracker que escucha el `start` del lote etiqueta la grabacion
        # (E0e); se expone para que `run_case` no tenga que recibirlo aparte.
        self.tracker = tracker
        self.total_tokens = 0
        self.usage = Usage()

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        json_mode: JsonMode | None = None,
        response_schema: dict[str, Any] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> LlmReply:
        # Solo se pasa si alguien escucha (021 R2): un puerto que no razona no tiene
        # por que aceptarlo.
        extra = {"on_reasoning": on_reasoning} if on_reasoning is not None else {}
        try:
            reply = self._inner.complete(
                system=system,
                prompt=prompt,
                max_tokens=max_tokens,
                json_mode=json_mode,
                response_schema=response_schema,
                **extra,
            )
        except LlmError as exc:
            # El turno fallido ocupa su sitio en la grabacion: sin su entrada, el
            # replay se salta la llamada y aborta por fingerprint (T0c).
            batch_index, turn = self.tracker.take() if self.tracker is not None else (None, None)
            record_call(
                self._recording,
                role=self._role,
                system=system,
                prompt=prompt,
                max_tokens=max_tokens,
                reply=None,
                json_mode=json_mode,
                response_schema=response_schema,
                batch_index=batch_index,
                turn=turn,
                error=exc,
            )
            raise
        batch_index, turn = self.tracker.take() if self.tracker is not None else (None, None)
        record_call(
            self._recording,
            role=self._role,
            system=system,
            prompt=prompt,
            max_tokens=max_tokens,
            reply=reply,
            json_mode=json_mode,
            response_schema=response_schema,
            batch_index=batch_index,
            turn=turn,
        )
        self.total_tokens += reply.total_tokens
        self.usage.add(reply)
        return reply


class UsageMeter:
    """Envuelve un `LlmPort` y acumula el gasto real de cada respuesta (023 M1).

    Lo usa `make eval-live`, que no graba nada y aun asi necesita el desglose de
    *prompt*/*completion*/razonamiento: el puerto del proveedor devuelve el `usage`
    por llamada y no acumula. Delega todo lo demas en el puerto envuelto.
    """

    def __init__(self, inner: LlmPort) -> None:
        self._inner = inner
        self.total_tokens = 0
        self.usage = Usage()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def complete(self, **kwargs: Any) -> LlmReply:
        reply = self._inner.complete(**kwargs)
        self.total_tokens += reply.total_tokens
        self.usage.add(reply)
        return reply


# ---------------------------------------------------------------------------
# Corrida del grafo real sobre el caso (E7/E9): el harness lo lee, no lo edita.
# ---------------------------------------------------------------------------


class CaseVcs:
    """`VcsPort` falso sobre un caso versionado: publica en memoria.

    Reproduce lo que el grafo publica sin tocar red. El `diff` sale del caso y el
    `scope` del grafo lo prioriza igual que en produccion (012). Los comentarios
    publicados quedan en `inlines` y `comments` para que el harness derive el
    conjunto publicado (E12).
    """

    def __init__(self, case: Case) -> None:
        self.case = case
        self.inlines: list[tuple[str, int, str]] = []
        self.comments: list[str] = []
        self._files = parse_unified_diff(case.diff)

    def get_pull_request(self, repo: str, pr_id: int) -> PullRequestInfo:
        return PullRequestInfo(
            repo=repo,
            pr_id=pr_id,
            title=self.case.name,
            source_branch="feature",
            destination_branch="main",
            author="author",
            head_sha=self.case.head_sha,
            url="https://example.test/pr",
        )

    def get_diff(self, repo: str, pr_id: int) -> str:
        del repo, pr_id
        return self.case.diff

    def get_file(self, repo: str, path: str, *, ref: str) -> str:
        del repo
        # Con fixtures versionados, el caso sirve el fichero a cada lado (008 L1 y
        # el lector de herramientas usan el mismo arbol). Sin ellos, vacio: el
        # replay del caso de juguete no ensambla contexto.
        root = self.case.fixtures
        if ref == self.case.base_sha and self.case.base_fixtures is not None:
            root = self.case.base_fixtures
        if root is None or is_denied(path, DEFAULT_CONTEXT_DENYLIST):
            return ""
        # Resolver la raiz: `root` puede llegar relativa (`--case tests/eval/pr_3`)
        # y la contencion compara contra `Path.parents`, que son absolutos.
        base = Path(root).resolve()
        target = (base / path).resolve()
        if target != base and base not in target.parents:
            return ""
        if not target.is_file():
            return ""
        return target.read_text(encoding="utf-8", errors="replace")

    def list_review_markers(self, repo: str, pr_id: int) -> list[ReviewMarker]:
        del repo, pr_id
        return []

    def resolve_inline_anchor(
        self, repo: str, pr_id: int, *, path: str, text: str
    ) -> InlineAnchor | None:
        del repo, pr_id
        file_diff = self._files.get(path)
        if file_diff is None:
            return None
        line = resolve_added_line(file_diff.added, text)
        if line is None:
            return None
        return InlineAnchor(path=path, line=line)

    def post_inline_comment(
        self, repo: str, pr_id: int, *, path: str, line: int, body: str, from_line=None
    ) -> None:
        del repo, pr_id, from_line
        self.inlines.append((path, line, body))

    def post_comment(self, repo: str, pr_id: int, body: str) -> None:
        del repo, pr_id
        self.comments.append(body)

    def post_task(self, repo: str, pr_id: int, body: str) -> None:
        del repo, pr_id, body


class IncompleteMeasurement(RuntimeError):
    """Una corrida en *replay* dejo lotes sin revisar: la medida no vale (R2c).

    Un `FAILED` significa que el lote no se reviso (grabacion corrupta, error de
    parseo, bug), no que la review encontrase menos. Confundirlos convierte un
    fallo en un numero de recall, que es el agujero que 023 T0c cierra. `BUDGET`
    no entra: es una omision legitima y se cuenta aparte (criterio 12).
    """


@dataclass(frozen=True)
class Run:
    """Resultado de una corrida del grafo: lo publicado y el gasto del run."""

    published: tuple[Finding, ...]
    tokens: int
    inlined: int
    reviewed: tuple[str, ...] = ()
    skipped: dict[str, SkipReason] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)
    operations: dict[str, int] = field(default_factory=dict)

    @property
    def failed(self) -> dict[str, SkipReason]:
        """Ficheros cuyo lote no se reviso (no confundir con presupuesto agotado)."""
        return {
            path: reason for path, reason in self.skipped.items() if reason is SkipReason.FAILED
        }


def run_case(
    case: Case,
    *,
    llm: LlmPort,
    policy: PolicyEngine,
    vcs: CaseVcs,
    reader_factory: ReviewerReaderFactory | None = None,
    tool_limits: ContextLimits | None = None,
    verifier: VerifierPort | None = None,
    verifier_max_findings: int = 5,
) -> Run:
    """Corre el **grafo real** sobre el caso con las personas y el VCS inyectados.

    Lo unico que cambia entre *replay* y real es el `LlmPort` (E7 de la spec). El
    resultado se lee del estado del grafo y de lo que el `CaseVcs` registro: el
    harness no reconstruye el recorte ni la publicacion a mano.

    Con `reader_factory` (014 RH10) el revisor corre el bucle de herramientas por
    lote; sin el, una llamada por lote como en 008. La fabrica la construye el
    llamante (`scripts/eval.py` la deriva de `REVIEW_TOOLS_ROLES`): el harness no
    conoce `Settings`.

    Con `verifier` (009) el nodo `verify` corre el segundo juicio adversarial entre
    `merge` y `policy`: es el escenario de produccion, donde un `REFUTED` sale
    `DROP` y los P0/P1 no verificados degradan. Sin el, el nodo es paso directo y
    se mide la precision **cruda** del revisor (el baseline de 008).

    El revisor escucha por el `tracker` del grabador (`RecordingLlm`), que es lo
    que permite etiquetar cada llamada con lote y turno (023 E0e).
    """
    reviewer = LlmReviewer(
        llm=llm,
        name="correctness",
        max_findings=int(case.scope.get("review_max_findings", 10)),
        reader_factory=reader_factory,
        tool_limits=tool_limits,
        # El grabador trae su tracker (`RecordingLlm.tracker`): el mismo objeto
        # escucha el `start` y etiqueta la llamada, sin pasarlo por dos sitios.
        reasoning_sink=getattr(llm, "tracker", None),
    )
    graph = build_graph(
        vcs=vcs,
        reviewers=[reviewer],
        policy=policy,
        verifier=verifier,
        verifier_max_findings=verifier_max_findings,
        verification_enabled=False,
    ).compile()
    state = graph.invoke(
        {
            "request": {
                "repo": case.repo,
                "pr_id": case.pr_id,
                "head_sha": case.head_sha,
            },
            "findings": [],
        }
    )
    merged = tuple(state.get("merged", []))
    coverage = state.get("coverage")
    usage = getattr(llm, "usage", None)
    tokens = usage.total if isinstance(usage, Usage) else int(getattr(llm, "total_tokens", 0))
    return Run(
        published=merged,
        tokens=int(tokens),
        inlined=len(vcs.inlines),
        reviewed=tuple(coverage.reviewed) if coverage is not None else (),
        skipped=dict(coverage.skipped) if coverage is not None else {},
        usage=usage.as_dict() if isinstance(usage, Usage) else {},
        # Peticiones servidas por herramienta y modo: el revisor las acumula por
        # review (023 M1); sin bucle de herramientas el mapa viene vacio.
        operations=reviewer.operation_counts,
    )


def evaluate(
    case: Case,
    *,
    llm: LlmPort,
    policy: PolicyEngine,
    mode: str = "replay",
    recording: Recording | None = None,
    coverage: float | None = None,
    reader_factory: ReviewerReaderFactory | None = None,
    tool_limits: ContextLimits | None = None,
    verifier: VerifierPort | None = None,
    verifier_max_findings: int = 5,
) -> Report:
    """Corre el grafo sobre el caso y devuelve el informe (E7/E9).

    Ata el runner (`run_case`), las metricas (`measure`) y las huellas de la
    grabacion para que el informe declare de donde salio cada respuesta.
    """
    run = run_case(
        case,
        llm=llm,
        policy=policy,
        vcs=CaseVcs(case),
        reader_factory=reader_factory,
        tool_limits=tool_limits,
        verifier=verifier,
        verifier_max_findings=verifier_max_findings,
    )
    # En *replay* un lote fallido no es una medida: es un fallo del harness o de la
    # grabacion. Publicarlo como recall seria medir el pasado con un numero (R2c).
    if mode == "replay" and run.failed:
        paths = ", ".join(sorted(run.failed))
        raise IncompleteMeasurement(
            f"la corrida dejo lotes sin revisar ({paths}): no es una medida; "
            "revisa la grabacion o el harness, o corre en vivo"
        )
    fingerprints = fingerprints_by_role(recording) if recording is not None else {}
    return measure(
        case,
        run.published,
        mode=mode,
        tokens=run.tokens,
        coverage=coverage if coverage is not None else coverage_of(case, run.reviewed),
        fingerprints=fingerprints,
        usage=run.usage,
        operations=run.operations,
        skipped=run.skipped,
        cache_by_batch=cache_by_batch(recording),
    )


# ---------------------------------------------------------------------------
# Cobertura (E11 de la spec): lo que el `LlmReviewer` vio de verdad.
# ---------------------------------------------------------------------------


def coverage_of(case: Case, reviewed: Sequence[str]) -> float | None:
    """Ficheros de codigo y configuracion revisados / totales de esas clases (E11).

    `reviewed` sale de la `ReviewCoverage` del grafo (012): lo que alguna persona
    leyo de verdad. La clase la decide `scope.classify`, la misma que usa la review
    para priorizar y para marcarla parcial. `None` si el PR no trae ninguno (nada
    que cubrir no es cobertura 0).
    """
    relevant = {path for path in changed_files(case.diff) if classify(path) in PARTIAL_CLASSES}
    if not relevant:
        return None
    seen = {path for path in reviewed if path in relevant}
    return round(len(seen) / len(relevant), 4)
