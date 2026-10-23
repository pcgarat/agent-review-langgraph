"""Proceso `claude-runner` (025 R4, ADR-0028).

Checkout en un volumen propio y subproceso `claude` con entorno cerrado. El
worker pide el stdout por HTTP; este proceso no escribe el artefacto ni tiene
el token con el que se comenta el PR.
"""

from __future__ import annotations

import hmac
import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from agent_review.adapters.claude_cli import SubprocessClaudeRunner
from agent_review.claude_runner.checkout import CheckoutError, checkout_directory, prepare_checkout
from agent_review.claude_session import CHILD_TOKEN_ENV, load_token, session_active
from agent_review.ports.claude import ClaudeProcessCrashed, ClaudeQuotaExceeded, ClaudeTimeout
from agent_review.settings import Settings, get_settings, is_missing, require_claude_runner_secrets

logger = logging.getLogger(__name__)


class RunBody(BaseModel):
    repo: str
    pr_id: int = Field(ge=1)
    head_sha: str


class RunResponse(BaseModel):
    stdout: str


def build_app(settings: Settings | None = None) -> FastAPI:
    current = settings or get_settings()
    app = FastAPI(title="Claude Runner", version="0.1.0")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/session")
    def session(authorization: str | None = Header(default=None)) -> dict[str, bool]:
        """Sí o no. Si el fichero caducó, lo retira. No devuelve el token."""
        _verify_token(current, authorization)
        return {"active": session_active(_session_path(current))}

    @app.post("/run", response_model=RunResponse)
    def run(body: RunBody, authorization: str | None = Header(default=None)) -> RunResponse:
        _verify_token(current, authorization)
        logger.info("claude %s#%s @ %s", body.repo, body.pr_id, body.head_sha)
        try:
            directory = checkout_directory(
                _workspace_root(current), repo=body.repo, head_sha=body.head_sha
            )
            prepare_checkout(
                directory,
                repo=body.repo,
                head_sha=body.head_sha,
                git_token=current.claude_git_token,
            )
            token = load_token(_session_path(current))
            extra = {CHILD_TOKEN_ENV: token} if token else None
            stdout = SubprocessClaudeRunner(
                argv_prefix=[current.claude_bin],
                max_turns=current.claude_max_turns,
                timeout_s=current.claude_timeout_s,
                extra_env=extra,
                cwd=directory,
            ).run(repo=body.repo, pr_id=body.pr_id, head_sha=body.head_sha)
        except CheckoutError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
        except ClaudeTimeout as exc:
            raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, str(exc)) from exc
        except ClaudeQuotaExceeded as exc:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
        except ClaudeProcessCrashed as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
        return RunResponse(stdout=stdout)

    return app


def _workspace_root(settings: Settings) -> Path:
    return Path(settings.claude_workspace_root)


def _session_path(settings: Settings) -> Path:
    return Path(settings.claude_session_path)


def _verify_token(settings: Settings, authorization: str | None) -> None:
    token = settings.claude_runner_token.strip()
    if is_missing(token):
        return
    expected = f"Bearer {token}"
    if not authorization or not hmac.compare_digest(expected, authorization):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token del runner invalido")


def main() -> None:
    settings = get_settings()
    require_claude_runner_secrets(settings)
    logging.basicConfig(level=settings.log_level)
    uvicorn.run(
        build_app(settings),
        host="0.0.0.0",
        port=settings.claude_runner_port,
    )


if __name__ == "__main__":
    main()
