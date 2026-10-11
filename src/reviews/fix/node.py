from __future__ import annotations

import logging

from reviews.domain.models import (
    CommandResult,
    Decision,
    Finding,
    FixResult,
    FixStatus,
    HitlDecision,
    ReviewRequest,
)
from reviews.errors import is_transient
from reviews.fix.selection import select_candidates
from reviews.fix.validation import validate_patch
from reviews.policy.engine import PolicyEngine
from reviews.ports.fixer import FixerPort
from reviews.ports.sandbox import SandboxPort, VerifyRequest
from reviews.ports.vcs import VcsPort

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


def run_fixes(
    *,
    request: ReviewRequest,
    findings: list[Finding],
    decisions: list[Decision],
    human: HitlDecision | None,
    vcs: VcsPort,
    fixer: FixerPort | None,
    sandbox: SandboxPort | None,
    policy: PolicyEngine,
    recipes: list[str],
    targets: list[str],
) -> dict[str, FixResult]:
    """Ejecuta el fix de los hallazgos aprobados. No escribe el repo (§5).

    Flujo por hallazgo (spec §4): comprobar el bloqueo de seguridad, leer el
    fichero al `head_sha`, pedir un parche, validarlo localmente, verificar en el
    sandbox y, si pasa, dejarlo listo para publicar. Secuencial y acotado por
    `max_fixes_per_pr`. Los que exceden el tope o quedan bloqueados se registran
    para que no desaparezcan en silencio.
    """
    guards = policy.guards
    max_fixes = int(guards.get("max_fixes_per_pr", 3) or 0)
    max_attempts = int(guards.get("max_fix_attempts", _DEFAULT_MAX_ATTEMPTS) or 1)
    max_lines = int(guards.get("max_fix_lines", _DEFAULT_MAX_LINES) or _DEFAULT_MAX_LINES)

    candidates, skipped = select_candidates(findings, decisions, human, max_fixes=max_fixes)
    results: dict[str, FixResult] = {
        finding_id: FixResult(
            finding_id=finding_id,
            status=FixStatus.SKIPPED,
            reason=f"supera max_fixes_per_pr={max_fixes}",
        )
        for finding_id in skipped
    }

    # Bloqueo de seguridad antes de gastar fixer ni sandbox. Es un gate duro: la
    # proteccion manda sobre la aprobacion humana (D3 invertida).
    actionable = []
    for candidate in candidates:
        reason = blocked_reason(candidate.finding, policy)
        if reason is not None:
            logger.warning(
                "Fase 3: fix bloqueado por seguridad en %s (%s)", candidate.finding.id, reason
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
    if sandbox is None:
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
        result = _fix_one(
            request=request,
            finding=candidate.finding,
            pr_diff=pr_diff,
            vcs=vcs,
            fixer=fixer,
            sandbox=sandbox,
            recipes=recipes,
            targets=targets,
            max_attempts=max_attempts,
            max_lines=max_lines,
        )
        results[candidate.finding.id] = result
    return results


def _fix_one(
    *,
    request: ReviewRequest,
    finding: Finding,
    pr_diff: str,
    vcs: VcsPort,
    fixer: FixerPort,
    sandbox: SandboxPort,
    recipes: list[str],
    targets: list[str],
    max_attempts: int,
    max_lines: int,
) -> FixResult:
    try:
        source = vcs.get_file(request.repo, finding.file, ref=request.head_sha)
    except Exception as exc:
        if is_transient(exc):
            raise
        logger.warning("Fase 3: no se pudo leer %s@%s (%s)", finding.file, request.head_sha, exc)
        return FixResult(
            finding_id=finding.id,
            status=FixStatus.NOT_VERIFIABLE,
            reason=f"no se pudo leer el fichero: {exc}",
        )

    previous: FixResult | None = None
    attempts = 0
    while attempts < max_attempts:
        attempts += 1
        try:
            patch = fixer.propose(finding, source=source, previous=previous)
        except Exception as exc:
            # Un fallo transitorio del modelo lo reintenta el grafo (`RetryPolicy`).
            if is_transient(exc):
                raise
            logger.info("Fase 3: fixer sin propuesta para %s (%s)", finding.id, exc)
            if previous is None:
                return FixResult(
                    finding_id=finding.id,
                    status=FixStatus.SKIPPED,
                    attempts=attempts,
                    reason=f"el fixer no propuso un parche: {exc}",
                )
            previous = previous.model_copy(
                update={
                    "attempts": attempts,
                    "reason": f"el fixer no propuso un parche: {exc}",
                }
            )
            continue

        invalid = validate_patch(patch, finding=finding, diff=pr_diff, max_fix_lines=max_lines)
        if invalid is not None:
            logger.info("Fase 3: parche invalido para %s (%s)", finding.id, invalid)
            previous = FixResult(
                finding_id=finding.id,
                status=FixStatus.INVALID_PATCH,
                attempts=attempts,
                patch=patch,
                reason=invalid,
            )
            continue

        report = sandbox.verify(
            VerifyRequest(
                run_key=f"{request.review_key}:{finding.id}:{attempts}",
                repo=request.repo,
                head_sha=request.head_sha,
                patch=patch.diff,
                recipes=recipes,
                targets=targets,
            )
        )
        if report.passed:
            return FixResult(
                finding_id=finding.id,
                status=FixStatus.VERIFIED,
                attempts=attempts,
                patch=patch,
                checks=report.results,
                image_digest=report.image_digest,
            )

        failed: CommandResult | None = next((c for c in report.results if not c.ok), None)
        reason = (
            f"{failed.recipe} fallo (exit {failed.exit_code})"
            if failed is not None
            else "verificacion fallida"
        )
        previous = FixResult(
            finding_id=finding.id,
            status=FixStatus.FAILED,
            attempts=attempts,
            patch=patch,
            checks=report.results,
            image_digest=report.image_digest,
            reason=reason,
        )

    assert previous is not None
    return previous.model_copy(update={"attempts": attempts})
