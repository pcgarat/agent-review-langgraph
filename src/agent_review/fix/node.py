from __future__ import annotations

import logging

from agent_review.domain.models import (
    CommandResult,
    Decision,
    Finding,
    FixResolution,
    FixResolutions,
    FixResult,
    FixStatus,
    FixWaitRequest,
    HitlDecision,
    ReviewRequest,
    VerifyJob,
)
from agent_review.errors import is_transient
from agent_review.fix.selection import select_candidates
from agent_review.fix.validation import validate_patch
from agent_review.policy.engine import PolicyEngine
from agent_review.ports.fixer import FixerPort
from agent_review.ports.vcs import VcsPort

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ATTEMPTS = 2
_DEFAULT_MAX_LINES = 30


class FixerUnavailable(RuntimeError):
    """Fallo no transitorio del fixer (respuesta inaprovechable, presupuesto)."""


def blocked_reason(finding: Finding, policy: PolicyEngine) -> str | None:
    """Motivo por el que un hallazgo no se arregla aunque una persona lo apruebe.

    La proteccion de seguridad manda sobre la decision humana (D3 invertida):
    una ruta nunca-auto o un tema protegido **nunca** produce un parche, ni con
    `APPROVED`. El humano decide sobre lo que el agente puede hacer; no amplia lo
    que tiene prohibido. Un fix bloqueado se publica como `BLOCKED` con su razon.
    """
    if finding.protected_subject is not None:
        return f"tema protegido ({finding.protected_subject.value}): no se arregla automaticamente"
    if policy.is_never_auto(finding.file):
        return f"ruta nunca-auto ({finding.file}): no se arregla automaticamente"
    return None


def run_key_for(review_key: str, finding_id: str, attempt: int) -> str:
    """Clave idempotente de una verificacion: una wave y un intento."""
    return f"{review_key}:{finding_id}:{attempt}"


def dispatch_fixes(
    *,
    request: ReviewRequest,
    findings: list[Finding],
    decisions: list[Decision],
    human: HitlDecision | None,
    vcs: VcsPort,
    fixer: FixerPort | None,
    policy: PolicyEngine,
    recipes: list[str],
    targets: list[str],
    existing: dict[str, FixResult] | None = None,
    verification_enabled: bool = True,
) -> dict[str, FixResult]:
    """Propone y valida localmente el parche de cada hallazgo aprobado (§5, E7).

    **No** verifica: deja `PENDING` (con el parche validado) los candidatos
    verificables y **terminal** el resto (`BLOCKED`, `SKIPPED`, `INVALID_PATCH`,
    `NOT_VERIFIABLE`). Quien espera al sandbox es el worker de fixes; la ruta de la
    review nunca llama a `SandboxPort.verify` (criterio 1 de la E7).

    `existing` lleva los resultados ya presentes en el estado para que una visita
    de reintento (el grafo vuelve aqui tras un `failed`) **solo** re-proponga los
    que agotaron intentos y no repita a los ya resueltos.

    `verification_enabled=False` (sin `SANDBOX_ENABLED` o sin runner) deja los
    candidatos `NOT_VERIFIABLE` sin pedir parche: no habria quien los verificase.
    """
    guards = policy.guards
    max_fixes = int(guards.get("max_fixes_per_pr", 3) or 0)
    max_attempts = int(guards.get("max_fix_attempts", _DEFAULT_MAX_ATTEMPTS) or 1)
    max_lines = int(guards.get("max_fix_lines", _DEFAULT_MAX_LINES) or _DEFAULT_MAX_LINES)
    done = existing or {}

    candidates, skipped = select_candidates(findings, decisions, human, max_fixes=max_fixes)
    results: dict[str, FixResult] = {}
    for finding_id in skipped:
        if finding_id in done:
            continue
        results[finding_id] = FixResult(
            finding_id=finding_id,
            status=FixStatus.SKIPPED,
            reason=f"supera max_fixes_per_pr={max_fixes}",
        )

    actionable = []
    for candidate in candidates:
        prior = done.get(candidate.finding.id)
        if prior is not None and not _retryable(prior, max_attempts):
            # Ya resuelto (o agotado): una visita de reintento no lo vuelve a tocar.
            continue
        reason = blocked_reason(candidate.finding, policy)
        if reason is not None:
            logger.warning(
                "Fase 3-E7: fix bloqueado por seguridad en %s (%s)", candidate.finding.id, reason
            )
            results[candidate.finding.id] = FixResult(
                finding_id=candidate.finding.id, status=FixStatus.BLOCKED, reason=reason
            )
            continue
        actionable.append(candidate)

    if not actionable:
        return results
    if fixer is None:
        for candidate in actionable:
            results[candidate.finding.id] = FixResult(
                finding_id=candidate.finding.id,
                status=FixStatus.NOT_VERIFIABLE,
                reason="no hay fixer configurado",
            )
        return results
    if not verification_enabled:
        # Sin sandbox/runner nadie resolveria la wave: dejarlo `PENDING` pausaria la
        # review hasta el TTL del handoff para acabar igual, `NOT_VERIFIABLE`.
        for candidate in actionable:
            results[candidate.finding.id] = FixResult(
                finding_id=candidate.finding.id,
                status=FixStatus.NOT_VERIFIABLE,
                reason="no hay sandbox/runner configurado",
            )
        return results

    # El diff del PR se lee una vez: la validacion local lo necesita por hallazgo.
    pr_diff = vcs.get_diff(request.repo, request.pr_id)
    for candidate in actionable:
        finding_id = candidate.finding.id
        previous = done.get(finding_id)
        results[finding_id] = _propose(
            request=request,
            finding=candidate.finding,
            pr_diff=pr_diff,
            vcs=vcs,
            fixer=fixer,
            previous=previous if _retryable(previous, max_attempts) else None,
            max_lines=max_lines,
        )
    return results


