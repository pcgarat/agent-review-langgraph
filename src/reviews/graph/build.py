from __future__ import annotations

import base64
import hashlib
import logging
import operator
import shlex
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy, Send, interrupt
from pydantic import ValidationError

from reviews.domain.models import (
    PENDING_ACTIONS,
    ApprovalRequest,
    Decision,
    DecisionOutcome,
    Finding,
    HitlDecision,
    PendingDecision,
    ReviewOutcome,
    ReviewRequest,
    Severity,
    ToolingSourceStatus,
)
from reviews.errors import is_transient
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
    # Persona que produjo cada hallazgo (`_finding_id` -> nombre). Solo la usa el
    # merge para fusionar el mismo problema visto por dos personas con anclas
    # cercanas sin fusionar dos hallazgos de la misma persona en lineas distintas.
    finding_sources: Annotated[dict[str, str], _merge_sources]
    # Estado de las fuentes estaticas por reviewer. Sin reducer: se sustituye, y
    # cada rama solo escribe el suyo (el fan-out escribe dicts sobre claves
    # distintas). Las claves ausentes se tratan como PRESENT.
    source_status: Annotated[dict[str, ToolingSourceStatus], _merge_status]
    # Resultado del merge: sin reducer, se sustituye (no se acumula).
    merged: list[Finding]
    # Juicio de la politica sobre cada hallazgo mergeado. Sin reducer.
    # `pending_approvals` no vive aqui: es derivado de decisions.
    decisions: list[Decision]
    # HITL (Fase 2): decision humana. La escribe `hitl` al reanudar; sin ella, si
    # hay pendientes, el grafo pausa antes de publicar.
    human_decisions: HitlDecision | None
    # Lo escribe `publish` cuando esta pasada publico de verdad (no si se salto por
    # marcas). `notify` solo avisa en Teams si esta a True: el aviso va en su propio
    # nodo para que un fallo de Teams no repita `publish` y el reintento (que
    # continua desde el checkpoint) retome justo el aviso.
    published: bool
    inlined_count: int


def _merge_sources(left: dict[str, str] | None, right: dict[str, str] | None) -> dict[str, str]:
    return {**(left or {}), **(right or {})}


def _merge_status(
    left: dict[str, ToolingSourceStatus] | None,
    right: dict[str, ToolingSourceStatus] | None,
) -> dict[str, ToolingSourceStatus]:
    return {**(left or {}), **(right or {})}


def build_approval(state: ReviewState) -> ApprovalRequest | None:
    """Carga del HITL si hay decisiones pendientes; `None` si no las hay.

    Nivel de modulo (no dentro de `build_graph`) porque el worker tambien lo
    necesita: al pausar, es lo que guarda en el inbox y publica en el aviso (§4.4).
    """
    request = ReviewRequest.model_validate(state["request"])
    by_id = {d.finding_id: d for d in state.get("decisions", [])}
    pending: list[PendingDecision] = []
    for finding in _merged(state):
        decision = by_id.get(_finding_id(finding))
        if decision is None or decision.action.value not in PENDING_ACTIONS:
            continue
        pending.append(
            PendingDecision(
                finding_id=decision.finding_id,
                proposed_action=decision.action,
                reason=decision.reason,
                matched_rule=decision.matched_rule,
                policy_version=decision.policy_version,
                title=finding.title,
                file=finding.file,
                line=finding.line,
                severity=finding.severity,
            )
        )
    if not pending:
        return None
    return ApprovalRequest(
        review_key=request.review_key,
        repo=request.repo,
        pr_id=request.pr_id,
        head_sha=request.head_sha,
        pending=pending,
        # La decision debe cubrir todos los pendientes (`build_decision`): la pista
        # los nombra todos; se cambia `--approve` por `--reject`/`--noted` donde toque.
        # Los ids pueden venir del LLM (espacios, parentesis...): se citan para que
        # la pista se pueda pegar tal cual en la shell.
        command_hint=(
            f"reviews decide {shlex.quote(request.review_key)} --actor <tu-nombre> "
            + " ".join(f"--approve {shlex.quote(item.finding_id)}" for item in pending)
        ),
    )


def render_pause_notice(approval: ApprovalRequest) -> str:
    """Cuerpo del aviso de pausa en el PR. **Sin** marca de dedupe (spec §4.5).

    Llevar el token de dedupe lo haria leer como review publicada y el `publish`
    tras decidir no publicaria nada. El aviso es informativo: que quedo pausada,
    por que, y como decidir.
    """
    lines = [
        f"Review pausada: {len(approval.pending)} hallazgo(s) esperan una decision humana.",
        "",
    ]
    for item in approval.pending:
        lines.append(
            f"- `{item.finding_id}` [{item.severity.value}] {item.proposed_action.value}: "
            f"{item.title} ({item.file}:{item.line})"
        )
        if item.reason.strip():
            lines.append(f"  _{item.reason.strip()}_")
    lines.append("")
    lines.append(f"Para decidir: `{approval.command_hint}`")
    return "\n".join(lines)


