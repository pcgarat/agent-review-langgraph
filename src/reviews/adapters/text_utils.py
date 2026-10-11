from __future__ import annotations

import re

from reviews.domain.models import ProtectedSubject

# Pistas que marcan temas sensibles: nunca se arreglan solos.
# Compartido por las personas (estatica y LLM) para que la clasificacion de
# subject sea una sola fuente de verdad.
# La lista es conservadora a proposito: en Fase 1 todo es `report`, asi que
# marcar de mas no rompe nada; en Fase 3 decide el autofix. C4: se amplia para
# cubrir los sujetos del contrato que antes no tenian ninguna pista.
_PROTECTED_HINTS: dict[str, ProtectedSubject] = {
    # Autenticacion / autorizacion
    "auth": ProtectedSubject.AUTHORIZATION_AUTHENTICATION,
    "autentic": ProtectedSubject.AUTHORIZATION_AUTHENTICATION,
    "authoriz": ProtectedSubject.AUTHORIZATION_AUTHENTICATION,
    "authenticat": ProtectedSubject.AUTHORIZATION_AUTHENTICATION,
    "permission": ProtectedSubject.AUTHORIZATION_AUTHENTICATION,
    "permis": ProtectedSubject.AUTHORIZATION_AUTHENTICATION,
    "privilege": ProtectedSubject.AUTHORIZATION_AUTHENTICATION,
    "access control": ProtectedSubject.AUTHORIZATION_AUTHENTICATION,
    "jwt": ProtectedSubject.AUTHORIZATION_AUTHENTICATION,
    "oauth": ProtectedSubject.AUTHORIZATION_AUTHENTICATION,
    # Exposicion de secretos
    "password": ProtectedSubject.SECRETS_EXPOSURE,
    "passwd": ProtectedSubject.SECRETS_EXPOSURE,
    "secret": ProtectedSubject.SECRETS_EXPOSURE,
    "token": ProtectedSubject.SECRETS_EXPOSURE,
    "api key": ProtectedSubject.SECRETS_EXPOSURE,
    "api-key": ProtectedSubject.SECRETS_EXPOSURE,
    "private key": ProtectedSubject.SECRETS_EXPOSURE,
    "credential": ProtectedSubject.SECRETS_EXPOSURE,
    # Inyeccion
    "sql": ProtectedSubject.INJECTION,
    "injection": ProtectedSubject.INJECTION,
    "eval": ProtectedSubject.INJECTION,
    "exec(": ProtectedSubject.INJECTION,
    "shell": ProtectedSubject.INJECTION,
    "deserial": ProtectedSubject.INJECTION,
    "unserialize": ProtectedSubject.INJECTION,
    "xss": ProtectedSubject.INJECTION,
    "sanitiz": ProtectedSubject.INJECTION,
    # Memoria
    "memory": ProtectedSubject.MEMORY_SAFETY,
    "buffer": ProtectedSubject.MEMORY_SAFETY,
    "overflow": ProtectedSubject.MEMORY_SAFETY,
    "use-after-free": ProtectedSubject.MEMORY_SAFETY,
    "null pointer": ProtectedSubject.MEMORY_SAFETY,
    "segfault": ProtectedSubject.MEMORY_SAFETY,
    # Concurrencia
    "concurren": ProtectedSubject.CONCURRENCY,
    "race condition": ProtectedSubject.CONCURRENCY,
    "deadlock": ProtectedSubject.CONCURRENCY,
    "thread": ProtectedSubject.CONCURRENCY,
    "mutex": ProtectedSubject.CONCURRENCY,
    "lock": ProtectedSubject.CONCURRENCY,
    "atomic": ProtectedSubject.CONCURRENCY,
    # Perdida de datos
    "data loss": ProtectedSubject.DATA_LOSS,
    "data-loss": ProtectedSubject.DATA_LOSS,
    "delete": ProtectedSubject.DATA_LOSS,
    "drop table": ProtectedSubject.DATA_LOSS,
    "migration": ProtectedSubject.DATA_LOSS,
    "overwrite": ProtectedSubject.DATA_LOSS,
    # Criptografia
    "crypt": ProtectedSubject.CRYPTOGRAPHY,
    "encrypt": ProtectedSubject.CRYPTOGRAPHY,
    "decrypt": ProtectedSubject.CRYPTOGRAPHY,
    "hash": ProtectedSubject.CRYPTOGRAPHY,
    "cipher": ProtectedSubject.CRYPTOGRAPHY,
    "md5": ProtectedSubject.CRYPTOGRAPHY,
    "sha1": ProtectedSubject.CRYPTOGRAPHY,
    # Contrato publico
    "public api": ProtectedSubject.PUBLIC_CONTRACT,
    "public-api": ProtectedSubject.PUBLIC_CONTRACT,
    "breaking change": ProtectedSubject.PUBLIC_CONTRACT,
    "backward": ProtectedSubject.PUBLIC_CONTRACT,
    "backwards compat": ProtectedSubject.PUBLIC_CONTRACT,
    "interface change": ProtectedSubject.PUBLIC_CONTRACT,
    "deprecat": ProtectedSubject.PUBLIC_CONTRACT,
}


def slug(value: str) -> str:
    """Identificador corto y estable a partir de texto libre."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:40]


def shorten_title(text: str, limit: int = 100) -> str:
    """Recorta a `limit` sin partir una palabra y sin puntos suspensivos.

    El contrato fija `title` a 100 caracteres; un corte duro dejaba cabos a media
    palabra ("...on every se"), que se leen como un error. Se normaliza el espaciado
    y se corta en la ultima frontera de palabra que quepa.
    """
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    cut = clean[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.…-")
    return cut or clean[:limit].rstrip()


def guess_protected_subject(text: str) -> ProtectedSubject | None:
    """Infere un tema protegido a partir de las pistas del texto.

    Es un ultimo recurso: la persona puede declarar el subject explicitamente y
    entonces no se infiere. Mejor pecar de conservador (marcar) que dejar pasar.
    """
    lowered = text.lower()
    for hint, subject in _PROTECTED_HINTS.items():
        if hint in lowered:
            return subject
    return None
