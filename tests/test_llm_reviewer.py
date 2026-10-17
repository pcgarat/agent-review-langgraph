from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from agent_review.adapters.budget_memory import InMemoryBudget
from agent_review.adapters.llm_reviewer import LlmReviewer
from agent_review.domain.models import (
    AutofixClass,
    Finding,
    Owner,
    ReviewerResult,
    Severity,
)
from agent_review.errors import TransientError
from agent_review.graph.build import build_graph
from agent_review.policy.engine import PolicyEngine
from agent_review.ports.llm import LlmReply
from agent_review.ports.vcs import InlineAnchor, PullRequestInfo

FIXTURES = Path(__file__).parent / "fixtures"
DIFF = (FIXTURES / "diff_emotivoo.diff").read_text(encoding="utf-8")
TRANSFER = "src/BackOffice/Application/Wallet/Transfer.php"


class FakeLlm:
    def __init__(
        self,
        response: str = "",
        *,
        raises: Exception | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        self._response = response
        self._raises = raises
        self._prompt_tokens = prompt_tokens
        self._completion_tokens = completion_tokens
        self.calls: list[tuple[str, str, int]] = []
        self.json_modes: list[str | None] = []

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        json_mode=None,
        response_schema=None,
    ) -> LlmReply:
        del response_schema
        self.calls.append((system, prompt, max_tokens))
        self.json_modes.append(json_mode)
        if self._raises is not None:
            raise self._raises
        return LlmReply(
            text=self._response,
            prompt_tokens=self._prompt_tokens,
            completion_tokens=self._completion_tokens,
        )


def _response(*findings: dict) -> str:
    return json.dumps({"findings": list(findings)})


class _ToggleBudget(InMemoryBudget):
    """Adaptador que se puede cortar a voluntad para provocar el corte (B4)."""

    def __init__(self) -> None:
        super().__init__(total=10_000_000)
        self.allow = True

    def reserve(self, review_key: str, tokens: int) -> bool:
        return self.allow and super().reserve(review_key, tokens)


