from __future__ import annotations

import re


def path_matches(path: str, pattern: str) -> bool:
    """Match de glob sobre rutas. `**` cruza directorios, `*` no.

    Una sola implementacion para las guardas de politica y el filtro de scope:
    si el significado de `**/auth/**` cambiara en un sitio y no en el otro, las
    decisiones dejarian de ser auditables.
    """
    regex = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.fullmatch(regex, path) is not None


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(path_matches(path, pattern) for pattern in patterns)
