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
