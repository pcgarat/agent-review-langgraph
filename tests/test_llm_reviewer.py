from __future__ import annotations

import json
import logging
from pathlib import Path

from reviews.adapters.llm_reviewer import LlmReviewer
from reviews.domain.models import (
    AutofixClass,
    Finding,
    Owner,
    ReviewerResult,
    Severity,
)
from reviews.graph.build import build_graph
from reviews.policy.engine import PolicyEngine
from reviews.ports.vcs import InlineAnchor, PullRequestInfo

FIXTURES = Path(__file__).parent / "fixtures"
DIFF = (FIXTURES / "diff_emotivoo.diff").read_text(encoding="utf-8")
TRANSFER = "src/BackOffice/Application/Wallet/Transfer.php"


class FakeLlm:
    def __init__(self, response: str = "", *, raises: Exception | None = None) -> None:
        self._response = response
        self._raises = raises
        self.calls: list[tuple[str, str, int]] = []

    def complete(self, *, system: str, prompt: str, max_tokens: int) -> str:
        self.calls.append((system, prompt, max_tokens))
        if self._raises is not None:
            raise self._raises
        return self._response


def _response(*findings: dict) -> str:
    return json.dumps({"findings": list(findings)})


def _finding_json(**overrides) -> dict:
    base = {
        "title": "Saldo negativo no validado antes de retirar",
        "severity": "P2",
        "file": TRANSFER,
        "line": 14,
        "why_it_matters": "Se lanza una excepcion de dominio sin proteger el estado",
        "autofix_class": "gated_auto",
        "owner": "downstream-resolver",
        "requires_verification": True,
        "confidence": 75,
        "evidence": [f"{TRANSFER}:14 throw new InsufficientBalance('no funds');"],
    }
    base.update(overrides)
    return base


def test_reviewer_valido_se_ancla_al_diff() -> None:
    reviewer = LlmReviewer(llm=FakeLlm(_response(_finding_json())))
    result = reviewer.review(repo="cqm/emotivoo-v2", pr_id=1, diff=DIFF)

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.file == TRANSFER
    assert finding.line == 14
    # Una persona LLM no depende de un artefacto del CI: no degrada la review.
    assert result.tooling_status is None
    assert finding.severity is Severity.P2


def test_salida_invalida_no_tumba_la_review() -> None:
    reviewer = LlmReviewer(llm=FakeLlm("{esto no es json"))
    result = reviewer.review(repo="r", pr_id=1, diff=DIFF)
    assert result.findings == []


def test_fallo_del_modelo_no_tumba_la_review() -> None:
    reviewer = LlmReviewer(llm=FakeLlm(raises=RuntimeError("cuota agotada")))
    result = reviewer.review(repo="r", pr_id=1, diff=DIFF)
    assert result.findings == []


def test_fichero_inventado_por_el_modelo_se_descarta() -> None:
    reviewer = LlmReviewer(llm=FakeLlm(_response(_finding_json(file="src/NoExiste.php"))))
    assert reviewer.review(repo="r", pr_id=1, diff=DIFF).findings == []


def test_linea_inexistente_cae_al_ancla_por_evidencia() -> None:
    # El modelo informa de una linea que no existe; la evidencia cita la linea
    # real, asi que el hallazgo no se pierde pero se ancla bien.
    reviewer = LlmReviewer(llm=FakeLlm(_response(_finding_json(line=9999))))
    result = reviewer.review(repo="r", pr_id=1, diff=DIFF)
    assert len(result.findings) == 1
    assert result.findings[0].line == 14


def test_confidence_no_anclada_se_normaliza() -> None:
    reviewer = LlmReviewer(llm=FakeLlm(_response(_finding_json(confidence=42))))
    result = reviewer.review(repo="r", pr_id=1, diff=DIFF)
    assert result.findings[0].confidence == 50


def test_tema_protegido_se_infiere_del_texto() -> None:
    entry = _finding_json(
        title="SQL construido por concatenacion",
        why_it_matters="Una entrada del usuario llega a una consulta sql",
    )
    reviewer = LlmReviewer(llm=FakeLlm(_response(entry)))
    result = reviewer.review(repo="r", pr_id=1, diff=DIFF)
    assert result.findings[0].protected_subject is not None


def test_evidencia_ausente_se_sintetiza_de_la_linea() -> None:
    reviewer = LlmReviewer(llm=FakeLlm(_response(_finding_json(evidence=[], line=13))))
    result = reviewer.review(repo="r", pr_id=1, diff=DIFF)
    assert len(result.findings) == 1
    assert result.findings[0].evidence


