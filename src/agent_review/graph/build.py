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

from agent_review.domain.models import (
    PENDING_ACTIONS,
    ApprovalRequest,
    Decision,
    DecisionOutcome,
    Finding,
    FixResolutions,
    FixResult,
    FixStatus,
    FixWaitRequest,
    HitlDecision,
    PendingDecision,
    ReviewOutcome,
    ReviewRequest,
    Severity,
    ToolingSourceStatus,
)
from agent_review.errors import is_transient
from agent_review.fix.delivery import DeliveryFormatter
from agent_review.fix.node import apply_resolutions, build_wave, dispatch_fixes
from agent_review.policy.engine import PolicyEngine
from agent_review.ports.chat import ChatPort
from agent_review.ports.fixer import FixerPort
from agent_review.ports.reviewer import ReviewerPort
from agent_review.ports.vcs import ReviewMarker, VcsPort
from agent_review.scope import filter_diff, limit_files, merge_ignore_patterns

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
    # Personas que se cortaron por presupuesto (017 B4). Reducer de union: cada rama
    # escribe la suya en paralelo y el merge no las pierde.
    budget_exhausted: Annotated[set[str], operator.or_]
    # Tokens confirmados por persona. Reducer de suma: el gasto total de la review.
    tokens_spent: Annotated[dict[str, int], _merge_tokens]
    # Tope del contador compartido, escrito una vez por `scope`. El saldo del
    # `ReviewOutcome` se deriva de el (`tokens_spent`) sin consultar el puerto: el
    # hecho viaja en el estado y el `replay` sigue siendo determinista (017 B4).
    budget_total: int | None
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
    # Fase 3: resultado del fix por hallazgo. Reducer de merge por `finding_id`
    # (como `_merge_sources`): un fix no re-ejecutado conserva su resultado.
    fixes: Annotated[dict[str, FixResult], _merge_fixes]
    # Recetas de la verificacion (las fija `scope` desde el perfil). Viajan en el
    # estado para que `build_fix_wait` (a nivel de modulo, la usa el worker) arme el
    # `VerifyJob` de cada `PENDING` sin conocer el perfil ni la configuracion.
    fix_recipes: list[str]
    # Tope de intentos del fix (de `guards.max_fix_attempts`). Lo necesita el
    # enrutado de `fix_wait` para decidir si un `FAILED` merece otra wave.
    fix_max_attempts: int
    # Valor del `Command(resume=...)` de `fix_wait`: la wave resuelta por el worker
    # de fixes. Al reanudar, el nodo lo lee; una reentrega sin resolucion no lo toca.
    fix_resolution: FixResolutions | None


def _merge_sources(left: dict[str, str] | None, right: dict[str, str] | None) -> dict[str, str]:
    return {**(left or {}), **(right or {})}


def _merge_status(
    left: dict[str, ToolingSourceStatus] | None,
    right: dict[str, ToolingSourceStatus] | None,
) -> dict[str, ToolingSourceStatus]:
    return {**(left or {}), **(right or {})}


def _merge_tokens(left: dict[str, int] | None, right: dict[str, int] | None) -> dict[str, int]:
    """Suma los tokens por persona: el fan-out escribe cada rama en su clave.

    Se suma y no se sustituye porque dos ramas pueden traer el mismo nombre (una
    persona reintentada); el gasto real es la suma de lo que gasto cada intento.
    """
    merged = dict(left or {})
    for name, tokens in (right or {}).items():
        merged[name] = merged.get(name, 0) + tokens
    return merged


def _merge_fixes(
    left: dict[str, FixResult] | None, right: dict[str, FixResult] | None
) -> dict[str, FixResult]:
    """Merge por `finding_id`: el resultado de un fix no re-ejecutado se conserva."""
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
                # La tarjeta del canal explica el problema con esto; el render de
                # texto del PR (`render_pause_notice`) sigue usando `reason`.
                why_it_matters=" ".join(finding.why_it_matters.split()),
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
            f"agent_review decide {shlex.quote(request.review_key)} --actor <tu-nombre> "
            + " ".join(f"--approve {shlex.quote(item.finding_id)}" for item in pending)
        ),
    )


