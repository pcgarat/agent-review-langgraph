"""Sesion con rol del panel (iniciativa 019, W7).

Cancelar es una accion con consecuencias. El bearer de servicio de la API autoriza a
un servicio, no identifica a una persona, asi que el panel usa una cabecera propia,
`X-Panel-Session`, con la forma `<usuario>:<rol>`; **no** reutiliza
`REVIEWS_DECISION_TOKEN` (son superficies distintas).

El valor de la cabecera se contrasta con `PANEL_SESSION_SECRET` mediante
`hmac.compare_digest`, igual que el bearer de decision de la API. Sin secreto
configurado (o con el valor de ejemplo) la sesion se relaja para dev/test.
"""

from __future__ import annotations

import hmac
import logging
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from pydantic import BaseModel

from agent_review.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# Valores de ejemplo que relajan la sesion, igual que `_verify_decision_token`.
_INSECURE_SECRETS = {"change-me", "changeme"}
# Usuario que se asume cuando no hay secreto ni cabecera (dev/test).
_DEV_USER = "dev"


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
    coincidir **exactamente** con el secreto, que a su vez es la sesion
    `<usuario>:<rol>`: el rol lo fija el despliegue, no quien llama, para que un
    `lector` no pueda reclamar ser `operador`.
    """
    secret = settings.panel_session_secret.strip()
    relaxed = not secret or secret in _INSECURE_SECRETS
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
    session = parse_session(secret)
    if session is None:
        logger.error("PANEL_SESSION_SECRET no tiene la forma <usuario>:<lector|operador>")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sesion del panel mal configurada")
    return session


def require_session(x_panel_session: str | None = Header(default=None)) -> Session:
    """Dependencia FastAPI: exige y valida la sesion del panel."""
    return verify_session(get_settings(), x_panel_session)


def require_operador(session: Annotated[Session, Depends(require_session)]) -> Session:
    """Dependencia FastAPI de los comandos: rechaza al rol `lector` con `403`."""
    if not session.can_command():
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"El rol {session.role.value} no puede ejecutar comandos",
        )
    return session
