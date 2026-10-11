from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from reviews.settings import Settings

logger = logging.getLogger(__name__)

# Herramienta estatica por defecto cuando el perfil declara `checks.typecheck`
# pero no una lista explicita de `expected_tools`.
_DEFAULT_TOOL = "phpstan"


def load_profile(settings: Settings) -> dict[str, Any]:
    """Perfil del repo revisado, o `{}` si no existe o no es un mapa.

    Nunca lanza: un perfil ausente o invalido degrada a "sin perfil" en vez de
    tumbar el arranque del contenedor.
    """
    path = Path(settings.repo_profile_config)
    try:
        profile = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        logger.warning("Sin perfil de repo en %s; se omite la persona estatica", path)
        return {}
    except yaml.YAMLError as exc:
        logger.error("Perfil de repo invalido en %s (%s); se omite la persona estatica", path, exc)
        return {}
    if not isinstance(profile, dict):
        logger.warning("Perfil de repo en %s no es un mapa; se omite la persona estatica", path)
        return {}
    return profile


def expected_tools(settings: Settings, profile: dict[str, Any] | None = None) -> list[str]:
    """Herramientas cuya salida espera la review antes de dispararse.

    Fuente unica (B4): manda el perfil del repo (`expected_tools`, o la clave de
    `checks.typecheck` como respaldo); `EXPECTED_TOOLS` solo actua como override
    explicito cuando difiere del valor por defecto. Asi el perfil y el `.env` no
    pueden declarar conjuntos distintos sin que sea intencional, y la barrera de
    la API y la del worker usan exactamente el mismo conjunto.
    """
    from_env = [tool.strip() for tool in settings.expected_tools.split(",") if tool.strip()]
    default_tools = type(settings).model_fields["expected_tools"].default
    if from_env and from_env != str(default_tools).split(","):
        return from_env
    if profile is None:
        profile = load_profile(settings)
    declared = profile.get("expected_tools")
    if isinstance(declared, list) and declared:
        return [str(tool) for tool in declared]
    checks = profile.get("checks") or {}
    if isinstance(checks, dict) and checks.get("typecheck"):
        return [_DEFAULT_TOOL]
    return from_env
