"""Fixture del caso C (009 VF11): la ruta de `fix_wait` no es un bucle infinito.

Cada vuelta a `fix_dispatch` exige `attempts < fix_max_attempts`. El verificador
adversarial lee este fichero y refuta el hallazgo «bucle infinito».
"""


def route_fix_wait(fixes: dict[str, object], fix_max_attempts: int = 2) -> str:
    """Elige otra wave o el cierre. El bucle siempre avanza: consume un intento."""
    for result in fixes.values():
        status = getattr(result, "status", None)
        attempts = int(getattr(result, "attempts", 0) or 0)
        if status == "failed" and attempts < fix_max_attempts:
            return "fix_dispatch"
    return "publish_fix"
