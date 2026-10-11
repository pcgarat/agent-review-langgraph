from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from reviews.domain.contracts import (
    ContractError,
    load_findings_schema,
    schema_matches_domain,
    validate_finding,
)


def test_esquema_no_se_desincroniza_del_modelo() -> None:
    assert schema_matches_domain(), "contrato y modelo Pydantic han divergido"


def test_contrato_tiene_campos_obligatorios() -> None:
    schema = load_findings_schema()
    assert "confidence" in schema["required"]
    assert "evidence" in schema["required"]
    assert schema["properties"]["confidence"]["enum"] == [0, 25, 50, 75, 100]


def _valid() -> dict:
    return {
        "id": "x",
        "title": "t",
        "severity": "P3",
        "file": "a.php",
        "line": 1,
        "why_it_matters": "w",
        "autofix_class": "advisory",
        "owner": "human",
        "requires_verification": False,
        "confidence": 75,
        "evidence": ["a.php:1 algo"],
    }


def test_valida_hallazgo_correcto() -> None:
    finding = validate_finding(_valid())
    assert finding.file == "a.php"


def test_rechaza_confidence_no_anclada() -> None:
    payload = _valid() | {"confidence": 87}
    with pytest.raises(ValidationError):
        validate_finding(payload)


def test_rechaza_evidencia_vacia() -> None:
    payload = _valid() | {"evidence": []}
    with pytest.raises(ValidationError):
        validate_finding(payload)


def test_rechaza_campo_faltante_del_contrato() -> None:
    payload = json.loads(json.dumps(_valid()))
    del payload["owner"]
    with pytest.raises(ContractError):
        validate_finding(payload)


def test_esquema_del_repo_es_json_valido() -> None:
    path = Path(__file__).resolve().parents[1] / "contracts" / "findings-schema.json"
    assert json.loads(path.read_text(encoding="utf-8"))["title"] == "ReviewFinding"


def test_la_ruta_del_contrato_no_depende_del_paquete_instalado(monkeypatch, tmp_path) -> None:
    """El contrato debe resolverse fuera de site-packages (p. ej. en la imagen).

    Se empaqueta solo `src/`, asi que `parents[3]` apunta a site-packages dentro
    del contenedor. La resolucion prueba candidatos y admite override.
    """
    from reviews.domain import contracts

    copia = tmp_path / "findings-schema.json"
    copia.write_text(json.dumps(load_findings_schema()), encoding="utf-8")
    monkeypatch.setenv("REVIEWS_CONTRACT_PATH", str(copia))
    assert contracts.default_contract_path() == copia
    assert contracts.validate_finding(_valid()).file == "a.php"