def apply_hitl(state: ReviewState, resume_value: object) -> ReviewState:
    """Valida la decision humana y, si cubre los pendientes, la sella en el estado.

    Nivel de modulo (no dentro de `build_graph`) porque el worker la usa para saber
    si un `resume` dejo la pausa atras: sin la decision completa, el hilo sigue
    pausado. El nodo `hitl` la llama con el valor del `interrupt`; al re-lanzar tras
    una decision incompleta, la pausa se renueva (el `interrupt` vuelve a dispararse).
    """
    approval = build_approval(state)
    if approval is None:
        return {}
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
        return {}
    missing = [p.finding_id for p in approval.pending if decision.outcome_for(p.finding_id) is None]
    if missing:
        logger.warning(
            "Decision incompleta de %s sobre %s (faltan: %s); se vuelve a pausar",
            decision.actor,
            approval.review_key,
            ", ".join(sorted(missing)),
        )
        return {}
    return {"human_decisions": decision}


def build_pause(
    state: ReviewState,
) -> tuple[ApprovalRequest | None, FixWaitRequest | None]:
    """Pausas vigentes: `(human, fix)`. Como mucho una no nula (un hilo, una pausa).

    El canal (`worker._on_paused`) y el dispatch de la reanudacion (`process_request`)
    la usan para saber que espera la review y por que `kind` reanudar.
    """
    return build_approval(state), build_fix_wait(state)


