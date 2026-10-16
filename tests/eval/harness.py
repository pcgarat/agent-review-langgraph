"""Harness offline de evaluacion: carga un caso, lo corre y mide la review.

Puro y sin red: no conoce el grafo ni el VCS; los recibe. Un *caso* es un
directorio versionado con `labels.yaml` (procedencia, `scope` y etiquetas),
`diff.patch` (el diff auditado) y, mas adelante, `recorded.json` (las respuestas
del modelo) y `expected_report.json` (el baseline congelado).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agent_review.domain.models import Finding

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
