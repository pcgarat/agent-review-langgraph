"""Proceso `claude-runner` (025 R4, ADR-0028).

Checkout en un volumen propio y subproceso `claude` con entorno cerrado. El
worker pide el stdout por HTTP; este proceso no escribe el artefacto. El fetch
puede usar el token de VCS (ADR-0032) y el subproceso `claude` no lo hereda.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import shutil
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from agent_review.adapters.claude_cli import SubprocessClaudeRunner, redact_text
from agent_review.claude_activity import ClaudeActivity
from agent_review.claude_runner.checkout import CheckoutError, checkout_directory, prepare_checkout
from agent_review.claude_session import CHILD_TOKEN_ENV, load_token, session_active
from agent_review.ports.claude import ClaudeCeiling, ClaudeProcessCrashed, ClaudeQuotaExceeded
from agent_review.settings import (
    CLAUDE_PANEL_TIMEOUT_MAX_S,
    CLAUDE_PANEL_TIMEOUT_MIN_S,
    CLAUDE_PANEL_TURNS_MAX,
    CLAUDE_PANEL_TURNS_MIN,
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
    base_sha: str
    # Vacío: manda el entorno de este proceso. El worker manda el dial publicado.
    max_turns: int | None = Field(
        default=None, ge=CLAUDE_PANEL_TURNS_MIN, le=CLAUDE_PANEL_TURNS_MAX
    )
    timeout_s: float | None = Field(
        default=None, ge=CLAUDE_PANEL_TIMEOUT_MIN_S, le=CLAUDE_PANEL_TIMEOUT_MAX_S
    )


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
                max_turns, timeout_s = resolve_ceilings(
                    current, max_turns=body.max_turns, timeout_s=body.timeout_s
                )
                prepare_checkout(
                    directory,
                    repo=body.repo,
                    head_sha=body.head_sha,
                    base_sha=body.base_sha,
                    git_token=credential,
                    provider=current.vcs_provider,
                    timeout_s=timeout_s,
                )
                extra = {CHILD_TOKEN_ENV: token} if token else {}
                config_dir = _config_dir(current)
                write_runner_settings(config_dir, workspace_root=_workspace_root(current))
                ensure_bundled_skill(config_dir)
                extra["CLAUDE_CONFIG_DIR"] = str(config_dir)
                # La imagen trae el binario. Una review no debe sustituirlo.
                extra["DISABLE_UPDATES"] = "1"
                activity = ClaudeActivity(
                    _session_path(current),
                    repo=body.repo,
                    pr_id=body.pr_id,
                    head_sha=body.head_sha,
                )
                activity.start()

                def _line(text: str) -> None:
                    activity.append(redact_text(text, secrets))

                try:
                    stdout = SubprocessClaudeRunner(
                        argv_prefix=[current.claude_bin],
                        max_turns=max_turns,
                        timeout_s=timeout_s,
                        extra_env=extra,
                        cwd=directory,
                        redact=secrets,
                    ).run(
                        repo=body.repo,
                        pr_id=body.pr_id,
                        head_sha=body.head_sha,
                        base_sha=body.base_sha,
                        on_line=_line,
                    )
                finally:
                    activity.finish()
        except CheckoutError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, _safe(str(exc), secrets)) from exc
        except ClaudeCeiling as exc:
            raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, _safe(str(exc), secrets)) from exc
        except ClaudeQuotaExceeded as exc:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS, _safe(str(exc), secrets)
            ) from exc
        except ClaudeProcessCrashed as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, _safe(str(exc), secrets)) from exc
        return RunResponse(stdout=redact_text(stdout, secrets))

    return app


def resolve_ceilings(
    settings: Settings, *, max_turns: int | None, timeout_s: float | None
) -> tuple[int, float]:
    """El cuerpo manda si viene. Si no, el entorno de este proceso."""
    return (
        settings.claude_max_turns if max_turns is None else max_turns,
        settings.claude_timeout_s if timeout_s is None else timeout_s,
    )


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


def _bundled_skill() -> Path:
    override = os.environ.get("CLAUDE_SKILL_SOURCE", "").strip()
    if override:
        return Path(override)
    return Path("/opt/claude-skills/ce-code-review")


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


def runner_sandbox_settings(*, workspace_root: Path) -> dict[str, object]:
    """Sandbox obligatorio: solo Anthropic, sin salir, checkout no escribible."""
    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            # Docker no deja montar un /proc nuevo. Sin esto cada Bash muere
            # antes de git. La jaula ve los procesos del contenedor.
            "enableWeakerNestedSandbox": True,
            "filesystem": {
                # El cwd es un checkout dentro de esta raíz. denyWrite de un
                # antepasado tapa el árbol aunque el sandbox permita el cwd.
                "denyWrite": [str(workspace_root.resolve())],
                "allowWrite": ["/tmp"],
            },
            "network": {
                "allowedDomains": ["api.anthropic.com"],
                "strictAllowlist": True,
            },
        }
    }


def write_runner_settings(config_dir: Path, *, workspace_root: Path) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "settings.json"
    payload = json.dumps(runner_sandbox_settings(workspace_root=workspace_root), indent=2)
    path.write_text(payload + "\n", encoding="utf-8")
    return path


def ensure_bundled_skill(config_dir: Path, source: Path | None = None) -> None:
    """Vuelca la skill de la imagen si el config dir todavía no la tiene."""
    origin = _bundled_skill() if source is None else source
    dest = config_dir / "skills" / "ce-code-review"
    if (dest / "SKILL.md").is_file():
        return
    if not (origin / "SKILL.md").is_file():
        raise RuntimeError("la imagen no trae la skill ce-code-review")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(origin, dest)


def require_sandbox_binaries() -> None:
    """Sin bubblewrap o sin socat el CLI no arranca: no hay sandbox de red."""
    missing = [name for name in ("bwrap", "socat") if shutil.which(name) is None]
    if missing:
        joined = " y ".join(missing)
        raise RuntimeError(f"el sandbox de claude no puede arrancar sin {joined} en el PATH")


def require_claude_binary(binary: str) -> None:
    """El proceso no sirve si el ejecutable no esta en el PATH de la imagen."""
    if shutil.which(binary) is None:
        raise RuntimeError(
            f"no se encontro el ejecutable {binary!r}. "
            "La imagen de claude-runner instala el paquete apt claude-code; "
            "el comando es claude."
        )


def main() -> None:
    settings = get_settings()
    require_claude_runner_secrets(settings)
    require_claude_binary(settings.claude_bin)
    require_sandbox_binaries()
    config_dir = _config_dir(settings)
    write_runner_settings(config_dir, workspace_root=_workspace_root(settings))
    ensure_bundled_skill(config_dir)
    _drop_secret_env()
    logging.basicConfig(level=settings.log_level)
    uvicorn.run(
        build_app(settings),
        host="0.0.0.0",
        port=settings.claude_runner_port,
    )


if __name__ == "__main__":
    main()
