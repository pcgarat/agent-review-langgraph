"""Destino del CLI: Anthropic, o un modelo del clúster a través del gateway.

El panel escribe la elección junto a la sesión. El runner la lee en cada
`POST /run`. Sin fichero manda `CLAUDE_UPSTREAM`. El worker no ve el modelo.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from urllib.request import urlopen

from agent_review.settings import CLUSTER_MODELS, Settings

logger = logging.getLogger(__name__)

# Lo que el panel puede guardar. `claude` es Anthropic; el resto, el clúster.
PANEL_CHOICES = frozenset({"claude", *CLUSTER_MODELS})

_TRAFFIC = "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"


@dataclass(frozen=True)
class ResolvedBackend:
    """A quién llama el hijo en esta pasada."""

    kind: Literal["anthropic", "gateway"]
    model: str


def backend_path(session_path: Path) -> Path:
    """Mismo volumen que la sesión, otro fichero. El token no va aquí."""
    return session_path.with_name("backend.json")


def read_choice(path: Path) -> str | None:
    """Elección del panel, o `None` si nadie ha elegido o el fichero no vale."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("No se pudo leer el destino de Claude")
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("El destino de Claude no es JSON")
        return None
    if not isinstance(data, dict):
        return None
    choice = data.get("choice")
    if not isinstance(choice, str) or choice not in PANEL_CHOICES:
        return None
    return choice


def write_choice(path: Path, choice: str) -> None:
    """Guarda la elección con modo `0600`. Un valor fuera de la lista no se escribe."""
    if choice not in PANEL_CHOICES:
        raise ValueError(f"destino desconocido: {choice}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"choice": choice})
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode())
    finally:
        os.close(fd)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def resolve_backend(choice: str | None, settings: Settings) -> ResolvedBackend:
    """La elección del panel gana. Sin ella, manda el entorno."""
    if choice == "claude":
        return ResolvedBackend("anthropic", "")
    if choice in CLUSTER_MODELS:
        return ResolvedBackend("gateway", choice)
    if settings.claude_upstream == "gateway":
        return ResolvedBackend("gateway", settings.claude_gateway_model)
    return ResolvedBackend("anthropic", "")


def load_gateway_token(settings: Settings) -> str:
    """El valor explícito gana. Si no hay, el fichero que crea el arranque."""
    explicit = settings.claude_gateway_token.strip()
    if explicit:
        return explicit
    path = settings.claude_gateway_token_file.strip()
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def gateway_domain(url: str) -> str:
    """Host de la jaula. Un `localhost` del host no se resuelve dentro de bwrap."""
    host = urlparse(url.strip()).hostname
    if not host:
        raise ValueError("CLAUDE_GATEWAY_URL no tiene host")
    return host


def child_gateway_env(*, url: str, token: str, model: str) -> dict[str, str]:
    """Lo que Claude Code lee. El token es el del gateway, no el de Anthropic."""
    return {
        "ANTHROPIC_BASE_URL": url,
        "ANTHROPIC_AUTH_TOKEN": token,
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "ANTHROPIC_SMALL_FAST_MODEL": model,
        "CLAUDE_CODE_SUBAGENT_MODEL": model,
        _TRAFFIC: "1",
    }


def gateway_healthy(url: str, *, timeout_s: float = 2.0) -> bool:
    """El gateway responde. Sin URL no hay destino que marcar como listo."""
    base = url.strip().rstrip("/")
    if not base:
        return False
    try:
        with urlopen(f"{base}/health/liveliness", timeout=timeout_s) as response:
            return 200 <= getattr(response, "status", 0) < 300
    except OSError:
        return False
