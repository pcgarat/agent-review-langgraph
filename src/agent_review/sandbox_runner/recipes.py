from __future__ import annotations

import re
import shlex
from typing import Any

from agent_review.errors import PermanentError

# Rutas de test que el perfil admite para la recipe dirigida. Es una allowlist,
# no un `fnmatch` generico: solo tests PHP dentro de `tests/`.
_TARGET_RE = re.compile(r"^tests/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.php$")


class RecipeError(PermanentError):
    """Recipe del perfil invalida: no se deduce un argv seguro."""


class InvalidTarget(PermanentError):
    """Una ruta de test no cumple la allowlist; se rechaza antes de ejecutar nada."""


def validate_targets(targets: list[str]) -> list[str]:
    """Valida y normaliza las rutas de test dirigidas.

    La allowlist (`tests/**.php`, sin `..`, sin rutas absolutas) evita que un
    `targets` forjado convierta `bin/phpunit {targets}` en una via para pasar
    flags o rutas fuera del workspace. La existencia se comprueba contra el
    workspace clonado en el runner, no aqui.
    """
    clean: list[str] = []
    for raw in targets:
        target = raw.strip()
        if not target:
            continue
        if target.startswith("/") or ".." in target.split("/"):
            raise InvalidTarget(f"ruta de test no permitida: {raw!r}")
        if not _TARGET_RE.fullmatch(target):
            raise InvalidTarget(f"ruta de test fuera de la allowlist (tests/**.php): {raw!r}")
        clean.append(target)
    return clean


def _split(command: str) -> list[str]:
    """argv de una plantilla del perfil, sin pasar por una shell.

    La plantilla la revisa una persona (el perfil es config versionada), pero se
    interpreta con `shlex.split` y nunca con `shell=True`: asi `;`, `&&` o una
    sustitucion `$(...)` no pueden encadenar un segundo comando.
    """
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise RecipeError(f"recipe ilegible ({command!r}): {exc}") from exc
    if not argv:
        raise RecipeError("recipe vacia")
    return argv


def expand_check(name: str, checks: dict[str, Any], targets: list[str]) -> list[str] | None:
    """argv de una recipe de check, o `None` si hay que saltarla.

    Una recipe dirigida (`{targets}`) sin targets se salta: no se convierte en una
    ejecucion del comando completo, que seria caro y no dirigido.
    """
    template = checks.get(name)
    if not isinstance(template, str) or not template.strip():
        raise RecipeError(f"la recipe {name!r} no esta declarada en `checks` del perfil")
    if "{targets}" in template:
        if not targets:
            return None
        head = _split(template.replace("{targets}", " ").strip())
        return [*head, *targets]
    return _split(template)


def expand_setup(commands: list[str]) -> list[list[str]]:
    """argv de cada comando de `setup`/`setup_network`, en orden."""
    return [_split(command) for command in commands]


def declared_checks(profile: dict[str, Any]) -> dict[str, Any]:
    checks = profile.get("checks")
    if not isinstance(checks, dict):
        return {}
    return checks
