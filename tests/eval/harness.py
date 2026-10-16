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
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from agent_review.domain.models import Finding
from agent_review.ports.llm import JsonMode, LlmReply

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

    def labels(self) -> tuple[Label, ...]:
        return (*self.expected, *self.must_not_report, *self.acceptable)


@dataclass(frozen=True)
class Report:
    """Informe de una corrida. Estable y serializable (claves ordenadas, 4 decimales).

    Los `id` de `matched`/`unmatched`/`missing` existen para que el fallo de CI
    diga **que** hallazgo se movio, no solo que un numero cambio.
    """

    case: str
    mode: str
    fingerprints: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    metrics: dict[str, float | None] = field(default_factory=dict)
    matched: tuple[str, ...] = ()
    unmatched: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    acceptable: tuple[str, ...] = ()
    inverted: tuple[str, ...] = ()


def load_case(directory: str | Path) -> Case:
    """Carga un caso versionado desde su directorio.

    El `diff.patch` es obligatorio (sin diff no hay review que medir). Las
    etiquetas se leen tal cual; el matcher (E2) es quien las interpreta.
    """
    base = Path(directory)
    data = _read_yaml(base / "labels.yaml")
    diff = (base / "diff.patch").read_text(encoding="utf-8")
    scope = data.get("scope") or {}
    return Case(
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
    )


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
    """Atribuye `published` contra las etiquetas del caso (1:1 determinista, E2).

    El orden no depende de como llegaron los hallazgos: ambos lados se ordenan en
    forma canonica (severidad, linea, id) y, en ese orden, cada etiqueta se queda
    con el primer hallazgo no consumido que cumpla su `match`. Dos etiquetas que
    casarian el mismo hallazgo se reparten distintos (la segunda busca el siguiente
    no consumido); un hallazgo que no casa ninguna etiqueta queda `unmatched`.
    """
    for label in case.expected + case.must_not_report:
        if not label.match:
            raise ValueError(f"la etiqueta {label.id!r} no declara 'match'")
    findings = sorted(published, key=_finding_order)
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


def _attribute(
    labels: tuple[Label, ...], findings: list[Finding], used: set[int]
) -> dict[str, Finding]:
    """Asigna a cada etiqueta su primer hallazgo no consumido, en orden canonico.

    Las etiquetas con `file`/`line` exigen ese anclaje (con la tolerancia del
    ADR-0014) y las que anclan por identificador (``file` sin `line`) solo el
    fichero.
    """
    result: dict[str, Finding] = {}
    for label in labels:
        for finding in findings:
            if id(finding) in used:
                continue
            if _matches(label, finding):
                result[label.id] = finding
                used.add(id(finding))
                break
    return result


def _matches(label: Label, finding: Finding) -> bool:
    """¿El hallazgo casa la etiqueta? `match` conjuntivo + anclaje opcional."""
    if label.file is not None and finding.file != label.file:
        return False
    if label.line is not None and abs(finding.line - label.line) > LINE_TOLERANCE:
        return False
    text = normalize(f"{finding.title} {finding.why_it_matters}")
    return all(normalize(fragment) in text for fragment in label.match)


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
) -> Report:
    """Traduce una corrida en el informe de seis metricas (E3, spec §5.4).

    `tokens` es el gasto del run (del `usage` del proveedor, E13) y `coverage` la
    fraccion de ficheros vistos por el `LlmReviewer` (E11); ambos llegan de fuera
    porque el harness puro no corre el grafo. Hasta que exista la instrumentacion,
    `coverage=None` (nunca 0: «no medido» no es «no visto»).
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
        matched=tuple(sorted(all_tp)),
        unmatched=tuple(sorted(finding.id for finding in result.unmatched)),
        missing=tuple(sorted(result.missing)),
        acceptable=tuple(sorted(result.acceptable)),
        inverted=tuple(sorted(inverted)),
    )


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
# diff o el scope. El fingerprint cubre los tres: una grabacion vieja con un
# prompt nuevo aborta en vez de medir el pasado en silencio (E4 de la spec).
_FINGERPRINT_VERSION = 1


class FingerprintMismatch(RuntimeError):
    """La grabacion no corresponde al prompt o al caso: hay que regrabar."""


def prompt_fingerprint(*, role: str, system: str, prompt: str, max_tokens: int) -> str:
    """Huella estable del prompt de un rol: rol + system + prompt + `max_tokens`.

    El rol forma parte del fingerprint porque dos roles con el mismo diff y tope
    de hallazgos generan el mismo prompt (E4 de la spec): sin el, regrabar un rol
    invalidaria los demas o pasaria por valido sin serlo.
    """
    digest = hashlib.sha256()
    for part in (_FINGERPRINT_VERSION, role, system, prompt, max_tokens):
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


@dataclass(frozen=True)
class Recording:
    """Respuestas grabadas de un caso, indexadas por fingerprint de prompt.

    Por rol y por llamada (E4): cada entrada lleva su `fingerprint` y el par
    `text`/`usage`; regrabar un rol no toca los demas.
    """

    case: str
    case_fingerprint: str
    calls: dict[str, dict[str, Any]]

    @classmethod
    def load(cls, path: str | Path) -> Recording:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            case=str(data["case"]),
            case_fingerprint=str(data["case_fingerprint"]),
            calls={str(key): value for key, value in data.get("calls", {}).items()},
        )

    def dump(self) -> str:
        return (
            json.dumps(
                {
                    "case": self.case,
                    "case_fingerprint": self.case_fingerprint,
                    "calls": self.calls,
                },
                sort_keys=True,
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )


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
    ) -> LlmReply:
        del json_mode, response_schema
        fingerprint = prompt_fingerprint(
            role=self._role, system=system, prompt=prompt, max_tokens=max_tokens
        )
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
        return LlmReply(
            text=str(entry["text"]),
            prompt_tokens=int(entry.get("prompt_tokens", 0)),
            completion_tokens=int(entry.get("completion_tokens", 0)),
            reasoning_tokens=int(entry.get("reasoning_tokens", 0)),
            finish_reason=str(entry.get("finish_reason", "")),
        )


def record_call(
    recording: Recording,
    *,
    role: str,
    system: str,
    prompt: str,
    max_tokens: int,
    reply: LlmReply,
) -> str:
    """Guarda una respuesta real en `recording` bajo su fingerprint. Devuelve la huella.

    Lo usa `make eval-record` (E7): graba cada llamada a medida que ocurre, por
    rol, para que regrabar un rol no invalide los demas.
    """
    fingerprint = prompt_fingerprint(role=role, system=system, prompt=prompt, max_tokens=max_tokens)
    recording.calls[fingerprint] = {
        "role": role,
        "text": reply.text,
        "prompt_tokens": reply.prompt_tokens,
        "completion_tokens": reply.completion_tokens,
        "reasoning_tokens": reply.reasoning_tokens,
        "finish_reason": reply.finish_reason,
    }
    return fingerprint
