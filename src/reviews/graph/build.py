from __future__ import annotations

import logging
import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from reviews.domain.models import (
    PENDING_ACTIONS,
    Decision,
    Finding,
    ReviewOutcome,
    ReviewRequest,
)
from reviews.policy.engine import PolicyEngine
from reviews.ports.chat import ChatPort
from reviews.ports.reviewer import ReviewerPort
from reviews.ports.sandbox import SandboxPort
from reviews.ports.vcs import VcsPort

logger = logging.getLogger(__name__)


class ReviewIntegrityError(RuntimeError):
    """El grafo produjo un estado incoherente (hallazgo sin decision)."""


class ReviewState(TypedDict, total=False):
    request: ReviewRequest
    diff: str
    # Las personas escriben aqui en paralelo; el reducer acumula.
    findings: Annotated[list[Finding], operator.add]
    # Resultado del merge: sin reducer, se sustituye (no se acumula).
    merged: list[Finding]
    # Juicio de la politica sobre cada hallazgo mergeado. Sin reducer.
    # `pending_approvals` no vive aqui: es derivado de decisions.
    decisions: list[Decision]


def _finding_id(finding: Finding) -> str:
    return finding.id or f"{finding.file}:{finding.line}:{finding.title}"


def _merge_key(finding: Finding) -> tuple[str, str]:
    """Criterio unico de dedupe. Debe coincidir en todo el grafo."""
    return (finding.file, finding.title)


def _merged(state: ReviewState) -> list[Finding]:
    """Hallazgos tras el merge; si el nodo aun no corrio, cae a los crudos."""
    return state.get("merged") or state.get("findings", [])


def build_graph(
    *,
    vcs: VcsPort,
    reviewers: list[ReviewerPort],
    policy: PolicyEngine,
    chat: ChatPort | None = None,
    sandbox: SandboxPort | None = None,
):
    """Construye el grafo de review.

    Fase 0 implementa: scope -> fan-out de personas -> merge -> policy -> publish.
    Los nodos de HITL (interrupt) y de fix llegan en las fases 2 y 3.
    """

    def scope(state: ReviewState) -> ReviewState:
        # langgraph puede entregar el estado como dict plano; normalizamos.
        request = ReviewRequest.model_validate(state["request"])
        diff = vcs.get_diff(request.repo, request.pr_id)
        return {"request": request, "diff": diff, "findings": []}

    def dispatch(state: ReviewState) -> list:
        """Fan-out: una rama por persona, sobre el mismo diff."""
        from langgraph.types import Send

        request = ReviewRequest.model_validate(state["request"])
        return [
            Send(
                "persona",
                {"request": request, "diff": state["diff"], "reviewer": reviewer.name},
            )
            for reviewer in reviewers
        ]

    def persona(state: ReviewState) -> ReviewState:
        name = state.get("reviewer", "")  # type: ignore[typeddict-item]
        reviewer = next((r for r in reviewers if r.name == name), None)
        if reviewer is None:
            return {"findings": []}
        request = ReviewRequest.model_validate(state["request"])
        found = reviewer.review(repo=request.repo, pr_id=request.pr_id, diff=state["diff"])
        return {"findings": found}

    def merge(state: ReviewState) -> ReviewState:
        """Deduplica por (fichero, titulo) y conserva la evidencia mas rica.

        Escribe en `merged` (sin reducer) para no reacumular lo ya acumulado.
        """
        seen: dict[tuple[str, str], Finding] = {}
        for finding in state.get("findings", []):
            key = _merge_key(finding)
            current = seen.get(key)
            if current is None or len(finding.evidence) > len(current.evidence):
                seen[key] = finding
        merged = list(seen.values())
        deduped = len(state.get("findings", [])) - len(merged)
        if deduped:
            logger.info("Merge: %d hallazgos duplicados eliminados", deduped)
        return {"merged": merged}

    def apply_policy(state: ReviewState) -> ReviewState:
        decisions: list[Decision] = []
        seen: set[tuple[str, str]] = set()
        for finding in _merged(state):
            key = _merge_key(finding)
            if key in seen:
                continue
            seen.add(key)
            result = policy.decide(finding)
            decisions.append(
                Decision(
                    finding_id=_finding_id(finding),
                    action=result.action,
                    reason=result.reason,
                    matched_rule=result.matched_rule,
                    policy_version=result.policy_version,
                )
            )
        return {"decisions": decisions}

    def publish(state: ReviewState) -> ReviewState:
        request = ReviewRequest.model_validate(state["request"])
        findings = _merged(state)
        decisions = state.get("decisions", [])
        if not findings:
            logger.info("PR %s#%s: sin hallazgos", request.repo, request.pr_id)
            return {}

        body = _render_summary(findings, decisions)
        vcs.post_comment(request.repo, request.pr_id, body)
        if chat is not None:
            chat.notify(
                title=f"Review de {request.repo}#{request.pr_id}",
                body=_render_teams_body(findings, decisions),
            )
        return {}

    graph = StateGraph(ReviewState)
    graph.add_node("scope", scope)
    graph.add_node("persona", persona)
    graph.add_node("merge", merge)
    graph.add_node("policy", apply_policy)
    graph.add_node("publish", publish)

    graph.add_edge(START, "scope")
    graph.add_conditional_edges("scope", dispatch, ["persona"])
    graph.add_edge("persona", "merge")
    graph.add_edge("merge", "policy")
    graph.add_edge("policy", "publish")
    graph.add_edge("publish", END)

    return graph


def _render_summary(findings: list[Finding], decisions: list[Decision]) -> str:
    by_key = {d.finding_id: d for d in decisions}
    lines = ["## Code review automatica", ""]
    for finding in findings:
        decision = by_key.get(_finding_id(finding))
        if decision is None:
            # Nunca inventar una accion: si falta la decision, se grita.
            raise ReviewIntegrityError(
                f"Hallazgo sin decision de politica: {_finding_id(finding)}"
            )
        lines.append(
            f"- **[{finding.severity.value}] {finding.title}** "
            f"(`{finding.file}:{finding.line}`) · confianza {finding.confidence} · "
            f"{decision.action.value}"
        )
        lines.append(f"  {finding.why_it_matters}")
    return "\n".join(lines)


def _render_teams_body(findings: list[Finding], decisions: list[Decision]) -> str:
    pending = [d for d in decisions if d.action.value in PENDING_ACTIONS]
    lines = [f"{len(findings)} hallazgos, {len(pending)} requieren decision."]
    for finding in findings[:5]:
        lines.append(f"{finding.severity.value} · {finding.title} ({finding.file})")
    return "\n".join(lines)


def outcome_from_state(state: ReviewState) -> ReviewOutcome:
    return ReviewOutcome(
        request=ReviewRequest.model_validate(state["request"]),
        findings=_merged(state),
        decisions=state.get("decisions", []),
    )