def build_fix_wait(state: ReviewState) -> FixWaitRequest | None:
    """Wave de verificaciones pendientes, o `None` si no hay `PENDING`.

    Nivel de modulo porque el worker la necesita al pausar: es lo que registra en el
    inbox y lo que encola en la cola de fixes (§8 de la E7).
    """
    request = ReviewRequest.model_validate(state["request"])
    fixes = state.get("fixes", {})
    pending = [r for r in fixes.values() if r.status is FixStatus.PENDING and r.patch is not None]
    if not pending:
        return None
    recipes = state.get("fix_recipes") or []
    return build_wave(request, fixes, recipes=recipes, targets=[])


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

    Dos reglas (B5; docs/adr/0014-criterio-de-merge-entre-personas.md):

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
    fixer: FixerPort | None = None,
    delivery: DeliveryFormatter | None = None,
    fix_recipes: list[str] | None = None,
    scope_ignore: str = "",
    retry_policy: RetryPolicy | None = None,
    verification_enabled: bool = True,
    budget_total: int | None = None,
) -> StateGraph[ReviewState]:
    """Construye el grafo de review (Fase 3-E7).

    scope -> fan-out -> merge -> policy -> hitl -> fix_dispatch -> publish ->
    fix_wait -> publish_fix -> notify_fix -> notify. El grafo **no** recibe un
    `SandboxPort`: la verificacion vive en el worker de fixes, detras del
    `interrupt` de `fix_wait`; la ruta de la review nunca espera al sandbox.
    `fix_dispatch` es un no-op sin hallazgos aprobados con accion `fix_*`.
    `verification_enabled=False` (sin sandbox ni runner) marca los candidatos
    `NOT_VERIFIABLE` en `fix_dispatch`, de modo que `fix_wait` no llega a pausar.
    """
    # Antes de definir los nodos: `persona` necesita saber cuantos intentos tiene.
    retry = retry_policy if retry_policy is not None else default_retry_policy()
    recipes = fix_recipes or []

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
        # Las recetas y targets se fijan aqui (una vez por review, del perfil) y
        # viajan en el estado: `build_fix_wait` los necesita para armar la wave sin
        # conocer la configuracion.
        return {
            "request": request,
            "diff": diff,
            "findings": [],
            "source_status": {},
            "fix_recipes": list(recipes),
            "fix_max_attempts": int(policy.guards.get("max_fix_attempts", 2) or 1),
            "budget_total": budget_total,
        }

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
                review_key=request.review_key,
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
        if result.budget_exhausted:
            update["budget_exhausted"] = {reviewer.name}
        if result.tokens_used:
            update["tokens_spent"] = {reviewer.name: result.tokens_used}
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
        worker no espera a nadie (ver docs/iniciativas/archivo/002-fase2-hitl/spec.md, §4.2).
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
            update = apply_hitl(state, resume_value)
            if update:
                return update

    def fix_dispatch(state: ReviewState) -> ReviewState:
        """Fase 3-E7: despacha el fix de los aprobados con `fix_*`; no toca sandbox.

        Propone y valida localmente los candidatos y deja `PENDING` los
        verificables; **no** llama al sandbox. El `existing` del estado hace la
        funcion idempotente entre visitas: una resuelta se conserva y solo se
        re-propone un `FAILED` con intentos disponibles (otra wave). No-op sin
        candidatos: el grafo de Fase 2 queda igual.
        """
        request = ReviewRequest.model_validate(state["request"])
        fixes = dispatch_fixes(
            request=request,
            findings=_merged(state),
            decisions=state.get("decisions", []),
            human=state.get("human_decisions"),
            vcs=vcs,
            fixer=fixer,
            policy=policy,
            recipes=recipes,
            targets=[],
            existing=state.get("fixes", {}),
            verification_enabled=verification_enabled,
        )
        if fixes:
            pending = sum(1 for r in fixes.values() if r.status is FixStatus.PENDING)
            logger.info(
                "Fase 3-E7: despachados %d fix(es), %d en verificacion en %s",
                len(fixes),
                pending,
                request.review_key,
            )
        return {"fixes": fixes} if fixes else {}

    def fix_wait(state: ReviewState) -> ReviewState:
        """Pausa durable esperando la wave; al reanudar, aplica la resolucion.

        No verifica: el `POST /verify` vive en el worker de fixes. La primera visita
        (los `PENDING` que dejo `fix_dispatch`) pausa con la wave. Al reanudar
        (el worker de reviews invoca con la wave), LangGraph vuelve a ejecutar el
        nodo desde el inicio y `interrupt` devuelve la wave resuelta **en la misma
        ejecucion**: se aplica aqui abajo. Una wave sin resolucion para algun
        `PENDING` lo deja `NOT_VERIFIABLE`, nunca colgado.

        `state["fix_resolution"]` permite inyectar la wave sin `Command` (lo usa el
        worker en F17 y los tests); si ya viene, no se vuelve a pausar.
        """
        fixes = state.get("fixes", {})
        resolutions = state.get("fix_resolution")
        if resolutions is None:
            wave = build_fix_wait(state)
            if wave is None:
                return {}
            resolutions = interrupt(wave)
        wave_value = (
            resolutions
            if isinstance(resolutions, FixResolutions)
            else FixResolutions.model_validate(resolutions)
        )
        request = ReviewRequest.model_validate(state["request"])
        applied = apply_resolutions(request=request, fixes=fixes, resolutions=wave_value)
        # Reset del valor: una re-pausa posterior espera otra wave (otro `Command`).
        update: ReviewState = {"fix_resolution": None}
        if applied:
            update["fixes"] = applied
        return update

    def publish_fix(state: ReviewState) -> ReviewState:
        """Publica el resultado del fix resuelto como comentario de seguimiento.

        **No** publica en el inline (esa pluma vive en `publish`); es un comentario
        general con marca de dedupe propia (`:fix`), una sola vez por review.
        """
        request = ReviewRequest.model_validate(state["request"])
        fixes = state.get("fixes", {})
        resolved = {k: v for k, v in fixes.items() if _is_terminal_fix(v.status)}
        if not resolved:
            return {}
        marker = _fix_token(request.dedupe_token)
        if _has_marker(_published_markers(vcs, request), marker):
            return {}
        body = _render_fix_followup(request, resolved, _merged(state), delivery=delivery)
        vcs.post_comment(request.repo, request.pr_id, body)
        return {}

    def notify_fix(state: ReviewState) -> ReviewState:
        """Aviso compacto en Teams del resultado del fix (Q3: nodo propio, breve)."""
        if chat is None:
            return {}
        fixes = state.get("fixes", {})
        resolved = {k: v for k, v in fixes.items() if _is_terminal_fix(v.status)}
        if not resolved:
            return {}
        request = ReviewRequest.model_validate(state["request"])
        try:
            pr = vcs.get_pull_request(request.repo, request.pr_id)
            chat.notify(
                repo=request.repo,
                pr_id=request.pr_id,
                review_key=request.review_key,
                title=f"Fix de {request.repo}#{request.pr_id}",
                body=_render_fix_teams_body(resolved),
                link=pr.url or None,
            )
        except Exception as exc:
            if is_transient(exc):
                raise
            logger.exception(
                "No se pudo avisar el fix de %s#%s; la review ya esta publicada",
                request.repo,
                request.pr_id,
            )
        return {}

    def publish(state: ReviewState) -> ReviewState:
        request = ReviewRequest.model_validate(state["request"])
        findings = _merged(state)
        decisions = state.get("decisions", [])
        human = state.get("human_decisions")
        fixes = state.get("fixes", {})
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
            body = _render_inline(
                finding,
                decision,
                token=request.dedupe_token,
                human=human,
                fix=fixes.get(_finding_id(finding)),
                delivery=delivery,
            )
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
            fixes=fixes,
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
                repo=request.repo,
                pr_id=request.pr_id,
                review_key=request.review_key,
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
    graph.add_node("fix_dispatch", fix_dispatch, retry_policy=retry)
    graph.add_node("publish", publish, retry_policy=retry)
    graph.add_node("fix_wait", fix_wait)
    graph.add_node("publish_fix", publish_fix, retry_policy=retry)
    graph.add_node("notify_fix", notify_fix, retry_policy=retry)
    graph.add_node("notify", notify, retry_policy=retry)

    graph.add_edge(START, "scope")
    graph.add_conditional_edges("scope", dispatch, ["persona"])
    graph.add_edge("persona", "merge")
    graph.add_edge("merge", "policy")
    graph.add_edge("policy", "hitl")
    graph.add_edge("hitl", "fix_dispatch")
    # `fix_dispatch` despacha y `publish` publica los hallazgos; despues la review
    # pausa en `fix_wait` esperando la wave. Al reanudar, vuelve a `fix_dispatch`:
    # si queda algun `FAILED`, re-propone (otra wave) y re-pausa; si ya son
    # terminales, `fix_wait` lo deja pasar a `publish_fix`. Con `PENDING` en la
    # primera visita, `fix_dispatch` sella la guarda para no repetir la propuesta.
    graph.add_edge("fix_dispatch", "publish")
    graph.add_edge("publish", "fix_wait")
    graph.add_conditional_edges(
        "fix_wait", _route_fix_wait, {"fix_dispatch": "fix_dispatch", "publish_fix": "publish_fix"}
    )
    graph.add_edge("publish_fix", "notify_fix")
    graph.add_edge("notify_fix", "notify")
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


