from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reviews.api import main
from reviews.api.main import app
from reviews.domain.pending import InMemoryPendingStore
from reviews.idempotency import InMemoryIdempotencyStore
from reviews.queue import InMemoryReviewQueue

client = TestClient(app)


@pytest.fixture(autouse=True)
def _artifacts_in_tmp(tmp_path: Path, monkeypatch) -> None:
    """Los artefactos se escriben en un directorio temporal, no en el repo."""
    monkeypatch.setattr(main._settings(), "tooling_output_root", str(tmp_path))


def _reset_fallbacks(expected_tools: list[str] | None = None) -> None:
    main._fallback_store = InMemoryIdempotencyStore()
    main._fallback_queue = InMemoryReviewQueue()
    main._fallback_pending = InMemoryPendingStore(expected_tools=expected_tools or ["phpstan"])


def _payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "repo": "cqm/emotivoo-v2",
        "pr_id": 42,
        "head_sha": "abc123",
        "pipeline_id": "7",
        "tool": "phpstan",
        "payload": '{"files": {}}',
    }
    body.update(overrides)
    return body


def test_healthz() -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_tooling_encola_cuando_la_barrera_se_completa() -> None:
    _reset_fallbacks()
    response = client.post("/webhook/tooling", json=_payload())
    assert response.status_code == 202
    assert response.json()["status"] == "aceptado"
    assert response.json()["key"] == "cqm/emotivoo-v2:42:abc123:7"
    assert main._fallback_queue is not None
    assert len(main._fallback_queue) == 1


def test_tooling_idempotente_por_artefacto_y_por_review() -> None:
    _reset_fallbacks()
    first = client.post("/webhook/tooling", json=_payload())
    assert first.status_code == 202
    assert first.json()["status"] == "aceptado"

    # Mismo artefacto otra vez: el evento de ingesta es duplicado, no se reencola.
    second = client.post("/webhook/tooling", json=_payload())
    assert second.status_code == 200
    assert second.json()["status"] == "duplicado"
    assert main._fallback_queue is not None
    assert len(main._fallback_queue) == 1


def test_tooling_barrera_de_dos_espera_al_segundo() -> None:
    _reset_fallbacks(expected_tools=["phpstan", "php-cs-fixer"])
    first = client.post("/webhook/tooling", json=_payload())
    assert first.status_code == 202
    assert first.json()["status"] == "pendiente"
    assert main._fallback_queue is not None
    assert len(main._fallback_queue) == 0

    second = client.post(
        "/webhook/tooling", json=_payload(tool="php-cs-fixer", payload='{"files": {}}')
    )
    assert second.status_code == 202
    assert second.json()["status"] == "aceptado"
    assert len(main._fallback_queue) == 1


def test_tooling_rechaza_payload_sin_campos() -> None:
    _reset_fallbacks()
    assert client.post("/webhook/tooling", json={"tool": "phpstan"}).status_code == 400
    assert client.post("/webhook/tooling", json=_payload(payload="   ")).status_code == 400
    assert client.post("/webhook/tooling", json=_payload(tool="")).status_code == 400


def test_tooling_firma_hmac_valida(monkeypatch) -> None:
    _reset_fallbacks()
    secret = "secreto"
    monkeypatch.setattr(main._settings(), "tooling_webhook_secret", secret)

    body = (
        b'{"repo": "cqm/emotivoo-v2", "pr_id": 1, "head_sha": "h", "tool": "phpstan", '
        b'"payload": "{}"}'
    )
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    ok = client.post(
        "/webhook/tooling",
        content=body,
        headers={"content-type": "application/json", "x-reviews-signature": signature},
    )
    assert ok.status_code == 202

    bad = client.post(
        "/webhook/tooling",
        content=body,
        headers={"content-type": "application/json", "x-reviews-signature": "malo"},
    )
    assert bad.status_code == 401


def test_tooling_firma_ausente_se_rechaza(monkeypatch) -> None:
    monkeypatch.setattr(main._settings(), "tooling_webhook_secret", "secreto")
    body = b'{"repo": "r", "pr_id": 1, "head_sha": "h", "tool": "phpstan", "payload": "{}"}'
    response = client.post(
        "/webhook/tooling",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 401


def test_webhook_de_pr_retirado() -> None:
    # La ingesta diferida sustituye al disparo por PR: el endpoint ya no existe.
    response = client.post("/webhook/bitbucket", json={"pullrequest": {"id": 1}})
    assert response.status_code == 404


def test_tooling_rechaza_payload_demasiado_grande(monkeypatch) -> None:
    _reset_fallbacks()
    monkeypatch.setattr(main._settings(), "max_tooling_payload_bytes", 16)
    response = client.post("/webhook/tooling", json=_payload())
    assert response.status_code == 413


def test_arranque_en_produccion_con_secreto_de_ejemplo_falla(monkeypatch) -> None:
    from reviews.settings import require_production_secrets

    settings = main._settings()
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "tooling_webhook_secret", "change-me")
    with pytest.raises(RuntimeError, match="TOOLING_WEBHOOK_SECRET"):
        require_production_secrets(settings)


def test_arranque_en_produccion_con_secreto_real_pasa(monkeypatch) -> None:
    from reviews.settings import require_production_secrets

    settings = main._settings()
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "tooling_webhook_secret", "secreto-real")
    monkeypatch.setattr(settings, "bitbucket_webhook_secret", "secreto-real")
    require_production_secrets(settings)