def _unavailable_sources(state: ReviewState) -> list[tuple[str, ToolingSourceStatus]]:
    return [
        (name, status)
        for name, status in sorted(state.get("source_status", {}).items())
        if not status.available
    ]


def _finding_id(finding: Finding) -> str:
    return finding.id or f"{finding.file}:{finding.line}:{finding.title}"


def _merge_key(finding: Finding) -> tuple[str, int, str]:
    """Criterio unico de dedupe. Debe coincidir en todo el grafo.

    La linea forma parte de la clave: PHPStan repite el mismo mensaje en lineas
    distintas de un fichero («Call to an undefined method…») y son hallazgos
    distintos. Solo se fusiona el mismo problema en el mismo sitio.
    """
    return (finding.file, finding.line, finding.title)


# Distancia maxima entre anclas para fusionar el mismo hallazgo visto por dos
# personas (p. ej. PHPStan en la linea del error y el LLM en la de la llamada).
_CROSS_PERSONA_NEAR_LINES = 5


def _merge_findings(findings: list[Finding], sources: dict[str, str]) -> list[Finding]:
    """Deduplica y conserva la evidencia mas rica de cada grupo.

    Dos reglas (B5 de docs/revision-main-20261011.md):

    - Mismo `_merge_key` (fichero, linea, titulo): es el mismo hallazgo.
    - Mismo fichero y titulo con anclas a `_CROSS_PERSONA_NEAR_LINES` o menos, de
      personas **distintas**: es el mismo problema detectado dos veces. Dos
      hallazgos de la misma persona en lineas distintas nunca acaban en el mismo
      grupo, ni siquiera a traves de un tercero de otra persona.

    El resultado no depende del orden del fan-out: se agrupa en un orden canonico
    (linea, persona, id) y solo se compara dentro de cada (fichero, titulo).
    """

    def source_of(finding: Finding) -> str:
        return sources.get(_finding_id(finding), "")

    def joins(group: list[Finding], finding: Finding) -> bool:
        source = source_of(finding)
        if any(_merge_key(other) == _merge_key(finding) for other in group):
            return True
        if not source:
            return False
        near_other = False
        for other in group:
            other_source = source_of(other)
            if other_source == source:
                # Misma persona en otra linea: hallazgo distinto, nunca se fusiona.
                return False
            if other_source and abs(other.line - finding.line) <= _CROSS_PERSONA_NEAR_LINES:
                near_other = True
        return near_other

    buckets: dict[tuple[str, str], list[list[Finding]]] = {}
    groups: list[list[Finding]] = []
    for finding in sorted(
        findings, key=lambda f: (f.file, f.title, f.line, source_of(f), _finding_id(f))
    ):
        candidates = buckets.setdefault((finding.file, finding.title), [])
        group = next((g for g in candidates if joins(g, finding)), None)
        if group is None:
            group = []
            candidates.append(group)
            groups.append(group)
        group.append(finding)

    # Se conserva el orden de llegada para no reordenar la publicacion.
    position = {id(f): i for i, f in enumerate(findings)}
    groups.sort(key=lambda group: min(position[id(f)] for f in group))
    # Representante: la evidencia mas rica; a igualdad, el primero en orden canonico.
    return [max(group, key=lambda f: len(f.evidence)) for group in groups]


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


def default_retry_policy() -> RetryPolicy:
    """Politica de reintento de nodo por defecto (fallos transitorios).

    Sustituye al `default_retry_on` de LangGraph, que **no** reintenta
    `RuntimeError` (de la que heredan las excepciones del repo), `TimeoutError` ni
    un `429`: con el default, ningun fallo de `twg` se reintentaria. Aplica
    solo a los nodos con efectos externos (`scope`, `persona`, `publish`, `notify`);
    `merge` y `policy` son puros y no la necesitan.
    """
    return RetryPolicy(
        max_attempts=3,
        initial_interval=0.5,
        backoff_factor=2.0,
        max_interval=8.0,
        retry_on=is_transient,
    )


