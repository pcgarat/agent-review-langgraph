"""Sesion con rol del panel (iniciativa 019, W7).

Cancelar es una accion con consecuencias. El bearer de servicio de la API autoriza a
un servicio, no identifica a una persona, asi que el panel usa una cabecera propia,
`X-Panel-Session`; **no** reutiliza `REVIEWS_DECISION_TOKEN` (son superficies distintas).

Con secreto real, la cabecera es un token opaco que se contrasta con
`PANEL_SESSION_SECRET` mediante `hmac.compare_digest`. Quien actua y su rol salen de
`PANEL_SESSION_USER` y `PANEL_SESSION_ROLE`, no del secreto: el actor se publica en
el PR y no puede ser parte de la credencial. Sin secreto (o con el de ejemplo) la
sesion se relaja y la cabecera, si viene, se lee como `<usuario>:<rol>`.
"""

from __future__ import annotations

import hmac
import logging
from enum import StrEnum
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Cookie, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel

from agent_review.settings import Settings, get_settings, is_missing

logger = logging.getLogger(__name__)

# Nombre de la cookie que lleva la sesion en el navegador. Los endpoints JSON
# aceptan las dos vias (cabecera y cookie) porque `curl` y los tests mandan cabecera
# y una navegacion normal, solo cookie.
SESSION_COOKIE = "panel_session"
# Usuario que se asume cuando no hay secreto ni cabecera (dev/test).
_DEV_USER = "dev"
# Valores de `Sec-Fetch-Site` que no delatan una peticion cruzada: el propio origen,
# o una accion directa del usuario (barra de direcciones, marcador).
_SAME_SITE_FETCH = {"same-origin", "none"}


class Role(StrEnum):
    """Que puede hacer una sesion del panel: mirar (`lector`) o mandar (`operador`)."""

    LECTOR = "lector"
    OPERADOR = "operador"


class Session(BaseModel):
    """Identidad autenticada del panel: quien es y que rol tiene."""

    user: str
    role: Role

    def can_command(self) -> bool:
        return self.role is Role.OPERADOR


def parse_session(value: str) -> Session | None:
    """`<usuario>:<rol>` -> sesion, o `None` si no tiene esa forma.

    Se parte por el **ultimo** `:` para que un nombre con dos puntos no robe el rol.
    """
    user, sep, role = value.rpartition(":")
    if not sep:
        return None
    user = user.strip()
    if not user or role not in {item.value for item in Role}:
        return None
    return Session(user=user, role=Role(role))


