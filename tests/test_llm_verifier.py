"""VF5: LlmVerifier sobre el bucle *ReAct* de 016 (009 §5.4).

Sin red: el modelo es un `LlmPort` guionizado y el lector es el
`FixtureContextReader`. Se prueba el camino feliz (read_file → veredicto) y el
cierre sin veredicto al agotar el tope.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from context_fakes import APP, DIFF, build_fixture_reader

from agent_review.adapters.context_fixture import FixtureContextReader
from agent_review.adapters.llm_verifier import LlmVerifier
from agent_review.adapters.tool_loop import citation_path
from agent_review.domain.models import (
    AutofixClass,
    Finding,
    Owner,
    Severity,
    Verification,
)
from agent_review.ports.context import ContextLimits, ContextReaderPort
from agent_review.ports.llm import LlmReply
from agent_review.ports.verifier import Verdict


class ScriptedLlm:
    """Devuelve respuestas en orden; se repite la ultima."""

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses) or ["{}"]
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        json_mode: str | None = None,
        response_schema: dict[str, Any] | None = None,
        on_reasoning: Any = None,
    ) -> LlmReply:
        del response_schema, on_reasoning
        index = min(len(self.calls), len(self._responses) - 1)
        self.calls.append(
            {"system": system, "prompt": prompt, "max_tokens": max_tokens, "json_mode": json_mode}
        )
        return LlmReply(text=self._responses[index], prompt_tokens=10, completion_tokens=5)


def _finding() -> Finding:
    return Finding(
        id="f-loop",
        title="bucle infinito en route",
        severity=Severity.P1,
        file=APP,
        line=3,
        why_it_matters="el bucle no termina",
        autofix_class=AutofixClass.MANUAL,
        owner=Owner.HUMAN,
        confidence=75,
        evidence=[f"{APP}:3 needle = 1"],
        failure_scenario=f"estado con intentos -> {APP}:3 -> bucle sin fin",
    )


def _tools(*entries: dict[str, Any]) -> str:
    return json.dumps({"tools": list(entries)})


def _verdict(**fields: Any) -> str:
    payload = {
        "verdict": "refuted",
        "evidence": [f"{APP}:3 needle = 1"],
        "reason": "el codigo termina",
        **fields,
    }
    return json.dumps(payload)


def test_prompt_refuta_y_pide_json(tmp_path: Path) -> None:
    llm = ScriptedLlm(_verdict())
    verifier = LlmVerifier(llm, build_fixture_reader(tmp_path))
    verifier.verify(_finding(), repo="r", head_sha="abc", diff=DIFF)

    system = llm.calls[0]["system"]
    assert "REFUTAR" in system
    assert llm.calls[0]["json_mode"] == "json_object"
    prompt = llm.calls[0]["prompt"]
    assert "bucle infinito" in prompt
    assert APP in prompt
    assert "needle" in prompt or "hunk" in prompt.lower() or "diff" in prompt


def test_read_file_y_veredicto_citan_fichero_servido(tmp_path: Path) -> None:
    # Aceptacion VF5: pide read_file, emite veredicto; la evidencia cita lo servido.
    reader = build_fixture_reader(tmp_path)
    llm = ScriptedLlm(
        _tools({"tool": "read_file", "path": APP}),
        _verdict(verdict="refuted", evidence=[f"{APP}:3 needle = 1"]),
    )
    result = LlmVerifier(llm, reader).verify(_finding(), repo="r", head_sha="abc", diff=DIFF)

    assert result.verdict is Verdict.REFUTED
    assert APP in result.files_read
    assert result.evidence
    assert all(citation_path(c) in result.files_read for c in result.evidence)
    assert "<tool_result>" in llm.calls[-1]["prompt"]


def test_sin_veredicto_en_el_tope_queda_unproven(tmp_path: Path) -> None:
    # Aceptacion VF5: agotado el tope sin objeto final → UNPROVEN.
    reader = build_fixture_reader(tmp_path)
    # Solo pide herramientas; el turno final forzado tampoco emite veredicto.
    llm = ScriptedLlm(_tools({"tool": "read_file", "path": APP}))
    result = LlmVerifier(llm, reader, limits=ContextLimits(max_turns=1, max_calls=8)).verify(
        _finding(), repo="r", head_sha="abc", diff=DIFF
    )

    assert result.verdict is Verdict.UNPROVEN
    assert "sin veredicto" in result.reason
    assert APP in result.files_read


def test_tope_de_llamadas_y_bytes_deja_unproven(tmp_path: Path) -> None:
    # 009 VF7 / criterio 8: VERIFIER_MAX_TOOL_CALLS / _CONTEXT_BYTES cortan a UNPROVEN.
    reader = build_fixture_reader(tmp_path)
    llm = ScriptedLlm(_tools({"tool": "read_file", "path": APP}))
    # max_calls=0: ninguna herramienta; el turno forzado sigue pidiendo tools → UNPROVEN.
    by_calls = LlmVerifier(llm, reader, limits=ContextLimits(max_turns=4, max_calls=0)).verify(
        _finding(), repo="r", head_sha="abc", diff=DIFF
    )
    assert by_calls.verdict is Verdict.UNPROVEN

    llm_bytes = ScriptedLlm(_tools({"tool": "read_file", "path": APP}))
    by_bytes = LlmVerifier(
        llm_bytes, reader, limits=ContextLimits(max_turns=4, max_calls=8, max_bytes=1)
    ).verify(_finding(), repo="r", head_sha="abc", diff=DIFF)
    assert by_bytes.verdict is Verdict.UNPROVEN


def test_reserve_false_deja_unproven(tmp_path: Path) -> None:
    # 009 VF7 / 017 B7: presupuesto compartido; reserve False corta como un tope.
    class RejectBudget:
        def reserve(self, review_key: str, tokens: int) -> bool:
            del review_key, tokens
            return False

        def commit(self, review_key: str, reserved: int, actual: int) -> None:
            del review_key, reserved, actual

        def remaining(self, review_key: str) -> int:
            del review_key
            return 10_000

    reader = build_fixture_reader(tmp_path)
    llm = ScriptedLlm(_tools({"tool": "read_file", "path": APP}))
    result = LlmVerifier(llm, reader, budget=RejectBudget()).verify(
        _finding(), repo="r", head_sha="abc", diff=DIFF, review_key="r:1"
    )
    assert result.verdict is Verdict.UNPROVEN
    assert llm.calls, "el turno de salvamento sigue llamando al modelo"


def test_caso_c_bucle_fix_wait_queda_refutado(tmp_path: Path) -> None:
    # 009 VF11 / criterio 3: leer la ruta real desmiente «bucle infinito»;
    # el hallazgo se conserva en merged (V6).
    from agent_review.graph.build import build_graph
    from agent_review.policy.engine import PolicyEngine
    from agent_review.ports.vcs import InlineAnchor, PullRequestInfo

    fixture = Path(__file__).parent / "fixtures" / "verifier_case_c" / "route.py"
    root = tmp_path / "repo"
    path = "src/agent_review/graph/build.py"
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    reader = FixtureContextReader(root)

    finding = Finding(
        id="caso-c",
        title="_route_fix_wait es un bucle infinito",
        severity=Severity.P1,
        file=path,
        line=1,
        why_it_matters="el grafo nunca saldria de fix_wait",
        autofix_class=AutofixClass.MANUAL,
        owner=Owner.HUMAN,
        confidence=75,
        evidence=[f"{path}:1 return fix_dispatch"],
        failure_scenario=f"siempre hay FAILED -> {path}:1 -> vuelve a fix_dispatch sin fin",
    )
    llm = ScriptedLlm(
        _tools({"tool": "read_file", "path": path}),
        _verdict(
            verdict="refuted",
            evidence=[f"{path}:1 attempts < fix_max_attempts"],
            reason="cada vuelta consume un intento; el bucle termina",
        ),
    )
    verifier = LlmVerifier(llm, reader)

    class _Vcs:
        def get_pull_request(self, repo: str, pr_id: int) -> PullRequestInfo:
            return PullRequestInfo(
                repo=repo,
                pr_id=pr_id,
                title="t",
                source_branch="f",
                destination_branch="main",
                author="a",
                head_sha="abc",
                url="u",
            )

        def get_diff(self, repo: str, pr_id: int) -> str:
            del repo, pr_id
            from diff_helpers import added_lines_diff

            return added_lines_diff((path, 1))

        def resolve_inline_anchor(self, repo, pr_id, *, path, text):  # noqa: ANN001
            del repo, pr_id, text
            return InlineAnchor(path=path, line=1)

        def list_review_markers(self, repo: str, pr_id: int) -> list:
            del repo, pr_id
            return []

        def post_comment(self, repo, pr_id, body):  # noqa: ANN001
            del repo, pr_id, body

        def post_inline_comment(self, repo, pr_id, *, path, line, body, from_line=None):  # noqa: ANN001
            del repo, pr_id, path, line, body, from_line

        def post_task(self, repo, pr_id, body):  # noqa: ANN001
            del repo, pr_id, body

    class _Reviewer:
        name = "correctness"

        def review(self, **kwargs):  # noqa: ANN003
            del kwargs
            from agent_review.domain.models import ReviewerResult

            return ReviewerResult(findings=[finding])

    graph = build_graph(
        vcs=_Vcs(),
        reviewers=[_Reviewer()],
        policy=PolicyEngine.from_file("config/autonomy.yaml"),
        verifier=verifier,
    ).compile()
    state = graph.invoke(
        {
            "request": {"repo": "r", "pr_id": 1, "head_sha": "abc"},
            "findings": [],
        }
    )
    assert len(state["merged"]) == 1
    assert state["merged"][0].id == "caso-c"
    assert state["merged"][0].verification is Verification.REFUTED
    assert state["verifications"]["caso-c"].verdict is Verdict.REFUTED
    assert path in state["verifications"]["caso-c"].files_read
    assert len(llm.calls) >= 2, "pidio read_file y luego el veredicto"


def test_confirmed_conserva_escenario_y_evidencia(tmp_path: Path) -> None:
    llm = ScriptedLlm(
        _verdict(
            verdict="confirmed",
            failure_scenario=f"intentos agotados -> {APP}:3 -> no avanza",
            evidence=[f"{APP}:3 needle = 1"],
            reason="la ruta falla",
        )
    )
    result = LlmVerifier(llm, build_fixture_reader(tmp_path)).verify(
        _finding(), repo="r", head_sha="abc", diff=DIFF
    )
    assert result.verdict is Verdict.CONFIRMED
    assert result.failure_scenario is not None
    assert "->" in result.failure_scenario
    assert result.evidence == [f"{APP}:3 needle = 1"]


def test_open_reader_recibe_base_sha_y_diff(tmp_path: Path) -> None:
    # 009 V8: la fabrica del worker necesita (repo, head_sha, base_sha, diff) para que
    # el modo degradado del lector pueda servir `ref=base` y sembrar los ficheros del
    # diff; sin ellos el verificador no puede comprobar `pre_existing`.
    seen: list[tuple[str, str, str, str]] = []

    def factory(repo: str, head_sha: str, base_sha: str, diff: str) -> ContextReaderPort:
        seen.append((repo, head_sha, base_sha, diff))
        return build_fixture_reader(tmp_path)

    LlmVerifier(ScriptedLlm(_verdict()), open_reader=factory).verify(
        _finding(), repo="r", head_sha="abc", base_sha="base1", diff=DIFF
    )
    assert seen == [("r", "abc", "base1", DIFF)]