# Limite del log crudo que se publica en el inline: suficiente para diagnosticar,
# no para inundar el PR.
_MAX_TAIL_CHARS = 1500


def _tail(text: str, limit: int = _MAX_TAIL_CHARS) -> str:
    clean = text.strip()
    if len(clean) <= limit:
        return clean
    return "…\n" + clean[-limit:]


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
    fix: FixResult | None = None,
    delivery: DeliveryFormatter | None = None,
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
    fix_block = _render_fix(fix, delivery=delivery)
    if fix_block:
        blocks.append(fix_block)
    elif finding.suggested_fix:
        # Sin fix ejecutado (aun), la pista libre de la persona es lo que hay.
        blocks.append(f"**Sugerencia:** {finding.suggested_fix}")
    body = "\n\n".join(blocks)
    if not token:
        return body
    # Mismo id que la comprobacion de `publish`: con `finding.id` vacio, todos los
    # inlines compartian marca y el segundo se daba por publicado.
    return body + f"\n\n{_inline_token(token, _finding_id(finding))}"


def _render_fix(fix: FixResult | None, *, delivery: DeliveryFormatter | None) -> str:
    """Bloque de fix para el inline: la sugerencia verificada o el fallo con log.

    Un `VERIFIED` usa el `DeliveryFormatter` (E6); cualquier otro estado explica
    por que no se entrega nada y con que check fallo, para que nadie aplique a
    ciegas. `None` significa "no habia fix que hacer" y no anade ruido.
    """
    if fix is None:
        return ""
    if fix.status is FixStatus.PENDING:
        # E7: el parche es valido y la verificacion esta despachada; el resultado
        # llega en un comentario de seguimiento. Se rotula para que nadie lo
        # confunda con un fix resuelto ni con un fallo.
        return "_Fix en verificacion (runner); se publicara como respuesta_"
    if fix.status is FixStatus.VERIFIED and fix.patch is not None and delivery is not None:
        return delivery.render(fix.patch, checks=fix.checks, image_digest=fix.image_digest or "")
    if fix.status in (FixStatus.SKIPPED, FixStatus.NOT_VERIFIABLE, FixStatus.BLOCKED):
        label = {
            FixStatus.SKIPPED: "sin arreglo automatico",
            FixStatus.NOT_VERIFIABLE: "no verificable",
            FixStatus.BLOCKED: "bloqueado por seguridad",
        }[fix.status]
        return f"_Fix {label}: {fix.reason}_" if fix.reason else f"_Fix {label}_"
    # FAILED / INVALID_PATCH: hay algo que contar y un check que fallo.
    lines = [f"_Fix no verificado ({fix.status.value}): {fix.reason}_"]
    failed = next((check for check in fix.checks if not check.ok), None)
    if failed is not None:
        tail = (failed.stderr_tail or failed.stdout_tail).strip()
        if tail:
            lines.append(f"```\n{_tail(tail)}\n```")
    return "\n\n".join(lines)


