from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_review.domain.contracts import (
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
        "failure_scenario": "escenario de prueba",
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
    from agent_review.domain import contracts

    copia = tmp_path / "findings-schema.json"
    copia.write_text(json.dumps(load_findings_schema()), encoding="utf-8")
    monkeypatch.setenv("REVIEWS_CONTRACT_PATH", str(copia))
    assert contracts.default_contract_path() == copia
    assert contracts.validate_finding(_valid()).file == "a.php"


def test_contrato_declara_la_verificacion_y_retira_requires_verification() -> None:
    schema = load_findings_schema()
    props = schema["properties"]
    for field in ("failure_scenario", "verification", "verification_evidence", "context_scope"):
        assert field in props
        assert field not in schema["required"]
    assert "requires_verification" not in props
    assert "requires_verification" not in schema["required"]
    assert schema["$defs"]["Verification"]["enum"] == [
        "unverified",
        "verified_static",
        "verified_grounded",
        "verified_executed",
        "refuted",
    ]
    assert schema["$defs"]["ContextScope"]["enum"] == ["in_diff", "outside_diff"]


def test_contrato_exige_escenario_a_p0_p2_sin_verificar() -> None:
    # El `if/then` del JSON publica lo mismo que I1 e I2 del modelo (spec 007 §5.5).
    rules = load_findings_schema()["allOf"]
    escenario = next(r for r in rules if r["then"].get("required") == ["failure_scenario"])
    assert escenario["if"]["properties"]["severity"]["enum"] == ["P0", "P1", "P2"]
    assert escenario["if"]["properties"]["verification"] == {"const": "unverified"}
    evidencia = next(r for r in rules if r["then"].get("required") == ["verification_evidence"])
    assert evidencia["then"]["properties"]["verification_evidence"]["minItems"] == 1


def test_validate_finding_aplica_los_invariantes() -> None:
    payload = {k: v for k, v in _valid().items() if k != "failure_scenario"}
    with pytest.raises(ValidationError, match="failure_scenario"):
        validate_finding(payload | {"severity": "P1"})