def apply_resolutions(
    *,
    request: ReviewRequest,
    fixes: dict[str, FixResult],
    resolutions: FixResolutions,
) -> dict[str, FixResult]:
    """Aplica la wave resuelta por el worker de fixes sobre los `PENDING`.

    Puro y reejecutable: al reanudar, el nodo vuelve a ejecutarse desde el inicio y
    esta funcion debe producir el mismo resultado con el mismo `resume`. Una
    `FixResolution` que falte (wave parcial o job perdido) deja el fix
    `NOT_VERIFIABLE`, nunca colgado en `PENDING`.
    """
    by_run_key = {item.run_key: item for item in resolutions.items}
    updated: dict[str, FixResult] = {}
    for finding_id, result in fixes.items():
        if result.status is not FixStatus.PENDING:
            continue
        run_key = run_key_for(request.review_key, finding_id, result.attempts)
        resolution = by_run_key.get(run_key)
        if resolution is None:
            updated[finding_id] = result.model_copy(
                update={
                    "status": FixStatus.NOT_VERIFIABLE,
                    "reason": "no se recibio la resolucion de la wave",
                }
            )
            continue
        updated[finding_id] = _apply_one(result, resolution)
    return updated


def build_wave(
    request: ReviewRequest,
    fixes: dict[str, FixResult],
    *,
    recipes: list[str],
    targets: list[str],
) -> FixWaitRequest:
    """Carga del `interrupt` de `fix_wait`: un `VerifyJob` por `PENDING`."""
    jobs: list[VerifyJob] = []
    for finding_id in sorted(fixes):
        result = fixes[finding_id]
        if result.status is not FixStatus.PENDING or result.patch is None:
            continue
        jobs.append(
            VerifyJob(
                run_key=run_key_for(request.review_key, finding_id, result.attempts),
                finding_id=finding_id,
                attempt=result.attempts,
                repo=request.repo,
                head_sha=request.head_sha,
                patch=result.patch.diff,
                recipes=list(recipes),
                targets=list(targets),
            )
        )
    return FixWaitRequest(
        review_key=request.review_key,
        repo=request.repo,
        pr_id=request.pr_id,
        head_sha=request.head_sha,
        jobs=jobs,
    )


