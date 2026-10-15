from __future__ import annotations

import pytest

from agent_review.adapters.http_sandbox import SandboxError, SandboxUnavailable
from agent_review.domain.models import (
    CommandResult,
    FixJob,
    FixWaitRequest,
    ReviewRequest,
    VerifyJob,
)
from agent_review.fixworker.main import _consume, verify_job
from agent_review.ports.sandbox import VerifyReport
from agent_review.queue import InMemoryFixQueue, InMemoryReviewQueue


class _Sandbox:
    def __init__(self, *, outcome: str = "passed", error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self._outcome = outcome
        self._error = error

    def verify(self, request) -> VerifyReport:  # noqa: ANN001
        self.calls.append(request.run_key)
        if self._error is not None:
            raise self._error
        return VerifyReport(
            run_key=request.run_key,
            image_digest="sha256:img",
            results=[
                CommandResult(
                    recipe="typecheck",
                    argv=["make"],
                    exit_code=0 if self._outcome == "passed" else 1,
                    duration_s=1.0,
                    stderr_tail="" if self._outcome == "passed" else "boom",
                )
            ],
            passed=self._outcome == "passed",
        )


def _job(finding_ids: list[str] | None = None) -> FixJob:
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="p1")
    ids = finding_ids or ["f1"]
    wave = FixWaitRequest(
        review_key=request.review_key,
        repo=request.repo,
        pr_id=request.pr_id,
        head_sha=request.head_sha,
        jobs=[
            VerifyJob(
                run_key=f"{request.review_key}:{fid}:1",
                finding_id=fid,
                attempt=1,
                repo=request.repo,
                head_sha=request.head_sha,
                patch="diff",
                recipes=["typecheck"],
            )
            for fid in ids
        ],
    )
    return FixJob(review_key=request.review_key, request=request, wave=wave)


def test_verify_job_traduce_el_informe_a_resoluciones() -> None:
    sandbox = _Sandbox(outcome="passed")
    resolutions = verify_job(sandbox, _job())

    assert len(resolutions.items) == 1
    item = resolutions.items[0]
    assert item.run_key.endswith(":f1:1")
    assert item.outcome == "passed"
    assert item.image_digest == "sha256:img"
    assert item.checks[0].recipe == "typecheck"


def test_verify_job_marca_failed_si_el_check_no_pasa() -> None:
    resolutions = verify_job(_Sandbox(outcome="failed"), _job())
    assert resolutions.items[0].outcome == "failed"
    assert resolutions.items[0].checks[0].exit_code == 1


def test_fallo_permanente_del_runner_es_outcome_error_sin_reintentar() -> None:
    """Un 409 del runner (peticion no atendible) no se reintenta: el fix no corrio."""
    resolutions = verify_job(_Sandbox(error=SandboxError("perfil sin digest")), _job())
    item = resolutions.items[0]
    assert item.outcome == "error"
    assert "perfil sin digest" in item.error


def test_consume_encola_la_reanudacion_con_la_resolucion() -> None:
    queue = InMemoryFixQueue()
    review_queue = InMemoryReviewQueue()
    job = _job()

    _consume(queue, review_queue, _Sandbox(outcome="passed"), job, max_attempts=3)

    resume = review_queue.dequeue()
    assert resume is not None
    assert resume.fix_resolution is not None
    assert resume.fix_resolution.items[0].outcome == "passed"
    assert queue.is_done(job.wave.jobs[0].run_key)


def test_consume_es_idempotente_si_la_wave_ya_esta_resuelta() -> None:
    queue = InMemoryFixQueue()
    review_queue = InMemoryReviewQueue()
    job = _job()
    queue.mark_done(job.wave.jobs[0].run_key)
    sandbox = _Sandbox()

    _consume(queue, review_queue, sandbox, job, max_attempts=3)

    assert sandbox.calls == [], "no se relanza la verificacion ya resuelta"
    assert review_queue.dequeue() is None


def test_consume_reencola_ante_un_fallo_transitorio() -> None:
    queue = InMemoryFixQueue()
    review_queue = InMemoryReviewQueue()
    job = _job()

    _consume(
        queue,
        review_queue,
        _Sandbox(error=SandboxUnavailable("el runner no responde")),
        job,
        max_attempts=3,
    )

    retried = queue.dequeue()
    assert retried is not None and retried.attempts == 1
    assert review_queue.dequeue() is None, "no se reanuda con un fallo transitorio"


def test_consume_manda_a_la_dlq_al_agotar_intentos() -> None:
    queue = InMemoryFixQueue()
    review_queue = InMemoryReviewQueue()
    job = _job().model_copy(update={"attempts": 2})

    _consume(
        queue,
        review_queue,
        _Sandbox(error=SandboxUnavailable("sigue caido")),
        job,
        max_attempts=3,
    )

    assert queue.dequeue() is None
    assert queue.dead_letter_count == 1
    assert queue.dead_letters()[0]["review_key"] == job.review_key


@pytest.mark.parametrize("ids", [["f1", "f2"]])
def test_verify_job_resuelve_toda_la_wave(ids: list[str]) -> None:
    sandbox = _Sandbox(outcome="passed")
    resolutions = verify_job(sandbox, _job(ids))

    assert len(resolutions.items) == len(ids)
    assert sandbox.calls == [f"r:1:abc:p1:{fid}:1" for fid in ids]