def test_respeta_el_tope_de_hallazgos() -> None:
    entries = [_finding_json(title=f"H{i}") for i in range(5)]
    reviewer = LlmReviewer(llm=FakeLlm(_response(*entries)), max_findings=2)
    assert len(reviewer.review(repo="r", pr_id=1, diff=DIFF).findings) == 2


def test_fence_markdown_se_tolera() -> None:
    raw = f"```json\n{_response(_finding_json())}\n```"
    reviewer = LlmReviewer(llm=FakeLlm(raw))
    assert len(reviewer.review(repo="r", pr_id=1, diff=DIFF).findings) == 1


def test_diff_vacio_no_llama_al_modelo() -> None:
    llm = FakeLlm(_response(_finding_json()))
    assert LlmReviewer(llm=llm).review(repo="r", pr_id=1, diff="  ").findings == []
    assert llm.calls == []


def test_presupuesto_se_registra(caplog) -> None:
    reviewer = LlmReviewer(llm=FakeLlm(_response(_finding_json())), budget_tokens_per_pr=500_000)
    with caplog.at_level(logging.INFO):
        reviewer.review(repo="r", pr_id=1, diff=DIFF)
    assert "presupuesto" in caplog.text


# --- Integracion con el grafo: merge deduplica por (fichero, titulo) ---


class StaticReviewer:
    def __init__(self, name: str, findings: list[Finding]) -> None:
        self.name = name
        self._findings = findings

    def review(self, *, repo: str, pr_id: int, diff: str, head_sha: str = "") -> ReviewerResult:
        del repo, pr_id, diff, head_sha
        return ReviewerResult(findings=list(self._findings))


class FakeVcs:
    def __init__(self) -> None:
        self.inlines: list[tuple[str, int, str]] = []
        self.comments: list[str] = []

    def get_pull_request(self, repo: str, pr_id: int) -> PullRequestInfo:
        return PullRequestInfo(
            repo=repo,
            pr_id=pr_id,
            title="t",
            source_branch="f",
            destination_branch="main",
            author="a",
            head_sha="h",
            url="https://example.test/pr",
        )

    def get_diff(self, repo: str, pr_id: int) -> str:
        del repo, pr_id
        return DIFF

    def get_diffstat(self, repo: str, pr_id: int) -> list[tuple[str, int, int]]:
        del repo, pr_id
        return [(TRANSFER, 3, 0)]

    def resolve_inline_anchor(self, repo: str, pr_id: int, *, path: str, text: str):
        del repo, pr_id, text
        return InlineAnchor(path=path, line=14)

    def post_inline_comment(
        self, repo: str, pr_id: int, *, path: str, line: int, body: str, from_line=None
    ) -> None:
        del repo, pr_id, from_line
        self.inlines.append((path, line, body))

    def post_comment(self, repo: str, pr_id: int, body: str) -> None:
        del repo, pr_id
        self.comments.append(body)

    def post_task(self, repo: str, pr_id: int, body: str) -> None:
        del repo, pr_id, body

    def list_review_markers(self, repo: str, pr_id: int) -> list:
        del repo, pr_id
        return []


def _static_duplicate() -> Finding:
    return Finding(
        id="standards-x",
        title="Saldo negativo no validado antes de retirar",
        severity=Severity.P3,
        file=TRANSFER,
        line=14,
        why_it_matters="Otra persona lo ve igual",
        autofix_class=AutofixClass.ADVISORY,
        owner=Owner.HUMAN,
        requires_verification=False,
        confidence=75,
        evidence=[f"{TRANSFER}:14 throw new InsufficientBalance('no funds');"],
    )


def test_merge_deduplica_persona_llm_y_estatica() -> None:
    llm_reviewer = LlmReviewer(llm=FakeLlm(_response(_finding_json())))
    vcs = FakeVcs()
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    graph = build_graph(
        vcs=vcs,
        reviewers=[llm_reviewer, StaticReviewer("standards", [_static_duplicate()])],
        policy=policy,
    ).compile()

    result = graph.invoke(
        {
            "request": {"repo": "cqm/emotivoo-v2", "pr_id": 7, "head_sha": "abc"},
            "findings": [],
        }
    )

    assert len(result["merged"]) == 1, "el merge debe unificar por (fichero, titulo)"
    assert len(vcs.inlines) == 1
