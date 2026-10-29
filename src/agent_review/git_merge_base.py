"""Merge-base de dos commits traídos con `--depth 1`.

El `base_sha` de un PR es la punta viva de la rama destino, no la bifurcación.
Con dos commits poco profundos no hay ancestro común: diffear o leer contra la
punta mete lo que entró en destino después de abrir la rama. Los runners que
traen la base profundizan por pasos hasta que `git merge-base` resuelve.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

# Cada paso se suma al anterior: ~1250 commits por lado como máximo. El coste
# crece con lo que divergen las ramas, no con el tamaño del repo.
DEEPEN_STEPS = (50, 200, 1000)

GitRun = Callable[[list[str]], "subprocess.CompletedProcess[str] | None"]


class DeepenFailed(Exception):
    """El `fetch --deepen` falló. Lleva el `stderr` sin redactar."""


def deepen_to_merge_base(run: GitRun, *, head_sha: str, base_sha: str) -> str | None:
    """Profundiza los dos lados contra `origin` hasta encontrar el ancestro común.

    `None` si no aparece tras el último paso. `run` ejecuta `git` en el repo con
    sus argumentos y devuelve `None` si no llegó a terminar.
    """
    for step in (0, *DEEPEN_STEPS):
        if step:
            fetched = run(["fetch", "-q", "--deepen", str(step), "origin", head_sha, base_sha])
            if fetched is None or fetched.returncode != 0:
                raise DeepenFailed((fetched.stderr if fetched is not None else "") or "")
        found = run(["merge-base", head_sha, base_sha])
        merge_base = found.stdout.strip() if found is not None else ""
        if found is not None and found.returncode == 0 and merge_base:
            return merge_base
    return None
