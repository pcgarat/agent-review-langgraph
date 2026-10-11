from __future__ import annotations

import base64
import hashlib
import logging
import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from reviews.domain.models import (
    PENDING_ACTIONS,
    Decision,
    Finding,
    ReviewOutcome,
    ReviewRequest,
    Severity,
    ToolingSourceStatus,
)
from reviews.policy.engine import PolicyEngine
from reviews.ports.chat import ChatPort
from reviews.ports.reviewer import ReviewerPort
from reviews.ports.sandbox import SandboxPort
from reviews.ports.vcs import ReviewMarker, VcsPort
from reviews.scope import filter_diff, limit_files, merge_ignore_patterns

logger = logging.getLogger(__name__)

_SEVERITY_ORDER = {
    Severity.P0: 0,
    Severity.P1: 1,
    Severity.P2: 2,
    Severity.P3: 3,
}

# Texto de cabecera por estado de fuente estatica, para que "sin hallazgos" no
# se lea como "sin errores" cuando el analisis no llego a ejecutarse.
_SOURCE_NOTE = {
    ToolingSourceStatus.MISSING: (
        "Aviso: no se encontro la salida de la herramienta estatica; "
        "la review NO cubre ese analisis."
    ),
    ToolingSourceStatus.UNREADABLE: (
        "Aviso: la salida de la herramienta estatica no se pudo leer; "
        "la review NO cubre ese analisis."
    ),
}


class ReviewIntegrityError(RuntimeError):
    """El grafo produjo un estado incoherente (hallazgo sin decision)."""


class ReviewState(TypedDict, total=False):
    request: ReviewRequest
    diff: str
    # Persona asignada a esta rama del fan-out (la escribe cada `Send`).
    reviewer: str
    # Las personas escriben aqui en paralelo; el reducer acumula.
    findings: Annotated[list[Finding], operator.add]
    # Estado de las fuentes estaticas por reviewer. Sin reducer: se sustituye, y
    # cada rama solo escribe el suyo (el fan-out escribe dicts sobre claves
    # distintas). Las claves ausentes se tratan como PRESENT.
    source_status: Annotated[dict[str, ToolingSourceStatus], _merge_status]
    # Resultado del merge: sin reducer, se sustituye (no se acumula).
    merged: list[Finding]
    # Juicio de la politica sobre cada hallazgo mergeado. Sin reducer.
    # `pending_approvals` no vive aqui: es derivado de decisions.
    decisions: list[Decision]


def _merge_status(
    left: dict[str, ToolingSourceStatus] | None,
    right: dict[str, ToolingSourceStatus] | None,
) -> dict[str, ToolingSourceStatus]:
    return {**(left or {}), **(right or {})}