def build_graph(
    *,
    vcs: VcsPort,
    reviewers: list[ReviewerPort],
    policy: PolicyEngine,
    chat: ChatPort | None = None,
    sandbox: SandboxPort | None = None,
    scope_ignore: str = "",
    retry_policy: RetryPolicy | None = None,
) -> StateGraph[ReviewState]:
    """Construye el grafo de review.

    scope -> fan-out -> merge -> policy -> hitl -> publish (inline + resumen) ->
    notify (Teams). El fix llega en la fase 3.
    """
    del sandbox  # reservado para Fase 3
    # Antes de definir los nodos: `persona` necesita saber cuantos intentos tiene.
    retry = retry_policy if retry_policy is not None else default_retry_policy()

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

    def persona(state: ReviewState, runtime: Runtime[None]) -> ReviewState:
        name = state["reviewer"]
        reviewer = next((r for r in reviewers if r.name == name), None)
        if reviewer is None:
            return {"findings": []}
        request = ReviewRequest.model_validate(state["request"])
        try:
            result = reviewer.review(
                repo=request.repo,
                pr_id=request.pr_id,
                diff=state["diff"],
                head_sha=request.head_sha,
            )
        except Exception as exc:
            # Un fallo transitorio se relanza mientras queden intentos (lo reintenta
            # la `RetryPolicy`). En el ultimo, la persona degrada a 0 hallazgos: una
            # persona caida (p. ej. el LLM) no tumba la review ni se lleva por
            # delante los hallazgos de las demas.
            info = runtime.execution_info
            attempt = info.node_attempt if info is not None else 1
            if not is_transient(exc) or attempt < retry.max_attempts:
                raise
            logger.warning(
                "Persona %s sin hallazgos: fallo transitorio tras %d intento(s) (%s)",
                name,
                attempt,
                exc,
            )
            return {"findings": []}
        update: ReviewState = {
            "findings": result.findings,
            "finding_sources": {_finding_id(f): reviewer.name for f in result.findings},
        }
        if result.tooling_status is not None:
            update["source_status"] = {reviewer.name: result.tooling_status}
        return update

    def merge(state: ReviewState) -> ReviewState:
        """Deduplica (ver `_merge_findings`) y conserva la evidencia mas rica."""
        merged = _merge_findings(state.get("findings", []), state.get("finding_sources", {}))
        deduped = len(state.get("findings", [])) - len(merged)
        if deduped:
            logger.info("Merge: %d hallazgos duplicados eliminados", deduped)
        return {"merged": merged}

    def apply_policy(state: ReviewState) -> ReviewState:
        decisions: list[Decision] = []
        seen: set[tuple[str, int, str]] = set()
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

    def hitl(state: ReviewState) -> ReviewState:
        """Pausa durable si hay decisiones pendientes; si no, sigue a publish.

        `interrupt` no bloquea: el `invoke` retorna con el hilo pendiente y el
        worker no espera a nadie (ver docs/fase2-hitl-20261011.md, §4.2).
        """
        if state.get("human_decisions") is not None:
            return {}
        approval = build_approval(state)
        if approval is None:
            return {}
        # Bucle de validacion: `build_decision` ya exige cubrir todos los
        # pendientes, pero un job forjado o antiguo podria no hacerlo. No se lanza:
        # LangGraph guarda el valor del resume en el checkpoint antes de ejecutar el
        # nodo y lo re-entregaria en cada reanudacion, dejando el hilo atascado.
        # Se vuelve a pausar y la siguiente decision responde al nuevo `interrupt`.
        while True:
            resume_value = interrupt(approval)
            try:
                decision = (
                    resume_value
                    if isinstance(resume_value, HitlDecision)
                    else HitlDecision.model_validate(resume_value)
                )
            except ValidationError as exc:
                logger.warning(
                    "Decision ilegible sobre %s (%d error(es)); se vuelve a pausar",
                    approval.review_key,
                    exc.error_count(),
                )
                continue
            missing = [
                p.finding_id for p in approval.pending if decision.outcome_for(p.finding_id) is None
            ]
            if not missing:
                return {"human_decisions": decision}
            logger.warning(
                "Decision incompleta de %s sobre %s (faltan: %s); se vuelve a pausar",
                decision.actor,
                approval.review_key,
                ", ".join(sorted(missing)),
            )

    def publish(state: ReviewState) -> ReviewState:
        request = ReviewRequest.model_validate(state["request"])
        findings = _merged(state)
        decisions = state.get("decisions", [])
        human = state.get("human_decisions")
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
            if (
                human is not None
                and human.outcome_for(decision.finding_id) is DecisionOutcome.REJECTED
            ):
                # Descartado por una persona: va al resumen, sin inline.
                summary_only.append(finding)
                continue
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
            body = _render_inline(finding, decision, token=request.dedupe_token, human=human)
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
            human=human,
        )
        if not _has_marker(published, _summary_token(request.dedupe_token, complete)):
            vcs.post_comment(request.repo, request.pr_id, summary)
        return {"published": True, "inlined_count": len(inlined)}

    def notify(state: ReviewState) -> ReviewState:
        """Aviso en Teams de una review recien publicada. Nodo propio (B9).

        Un fallo transitorio lo reintenta la `RetryPolicy` del nodo y, si se agota,
        el reintento del job continua aqui (no repite `publish`). Un fallo no
        transitorio es un bug del canal: se registra y no tumba una review que ya
        esta publicada en el PR.
        """
        if chat is None or not state.get("published"):
            return {}
        request = ReviewRequest.model_validate(state["request"])
        try:
            pr = vcs.get_pull_request(request.repo, request.pr_id)
            chat.notify(
                title=f"Review de {request.repo}#{request.pr_id}",
                body=_render_teams_body(
                    _merged(state),
                    state.get("decisions", []),
                    inlined_count=state.get("inlined_count", 0),
                ),
                link=pr.url or None,
            )
        except Exception as exc:
            if is_transient(exc):
                raise
            logger.exception(
                "No se pudo avisar en Teams de %s#%s; la review ya esta publicada",
                request.repo,
                request.pr_id,
            )
        return {}

    graph = StateGraph(ReviewState)
    # Reintento de nodo (RetryPolicy) solo en los nodos que llaman a fuera: un
    # fallo transitorio de twg/LLM/Teams se recupera sin sacar el job de la cola.
    # `merge` y `policy` son deterministas y no lo llevan; `hitl` tampoco (un
    # reintento re-ejecutaria `interrupt` y duplicaria la pausa).
    graph.add_node("scope", scope, retry_policy=retry)
    graph.add_node("persona", persona, retry_policy=retry)
    graph.add_node("merge", merge)
    graph.add_node("policy", apply_policy)
    graph.add_node("hitl", hitl)
    graph.add_node("publish", publish, retry_policy=retry)
    graph.add_node("notify", notify, retry_policy=retry)

    graph.add_edge(START, "scope")
    graph.add_conditional_edges("scope", dispatch, ["persona"])
    graph.add_edge("persona", "merge")
    graph.add_edge("merge", "policy")
    graph.add_edge("policy", "hitl")
    graph.add_edge("hitl", "publish")
    graph.add_edge("publish", "notify")
    graph.add_edge("notify", END)

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


