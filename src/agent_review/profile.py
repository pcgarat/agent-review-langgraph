from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from agent_review.settings import Settings

logger = logging.getLogger(__name__)

# Herramienta estatica por defecto cuando el perfil declara `checks.typecheck`
# pero no una lista explicita de `expected_tools`.
_DEFAULT_TOOL = "phpstan"

# Contrato del perfil: claves conocidas y tipo del valor. Un perfil sin `repo` o
# con una clave desconocida es un error de configuracion, no una variante valida
# (B4: la barrera de ingesta depende de `expected_tools`; su ausencia silenciosa
# degrada la review sin avisar).
_FLAT_KEYS = {
    "repo",
    "language",
    "framework",
    "package_manager",
    "ci_image",
    "ci_image_digest",
    "source",
}
_MAP_KEYS = {"env", "checks", "extra_checks", "caches", "sandbox"}
_LIST_KEYS = {"setup", "setup_network", "verify", "expected_tools"}
_LIST_OF_STR_KEYS = {"setup", "setup_network", "verify", "expected_tools"}


def validate_profile(profile: dict[str, Any]) -> list[str]:
    """Problemas del perfil. Lista vacia = contrato cumplido.

    No lanza: el perfil ausente o invalido degrada a "sin perfil" (§B4). Pero un
    perfil mal escrito deja de fallar en silencio: se registra en el arranque y el
    test de contrato (`tests/test_profile.py`) lo detecta en CI.
    """
    problems: list[str] = []
    known = _FLAT_KEYS | _MAP_KEYS | _LIST_KEYS
    for key, value in profile.items():
        if key not in known:
            problems.append(f"clave desconocida: {key!r}")
            continue
        if key in _FLAT_KEYS and not isinstance(value, str):
            problems.append(f"{key!r} debe ser texto, es {type(value).__name__}")
        elif key in _MAP_KEYS and not isinstance(value, dict):
            problems.append(f"{key!r} debe ser un mapa, es {type(value).__name__}")
        elif key in _LIST_KEYS and not isinstance(value, list):
            problems.append(f"{key!r} debe ser una lista, es {type(value).__name__}")
        elif key in _LIST_OF_STR_KEYS and any(not isinstance(item, str) for item in value):
            problems.append(f"{key!r} debe contener solo texto")
    if not profile.get("repo"):
        problems.append("falta 'repo' (identidad del perfil)")
    return problems


def load_profile(settings: Settings) -> dict[str, Any]:
    """Perfil del repo revisado, o `{}` si no existe o no es un mapa.

    Nunca lanza: un perfil ausente o invalido degrada a "sin perfil" en vez de
    tumbar el arranque del contenedor. Los problemas de contrato se registran.
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
    for problem in validate_profile(profile):
        logger.warning("Perfil de repo (%s): %s", path, problem)
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


def fix_recipes(
    settings: Settings,
    profile: dict[str, Any] | None = None,
    *,
    names: list[str] | None = None,
) -> list[str]:
    """Nombres de recipe que el sandbox verifica, en orden (fuente unica).

    Manda el parametro explicito (inyeccion/tests), luego el `verify` del perfil
    (versionado, con la procedencia en `source`) y, si no hay perfil, `FIX_RECIPES`
    del entorno. El orden vive en el perfil: el `fix_recipes` de settings solo es
    fallback para entornos sin perfil, no una segunda fuente de verdad.
    """
    if names is not None:
        return list(names)
    if profile is None:
        profile = load_profile(settings)
    declared = profile.get("verify")
    if isinstance(declared, list) and declared:
        return [str(item) for item in declared]
    return settings.fix_recipes_list


def sandbox_timeout_s(settings: Settings, profile: dict[str, Any] | None = None) -> float:
    """Techo de una verificacion en el sandbox, del perfil (`sandbox.timeout_s`).

    Vive en el perfil porque el coste del CI varia por repo (aqui ~20 min). El
    cliente HTTP lo usa como deadline del `POST /verify` bloqueante; el fake de
    settings es el fallback si no hay perfil.
    """
    if profile is None:
        profile = load_profile(settings)
    sandbox = profile.get("sandbox")
    if isinstance(sandbox, dict):
        declared = sandbox.get("timeout_s")
        if isinstance(declared, (int, float)) and declared > 0:
            return float(declared)
    return settings.sandbox_timeout_s


def ci_image_digest(profile: dict[str, Any]) -> str:
    """Digest inmutable de la imagen del CI, o cadena vacia si no es valido.

    Solo se acepta un `sha256:...`. Un tag mutable (`:latest`) permitiria que la
    verificacion corriera con una imagen distinta de la que el perfil declara, que
    es justo lo que `ci_image_digest` existe para evitar (spec §5.4, riesgo R3).
    """
    digest = str(profile.get("ci_image_digest", "")).strip()
    if digest.startswith("sha256:") and len(digest) > len("sha256:"):
        return digest
    return ""


def resolve_ci_image(profile: dict[str, Any]) -> str | None:
    """Referencia `repo@sha256:...` para el runner, o `None` si falta el digest.

    El perfil declara `ci_image` (con tag) y `ci_image_digest` (solo el sha256);
    el runner necesita la referencia pinada, que se compone quitando el tag
    mutable a `ci_image` y anclando el digest.
    """
    digest = ci_image_digest(profile)
    if not digest:
        return None
    reference = str(profile.get("ci_image", "")).strip()
    if not reference:
        return None
    # Se quita el tag solo si lo lleva el ultimo segmento (el de la imagen): un
    # `host:puerto/...` tiene dos puntos y el primero no es un tag.
    last_segment = reference.rsplit("/", 1)[-1]
    repo = reference.rsplit(":", 1)[0] if ":" in last_segment else reference
    return f"{repo}@{digest}"