def verify_session(settings: Settings, header: str | None) -> Session:
    """Valida la cabecera de sesion y devuelve la identidad; `401` si no vale.

    Sin secreto configurado (o con el de ejemplo) se relaja como
    `_verify_decision_token`: la cabecera es opcional y, sin ella, la sesion es la
    local de operador. Con un secreto real, la cabecera es obligatoria y debe
    coincidir **exactamente** con el token. El rol lo fija el despliegue
    (`PANEL_SESSION_ROLE`, o el sufijo legado `<usuario>:<rol>` del secreto si no
    hay rol explicito), nunca quien llama, para que un `lector` no pueda reclamar
    ser `operador`. El usuario publicado no se lee del secreto.
    """
    secret = settings.panel_session_secret.strip()
    relaxed = is_missing(secret)
    if header is None:
        if relaxed:
            return Session(user=_DEV_USER, role=Role.OPERADOR)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Falta la sesion del panel")
    if relaxed:
        session = parse_session(header)
        if session is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sesion del panel invalida")
        return session
    # `compare_digest` sobre bytes: con `str` no ASCII lanzaria `TypeError`.
    if not hmac.compare_digest(header.encode(), secret.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sesion del panel invalida")
    return _deployed_session(settings, secret)


def _deployed_session(settings: Settings, secret: str) -> Session:
    """Identidad del despliegue. El secreto solo se usa para el rol legado."""
    user = settings.panel_session_user.strip() or "panel"
    role_name = settings.panel_session_role.strip()
    if not role_name:
        legacy = parse_session(secret)
        role_name = legacy.role.value if legacy is not None else Role.OPERADOR.value
    if role_name not in {item.value for item in Role}:
        logger.error("PANEL_SESSION_ROLE no es lector ni operador: %s", role_name)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sesion del panel mal configurada")
    return Session(user=user, role=Role(role_name))


def require_session(
    x_panel_session: str | None = Header(default=None),
    panel_session: str | None = Cookie(default=None),
) -> Session:
    """Dependencia FastAPI: exige y valida la sesion del panel.

    Acepta la cabecera `X-Panel-Session` (CLI, `curl`, tests) o la cookie
    `panel_session` (navegador: una navegacion normal no puede mandar cabeceras).
    """
    return verify_session(get_settings(), x_panel_session or panel_session)


# --- La cookie de sesion (029, P6/P7) --------------------------------------------


def cookie_secure(settings: Settings) -> bool:
    """`Secure` cuando el panel se sirve por HTTPS.

    Se mira `PANEL_PUBLIC_URL` y no el esquema de la peticion: detras de un proxy
    TLS el `web` ve `http` y marcaria la cookie como insegura, con lo que el
    navegador la aceptaria y el despliegue creeria estar protegido.
    """
    return settings.panel_public_url.strip().lower().startswith("https://")


def set_session_cookie(response: Response, settings: Settings) -> None:
    """Deja la cookie de sesion en la respuesta.

    `HttpOnly` para que el JavaScript del panel no pueda leerla (el rol se
    pregunta por `GET /api/session`), `SameSite=Lax` porque el panel no se embebe
    en otro sitio, y `Secure` si el despliegue es HTTPS. Sin `Max-Age`: es una
    cookie de sesion del navegador.
    """
    response.set_cookie(
        SESSION_COOKIE,
        settings.panel_session_secret.strip(),
        httponly=True,
        samesite="lax",
        secure=cookie_secure(settings),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def session_payload(session: Session) -> dict[str, str]:
    """Lo que la SPA necesita para saber quién es y qué puede.

    Nunca lleva el token: es la identidad, no la credencial.
    """
    return {"user": session.user, "role": session.role.value}


def require_operador(session: Annotated[Session, Depends(require_session)]) -> Session:
    """Dependencia FastAPI de los comandos: rechaza al rol `lector` con `403`."""
    if not session.can_command():
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"El rol {session.role.value} no puede ejecutar comandos",
        )
    return session


def require_command_session(
    request: Request,
    session: Annotated[Session, Depends(require_operador)],
) -> Session:
    """Dependencia FastAPI de los comandos: `require_operador` + defensa CSRF.

    La cookie `panel_session` la manda el navegador **solo**, tambien en un `POST`
    que dispare otra web (un formulario autoenviado); sin esta guarda, visitar esa
    web bastaria para cancelar o reanudar una review. Con la cabecera
    `X-Panel-Session` no hay riesgo (otro origen no puede ponerla sin un *preflight*
    de CORS, que el panel no concede), asi que solo se endurece la via de la cookie:

    - `Content-Type: application/json`, que un formulario no puede mandar y que un
      `fetch` de otro origen solo manda tras un *preflight* que falla;
    - `Origin` (si viene) del mismo host, y `Sec-Fetch-Site` (si viene) del mismo
      origen. Es lo que ya hace la UI (`fetch` con `credentials: "same-origin"`).

    Se compara el host y no el esquema: detras de un proxy TLS el panel ve `http`.
    """
    if request.headers.get("x-panel-session") is not None:
        return session
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Un comando por cookie debe enviarse como JSON"
        )
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site.lower() not in _SAME_SITE_FETCH:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Comando desde otro origen")
    origin = request.headers.get("origin")
    if origin is not None and urlsplit(origin).netloc != request.headers.get("host", ""):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Comando desde otro origen")
    return session