def _unavailable_sources(state: ReviewState) -> list[tuple[str, ToolingSourceStatus]]:
    return [
        (name, status)
        for name, status in sorted(state.get("source_status", {}).items())
        if not status.available
    ]


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
    scope_ignore: str = "",
) -> StateGraph[ReviewState]:
    """Construye el grafo de review.

    Fase 1: scope -> fan-out -> merge -> policy -> publish (inline + resumen).
    HITL y fix llegan en las fases 2 y 3.
    """
    del sandbox  # reservado para Fase 3

    def scope(state: ReviewState) -> ReviewState:
        request = ReviewRequest.model_validate(state["request"])
        diff = vcs.get_diff(request.repo, request.pr_id)
        # Ruido fuera (lockfiles, generados, vendor) y, si se declara, tope de
        # ficheros por PR. Ambas cosas acotan lo que ven las personas.
        allowed: list[str] | None = None
        max_files = int(policy.guards.get("max_files_per_pr", 0) or 0)
        if max_files > 0:
            diffstat = vcs.get_diffstat(request.repo, request.pr_id)
            if diffstat:
                kept, truncated = limit_files(diffstat, max_files)
                if truncated:
                    logger.warning(
                        "PR %s#%s: %d ficheros superan max_files_per_pr=%d; se revisan %d",
                        request.repo,
                        request.pr_id,
                        len(diffstat),
                        max_files,
                        len(kept),
                    )
                allowed = kept
        diff = filter_diff(diff, merge_ignore_patterns(scope_ignore), allowed=allowed)
        return {"request": request, "diff": diff, "findings": [], "source_status": {}}

    def dispatch(state: ReviewState) -> list[Send]:
        """Fan-out: una rama por persona, sobre el mismo diff."""
        request = ReviewRequest.model_validate(state["request"])
        return [
            Send(
                "persona",
                {"request": request, "diff": state["diff"], "reviewer": reviewer.name},
            )
            for reviewer in reviewers
        ]

    def persona(state: ReviewState) -> ReviewState:
        name = state["reviewer"]
        reviewer = next((r for r in reviewers if r.name == name), None)
        if reviewer is None:
            return {"findings": []}
        request = ReviewRequest.model_validate(state["request"])
        result = reviewer.review(
            repo=request.repo,
            pr_id=request.pr_id,
            diff=state["diff"],
            head_sha=request.head_sha,
        )
        update: ReviewState = {"findings": result.findings}
        if result.tooling_status is not None:
            update["source_status"] = {reviewer.name: result.tooling_status}
        return update

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
        unavailable = _unavailable_sources(state)
        if not findings and not unavailable:
            logger.info("PR %s#%s: sin hallazgos", request.repo, request.pr_id)
            return {}

        complete = not unavailable
        published = _published_markers(vcs, request)
        # Solo se salta si ESTA ejecucion ya dejo su marca con la misma
        # completitud (o una completa, que no se degrada). Un reintento
        # degradado no repite el aviso; uno completo posterior si publica y
        # sustituye al degradado. La marca es de la ejecucion, no del commit:
        # un `pipeline_id` nuevo sobre el mismo `sha` es una review nueva.
        already_complete = _has_marker(
            published, _summary_token(request.dedupe_token, complete=True)
        )
        already_same = _has_marker(
            published, _summary_token(request.dedupe_token, complete=complete)
        )
        if already_complete or already_same:
            logger.info(
                "PR %s#%s: esta ejecucion (%s) ya publico una review (%s); no se re-publica",
                request.repo,
                request.pr_id,
                request.thread_id,
                "completa" if complete else "degradada",
            )
            return {}
        if unavailable:
            # La review NO esta completa: no se puede leer "sin hallazgos" como
            # "sin errores". Se publica igual, con el aviso por delante.
            logger.warning(
                "PR %s#%s: review incompleta, fuente estatica sin cubrir (%s)",
                request.repo,
                request.pr_id,
                ", ".join(f"{name}={status.value}" for name, status in unavailable),
            )

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
                logger.exception("Fallo al resolver ancla %s:%s", finding.file, finding.line)
                anchor = None
            if anchor is None:
                summary_only.append(finding)
                continue
            body = _render_inline(finding, decision, token=request.dedupe_token)
            if _has_marker(published, _inline_token(request.dedupe_token, _finding_id(finding))):
                # Ya esta publicado (p.ej. el resumen no llego a escribirse por un
                # fallo transitorio): no se repite el inline. La marca lleva el id
                # del hallazgo, asi que dos titulos iguales en ficheros distintos
                # no se confunden.
                inlined.append(finding)
                continue
            try:
                vcs.post_inline_comment(
                    request.repo,
                    request.pr_id,
                    path=anchor.path,
                    line=anchor.line,
                    body=body,
                    from_line=anchor.from_line,
                )
                published.append(ReviewMarker(id="", body=body))
                inlined.append(finding)
            except Exception:
                logger.exception("Fallo al publicar inline %s:%s", finding.file, finding.line)
                summary_only.append(finding)

        summary = _render_summary(
            findings,
            decisions,
            summary_only=summary_only,
            inlined=inlined,
            unavailable=unavailable,
            token=request.dedupe_token,
            complete=complete,
        )
        if not _has_marker(published, _summary_token(request.dedupe_token, complete)):
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


def _published_markers(vcs: VcsPort, request: ReviewRequest) -> list[ReviewMarker]:
    """Lo que el agente ya publico en este PR (marca leida como respaldo).

    Nunca aborta: si el VCS no responde, se publica (mejor un duplicado que una
    review perdida).
    """
    try:
        return vcs.list_review_markers(request.repo, request.pr_id)
    except Exception:
        logger.exception(
            "No se pudieron leer los comentarios del PR %s#%s; se publica igual",
            request.repo,
            request.pr_id,
        )
        return []


def _has_marker(markers: list[ReviewMarker], needle: str) -> bool:
    """¿Algun comentario publicado lleva esta marca?"""
    return any(needle in (marker.body or "") for marker in markers)


def _summary_token(token: str, complete: bool) -> str:
    suffix = ":ok" if complete else ":partial"
    return f"<!-- {token}{suffix} -->"


def _inline_token(token: str, finding_id: str) -> str:
    """Marca por hallazgo, corta a proposito.

    Bitbucket pinta los comentarios HTML en el cuerpo, asi que el id del hallazgo
    (que puede ser largo) se comprime a base32 para que el pie sea lo mas discreto
    posible sin perder la distincion entre dos hallazgos con el mismo titulo.
    """
    digest = base64.b32encode(hashlib.sha256(finding_id.encode()).digest()).decode()[:8].lower()
    return f"<!-- {token}:i:{digest} -->"


def _footer(token: str, *, complete: bool) -> str:
    """Marca de dedupe: identifica lo publicado y si la review fue completa.

    Bitbucket no oculta los comentarios HTML, asi que esta marca se ve; se
    mantiene deliberadamente en una sola linea al pie para minimizar el ruido.
    """
    return f"\n\n<!-- {token}{_token_suffix(complete=complete)} -->"


