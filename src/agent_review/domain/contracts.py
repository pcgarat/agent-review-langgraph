from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from agent_review.domain.models import Finding

# Candidatos en orden: repo (dev/tests), imagen docker (Dockerfile copia
# `contracts/` en /app) y una copia empaquetada, si la hubiera.
_CONTRACT_CANDIDATES = (
    Path(__file__).resolve().parents[3] / "contracts" / "findings-schema.json",
    Path("/app/contracts/findings-schema.json"),
    Path(__file__).resolve().parents[1] / "contracts" / "findings-schema.json",
)


class ContractError(RuntimeError):
    """Un hallazgo no cumple el contrato versionado."""


def default_contract_path() -> Path:
    """Ruta del esquema versionado; `REVIEWS_CONTRACT_PATH` la sobreescribe."""
    override = os.environ.get("REVIEWS_CONTRACT_PATH", "").strip()
    if override:
        return Path(override)
    for candidate in _CONTRACT_CANDIDATES:
        if candidate.is_file():
            return candidate
    return _CONTRACT_CANDIDATES[0]


def load_findings_schema(path: Path | None = None) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((path or default_contract_path()).read_text(encoding="utf-8"))
    return data


def validate_finding(payload: dict[str, Any]) -> Finding:
    """Valida contra el modelo y contra el esquema versionado.

    El modelo Pydantic es la fuente de verdad; el esquema detecta drift entre el
    contrato publicado en `contracts/` y el codigo.
    """
    expected = set(load_findings_schema().get("required", []))
    missing = expected - set(payload)
    if missing:
        raise ContractError(f"Faltan campos del contrato: {sorted(missing)}")
    return Finding.model_validate(payload)


def schema_matches_domain() -> bool:
    """Comprueba que `contracts/findings-schema.json` no se ha desincronizado."""
    schema = load_findings_schema()
    generated = Finding.model_json_schema()
    if set(schema.get("required", [])) != set(generated.get("required", [])):
        return False
    for key, prop in generated.get("properties", {}).items():
        if key not in schema.get("properties", {}):
            return False
        if (
            prop.get("type")
            and "$ref" not in prop
            and schema["properties"][key].get("type") != prop.get("type")
        ):
            # $ref (enums) no exponen "type" en el generado: no es drift.
            return False
    return True