def _fix_reason_line(fix: FixResult | None) -> str:
    """Linea de estado del fix para el resumen (compacta)."""
    if fix is None:
        return ""
    icons = {
        FixStatus.PENDING: "en verificacion",
        FixStatus.VERIFIED: "sugerencia verificada",
        FixStatus.FAILED: "fix no verificado",
        FixStatus.INVALID_PATCH: "parche invalido",
        FixStatus.NOT_VERIFIABLE: "no verificable",
        FixStatus.SKIPPED: "sin arreglo automatico",
        FixStatus.BLOCKED: "bloqueado por seguridad",
    }
    label = icons.get(fix.status, fix.status.value)
    return f"Fix: {label}" + (f" · {fix.reason}" if fix.reason else "")


def _render_summary(
    findings: list[Finding],
    decisions: list[Decision],
    *,
    summary_only: list[Finding] | None = None,
    inlined: list[Finding] | None = None,
    unavailable: list[tuple[str, ToolingSourceStatus]] | None = None,
    fixes: dict[str, FixResult] | None = None,
    token: str = "",
    complete: bool = True,
    human: HitlDecision | None = None,
) -> str:
    by_key = {d.finding_id: d for d in decisions}
    fixes = fixes or {}
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
    if fixes:
        lines.extend(_render_fixes_section(fixes))
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
        fix_line = _fix_reason_line(fixes.get(_finding_id(finding)))
        if fix_line:
            lines.append(f"  {fix_line}")
        if human is not None and human.outcome_for(decision.finding_id) is DecisionOutcome.REJECTED:
            lines.append("  Descartado por una persona: no se anota inline.")
    summary = "\n".join(lines)
    return summary + _footer(token, complete=complete) if token else summary


def _render_fixes_section(fixes: dict[str, FixResult]) -> list[str]:
    """Seccion "Fixes" del resumen: recuento por estado (spec §5.5)."""
    counts: dict[FixStatus, int] = {}
    for result in fixes.values():
        counts[result.status] = counts.get(result.status, 0) + 1
    detail = ", ".join(f"{status.value}: {count}" for status, count in sorted(counts.items()))
    return ["### Fixes", "", detail, ""]


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