def _finding_json(**overrides) -> dict:
    base = {
        "title": "Saldo negativo no validado antes de retirar",
        "severity": "P2",
        "file": TRANSFER,
        "line": 14,
        "why_it_matters": "Se lanza una excepcion de dominio sin proteger el estado",
        "autofix_class": "gated_auto",
        "owner": "downstream-resolver",
        "failure_scenario": "escenario de prueba",
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


def test_presupuesto_agotado_corta_la_llamada_al_modelo(caplog) -> None:
    # B4: sin saldo en el contador compartido, la persona no llama al modelo y
    # declara el hecho del corte.
    llm = FakeLlm(_response(_finding_json()))
    budget = InMemoryBudget(total=1)
    reviewer = LlmReviewer(llm=llm, budget=budget)
    with caplog.at_level(logging.WARNING):
        result = reviewer.review(repo="r", pr_id=1, diff=DIFF, review_key="r:1")
    assert result.findings == []
    assert result.budget_exhausted is True
    assert llm.calls == [], "no debe llamarse al modelo si excede el presupuesto"
    assert "presupuesto" in caplog.text


def test_tope_blando_por_persona_corta_antes_de_reservar(caplog) -> None:
    # B10: el tope blando acota lo que una persona con prompt grande se come del
    # presupuesto comun, aunque al contador le quede saldo de sobra.
    llm = FakeLlm(_response(_finding_json()))
    budget = InMemoryBudget(total=500_000)
    reviewer = LlmReviewer(llm=llm, budget=budget, budget_limit=1)
    with caplog.at_level(logging.WARNING):
        result = reviewer.review(repo="r", pr_id=1, diff=DIFF, review_key="r:1")
    assert result.budget_exhausted is True
    assert llm.calls == []
    assert budget.reserved("r:1") == 0, "no se reserva si el tope blando ya corta"
    assert "tope de la persona" in caplog.text


def test_presupuesto_amplio_permite_la_llamada() -> None:
    llm = FakeLlm(_response(_finding_json()))
    budget = InMemoryBudget(total=500_000)
    reviewer = LlmReviewer(llm=llm, budget=budget)
    result = reviewer.review(repo="r", pr_id=1, diff=DIFF, review_key="r:1")
    assert len(result.findings) == 1
    assert reviewer.last_estimated_tokens > 0
    assert result.budget_exhausted is False
    assert result.tokens_used >= 0
    assert budget.reserved("r:1") == 0, "la reserva se confirma al volver"


def test_el_gasto_real_ajusta_la_reserva() -> None:
    # B3: se reserva la estimacion y se confirma el `usage` real del proveedor.
    llm = FakeLlm(_response(_finding_json()), prompt_tokens=7, completion_tokens=3)
    budget = InMemoryBudget(total=500_000)
    reviewer = LlmReviewer(llm=llm, budget=budget, budget_limit=500_000)
    reviewer.review(repo="r", pr_id=1, diff=DIFF, review_key="r:1")
    assert reviewer.tokens_used == 10
    assert budget.spent("r:1") == 10, "el contador confirma el gasto real, no la estimacion"
    assert budget.reserved("r:1") == 0, "la reserva se ajusta al confirmar"


def test_los_hechos_no_se_arrastran_entre_reviews() -> None:
    # El worker reutiliza la instancia: el corte de una review no puede marcar la
    # siguiente (los hechos son por review, B4).
    budget = _ToggleBudget()
    llm = FakeLlm(_response(_finding_json()), prompt_tokens=7, completion_tokens=3)
    reviewer = LlmReviewer(llm=llm, budget=budget)

    limpia = reviewer.review(repo="r", pr_id=1, diff=DIFF, review_key="r:1")
    assert limpia.budget_exhausted is False
    assert limpia.tokens_used == 10

    budget.allow = False
    cortada = reviewer.review(repo="r", pr_id=1, diff=DIFF, review_key="r:2")
    assert cortada.budget_exhausted is True
    assert cortada.tokens_used == 0, "el gasto de la review anterior no se arrastra"

    budget.allow = True
    de_nuevo = reviewer.review(repo="r", pr_id=1, diff=DIFF, review_key="r:3")
    assert de_nuevo.budget_exhausted is False


def test_un_fallo_transitorio_del_modelo_devuelve_la_reserva() -> None:
    # B8: la reserva no se queda colgada si la llamada lanza: el reintento del nodo
    # no debe reservar dos veces.
    llm = FakeLlm(raises=TransientError("boom"))
    budget = InMemoryBudget(total=500_000)
    reviewer = LlmReviewer(llm=llm, budget=budget)
    with pytest.raises(TransientError):
        reviewer.review(repo="r", pr_id=1, diff=DIFF, review_key="r:1")
    assert budget.reserved("r:1") == 0
    assert budget.spent("r:1") == 0


def test_last_seen_files_expoe_lo_que_entro_en_el_prompt() -> None:
    # E11 de 006: la cobertura se mide sobre lo que el modelo vio de verdad.
    llm = FakeLlm(_response(_finding_json()))
    reviewer = LlmReviewer(llm=llm)
    reviewer.review(repo="r", pr_id=1, diff=DIFF)
    assert TRANSFER in reviewer.last_seen_files
    # Deduplicado y en el orden del diff (cada fichero una sola vez).
    assert len(reviewer.last_seen_files) == len(set(reviewer.last_seen_files))
    assert reviewer.last_seen_files == tuple(reviewer.last_seen_files)


def test_last_seen_files_vacio_si_no_hay_diff() -> None:
    reviewer = LlmReviewer(llm=FakeLlm(_response(_finding_json())))
    reviewer.review(repo="r", pr_id=1, diff="   ")
    assert reviewer.last_seen_files == ()


# --- Integracion con el grafo: merge deduplica por (fichero, titulo) ---


class StaticReviewer:
    def __init__(self, name: str, findings: list[Finding]) -> None:
        self.name = name
        self._findings = findings

    def review(
        self,
        *,
        repo: str,
        pr_id: int,
        diff: str,
        head_sha: str = "",
        review_key: str = "",
    ) -> ReviewerResult:
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
        failure_scenario="escenario de prueba",
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


def test_p0_p2_sin_escenario_se_descarta_por_el_contrato() -> None:
    # 007 I1: el modelo tiene que trazar el fallo; opinar no basta.
    sin = {k: v for k, v in _finding_json().items() if k != "failure_scenario"}
    reviewer = LlmReviewer(llm=FakeLlm(_response(sin, sin | {"severity": "P3"})))
    findings = reviewer.review(repo="r", pr_id=1, diff=DIFF).findings
    assert [f.severity for f in findings] == [Severity.P3]


def test_el_modelo_no_puede_autoverificarse() -> None:
    from agent_review.domain.models import Verification

    entry = _finding_json(verification="verified_grounded", verification_evidence=["lo digo yo"])
    [finding] = (
        LlmReviewer(llm=FakeLlm(_response(entry))).review(repo="r", pr_id=1, diff=DIFF).findings
    )
    assert finding.verification is Verification.UNVERIFIED
    assert finding.verification_evidence == []
    assert finding.failure_scenario == "escenario de prueba"


def test_context_scope_del_modelo_se_lee_y_lo_desconocido_cae_a_none() -> None:
    from agent_review.domain.models import ContextScope

    fuera = _finding_json(context_scope="outside_diff")
    raro = _finding_json(context_scope="quizas", line=13, title="Otro hallazgo")
    findings = (
        LlmReviewer(llm=FakeLlm(_response(fuera, raro)))
        .review(repo="r", pr_id=1, diff=DIFF)
        .findings
    )
    assert [f.context_scope for f in findings] == [ContextScope.OUTSIDE_DIFF, None]


def test_el_prompt_pide_escenario_y_alcance_y_no_requires_verification() -> None:
    llm = FakeLlm(_response())
    LlmReviewer(llm=llm).review(repo="r", pr_id=1, diff=DIFF)
    prompt = llm.calls[0][1]
    assert "failure_scenario" in prompt
    assert "context_scope" in prompt
    assert "requires_verification" not in prompt
