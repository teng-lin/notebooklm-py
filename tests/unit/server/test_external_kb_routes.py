from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from notebooklm.server.database import close_db, get_session, init_db
from notebooklm.server.models import ExternalKBConnection, User


@pytest.fixture(autouse=True)
def _db():
    tmp = tempfile.mktemp(suffix=".db")
    os.environ["NOTEBOOKLM_DATABASE_URL"] = f"sqlite:///{tmp}"
    init_db(f"sqlite:///{tmp}")
    yield
    close_db()
    Path(tmp).unlink(missing_ok=True)


def _make_user() -> User:
    from notebooklm.server.auth_deps import hash_password
    db = get_session()
    u = User(username="testuser", password_hash=hash_password("pass"), display_name="Test")
    db.add(u)
    db.commit()
    db.refresh(u)
    db.close()
    return u


class TestExternalKbRoutes:

    def test_create_and_list_connection(self) -> None:
        user = _make_user()
        from notebooklm.server.routes.external_kb import create_connection, list_connections
        db = get_session()
        resp = create_connection(
            {
                "name": "Test KB",
                "provider_type": "openapi",
                "api_base_url": "https://example.com/api",
                "auth_type": "api_key",
                "auth_credentials": {"api_key": "test-key"},
            },
            user,
            db,
        )
        assert resp["name"] == "Test KB"
        assert resp["provider_type"] == "openapi"

        items = list_connections(user, db)
        assert len(items) == 1
        assert items[0]["name"] == "Test KB"

    def test_create_connection_encrypts_credentials(self) -> None:
        user = _make_user()
        from notebooklm.server.routes.external_kb import create_connection
        db = get_session()
        create_connection(
            {
                "name": "Secure KB",
                "provider_type": "dify",
                "api_base_url": "https://dify.example.com",
                "auth_type": "api_key",
                "auth_credentials": {"api_key": "super-secret-key"},
            },
            user,
            db,
        )
        conn = db.query(ExternalKBConnection).first()
        assert conn is not None
        assert conn.auth_credentials != '{"api_key": "super-secret-key"}'
        assert "super-secret-key" not in conn.auth_credentials
        db.close()
