from __future__ import annotations

import logging
import time
from pathlib import Path

from agent_review.adapters.http_sandbox import HttpSandboxAdapter, SandboxError
from agent_review.domain.models import (
    FixJob,
    FixResolution,
    FixResolutions,
)
from agent_review.errors import is_transient
from agent_review.ports.sandbox import SandboxPort, VerifyRequest
from agent_review.profile import sandbox_timeout_s
from agent_review.queue import (
    FixQueue,
    InMemoryFixQueue,
    InMemoryReviewQueue,
    RedisFixQueue,
    RedisReviewQueue,
    ReviewQueue,
)
from agent_review.settings import Settings, get_settings, require_production_secrets

logger = logging.getLogger(__name__)

# Latido que refleja que el worker de fixes sigue vivo (healthcheck de compose).
HEARTBEAT_PATH = "/tmp/reviews-fix-worker-heartbeat"


def build_fix_queue(settings: Settings) -> FixQueue:
    queue = RedisFixQueue(url=settings.redis_url)
    if queue.available():
        return queue
    logger.warning("Redis no disponible en el worker de fixes; cola en memoria")
    return InMemoryFixQueue()


def build_review_queue(settings: Settings) -> ReviewQueue:
    """Cola de reanudacion: la misma de reviews, donde encola la resolucion."""
    queue = RedisReviewQueue(url=settings.redis_url)
    if queue.available():
        return queue
    logger.warning("Redis no disponible; cola de reviews en memoria")
    return InMemoryReviewQueue()


def build_sandbox(settings: Settings) -> SandboxPort:
    """Adaptador HTTP del `sandbox-runner`. Es la unica dependencia externa."""
    return HttpSandboxAdapter(
        base_url=settings.sandbox_runner_url,
        token=settings.sandbox_runner_token,
        timeout_s=sandbox_timeout_s(settings),
    )


def verify_job(
    sandbox: SandboxPort, job: FixJob, *, recipes: list[str] | None = None
) -> FixResolutions:
    """Verifica la wave entera de un job (`POST /verify` por cada `VerifyJob`).

    Secuencial en v1 (el runner es concurrencia 1). Un fallo transitorio del
    runner sube para que el job se reencole; un `SandboxError` (4xx: peticion no
    atendible) se traduce a `outcome="error"` por `VerifyJob`, sin reintentar (el
    fix quedara `NOT_VERIFIABLE`, que es lo honesto: la verificacion no corrio).
    """
    del recipes  # las recipes viajan en cada `VerifyJob` del contrato
    items: list[FixResolution] = []
    for verify in job.wave.jobs:
        request = VerifyRequest(
            run_key=verify.run_key,
            repo=verify.repo,
            head_sha=verify.head_sha,
            patch=verify.patch,
            recipes=verify.recipes,
            targets=verify.targets,
        )
        try:
            report = sandbox.verify(request)
        except SandboxError as exc:
            items.append(FixResolution(run_key=verify.run_key, outcome="error", error=str(exc)))
            continue
        items.append(
            FixResolution(
                run_key=verify.run_key,
                outcome="passed" if report.passed else "failed",
                checks=report.results,
                image_digest=report.image_digest,
            )
        )
    return FixResolutions(items=items)


def _consume(
    queue: FixQueue,
    review_queue: ReviewQueue,
    sandbox: SandboxPort,
    job: FixJob,
    *,
    max_attempts: int,
) -> None:
    """Resuelve una wave y encola la reanudacion. Reintenta ante fallo transitorio."""
    # Idempotencia (R8): una reentrega tras una caida no relanza el runner.
    done_keys = [v.run_key for v in job.wave.jobs]
    if done_keys and all(queue.is_done(key) for key in done_keys):
        logger.info("Wave %s ya resuelta; no se relanza la verificacion", job.review_key)
        return
    try:
        resolutions = verify_job(sandbox, job)
    except Exception as exc:
        if is_transient(exc):
            logger.warning(
                "Fallo transitorio verificando %s (%s); se reencola", job.review_key, exc
            )
        else:
            logger.exception("Fallo inesperado verificando %s", job.review_key)
        _requeue_or_drop(queue, job, max_attempts=max_attempts)
        return
    for key in done_keys:
        queue.mark_done(key)
    # Marca las verificaciones resueltas por si una reentrega llega despues del
    # `mark_done` y antes del `enqueue`.
    resume = job.request.model_copy(update={"fix_resolution": resolutions})
    review_queue.enqueue(resume)
    logger.info(
        "Wave %s resuelta (%d verificacion(es)); reanudacion encolada",
        job.review_key,
        len(resolutions.items),
    )


def _requeue_or_drop(queue: FixQueue, job: FixJob, *, max_attempts: int) -> None:
    if job.attempts + 1 >= max_attempts:
        logger.error(
            "Wave %s agoto los %d intentos; se manda a la DLQ de fixes",
            job.review_key,
            max_attempts,
        )
        queue.dead_letter(job, reason=f"intentos agotados ({max_attempts})")
        return
    retry = job.model_copy(update={"attempts": job.attempts + 1})
    queue.enqueue(retry)
    logger.warning(
        "Wave %s reencolada para el intento %d/%d",
        job.review_key,
        retry.attempts + 1,
        max_attempts,
    )


def start_heartbeat(path: str = HEARTBEAT_PATH, interval_s: float = 10.0) -> None:
    import threading

    def _loop() -> None:
        while True:
            _touch_heartbeat(path)
            time.sleep(interval_s)

    threading.Thread(target=_loop, name="fix-worker-heartbeat", daemon=True).start()


def _touch_heartbeat(path: str = HEARTBEAT_PATH) -> None:
    try:
        Path(path).write_text(str(time.time()), encoding="utf-8")
    except OSError:
        logger.debug("No se pudo escribir el latido del worker de fixes en %s", path)


def run_forever(
    *,
    settings: Settings | None = None,
    queue: FixQueue | None = None,
    review_queue: ReviewQueue | None = None,
    sandbox: SandboxPort | None = None,
    poll_s: float | None = None,
) -> None:
    settings = settings or get_settings()
    require_production_secrets(settings)
    logging.basicConfig(level=settings.log_level)
    queue = queue or build_fix_queue(settings)
    review_queue = review_queue or build_review_queue(settings)
    sandbox = sandbox or build_sandbox(settings)
    poll = poll_s if poll_s is not None else settings.fix_poll_s
    logger.info("Worker de fixes arrancado (Fase 3-E7)")
    start_heartbeat()
    while True:
        job = queue.dequeue(timeout_s=poll)
        if job is None:
            continue
        _consume(
            queue,
            review_queue,
            sandbox,
            job,
            max_attempts=settings.max_fix_attempts_job,
        )


def main() -> None:
    run_forever()


if __name__ == "__main__":
    main()
