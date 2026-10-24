"""Contrato JSON de la configuración de diales (iniciativa 027, spec 029 P9).

`GET /api/config` y `POST /config` vivían en `ui.py` junto a las páginas HTML, que
se retiran en el bloque R de la 029. Se separan aquí para que el contrato de la SPA
no comparta módulo con el HTML servido: mover el endpoint es más barato que
reescribir su test cuando desaparezca la página.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from agent_review.config_store import ConfigReadError, ConfigStore
from agent_review.panel_config import initial_values, lock_names
from agent_review.settings import Settings, get_settings
from agent_review.web.auth import Session, require_command_session, require_session
from agent_review.web.deps import get_read_model
from agent_review.web.read_model import PanelReadModel

router = APIRouter()

logger = logging.getLogger(__name__)


@router.get("/api/config")
def config_document(
    session: Annotated[Session, Depends(require_session)],
    request: Request,
    model: Annotated[PanelReadModel, Depends(get_read_model)],
) -> dict[str, object]:
    """El documento vigente en JSON, con las revisiones fijadas (027, 029 P9).

    La página es para una persona; esta lectura permite a un `curl`, al CLI o a la
    SPA saber qué hay publicado sin parsear el formulario.

    `pins` **se omite** cuando `dial_pins()` devuelve `None`, que significa «no se
    pudo leer» (el checkpointer no respondió). Publicar `[]` convertiría ese fallo
    en «ninguna review fijó revisión», una afirmación distinta y falsa; omitirlo es
    compatible con el cliente, que lo tipa opcional.
    """
    del session  # cualquier rol lee: el documento no es secreto
    settings = get_settings()
    store = config_store(request, settings)
    try:
        publication = store.read()
    except ConfigReadError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    document: dict[str, object] = {
        "revision": publication.revision,
        "values": publication.overrides,
        "initial": initial_values(settings),
        "locked": sorted(lock_names(settings)),
        "actor": publication.actor,
        "updated_at": publication.updated_at.isoformat() if publication.updated_at else None,
    }
    pins = dial_pins(model)
    if pins is not None:
        document["pins"] = [{"review_key": key, "revision": revision} for key, revision in pins]
    return document


@router.post("/config")
async def save_config(
    request: Request,
    session: Annotated[Session, Depends(require_command_session)],
) -> dict[str, int]:
    """Publica el formulario entero. Una revisión vieja no escribe."""
    from agent_review.panel_config import (
        ConfigError,
        RevisionConflict,
        StaleForm,
        document_for,
    )

    revision, values, painted = await config_form(request)
    settings = get_settings()
    try:
        document = document_for(
            settings,
            values,
            autonomy_ceiling=lambda: autonomy_ceiling(settings),
            painted=painted,
        )
        published = config_store(request, settings).publish(
            expected=revision, overrides=document, actor=session.user
        )
    except (RevisionConflict, StaleForm) as exc:
        # Las dos se resuelven recargando: la revisión la movió otro y el formulario
        # se pintó con otros valores iniciales. El cuerpo lleva cuál es la vigente.
        detail: dict[str, object] = {"detail": str(exc)}
        if isinstance(exc, RevisionConflict):
            detail["revision"] = exc.current
        else:
            detail["stale"] = True
        raise HTTPException(status.HTTP_409_CONFLICT, detail) from exc
    except ConfigError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, {"detail": str(exc), "field": exc.field}
        ) from exc
    except ConfigReadError as exc:
        # La escritura no llegó a confirmarse: es la infraestructura, no el
        # formulario. Un 400 culparía al operador de un Postgres caído.
        logger.warning("No se pudo publicar la configuración: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "No se pudo guardar la configuración"
        ) from exc
    return {"revision": published.revision}


async def config_form(
    request: Request,
) -> tuple[int, dict[str, object], dict[str, object] | None]:
    """Cuerpo del formulario, con `400` propio.

    FastAPI tiparía el cuerpo como objeto y un array daría `422` con el detalle de
    pydantic; el resto de comandos del panel responde `400` para un cuerpo que no
    encaja, así que aquí se parsea igual.
    """
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El cuerpo no es JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "el cuerpo debe traer revision y values")
    revision = body.get("revision")
    values = body.get("values")
    if not isinstance(revision, int) or isinstance(revision, bool) or not isinstance(values, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "el cuerpo debe traer revision y values")
    painted = body.get("initial")
    if painted is not None and not isinstance(painted, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "initial debe ser un objeto")
    return revision, values, painted


def config_store(request: Request, settings: Settings) -> ConfigStore:
    """Almacén del proceso. Los tests inyectan uno en `app.state.config_store`."""
    store: ConfigStore | None = getattr(request.app.state, "config_store", None)
    if store is None:
        from agent_review.config_store import build_config_store

        store = build_config_store(settings)
        request.app.state.config_store = store
    return store


def build_config_store_for_panel(settings: Settings) -> ConfigStore:
    """Almacén del panel, validado en el arranque (027, ADR-0031).

    El candado se resuelve aquí y no en la primera petición: un nombre fuera del
    catálogo tiene que abortar el arranque, como en el worker. Construir también el
    almacén deja el fallo de Postgres en el mismo sitio.
    """
    from agent_review.config_store import build_config_store
    from agent_review.panel_config import lock_names

    lock_names(settings)
    return build_config_store(settings)


def autonomy_ceiling(settings: Settings) -> str:
    """Techo de `max_autonomy` declarado en el YAML, leído con el motor de política.

    Se resuelve solo al validar un override: el resto de guardados no depende de
    que este proceso pueda leer `config/autonomy.yaml`.
    """
    from agent_review.panel_config import ConfigError
    from agent_review.policy.engine import PolicyEngine, PolicyError

    try:
        return str(PolicyEngine.from_file(settings.autonomy_config).raw()["max_autonomy"])
    except OSError as exc:
        raise ConfigError(
            "max_autonomy_override", f"no se pudo leer {settings.autonomy_config}"
        ) from exc
    except (PolicyError, ValueError, TypeError) as exc:
        raise ConfigError("max_autonomy_override", f"autonomy.yaml inválido: {exc}") from exc


def dial_pins(model: object) -> list[tuple[str, int]] | None:
    """Fotos fijadas por las reviews en curso; `None` si no se pudieron leer."""
    reader = getattr(model, "dial_pins", None)
    if not callable(reader):
        return []
    found = reader()
    if found is None:
        return None
    if not isinstance(found, list):
        return []
    pins: list[tuple[str, int]] = []
    for item in found:
        if (
            isinstance(item, tuple)
            and len(item) == 2
            and isinstance(item[0], str)
            and isinstance(item[1], int)
            and not isinstance(item[1], bool)
        ):
            pins.append((item[0], item[1]))
    return pins
