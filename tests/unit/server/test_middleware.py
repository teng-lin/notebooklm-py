from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from notebooklm.server.database import close_db, get_session, init_db
from notebooklm.server.models import RequestLog
from notebooklm.server.routes.middleware import RequestLogMiddleware


class _TestApp:
    """A minimal FastAPI app to test the middleware."""

    def __init__(self, db_url: str):
        init_db(db_url)
        self.app = FastAPI()

        @self.app.get("/test-get")
        async def test_get():
            return {"msg": "ok"}

        @self.app.post("/test-post")
        async def test_post(body: dict[str, Any]):
            return {"received": body}

        self.app.add_middleware(RequestLogMiddleware)

    def close(self):
        close_db()


@pytest.fixture
def test_app():
    tmp = tempfile.mktemp(suffix=".db")
    ta = _TestApp(f"sqlite:///{tmp}")
    yield ta
    ta.close()
    Path(tmp).unlink(missing_ok=True)


class TestRequestLogMiddleware:
    def test_get_request_is_logged(self, test_app: _TestApp):
        client = TestClient(test_app.app)
        resp = client.get("/test-get")
        assert resp.status_code == 200
        db = get_session()
        logs = db.query(RequestLog).all()
        assert len(logs) == 1
        assert logs[0].method == "GET"
        assert logs[0].endpoint == "/test-get"
        assert logs[0].response_status == 200
        assert logs[0].latency_ms is not None

    def test_post_request_is_logged(self, test_app: _TestApp):
        client = TestClient(test_app.app)
        resp = client.post("/test-post", json={"key": "value"})
        assert resp.status_code == 200
        db = get_session()
        logs = db.query(RequestLog).all()
        assert len(logs) == 1
        assert logs[0].method == "POST"
        assert logs[0].endpoint == "/test-post"
        assert logs[0].response_status == 200

    def test_request_body_is_captured(self, test_app: _TestApp):
        client = TestClient(test_app.app)
        resp = client.post("/test-post", json={"hello": "world"})
        assert resp.status_code == 200
        db = get_session()
        logs = db.query(RequestLog).all()
        assert len(logs) == 1
        body = logs[0].request_body
        assert body is not None
        assert "hello" in body

    def test_latency_is_recorded(self, test_app: _TestApp):
        client = TestClient(test_app.app)
        client.get("/test-get")
        db = get_session()
        logs = db.query(RequestLog).all()
        assert logs[0].latency_ms is not None
        assert logs[0].latency_ms >= 0

    def test_client_ip_is_captured(self, test_app: _TestApp):
        client = TestClient(test_app.app, client=("192.168.1.1", 12345))
        client.get("/test-get")
        db = get_session()
        logs = db.query(RequestLog).all()
        assert len(logs) == 1
        assert logs[0].client_ip == "192.168.1.1"

    def test_user_agent_is_captured(self, test_app: _TestApp):
        client = TestClient(test_app.app)
        client.get("/test-get", headers={"User-Agent": "TestAgent/1.0"})
        db = get_session()
        logs = db.query(RequestLog).all()
        assert len(logs) == 1
        assert logs[0].user_agent == "TestAgent/1.0"
