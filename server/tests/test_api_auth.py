"""The bearer token, and what stays open without one."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from jobstitch_server.api.app import create_app


@pytest.fixture
def secured_client(app_state):
    app_state.settings = replace(app_state.settings, api_token="s3cret")
    with TestClient(create_app(state=app_state)) as client:
        yield client


def test_without_a_token_configured_the_api_is_open(client):
    assert client.post("/v1/jd/detect", data={"jd_text": "short"}).status_code == 200


def test_a_configured_token_is_required(secured_client):
    response = secured_client.post("/v1/jd/detect", data={"jd_text": "short"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["type"] == "unauthorized"


def test_the_wrong_token_is_refused(secured_client):
    response = secured_client.post(
        "/v1/jd/detect", data={"jd_text": "short"}, headers={"Authorization": "Bearer wrong"}
    )
    assert response.status_code == 401


def test_the_right_token_gets_through(secured_client):
    response = secured_client.post(
        "/v1/jd/detect", data={"jd_text": "short"}, headers={"Authorization": "Bearer s3cret"}
    )
    assert response.status_code == 200


def test_health_stays_open_so_a_platform_probe_works(secured_client):
    body = secured_client.get("/healthz").json()
    assert body["data"]["auth_required"] is True
    assert body["data"]["models"]["cv"] == "fake-cv"


def test_every_response_carries_its_request_id(client):
    response = client.get("/healthz", headers={"X-Request-Id": "my-own-id"})
    assert response.headers["x-request-id"] == "my-own-id"
    assert response.json()["request_id"] == "my-own-id"


def test_an_id_is_generated_when_the_client_sends_none(client):
    response = client.get("/healthz")
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert len(response.json()["request_id"]) == 12


def test_a_provider_failure_is_a_gateway_error_not_a_bug(client, fake_models, monkeypatch):
    """The server is fine; the thing behind it is not. That is a 502, and the
    provider's own message never reaches the caller."""
    from openai import APIConnectionError
    import httpx

    def explode(**kwargs):
        raise APIConnectionError(request=httpx.Request("POST", "http://provider.invalid"))

    monkeypatch.setattr(fake_models["summary"].llm.chat.completions, "create", explode)

    response = client.post("/v1/jd/detect", data={"jd_text": "Job posting. " * 120})

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["type"] == "provider_unreachable"
    assert "provider.invalid" not in response.text
