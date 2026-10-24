"""Proceso `claude-runner` (025 R4, ADR-0028).

Checkout en un volumen propio y subproceso `claude` con entorno cerrado. El
worker pide el stdout por HTTP; este proceso no escribe el artefacto ni tiene
el token con el que se comenta el PR.
"""

from __future__ import annotations

import hmac
import logging
import os
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from agent_review.adapters.claude_cli import SubprocessClaudeRunner, redact_text
from agent_review.claude_runner.checkout import CheckoutError, checkout_directory, prepare_checkout
from agent_review.claude_session import CHILD_TOKEN_ENV, load_token, session_active
from agent_review.ports.claude import ClaudeProcessCrashed, ClaudeQuotaExceeded, ClaudeTimeout
from agent_review.settings import (
    Settings,
    clone_token,
    get_settings,
    is_missing,
    require_claude_runner_secrets,
)

logger = logging.getLogger(__name__)

# Tras cargar Settings, el proceso no conserva estos valores en el entorno:
# un `Read` de `/proc/1/environ` no debe verlos.
_SECRET_ENV = (
    "CLAUDE_GIT_TOKEN",
    "CLAUDE_RUNNER_TOKEN",
    "TWG_BBC_TOKEN",
    "GITHUB_TOKEN",
    "TEAMS_BOT_TOKEN",
    "TEAMS_BOT_SHARED_SECRET",
    "REVIEWS_DECISION_TOKEN",
)

_locks_guard = threading.Lock()
_checkout_locks: dict[str, threading.Lock] = {}


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
        token = load_token(_session_path(current))
        credential = clone_token(current)
        secrets = [credential, current.claude_runner_token, token or ""]
        try:
            directory = checkout_directory(
                _workspace_root(current), repo=body.repo, head_sha=body.head_sha
            )
            with _directory_lock(directory):
                prepare_checkout(
                    directory,
                    repo=body.repo,
                    head_sha=body.head_sha,
                    git_token=credential,
                    provider=current.vcs_provider,
                    timeout_s=current.claude_timeout_s,
                )
                extra = {CHILD_TOKEN_ENV: token} if token else {}
                extra["CLAUDE_CONFIG_DIR"] = str(_config_dir(current))
                stdout = SubprocessClaudeRunner(
                    argv_prefix=[current.claude_bin],
                    max_turns=current.claude_max_turns,
                    timeout_s=current.claude_timeout_s,
                    extra_env=extra,
                    cwd=directory,
                    redact=secrets,
                ).run(repo=body.repo, pr_id=body.pr_id, head_sha=body.head_sha)
        except CheckoutError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, _safe(str(exc), secrets)) from exc
        except ClaudeTimeout as exc:
            raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, _safe(str(exc), secrets)) from exc
        except ClaudeQuotaExceeded as exc:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS, _safe(str(exc), secrets)
            ) from exc
        except ClaudeProcessCrashed as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, _safe(str(exc), secrets)) from exc
        return RunResponse(stdout=redact_text(stdout, secrets))

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


def _directory_lock(directory: Path) -> threading.Lock:
    key = str(directory)
    with _locks_guard:
        lock = _checkout_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _checkout_locks[key] = lock
        return lock


def _config_dir(settings: Settings) -> Path:
    """Config del CLI fuera del árbol del PR, para no cargar su `.claude/`."""
    path = Path(settings.claude_workspace_root).resolve().parent / "claude-config"
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _safe(text: str, secrets: list[str]) -> str:
    return redact_text(text, secrets)[-500:]


def _drop_secret_env() -> None:
    for key in _SECRET_ENV:
        os.environ.pop(key, None)


def main() -> None:
    settings = get_settings()
    require_claude_runner_secrets(settings)
    _drop_secret_env()
    logging.basicConfig(level=settings.log_level)
    uvicorn.run(
        build_app(settings),
        host="0.0.0.0",
        port=settings.claude_runner_port,
    )


if __name__ == "__main__":
    main()