# ---------------------------------------------------------------------------
# Fase 3-E7: despacho del fix, wave de verificacion y comentario de seguimiento.
# ---------------------------------------------------------------------------

_TERMINAL_FIX = frozenset(
    {
        FixStatus.VERIFIED,
        FixStatus.FAILED,
        FixStatus.INVALID_PATCH,
        FixStatus.NOT_VERIFIABLE,
        FixStatus.SKIPPED,
        FixStatus.BLOCKED,
    }
)


def _is_terminal_fix(status: FixStatus) -> bool:
    """Un fix deja de esperar a la wave (`PENDING` no es terminal)."""
    return status in _TERMINAL_FIX


def _route_fix_wait(state: ReviewState) -> str:
    """Tras `fix_wait`, elige otra wave de reintento o el cierre de la publicacion.

    Se vuelve a `fix_dispatch` mientras quede algun `FAILED` con intentos
    disponibles (el tope viaja en `fix_max_attempts`); en cualquier otro caso se
    sigue a `publish_fix`, que publica lo terminal (o no hace nada, si no hay nada
    que publicar). El bucle siempre avanza: cada vuelta consume un intento.
    """
    fixes = state.get("fixes", {})
    max_attempts = int(state.get("fix_max_attempts", 2) or 1)
    for result in fixes.values():
        if result.status is FixStatus.FAILED and result.attempts < max_attempts:
            return "fix_dispatch"
    return "publish_fix"


def _fix_token(token: str) -> str:
    """Marca de dedupe del comentario de seguimiento del fix (propia de E7)."""
    return f"<!-- {token}:fix -->"


def _render_fix_followup(
    request: ReviewRequest,
    fixes: dict[str, FixResult],
    findings: list[Finding],
    *,
    delivery: DeliveryFormatter | None,
) -> str:
    """Comentario general con el resultado ya resuelto de los fixes de la review.

    No re-publica: lleva la marca `:fix` y el `publish_fix` la lee antes de escribir.
    """
    by_id = {_finding_id(finding): finding for finding in findings}
    header = "## Fix verificado" if _all_verified(fixes) else "## Resultado del fix"
    lines = [header, ""]
    for finding_id in sorted(fixes):
        result = fixes[finding_id]
        finding = by_id.get(finding_id)
        title = finding.title if finding is not None else finding_id
        lines.append(f"### `{finding_id}` · {title}")
        lines.append("")
        lines.append(_render_fix(result, delivery=delivery))
        lines.append("")
    body = "\n".join(lines).rstrip()
    return f"{body}\n\n{_fix_token(request.dedupe_token)}"


def _all_verified(fixes: dict[str, FixResult]) -> bool:
    return bool(fixes) and all(result.status is FixStatus.VERIFIED for result in fixes.values())


def _render_fix_teams_body(fixes: dict[str, FixResult]) -> str:
    counts: dict[FixStatus, int] = {}
    for result in fixes.values():
        counts[result.status] = counts.get(result.status, 0) + 1
    detail = ", ".join(f"{status.value}: {count}" for status, count in sorted(counts.items()))
    return f"{len(fixes)} fix(es) resuelto(s): {detail}"


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
        fixes=state.get("fixes", {}),
        budget_exhausted=sorted(state.get("budget_exhausted", set())),
        tokens_spent=dict(state.get("tokens_spent", {})),
        tokens_remaining=_remaining(state),
    )


def _remaining(state: ReviewState) -> int | None:
    """Saldo del contador compartido al cerrar, derivado del estado (017 B4).

    `None` si el grafo no sabe el tope (tests sin presupuesto): el hecho del gasto
    (`tokens_spent`) sigue viajando; el saldo es informativo y nunca se consulta al
    puerto durante el `replay`.
    """
    total = state.get("budget_total")
    if total is None:
        return None
    return max(0, total - sum(state.get("tokens_spent", {}).values()))
