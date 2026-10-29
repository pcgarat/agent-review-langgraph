"""Fichero de sesión de Claude (025 S3).

Lo escribe el panel y lo lee `claude-runner`. El worker no abre este fichero:
pregunta `GET /session`, que responde sí o no. El token no entra en Redis ni en
el aviso.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Lo inyecta el runner en el hijo, a propósito. No está en la lista de
# herencia: el proceso padre no lo tiene en el entorno.
CHILD_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"


def write_session(path: Path, token: str, *, ttl_s: float, now: float | None = None) -> None:
    """Guarda el token con modo `0600`. No lo registra."""
    if ttl_s <= 0:
        raise ValueError("la caducidad de la sesion tiene que ser positiva")
    path.parent.mkdir(parents=True, exist_ok=True)
    moment = time.time() if now is None else now
    payload = json.dumps({"token": token, "expires_at": moment + ttl_s})
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode())
    finally:
        os.close(fd)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def session_active(path: Path, *, now: float | None = None) -> bool:
    """Sí si el fichero sigue vigente. Si caducó, lo retira."""
    data = _read(path)
    if data is None:
        return False
    moment = time.time() if now is None else now
    token = data.get("token")
    expires = data.get("expires_at")
    if (
        not isinstance(token, str)
        or not token.strip()
        or isinstance(expires, bool)
        or not isinstance(expires, (int, float))
        or float(expires) <= moment
    ):
        _unlink(path)
        return False
    return True


def load_token(path: Path, *, now: float | None = None) -> str | None:
    """Token vigente para el subproceso, o `None` si no hay sesión."""
    if not session_active(path, now=now):
        return None
    data = _read(path)
    if data is None:
        return None
    token = data.get("token")
    if not isinstance(token, str) or not token.strip():
        return None
    return token


def _read(path: Path) -> dict[str, object] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("No se pudo leer la sesion de Claude")
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        _unlink(path)
        return None
    if not isinstance(data, dict):
        _unlink(path)
        return None
    return data


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        logger.warning("No se pudo retirar la sesion de Claude caducada")