def _token_suffix(*, complete: bool) -> str:
    return ":ok" if complete else ":partial"


def _policy_line(decision: Decision) -> str | None:
    """Razon de la politica, solo si aporta (no la generica de relleno)."""
    reason = decision.reason.strip()
    if not reason or reason == "Regla de politica":
        return None
    return f"_Politica: {reason}_"


def _render_inline(finding: Finding, decision: Decision, *, token: str = "") -> str:
    # La persona estatica propaga el mensaje crudo de la herramienta como titulo
    # (truncado a 100) y como `why_it_matters` (completo). Repetir ambos ensena
    # lo mismo dos veces; mostrar solo el titulo perderia el final del mensaje.
    # Cuando el titulo es el mensaje truncado, la cabecera usa el mensaje entero
    # y no hay cuerpo aparte.
    why = " ".join(finding.why_it_matters.split())
    title = " ".join(finding.title.split())
    title_is_truncation = bool(why) and why.lower().startswith(title.lower())
    header = why if title_is_truncation else title
    blocks = [f"**[{finding.severity.value}] {header}**"]
    if why and not title_is_truncation:
        blocks.append(why)
    blocks.append(f"_Accion: {decision.action.value}_ · confianza {finding.confidence}")
    policy = _policy_line(decision)
    if policy:
        blocks.append(policy)
    if finding.suggested_fix:
        blocks.append(f"**Sugerencia:** {finding.suggested_fix}")
    body = "\n\n".join(blocks)
    if not token:
        return body
    return body + f"\n\n{_inline_token(token, finding.id)}"


def _render_summary(
    findings: list[Finding],
    decisions: list[Decision],
    *,
    summary_only: list[Finding] | None = None,
    inlined: list[Finding] | None = None,
    unavailable: list[tuple[str, ToolingSourceStatus]] | None = None,
    token: str = "",
    complete: bool = True,
) -> str:
    by_key = {d.finding_id: d for d in decisions}
    inlined = inlined or []
    summary_only = summary_only or []
    lines: list[str] = []
    if unavailable:
        lines.extend(_render_incomplete_header(unavailable))
    lines.extend(
        [
            "## Code review automatica",
            "",
            f"{len(findings)} hallazgos · {len(inlined)} inline · "
            f"{len(summary_only)} solo en resumen",
            "",
        ]
    )
    for finding in sorted(findings, key=lambda f: _SEVERITY_ORDER[f.severity]):
        decision = by_key.get(_finding_id(finding))
        if decision is None:
            raise ReviewIntegrityError(f"Hallazgo sin decision de politica: {_finding_id(finding)}")
        marker = "inline" if finding in inlined else "resumen"
        lines.append(
            f"- **[{finding.severity.value}] {finding.title}** "
            f"(`{finding.file}:{finding.line}`) · {decision.action.value} · {marker}"
        )
        # El detalle ya vive en el inline; no se repite aqui salvo que sea
        # imprescindible (hallazgo que solo figura en el resumen).
        if finding not in inlined:
            lines.append(f"  {finding.why_it_matters.strip()}")
    summary = "\n".join(lines)
    return summary + _footer(token, complete=complete) if token else summary


def _render_incomplete_header(
    unavailable: list[tuple[str, ToolingSourceStatus]],
) -> list[str]:
    detail = "; ".join(
        f"{name} ({status.value}): {_SOURCE_NOTE.get(status, 'salida no disponible')}"
        for name, status in unavailable
    )
    return [
        "> **Review incompleta:** falta la salida de una o mas herramientas "
        "estaticas declaradas por el perfil del repo. Esta review no cubre ese "
        f"analisis y su ausencia de hallazgos no equivale a un PR limpio. {detail}",
        "",
    ]


def _render_teams_body(
    findings: list[Finding],
    decisions: list[Decision],
    *,
    inlined_count: int = 0,
) -> str:
    pending = [d for d in decisions if d.action.value in PENDING_ACTIONS]
    lines = [
        f"{len(findings)} hallazgos ({inlined_count} inline), {len(pending)} requieren decision."
    ]
    for finding in sorted(findings, key=lambda f: _SEVERITY_ORDER[f.severity])[:5]:
        lines.append(f"{finding.severity.value} · {finding.title} ({finding.file})")
    return "\n".join(lines)


def outcome_from_state(state: ReviewState) -> ReviewOutcome:
    return ReviewOutcome(
        request=ReviewRequest.model_validate(state["request"]),
        findings=_merged(state),
        decisions=state.get("decisions", []),
        unavailable_sources={
            name: status
            for name, status in state.get("source_status", {}).items()
            if not status.available
        },
    )
