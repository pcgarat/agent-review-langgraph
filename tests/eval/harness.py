"""Harness offline de evaluacion: carga un caso, lo corre y mide la review.

Puro y sin red: no conoce el grafo ni el VCS; los recibe. Un *caso* es un
directorio versionado con `labels.yaml` (procedencia, `scope` y etiquetas),
`diff.patch` (el diff auditado) y, mas adelante, `recorded.json` (las respuestas
del modelo) y `expected_report.json` (el baseline congelado).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Tres etiquetas (E9 de la spec): lo que debe salir, lo que no, y lo tolerable.
LBL_EXPECTED = "expected"
LBL_MUST_NOT = "must_not_report"
LBL_ACCEPTABLE = "acceptable"

_WHITESPACE = re.compile(r"\s+")


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