def _decision_line(human: HitlDecision | None, decision: Decision) -> str | None:
    """Linea de decision humana, solo si la hay para este hallazgo."""
    if human is None:
        return None
    outcome = human.outcome_for(decision.finding_id)
    if outcome is None:
        return None
    label = _OUTCOME_LABEL.get(outcome, outcome.value)
    return f"_Decision: {label} · {human.actor}_"


_OUTCOME_LABEL = {
    DecisionOutcome.APPROVED: "aprobado",
    DecisionOutcome.REJECTED: "descartado",
    DecisionOutcome.NOTED: "anotado",
    DecisionOutcome.EXPIRED: "sin decision (expirado)",
}


def _render_inline(
    finding: Finding,
    decision: Decision,
    *,
    token: str = "",
    human: HitlDecision | None = None,
) -> str:
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
    decision_line = _decision_line(human, decision)
    if decision_line:
        blocks.append(decision_line)
    policy = _policy_line(decision)
    if policy:
        blocks.append(policy)
    if finding.suggested_fix:
        blocks.append(f"**Sugerencia:** {finding.suggested_fix}")
    body = "\n\n".join(blocks)
    if not token:
        return body
    # Mismo id que la comprobacion de `publish`: con `finding.id` vacio, todos los
    # inlines compartian marca y el segundo se daba por publicado.
    return body + f"\n\n{_inline_token(token, _finding_id(finding))}"


def _render_summary(
    findings: list[Finding],
    decisions: list[Decision],
    *,
    summary_only: list[Finding] | None = None,
    inlined: list[Finding] | None = None,
    unavailable: list[tuple[str, ToolingSourceStatus]] | None = None,
    token: str = "",
    complete: bool = True,
    human: HitlDecision | None = None,
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
        decision_line = _decision_line(human, decision)
        if decision_line:
            lines.append(f"  {decision_line}")
        if human is not None and human.outcome_for(decision.finding_id) is DecisionOutcome.REJECTED:
            lines.append("  Descartado por una persona: no se anota inline.")
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


def outcome_from_state(state: ReviewState, *, paused: bool = False) -> ReviewOutcome:
    """Construye el resultado. `paused` se pasa explicito al leer un hilo pausado.

    El `__interrupt__` no vive en los `values` del snapshot (LangGraph lo aparta en
    `snapshot.interrupts`), asi que la mera presencia en el estado no basta: quien
    lee un `get_state` de un hilo pausado lo sabe y lo declara.
    """
    return ReviewOutcome(
        request=ReviewRequest.model_validate(state["request"]),
        findings=_merged(state),
        decisions=state.get("decisions", []),
        unavailable_sources={
            name: status
            for name, status in state.get("source_status", {}).items()
            if not status.available
        },
        human_decisions=state.get("human_decisions"),
        paused=paused or bool(state.get("__interrupt__")),
    )
