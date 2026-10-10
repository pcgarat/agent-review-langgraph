from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from reviews.domain.models import Finding

_CONTRACT_PATH = Path(__file__).resolve().parents[3] / "contracts" / "findings-schema.json"


class ContractError(RuntimeError):
    """Un hallazgo no cumple el contrato versionado."""


def load_findings_schema(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or _CONTRACT_PATH).read_text(encoding="utf-8"))


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
