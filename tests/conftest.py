from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_review.settings import Settings, get_settings

_ROOT = Path(__file__).resolve().parents[1]
_ENV_FILES = (_ROOT / ".env", _ROOT / ".env.example")


def _claves_del_entorno() -> set[str]:
    """Variables del proceso que `Settings` podria leer.

    Cubre los campos del modelo (pydantic-settings los busca en mayusculas) y las
    claves declaradas en `.env`/`.env.example`, que incluyen extras que no son
    campos del modelo (p. ej. `REVIEWS_URL`).
    """
    claves = {nombre.upper() for nombre in Settings.model_fields}
    for ruta in _ENV_FILES:
        if not ruta.exists():
            continue
        for linea in ruta.read_text(encoding="utf-8").splitlines():
            nombre = linea.split("=", 1)[0].strip()
            if nombre and not nombre.startswith("#"):
                claves.add(nombre)
    return claves


_CLAVES_DEL_ENTORNO = _claves_del_entorno()


@pytest.fixture(autouse=True)
def aislar_entorno_del_host(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[None]:
    """Hace que la suite no dependa del `.env` ni del entorno del host.

    `Settings` lee `env_file=".env"` (gitignored) y `os.environ`, y las variables
    de entorno ganan al fichero. Sin aislar, un `VCS_PROVIDER=github` local
    cambiaria el repo que admite la API (tumbando la suite), un secreto real
    endureceria la firma del webhook y `ENVIRONMENT=production` haria fallar el
    arranque. Se desactiva el fichero y se borran del proceso las claves que el
    modelo conoce, de modo que manda el valor por defecto del modelo. Cada test
    parte de un estado limpio y `monkeypatch` restaura todo al terminar.

    Los tests `live` quedan fuera: su proposito es ejercer contra un despliegue
    real y necesitan las credenciales del entorno.
    """
    if request.node.get_closest_marker("live") is not None:
        yield
        return

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for clave in _CLAVES_DEL_ENTORNO:
        monkeypatch.delenv(clave, raising=False)
    # Un `Settings` cacheado antes de este test (p. ej. el que disparo el fallo)
    # arrastraria valores del host: se descarta para reevaluar el estado limpio.
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
