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
    Severity,
)
from reviews.policy.engine import PolicyEngine
from reviews.ports.chat import ChatPort
from reviews.ports.reviewer import ReviewerPort
from reviews.ports.sandbox import SandboxPort
from reviews.ports.vcs import VcsPort

logger = logging.getLogger(__name__)

_SEVERITY_ORDER = {
    Severity.P0: 0,
    Severity.P1: 1,
    Severity.P2: 2,
    Severity.P3: 3,
}


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


def _quote_for_anchor(finding: Finding) -> str:
    """Texto distintivo para diff-line: la parte citada, sin prefijo file:line."""
    raw = finding.evidence[0] if finding.evidence else finding.title
    head, _, rest = raw.partition(" ")
    if rest and ":" in head:
        # p.ej. "src/foo.py:10 $balance->frozen" -> "$balance->frozen"
        return rest.strip()
    return raw.strip()


def build_graph(
    *,
    vcs: VcsPort,
    reviewers: list[ReviewerPort],
    policy: PolicyEngine,
    chat: ChatPort | None = None,
    sandbox: SandboxPort | None = None,
):
    """Construye el grafo de review.

    Fase 1: scope -> fan-out -> merge -> policy -> publish (inline + resumen).
    HITL y fix llegan en las fases 2 y 3.
    """
    del sandbox  # reservado para Fase 3

    def scope(state: ReviewState) -> ReviewState:
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
        found = reviewer.review(
            repo=request.repo,
            pr_id=request.pr_id,
            diff=state["diff"],
            head_sha=request.head_sha,
        )
        return {"findings": found}

    def merge(state: ReviewState) -> ReviewState:
        """Deduplica por (fichero, titulo) y conserva la evidencia mas rica."""
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

        by_id = {d.finding_id: d for d in decisions}
        max_inline = int(policy.guards.get("max_inline_comments", 20))
        ordered = sorted(findings, key=lambda f: _SEVERITY_ORDER[f.severity])

        inlined: list[Finding] = []
        summary_only: list[Finding] = []

        for finding in ordered:
            decision = by_id.get(_finding_id(finding))
            if decision is None:
                raise ReviewIntegrityError(
                    f"Hallazgo sin decision de politica: {_finding_id(finding)}"
                )
            if len(inlined) >= max_inline:
                summary_only.append(finding)
                continue
            quote = _quote_for_anchor(finding)
            try:
                anchor = vcs.resolve_inline_anchor(
                    request.repo, request.pr_id, path=finding.file, text=quote
                )
            except Exception:
                logger.exception(
                    "Fallo al resolver ancla %s:%s", finding.file, finding.line
                )
                anchor = None
            if anchor is None:
                summary_only.append(finding)
                continue
            body = _render_inline(finding, decision)
            try:
                vcs.post_inline_comment(
                    request.repo,
                    request.pr_id,
                    path=anchor.path,
                    line=anchor.line,
                    body=body,
                    from_line=anchor.from_line,
                )
                inlined.append(finding)
            except Exception:
                logger.exception(
                    "Fallo al publicar inline %s:%s", finding.file, finding.line
                )
                summary_only.append(finding)

        summary = _render_summary(findings, decisions, summary_only=summary_only, inlined=inlined)
        vcs.post_comment(request.repo, request.pr_id, summary)
        if chat is not None:
            pr = vcs.get_pull_request(request.repo, request.pr_id)
            chat.notify(
                title=f"Review de {request.repo}#{request.pr_id}",
                body=_render_teams_body(findings, decisions, inlined_count=len(inlined)),
                link=pr.url or None,
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


def _render_inline(finding: Finding, decision: Decision) -> str:
    lines = [
        f"**[{finding.severity.value}] {finding.title}**",
        "",
        finding.why_it_matters,
        "",
        f"_Accion: {decision.action.value}_ · confianza {finding.confidence}",
    ]
    if finding.suggested_fix:
        lines.extend(["", f"**Sugerencia:** {finding.suggested_fix}"])
    if decision.action.value == "report" and decision.reason:
        lines.append(f"_Politica: {decision.reason}_")
    return "\n".join(lines)


def _render_summary(
    findings: list[Finding],
    decisions: list[Decision],
    *,
    summary_only: list[Finding] | None = None,
    inlined: list[Finding] | None = None,
) -> str:
    by_key = {d.finding_id: d for d in decisions}
    inlined = inlined or []
    summary_only = summary_only or []
    lines = [
        "## Code review automatica",
        "",
        f"{len(findings)} hallazgos · {len(inlined)} inline · "
        f"{len(summary_only)} solo en resumen",
        "",
    ]
    for finding in sorted(findings, key=lambda f: _SEVERITY_ORDER[f.severity]):
        decision = by_key.get(_finding_id(finding))
        if decision is None:
            raise ReviewIntegrityError(
                f"Hallazgo sin decision de politica: {_finding_id(finding)}"
            )
        marker = "inline" if finding in inlined else "resumen"
        lines.append(
            f"- **[{finding.severity.value}] {finding.title}** "
            f"(`{finding.file}:{finding.line}`) · {decision.action.value} · {marker}"
        )
        lines.append(f"  {finding.why_it_matters}")
    return "\n".join(lines)


def _render_teams_body(
    findings: list[Finding],
    decisions: list[Decision],
    *,
    inlined_count: int = 0,
) -> str:
    pending = [d for d in decisions if d.action.value in PENDING_ACTIONS]
    lines = [
        f"{len(findings)} hallazgos ({inlined_count} inline), "
        f"{len(pending)} requieren decision."
    ]
    for finding in sorted(findings, key=lambda f: _SEVERITY_ORDER[f.severity])[:5]:
        lines.append(f"{finding.severity.value} · {finding.title} ({finding.file})")
    return "\n".join(lines)


def outcome_from_state(state: ReviewState) -> ReviewOutcome:
    return ReviewOutcome(
        request=ReviewRequest.model_validate(state["request"]),
        findings=_merged(state),
        decisions=state.get("decisions", []),
    )
