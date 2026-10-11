from __future__ import annotations

import pytest

from agent_review.adapters.text_utils import guess_protected_subject, slug
from agent_review.domain.models import ProtectedSubject


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("La consulta SQL se construye por concatenacion", ProtectedSubject.INJECTION),
        ("Falta validar el token de sesion", ProtectedSubject.SECRETS_EXPOSURE),
        ("La contrasena se guarda en claro (password)", ProtectedSubject.SECRETS_EXPOSURE),
        ("Hay una race condition al incrementar el contador", ProtectedSubject.CONCURRENCY),
        ("Se puede producir un deadlock entre dos hilos", ProtectedSubject.CONCURRENCY),
        ("Uso de md5 para firmar el mensaje", ProtectedSubject.CRYPTOGRAPHY),
        ("Se aplica un drop table en la migracion", ProtectedSubject.DATA_LOSS),
        (
            "Esta autenticacion omite la comprobacion de permisos",
            ProtectedSubject.AUTHORIZATION_AUTHENTICATION,
        ),
        ("Cambio incompatible en la public api", ProtectedSubject.PUBLIC_CONTRACT),
        ("Posible buffer overflow al copiar", ProtectedSubject.MEMORY_SAFETY),
    ],
)
def test_c4_infiere_los_sujetos_del_contrato(text: str, expected: ProtectedSubject) -> None:
    assert guess_protected_subject(text) is expected


def test_texto_neutro_no_marca_tema() -> None:
    assert guess_protected_subject("Mejorar el nombre de la variable") is None


def test_slug_es_estable_y_corto() -> None:
    assert slug("Cannot call method withdraw()!") == "cannot-call-method-withdraw"
    assert len(slug("x" * 100)) <= 40
