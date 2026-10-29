"""Artefacto validado del motor claude (025 B1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_review.adapters.agent_mode import AgentModeRejected, adapt_agent_mode
from agent_review.adapters.claude_artifact import ClaudeArtifactReviewer, ClaudeArtifactStore
from agent_review.domain.contracts import validate_finding
from agent_review.domain.models import DiscardedFinding, ReviewRequest
from agent_review.ports.claude import ClaudeInvalidOutput
from agent_review.settings import Settings
from agent_review.worker.main import ensure_claude_artifact

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


def test_evidence_como_texto_se_envuelve_en_lista() -> None:
    cuerpo = _agente(evidence="src/Transfer.php:14 throw new InsufficientBalance();")

    review = adapt_agent_mode(cuerpo)

    assert review.discarded == 0
    assert review.findings[0].evidence == ["src/Transfer.php:14 throw new InsufficientBalance();"]


def test_sin_evidence_usa_first_evidence() -> None:
    cuerpo = _agente(evidence=None, first_evidence="throw new InsufficientBalance();")

    review = adapt_agent_mode(cuerpo)

    assert review.findings[0].evidence == ["throw new InsufficientBalance();"]


def test_first_evidence_va_delante_de_una_evidence_en_prosa() -> None:
    # Caso real (PR 27, ruta lite): la cita literal venía solo en first_evidence
    # y L0, que busca la primera cita en el diff, tiraba el hallazgo.
    cuerpo = _agente(
        evidence="Transfer.php:14. Con saldo 0 la retirada deja el estado a medias.",
        first_evidence="throw new InsufficientBalance('no funds');",
    )

    review = adapt_agent_mode(cuerpo)

    assert review.findings[0].evidence == [
        "throw new InsufficientBalance('no funds');",
        "Transfer.php:14. Con saldo 0 la retirada deja el estado a medias.",
    ]


def test_first_evidence_ya_citada_no_se_duplica() -> None:
    cuerpo = _agente(first_evidence="throw new InsufficientBalance('no funds');")

    review = adapt_agent_mode(cuerpo)

    assert review.findings[0].evidence == [
        "src/Transfer.php:14 throw new InsufficientBalance('no funds');"
    ]


def test_sin_evidence_ni_first_evidence_no_entra() -> None:
    cuerpo = _agente(evidence="  ")

    with pytest.raises(AgentModeRejected) as exc:
        adapt_agent_mode(cuerpo)

    assert exc.value.discarded == 1


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


def test_un_failed_conserva_la_razon() -> None:
    cuerpo = _agente()
    cuerpo["status"] = "failed"
    cuerpo["reason"] = "no se pudo despachar el revisor"

    with pytest.raises(AgentModeRejected, match="no se pudo despachar el revisor"):
        adapt_agent_mode(cuerpo)


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

    assert exc.value.reasons == ("#4: sin why_it_matters",)


def test_el_motivo_del_contrato_nombra_el_campo() -> None:
    bueno = _agente()["findings"][0]
    cuerpo = _agente()
    cuerpo["findings"] = [bueno, {**bueno, "#": 8, "confidence": "alta"}]  # type: ignore[dict-item]

    review = adapt_agent_mode(cuerpo)

    assert len(review.findings) == 1
    assert len(review.reasons) == 1
    assert review.reasons[0].startswith("#8: confidence: ")


class _StdoutRunner:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout

    def run(
        self,
        *,
        repo: str,
        pr_id: int,
        head_sha: str,
        base_sha: str,
        max_turns: int | None = None,
        timeout_s: float | None = None,
    ) -> str:
        del repo, pr_id, head_sha, base_sha, max_turns, timeout_s
        return self.stdout


def test_el_log_dice_por_que_se_descarto_cada_hallazgo(tmp_path: Path) -> None:
    cuerpo = _agente(why_it_matters="")
    notas: list[str] = []
    request = ReviewRequest(repo=_REPO, pr_id=_PR, head_sha=_SHA, pipeline_id="7")

    with pytest.raises(ClaudeInvalidOutput, match="todos los hallazgos se descartaron"):
        ensure_claude_artifact(
            request,
            runner=_StdoutRunner(json.dumps(cuerpo)),
            store=_store(tmp_path),
            note=notas.append,
        )

    assert "Claude: se descartaron 1 hallazgos (#7: sin why_it_matters)." in notas


def test_el_descarte_del_contrato_lleva_id_titulo_y_fichero() -> None:
    bueno = _agente()["findings"][0]
    cuerpo = _agente()
    cuerpo["findings"] = [bueno, {**bueno, "#": 8, "confidence": "alta"}]  # type: ignore[dict-item]

    review = adapt_agent_mode(cuerpo)

    [descarte] = review.rejected
    assert descarte.stage == "contract"
    assert descarte.id == "8"
    assert descarte.title == "Saldo negativo no validado antes de retirar"
    assert descarte.file == "src/Transfer.php"
    assert descarte.reason.startswith("confidence: ")


def test_los_descartes_viven_junto_al_artefacto_y_se_olvidan_con_el(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.load_discarded(repo=_REPO, pr_id=_PR, head_sha=_SHA) == []
    descarte = DiscardedFinding(stage="contract", id="8", reason="sin why_it_matters")
    store.save_discarded([descarte], repo=_REPO, pr_id=_PR, head_sha=_SHA)
    assert store.save("[]", repo=_REPO, pr_id=_PR, head_sha=_SHA)

    assert store.load_discarded(repo=_REPO, pr_id=_PR, head_sha=_SHA) == [descarte]
    assert store.load_discarded(repo=_REPO, pr_id=_PR, head_sha=_SHA_NUEVA) == []

    store.discard(repo=_REPO, pr_id=_PR, head_sha=_SHA)

    assert store.load_discarded(repo=_REPO, pr_id=_PR, head_sha=_SHA) == []
    assert list(tmp_path.rglob("*.json")) == []


def test_el_worker_guarda_los_descartes_del_contrato(tmp_path: Path) -> None:
    bueno = _agente()["findings"][0]
    cuerpo = _agente()
    cuerpo["findings"] = [bueno, {**bueno, "#": 8, "why_it_matters": ""}]  # type: ignore[dict-item]
    request = ReviewRequest(repo=_REPO, pr_id=_PR, head_sha=_SHA, pipeline_id="7")
    store = _store(tmp_path)

    ensure_claude_artifact(request, runner=_StdoutRunner(json.dumps(cuerpo)), store=store)

    [descarte] = store.load_discarded(repo=_REPO, pr_id=_PR, head_sha=_SHA)
    assert (descarte.stage, descarte.id, descarte.reason) == ("contract", "8", "sin why_it_matters")


_DIFF = """diff --git a/src/Transfer.php b/src/Transfer.php
--- a/src/Transfer.php
+++ b/src/Transfer.php
@@ -13,1 +13,2 @@
 function withdraw() {
+    throw new InsufficientBalance('no funds');
"""


def test_l0_dice_por_que_tira_cada_hallazgo(tmp_path: Path) -> None:
    # Caso PR 27: la primera evidencia es prosa y no está en el diff.
    store = _store(tmp_path)
    hallazgos = [
        _hallazgo(id="dentro"),
        _hallazgo(id="prosa", evidence=["Transfer.php:14. Con saldo 0 se rompe."]),
        _hallazgo(id="otro", file="src/Otro.php"),
    ]
    assert store.save(json.dumps(hallazgos), repo=_REPO, pr_id=_PR, head_sha=_SHA)
    contrato = DiscardedFinding(stage="contract", id="8", reason="sin why_it_matters")
    store.save_discarded([contrato], repo=_REPO, pr_id=_PR, head_sha=_SHA)

    result = ClaudeArtifactReviewer(store).review(repo=_REPO, pr_id=_PR, head_sha=_SHA, diff=_DIFF)

    assert [item.id for item in result.findings] == ["dentro"]
    assert [(item.stage, item.id, item.reason) for item in result.discarded] == [
        ("contract", "8", "sin why_it_matters"),
        ("l0", "prosa", "la cita no aparece en el diff"),
        ("l0", "otro", "el fichero no está en el diff"),
    ]
    assert result.discarded[1].title == "Saldo negativo no validado antes de retirar"
