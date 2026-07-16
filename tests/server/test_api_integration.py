"""API integration tests for the baoku clone server.

Tests run against the FastAPI TestClient with an in-memory SQLite database.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from notebooklm.server.database import init_db
from notebooklm.server.server import create_app

TEST_DB = "sqlite:///:memory:"


@pytest.fixture(autouse=True)
def _test_db(monkeypatch: pytest.MonkeyPatch) -> None:
    import notebooklm.server.database as db_mod

    monkeypatch.setenv("BAOKU_DATABASE_URL", TEST_DB)
    init_db(TEST_DB)
    yield
    from sqlalchemy import text

    with db_mod.engine.connect() as conn:
        for table in reversed(db_mod.Base.metadata.sorted_tables):
            conn.execute(text(f"DROP TABLE IF EXISTS {table.name}"))
        conn.commit()


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def token(client: TestClient) -> str:
    resp = client.post(
        "/api/auth/register",
        json={"username": "testuser", "password": "testpass123", "display_name": "Test"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    return data["access_token"]


@pytest.fixture
def authed(client: TestClient, token: str) -> TestClient:
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


class TestAuth:
    def test_register_and_login(self, client: TestClient) -> None:
        resp = client.post(
            "/api/auth/register",
            json={"username": "newuser", "password": "SecurePass1", "display_name": "New"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data

        resp2 = client.post(
            "/api/auth/login",
            json={"username": "newuser", "password": "SecurePass1"},
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert "access_token" in data2

    def test_login_wrong_password(self, client: TestClient) -> None:
        client.post(
            "/api/auth/register",
            json={"username": "u1", "password": "pass1"},
        )
        resp = client.post(
            "/api/auth/login",
            json={"username": "u1", "password": "wrong"},
        )
        assert resp.status_code == 401

    def test_me_endpoint(self, client: TestClient, token: str) -> None:
        resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "testuser"
        assert data["display_name"] == "Test"


class TestGeneration:
    def test_list_templates(self, authed: TestClient) -> None:
        resp = authed.get("/api/generation/templates?content_type=document")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0
        assert data[0]["content_type"] == "document"

    def test_generate_returns_400_when_missing_fields(self, authed: TestClient) -> None:
        resp = authed.post("/api/generation/generate", json={})
        assert resp.status_code == 400

    def test_generate_returns_500_for_invalid_content_type(self, authed: TestClient) -> None:
        resp = authed.post(
            "/api/generation/generate",
            json={"notebook_id": 1, "content_type": "invalid_type", "prompt": "test"},
        )
        assert resp.status_code == 500


class TestExternalKB:
    def test_create_connection(self, authed: TestClient) -> None:
        resp = authed.post(
            "/api/external-kb/connections",
            json={
                "name": "Test KB",
                "provider_type": "openapi",
                "api_base_url": "https://example.com/api",
                "auth_type": "api_key",
            },
        )
        assert resp.status_code == 200
        conn = resp.json()
        assert conn["name"] == "Test KB"

    def test_list_connections(self, authed: TestClient) -> None:
        authed.post(
            "/api/external-kb/connections",
            json={"name": "KB 1", "provider_type": "openapi", "api_base_url": "https://ex.com/api"},
        )
        resp = authed.get("/api/external-kb/connections")
        assert resp.status_code == 200
        assert len(resp.json()) == 1


class TestAuthMiddleware:
    def test_no_token_returns_401(self, client: TestClient) -> None:
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401

    def test_bad_token_returns_401(self, client: TestClient) -> None:
        resp = client.get("/api/auth/me", headers={"Authorization": "Bearer invalidtoken"})
        assert resp.status_code == 401


class TestCORS:
    def test_cors_headers_present_in_dev(self) -> None:
        os.environ["BAOKU_DEV"] = "1"
        try:
            app = create_app()
            with TestClient(app) as c:
                resp = c.options(
                    "/api/health",
                    headers={
                        "Origin": "http://localhost:5173",
                        "Access-Control-Request-Method": "GET",
                    },
                )
                assert resp.status_code == 200
                assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"
        finally:
            os.environ.pop("BAOKU_DEV", None)

    def test_no_cors_in_production(self) -> None:
        os.environ.pop("BAOKU_DEV", None)
        app = create_app()
        with TestClient(app) as c:
            resp = c.options(
                "/api/health",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": "GET",
                },
            )
            cors = resp.headers.get("access-control-allow-origin")
            assert cors is None or cors == ""


class TestHealth:
    def test_health_endpoint(self, client: TestClient) -> None:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
