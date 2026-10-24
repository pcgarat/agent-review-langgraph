"""Artefacto validado del motor claude (025 B1)."""

from __future__ import annotations

import json
from pathlib import Path

from agent_review.adapters.claude_artifact import ClaudeArtifactStore
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
