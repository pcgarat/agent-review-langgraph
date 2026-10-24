"""Servido del build de la SPA desde el proceso `web` (029, P5/P14).

El panel deja de ser HTML generado en el servidor: `web` sirve los ficheros que
produce Vite (`frontend/dist` en el repo, `web/static` dentro de la imagen) y un
*fallback* de `index.html` para las rutas del navegador. El `BrowserRouter` del
cliente resuelve `/review/…`, `/bandeja`, etc.; FastAPI solo entrega el armazón.

El *fallback* **no** captura `/api/*` ni `/healthz`: un `GET /api/inexistente` tiene
que seguir siendo un `404`, no un HTML con `200` que haga creer que el endpoint
existe (P5). Por eso, al final de la pila, primero hay un comodín de backend que
responde `404` a cualquier método y solo después el de la SPA.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

# Primer segmento de una ruta que pertenece al backend. Con el comodín delante, un
# endpoint que no existe devolvería el armazón y el cliente lo leería como un `200`.
_BACKEND_PREFIXES = frozenset({"api", "healthz"})

# Métodos que puede ver un comodín de backend: un `POST` a una ruta inexistente
# también es `404`, no un `405` por el comodín `GET` de la SPA.
_BACKEND_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")

# Ruta comodín de la SPA, publicada para que un test compruebe que `main` la monta
# sin depender de que exista un build.
SPA_ROUTE = "/{full_path:path}"


def checkout_dist() -> Path:
    """`frontend/dist` en un checkout: `src/agent_review/web/spa.py` → raíz del repo."""
    return Path(__file__).resolve().parents[3] / "frontend" / "dist"


def bundled_static() -> Path:
    """Dónde la imagen copia el build: dentro del paquete, junto a este módulo."""
    return Path(__file__).resolve().parent / "static"


def spa_directory() -> Path | None:
    """Build a servir, o `None` si no hay ninguno.

    Se mira primero la copia empaquetada (imagen) y después el checkout. Sin build
    el proceso arranca igual: solo las rutas del panel responden `503`, para que un
    despliegue sin `make frontend-build` no tumbe los endpoints del `web`.
    """
    for candidate in (bundled_static(), checkout_dist()):
        if (candidate / "index.html").is_file():
            return candidate
    return None


def _is_backend(full_path: str) -> bool:
    """La ruta pertenece al backend aunque el endpoint no exista."""
    return full_path.split("/", 1)[0] in _BACKEND_PREFIXES


def _inside(root: Path, target: Path) -> bool:
    """`target` no sale del directorio que se sirve (un `..` en la URL)."""
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _index_or_missing(root: Path | None) -> FileResponse:
    if root is None or not (root / "index.html").is_file():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "El panel no está construido: falta el build de frontend/",
        )
    return FileResponse(root / "index.html")


def mount_spa(app: FastAPI, directory: Path | None = None) -> Path | None:
    """Monta los assets y registra los dos comodines, en este orden.

    `directory` permite a los tests servir un build de mentira. Sin él, cada
    petición resuelve el build con `spa_directory()`, que un test puede sustituir.
    Devuelve el directorio de assets montado (o el build) para poder afirmarlo.
    """
    root = directory if directory is not None else spa_directory()
    if root is None:
        logger.warning("El panel no tiene build: falta frontend/dist o web/static")
    if root is not None and (root / "assets").is_dir():
        # Los nombres llevan el hash del contenido: caché larga y sin sorpresas.
        app.mount("/assets", StaticFiles(directory=root / "assets"), name="panel-assets")

    def current() -> Path | None:
        return directory if directory is not None else spa_directory()

    @app.api_route("/api/{full_path:path}", methods=list(_BACKEND_METHODS), include_in_schema=False)
    @app.api_route(
        "/healthz/{full_path:path}", methods=list(_BACKEND_METHODS), include_in_schema=False
    )
    def missing_backend(full_path: str) -> FileResponse:
        """Un endpoint que no existe sigue siendo `404`, con el cuerpo de siempre."""
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Ruta inexistente: /{full_path}")

    @app.get(SPA_ROUTE, include_in_schema=False)
    def serve_spa(full_path: str) -> FileResponse:
        if _is_backend(full_path):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Ruta inexistente: /{full_path}")
        base = current()
        if base is None:
            return _index_or_missing(None)
        target = base / full_path
        if full_path and target.is_file() and _inside(base, target):
            return FileResponse(target)
        # Un asset que falta es un `404`, no el HTML: el navegador lo leería como un
        # fichero válido y el error aparecería en un sitio que no toca.
        if "." in Path(full_path).name:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No existe /{full_path}")
        return _index_or_missing(base)

    return root
