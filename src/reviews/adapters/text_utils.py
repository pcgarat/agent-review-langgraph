from __future__ import annotations

import re

from reviews.domain.models import ProtectedSubject

# Pistas que marcan temas sensibles: nunca se arreglan solos.
# Compartido por las personas (estatica y LLM) para que la clasificacion de
# subject sea una sola fuente de verdad.
_PROTECTED_HINTS = {
    "auth": ProtectedSubject.AUTHORIZATION_AUTHENTICATION,
    "password": ProtectedSubject.SECRETS_EXPOSURE,
    "secret": ProtectedSubject.SECRETS_EXPOSURE,
    "token": ProtectedSubject.SECRETS_EXPOSURE,
    "sql": ProtectedSubject.INJECTION,
    "eval": ProtectedSubject.INJECTION,
}


def slug(value: str) -> str:
    """Identificador corto y estable a partir de texto libre."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:40]


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
