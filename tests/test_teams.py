from __future__ import annotations

import httpx
import pytest

from reviews.adapters.teams_webhook import TeamsAdaptiveCardPayload, TeamsWebhookAdapter


def test_tarjeta_incluye_accion_al_pr() -> None:
    payload = TeamsAdaptiveCardPayload(
        title="Review", body="2 hallazgos", link="https://pr/1"
    ).as_dict()
    assert payload["@type"] == "MessageCard"
    assert payload["potentialAction"]


def test_webhook_sin_url_no_falla() -> None:
    TeamsWebhookAdapter(webhook_url="").notify(title="t", body="b")


def test_webhook_propaga_error_http(monkeypatch) -> None:
    adapter = TeamsWebhookAdapter(webhook_url="https://teams.test/hook")

    def boom(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise httpx.HTTPError("caido")

    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(httpx.HTTPError):
        adapter.notify(title="t", body="b")


def test_webhook_envia_messagecard_al_endpoint(monkeypatch) -> None:
    adapter = TeamsWebhookAdapter(webhook_url="https://teams.test/hook")
    captured: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

    def fake_post(url, *, json, timeout):  # noqa: ANN001
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(httpx, "post", fake_post)
    adapter.notify(title="Review", body="2 hallazgos", link="https://pr/1")

    assert captured["url"] == "https://teams.test/hook"
    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["@type"] == "MessageCard"
    assert payload["summary"] == "Review"
    assert payload["sections"] == [{"text": "2 hallazgos"}]
    assert payload["potentialAction"]


def test_webhook_no_hace_http_sin_url(monkeypatch) -> None:
    calls: list[object] = []

    def fake_post(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append(args)
        raise AssertionError("no debe llamarse sin webhook")

    monkeypatch.setattr(httpx, "post", fake_post)
    TeamsWebhookAdapter(webhook_url="").notify(title="t", body="b")
    assert calls == []


def test_notify_decision_pending_anade_prefijo(monkeypatch) -> None:
    adapter = TeamsWebhookAdapter(webhook_url="https://teams.test/hook")
    captured: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

    def fake_post(url, *, json, timeout):  # noqa: ANN001
        captured["json"] = json
        return Response()

    monkeypatch.setattr(httpx, "post", fake_post)
    adapter.notify_decision_pending(title="Review", body="1 pendiente")
    payload = captured["json"]
    assert isinstance(payload, dict)
    assert str(payload["title"]).startswith("Decision pendiente")
