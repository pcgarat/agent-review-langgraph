from __future__ import annotations

import hashlib
import hmac

from fastapi.testclient import TestClient

from reviews.api import main
from reviews.api.main import app

client = TestClient(app)


def test_healthz() -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_webhook_acepta_y_es_idempotente() -> None:
    payload = {
        "pullrequest": {
            "id": 42,
            "source": {"commit": {"hash": "abc123"}},
            "destination": {"repository": {"full_name": "cqm/emotivoo-v2"}},
        }
    }
    first = client.post("/webhook/bitbucket", json=payload)
    assert first.status_code == 202
    assert first.json()["key"] == "cqm/emotivoo-v2:42:abc123"

    second = client.post("/webhook/bitbucket", json=payload)
    assert second.status_code == 200
    assert second.json()["status"] == "duplicado"


def test_webhook_rechaza_sin_pr_id() -> None:
    response = client.post("/webhook/bitbucket", json={"pullrequest": {}})
    assert response.status_code == 400


def test_firma_hmac_valida(monkeypatch) -> None:
    secret = "secreto"
    monkeypatch.setattr(main._settings(), "bitbucket_webhook_secret", secret)

    body = (
        b'{"pullrequest": {"id": 1, "source": {"commit": {"hash": "h"}}, '
        b'"destination": {"repository": {"full_name": "r"}}}}'
    )
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    ok = client.post(
        "/webhook/bitbucket",
        content=body,
        headers={"content-type": "application/json", "x-hub-signature": signature},
    )
    assert ok.status_code == 202

    bad = client.post(
        "/webhook/bitbucket",
        content=body,
        headers={"content-type": "application/json", "x-hub-signature": "malo"},
    )
    assert bad.status_code == 401


def test_firma_ausente_se_rechaza(monkeypatch) -> None:
    monkeypatch.setattr(main._settings(), "bitbucket_webhook_secret", "secreto")
    body = b'{"pullrequest": {"id": 9, "source": {"commit": {"hash": "z"}}}}'
    response = client.post(
        "/webhook/bitbucket",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 401