def _retryable(result: FixResult | None, max_attempts: int) -> bool:
    """Un `FAILED` con intentos disponibles se vuelve a proponer; el resto no."""
    return (
        result is not None and result.status is FixStatus.FAILED and result.attempts < max_attempts
    )


def _propose(
    *,
    request: ReviewRequest,
    finding: Finding,
    pr_diff: str,
    vcs: VcsPort,
    fixer: FixerPort,
    previous: FixResult | None,
    max_lines: int,
) -> FixResult:
    """Leer el fichero, pedir el parche y validarlo localmente. Sin sandbox."""
    try:
        source = vcs.get_file(request.repo, finding.file, ref=request.head_sha)
    except Exception as exc:
        if is_transient(exc):
            raise
        logger.warning("Fase 3-E7: no se pudo leer %s@%s (%s)", finding.file, request.head_sha, exc)
        return FixResult(
            finding_id=finding.id,
            status=FixStatus.NOT_VERIFIABLE,
            reason=f"no se pudo leer el fichero: {exc}",
        )

    attempt = (previous.attempts if previous is not None else 0) + 1
    try:
        patch = fixer.propose(
            finding, source=source, previous=previous, review_key=request.review_key
        )
    except Exception as exc:
        # Un fallo transitorio del modelo lo reintenta el grafo (`RetryPolicy`).
        if is_transient(exc):
            raise
        logger.info("Fase 3-E7: fixer sin propuesta para %s (%s)", finding.id, exc)
        reason = f"el fixer no propuso un parche: {exc}"
        if previous is None:
            return FixResult(
                finding_id=finding.id, status=FixStatus.SKIPPED, attempts=attempt, reason=reason
            )
        return previous.model_copy(update={"attempts": attempt, "reason": reason})

    invalid = validate_patch(patch, finding=finding, diff=pr_diff, max_fix_lines=max_lines)
    if invalid is not None:
        logger.info("Fase 3-E7: parche invalido para %s (%s)", finding.id, invalid)
        return FixResult(
            finding_id=finding.id,
            status=FixStatus.INVALID_PATCH,
            attempts=attempt,
            patch=patch,
            reason=invalid,
        )
    return FixResult(
        finding_id=finding.id,
        status=FixStatus.PENDING,
        attempts=attempt,
        patch=patch,
    )


def _apply_one(result: FixResult, resolution: FixResolution) -> FixResult:
    """Traduce una resolucion del runner al estado terminal del fix."""
    if resolution.outcome == "passed":
        return result.model_copy(
            update={
                "status": FixStatus.VERIFIED,
                "checks": resolution.checks,
                "image_digest": resolution.image_digest,
                "reason": "",
            }
        )
    if resolution.outcome == "error":
        # Fallo del runner/peticion, no un check rojo: no se ejecuto la verificacion.
        return result.model_copy(
            update={
                "status": FixStatus.NOT_VERIFIABLE,
                "checks": resolution.checks,
                "image_digest": resolution.image_digest,
                "reason": resolution.error or "el runner no pudo verificar el parche",
            }
        )
    failed = _first_failed(resolution.checks)
    reason = (
        f"{failed.recipe} fallo (exit {failed.exit_code})"
        if failed is not None
        else "verificacion fallida"
    )
    return result.model_copy(
        update={
            "status": FixStatus.FAILED,
            "checks": resolution.checks,
            "image_digest": resolution.image_digest,
            "reason": reason,
        }
    )


def _first_failed(checks: list[CommandResult]) -> CommandResult | None:
    return next((check for check in checks if not check.ok), None)
