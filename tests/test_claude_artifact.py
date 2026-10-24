"""Artefacto validado del motor claude (025 B1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_review.adapters.agent_mode import AgentModeRejected, adapt_agent_mode
from agent_review.adapters.claude_artifact import ClaudeArtifactStore
from agent_review.domain.contracts import validate_finding
from agent_review.settings import Settings

_REPO = "cqm/emotivoo-v2"
_PR = 12
_SHA = "abc123"
_SHA_NUEVA = "def456"


def _hallazgo(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "claude-1",
        "title": "Saldo negativo no validado antes de retirar",
        "severity": "P2",
        "file": "src/Transfer.php",
        "line": 14,
        "why_it_matters": "Se lanza una excepcion de dominio sin proteger el estado",
        "autofix_class": "gated_auto",
        "owner": "downstream-resolver",
        "failure_scenario": "retirar sin saldo deja el estado a medias",
        "confidence": 75,
        "evidence": ["src/Transfer.php:14 throw new InsufficientBalance('no funds');"],
    }
    base.update(overrides)
    return base


def _store(tmp_path: Path) -> ClaudeArtifactStore:
    return ClaudeArtifactStore(root=tmp_path)


def test_lista_valida_se_lee_dos_veces_por_la_misma_clave(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.save(json.dumps([_hallazgo()]), repo=_REPO, pr_id=_PR, head_sha=_SHA)

    primera = store.load(repo=_REPO, pr_id=_PR, head_sha=_SHA)
    segunda = store.load(repo=_REPO, pr_id=_PR, head_sha=_SHA)

    assert primera is not None
    assert [item.id for item in primera] == ["claude-1"]
    assert [item.id for item in segunda or []] == ["claude-1"]


def test_lista_vacia_es_artefacto_valido(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.save("[]", repo=_REPO, pr_id=_PR, head_sha=_SHA)
    assert store.load(repo=_REPO, pr_id=_PR, head_sha=_SHA) == []


def test_cuerpo_invalido_no_cuenta_como_review_sin_hallazgos(tmp_path: Path) -> None:
    store = _store(tmp_path)
    cuerpos = [
        "",
        "   ",
        "no es json",
        json.dumps(_hallazgo()),
        json.dumps([{"id": "solo-id"}]),
        json.dumps([1]),
        json.dumps([_hallazgo(), {"id": "roto"}]),
    ]
    for cuerpo in cuerpos:
        assert store.save(cuerpo, repo=_REPO, pr_id=_PR, head_sha=_SHA) is False
        assert store.load(repo=_REPO, pr_id=_PR, head_sha=_SHA) is None


def test_un_invalido_no_borra_el_artefacto_valido(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.save(json.dumps([_hallazgo()]), repo=_REPO, pr_id=_PR, head_sha=_SHA)
    assert store.save("no es json", repo=_REPO, pr_id=_PR, head_sha=_SHA) is False

    cargado = store.load(repo=_REPO, pr_id=_PR, head_sha=_SHA)
    assert cargado is not None
    assert [item.id for item in cargado] == ["claude-1"]


def test_la_clave_distingue_dos_head_sha_del_mismo_pr(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.save(json.dumps([_hallazgo()]), repo=_REPO, pr_id=_PR, head_sha=_SHA)

    assert store.load(repo=_REPO, pr_id=_PR, head_sha=_SHA_NUEVA) is None
    assert store.load(repo=_REPO, pr_id=_PR, head_sha=_SHA) is not None


def test_la_raiz_por_defecto_no_es_la_del_tooling() -> None:
    assert Settings().claude_artifact_root == "var/claude"
    assert "tooling" not in Settings().claude_artifact_root


def _agente(**overrides: object) -> dict[str, object]:
    hallazgo: dict[str, object] = {
        "#": 7,
        "title": "Saldo negativo no validado antes de retirar",
        "severity": "P2",
        "file": "src/Transfer.php",
        "line": 14,
        "why_it_matters": "retirar sin saldo deja el estado a medias",
        "autofix_class": "gated_auto",
        "owner": "downstream-resolver",
        "confidence": 75,
        "evidence": ["src/Transfer.php:14 throw new InsufficientBalance('no funds');"],
        "pre_existing": False,
        "failure_scenario": "retirar sin saldo deja el estado a medias",
    }
    hallazgo.update(overrides)
    return {
        "status": "complete",
        "verdict": "Ready with fixes",
        "findings": [hallazgo],
        "actionable_findings": [],
        "pre_existing_findings": [],
        "campo_nuevo": "se ignora",
    }


def test_complete_con_un_hallazgo_pasa_el_contrato() -> None:
    review = adapt_agent_mode(_agente())

    assert review.discarded == 0
    assert len(review.findings) == 1
    validado = validate_finding(review.findings[0].model_dump(mode="json"))
    assert validado.id == "7"
    assert validado.pre_existing is False


def test_pre_existing_entra_aunque_no_este_en_actionable() -> None:
    cuerpo = _agente()
    cuerpo["findings"] = []
    cuerpo["actionable_findings"] = [
        {
            "#": 99,
            "title": "Solo en actionable no entra",
            "severity": "P3",
            "file": "src/Otro.php",
            "line": 2,
            "why_it_matters": "no deberia mapearse",
            "autofix_class": "advisory",
            "owner": "human",
            "confidence": 50,
            "evidence": ["src/Otro.php:2 echo 1;"],
        }
    ]
    cuerpo["pre_existing_findings"] = [
        {
            "#": 3,
            "title": "Null no comprobado en el lector",
            "severity": "P1",
            "file": "src/Reader.php",
            "line": 4,
            "why_it_matters": "un null revienta el lote",
            "autofix_class": "manual",
            "owner": "human",
            "confidence": 50,
            "evidence": ["src/Reader.php:4 return $row->id;"],
            "pre_existing": False,
            "failure_scenario": "un null revienta el lote",
        }
    ]

    review = adapt_agent_mode(cuerpo)

    assert [item.id for item in review.findings] == ["3"]
    assert review.findings[0].pre_existing is True


def test_p0_sin_escenario_copia_why_it_matters() -> None:
    cuerpo = _agente(
        **{
            "#": 1,
            "severity": "P0",
            "failure_scenario": None,
            "why_it_matters": "el token sale en el log",
        }
    )

    review = adapt_agent_mode(cuerpo)

    assert review.findings[0].failure_scenario == "el token sale en el log"


def test_p0_sin_escenario_ni_why_no_entra_y_se_cuenta() -> None:
    bueno = _agente()["findings"][0]
    assert isinstance(bueno, dict)
    cuerpo = _agente()
    cuerpo["findings"] = [
        bueno,
        {
            "#": 2,
            "title": "Hipotesis sin camino",
            "severity": "P1",
            "file": "src/A.php",
            "line": 1,
            "why_it_matters": "   ",
            "autofix_class": "advisory",
            "owner": "human",
            "confidence": 25,
            "evidence": ["src/A.php:1 return 1;"],
        },
    ]

    review = adapt_agent_mode(cuerpo)

    assert [item.id for item in review.findings] == ["7"]
    assert review.discarded == 1


def test_complete_sin_hallazgos_de_origen_es_lista_vacia() -> None:
    cuerpo = _agente()
    cuerpo["findings"] = []

    review = adapt_agent_mode(json.dumps(cuerpo))

    assert review.findings == []
    assert review.discarded == 0


@pytest.mark.parametrize("status", ["failed", "skipped", "degraded"])
def test_un_estado_que_no_es_complete_no_es_lista_vacia(status: str) -> None:
    cuerpo = _agente()
    cuerpo["status"] = status
    cuerpo["findings"] = []

    with pytest.raises(AgentModeRejected):
        adapt_agent_mode(cuerpo)


@pytest.mark.parametrize(
    "cuerpo",
    [
        [],
        {"status": "complete"},
        {"status": "complete", "findings": "no"},
        "no es json",
        {"findings": []},
    ],
)
def test_un_json_que_no_es_el_objeto_falla(cuerpo: object) -> None:
    with pytest.raises(AgentModeRejected):
        adapt_agent_mode(cuerpo)


def test_complete_con_todos_los_hallazgos_descartados_falla() -> None:
    cuerpo = _agente()
    cuerpo["findings"] = [
        {
            "#": 4,
            "title": "Sin camino",
            "severity": "P2",
            "file": "src/B.php",
            "line": 8,
            "why_it_matters": "",
            "autofix_class": "manual",
            "owner": "human",
            "confidence": 50,
            "evidence": ["src/B.php:8 return;"],
        }
    ]

    with pytest.raises(AgentModeRejected) as exc:
        adapt_agent_mode(cuerpo)

    assert exc.value.discarded == 1
