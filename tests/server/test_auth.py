"""U2: bearer-token + loopback-Host auth on the /v1 router."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from .conftest import TEST_TOKEN


def test_missing_authorization_is_401(raw_client: TestClient) -> None:
    resp = raw_client.get("/v1/notebooks", headers={"Host": "127.0.0.1"})
    assert resp.status_code == 401


def test_wrong_token_is_401(raw_client: TestClient) -> None:
    resp = raw_client.get(
        "/v1/notebooks",
        headers={"Authorization": "Bearer nope", "Host": "127.0.0.1"},
    )
    assert resp.status_code == 401


def test_correct_token_runs_handler(authed_client: TestClient) -> None:
    resp = authed_client.get("/v1/notebooks")
    assert resp.status_code == 200
    assert resp.json() == {"notebooks": []}


def test_off_loopback_host_is_403(raw_client: TestClient) -> None:
    resp = raw_client.get(
        "/v1/notebooks",
        headers={"Authorization": f"Bearer {TEST_TOKEN}", "Host": "evil.com"},
    )
    assert resp.status_code == 403


def test_loopback_host_with_port_is_accepted(raw_client: TestClient) -> None:
    resp = raw_client.get(
        "/v1/notebooks",
        headers={"Authorization": f"Bearer {TEST_TOKEN}", "Host": "127.0.0.1:8000"},
    )
    assert resp.status_code == 200


def test_healthz_needs_no_token(raw_client: TestClient) -> None:
    assert raw_client.get("/healthz").status_code == 200


def test_token_never_appears_in_logs(
    authed_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        authed_client.get("/v1/notebooks")
    assert TEST_TOKEN not in caplog.text
