from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from notebooklm.server.app import create_app
from notebooklm.server.database import close_db, init_db


@pytest.fixture
def client():
    tmp = tempfile.mktemp(suffix=".db")
    os.environ["NOTEBOOKLM_DATABASE_URL"] = f"sqlite:///{tmp}"
    init_db(f"sqlite:///{tmp}")
    from notebooklm.server.database import get_session
    from notebooklm.server.models import User
    from notebooklm.server.auth_deps import hash_password
    db = get_session()
    u = User(username="testuser", password_hash=hash_password("pass"), display_name="Test")
    db.add(u)
    db.commit()
    db.close()
    app = create_app()
    yield TestClient(app)
    close_db()
    Path(tmp).unlink(missing_ok=True)


def _get_token() -> str:
    from notebooklm.server.auth_deps import create_access_token
    return create_access_token(1, "testuser")


class TestGenerationRoutes:

    def test_generate_missing_auth(self, client) -> None:
        resp = client.post("/api/generation/generate", json={
            "content_type": "document",
            "notebook_id": "nb-1",
            "prompt": "Write a summary",
        })
        assert resp.status_code in (401, 403)

    def test_list_missing_auth(self, client) -> None:
        resp = client.get("/api/generation/list")
        assert resp.status_code in (401, 403)

    def test_templates_public_or_authed(self, client) -> None:
        resp = client.get("/api/generation/templates")
        assert resp.status_code in (200, 401)

    def test_generate_document(self, client) -> None:
        token = _get_token()
        headers = {"Authorization": f"Bearer {token}"}
        resp = client.post(
            "/api/generation/generate",
            json={
                "content_type": "document",
                "notebook_id": "1",
                "prompt": "Write a summary",
                "template": "summary",
            },
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["content_type"] == "document"
        assert data["status"] == "completed"

    def test_generate_without_notebook_id(self, client) -> None:
        token = _get_token()
        headers = {"Authorization": f"Bearer {token}"}
        resp = client.post(
            "/api/generation/generate",
            json={"content_type": "document", "prompt": "test"},
            headers=headers,
        )
        assert resp.status_code == 400

    def test_list_returns_results(self, client) -> None:
        token = _get_token()
        headers = {"Authorization": f"Bearer {token}"}
        client.post(
            "/api/generation/generate",
            json={"content_type": "document", "notebook_id": "1", "prompt": "test"},
            headers=headers,
        )
        resp = client.get("/api/generation/list", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1

    def test_get_generated_content(self, client) -> None:
        token = _get_token()
        headers = {"Authorization": f"Bearer {token}"}
        create_resp = client.post(
            "/api/generation/generate",
            json={"content_type": "document", "notebook_id": "1", "prompt": "test"},
            headers=headers,
        )
        content_id = create_resp.json()["id"]
        resp = client.get(f"/api/generation/{content_id}", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["id"] == content_id

    def test_delete_generated_content(self, client) -> None:
        token = _get_token()
        headers = {"Authorization": f"Bearer {token}"}
        create_resp = client.post(
            "/api/generation/generate",
            json={"content_type": "document", "notebook_id": "1", "prompt": "test"},
            headers=headers,
        )
        content_id = create_resp.json()["id"]
        resp = client.delete(f"/api/generation/{content_id}", headers=headers)
        assert resp.status_code == 204

    def test_list_templates(self, client) -> None:
        resp = client.get("/api/generation/templates")
        if resp.status_code == 200:
            data = resp.json()
            assert len(data) >= 1

    def test_regenerate(self, client) -> None:
        token = _get_token()
        headers = {"Authorization": f"Bearer {token}"}
        create_resp = client.post(
            "/api/generation/generate",
            json={"content_type": "document", "notebook_id": "1", "prompt": "first"},
            headers=headers,
        )
        content_id = create_resp.json()["id"]
        resp = client.post(
            f"/api/generation/{content_id}/regenerate",
            json={"prompt": "regenerated"},
            headers=headers,
        )
        assert resp.status_code == 200
