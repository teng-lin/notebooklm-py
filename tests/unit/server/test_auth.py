from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException
from jose import jwt

from notebooklm.server.auth_deps import (
    SECRET_KEY,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from notebooklm.server.database import close_db, get_session, init_db
from notebooklm.server.models import User


@pytest.fixture(autouse=True)
def _db():
    tmp = tempfile.mktemp(suffix=".db")
    init_db(f"sqlite:///{tmp}")
    yield
    close_db()
    Path(tmp).unlink(missing_ok=True)


class TestPasswordHashing:
    def test_hash_and_verify(self):
        h = hash_password("mysecret")
        assert h != "mysecret"
        assert verify_password("mysecret", h) is True
        assert verify_password("wrong", h) is False

    def test_hash_is_different_each_time(self):
        h1 = hash_password("same")
        h2 = hash_password("same")
        assert h1 != h2


class TestJWTToken:
    def test_create_and_decode_access_token(self):
        token = create_access_token(1, "alice")
        payload = decode_token(token)
        assert payload["sub"] == "1"
        assert payload["username"] == "alice"
        assert payload["type"] == "access"

    def test_create_and_decode_refresh_token(self):
        token = create_refresh_token(1)
        payload = decode_token(token)
        assert payload["sub"] == "1"
        assert payload["type"] == "refresh"

    def test_expired_token_raises(self):
        import time
        payload = {
            "sub": "1",
            "type": "access",
            "exp": int(time.time()) - 10,
            "iat": int(time.time()) - 3600,
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
        with pytest.raises(HTTPException) as exc:
            decode_token(token)
        assert exc.value.status_code == 401


class TestAuthFlow:
    def test_register_user(self):
        from notebooklm.server.database import init_db as _init
        from notebooklm.server.routes.auth import RegisterRequest, register
        tmp = tempfile.mktemp(suffix=".db")
        _init(f"sqlite:///{tmp}")
        try:
            from notebooklm.server.database import get_session as gs
            db = gs()
            req = RegisterRequest(username="newuser", password="secret123", display_name="New User")
            resp = register(req, db)
            assert resp.access_token is not None
            assert resp.refresh_token is not None
            assert resp.token_type == "bearer"
        finally:
            close_db()
            Path(tmp).unlink(missing_ok=True)

    def test_register_duplicate_raises(self):
        from notebooklm.server.routes.auth import RegisterRequest, register
        tmp = tempfile.mktemp(suffix=".db")
        init_db(f"sqlite:///{tmp}")
        try:
            db = get_session()
            req = RegisterRequest(username="dup", password="p")
            register(req, db)
            with pytest.raises(HTTPException) as exc:
                register(req, db)
            assert exc.value.status_code == 409
        finally:
            close_db()
            Path(tmp).unlink(missing_ok=True)

    def test_login_valid(self):
        from notebooklm.server.routes.auth import LoginRequest, RegisterRequest, login
        from notebooklm.server.routes.auth import register as reg
        tmp = tempfile.mktemp(suffix=".db")
        init_db(f"sqlite:///{tmp}")
        try:
            db = get_session()
            reg(RegisterRequest(username="user1", password="pass"), db)
            resp = login(LoginRequest(username="user1", password="pass"), db)
            assert resp.access_token is not None
        finally:
            close_db()
            Path(tmp).unlink(missing_ok=True)

    def test_login_invalid_password(self):
        from notebooklm.server.routes.auth import LoginRequest, RegisterRequest, login
        from notebooklm.server.routes.auth import register as reg
        tmp = tempfile.mktemp(suffix=".db")
        init_db(f"sqlite:///{tmp}")
        try:
            db = get_session()
            reg(RegisterRequest(username="user2", password="correct"), db)
            with pytest.raises(HTTPException) as exc:
                login(LoginRequest(username="user2", password="wrong"), db)
            assert exc.value.status_code == 401
        finally:
            close_db()
            Path(tmp).unlink(missing_ok=True)

    def test_me_endpoint(self):
        from notebooklm.server.models import User
        from notebooklm.server.routes.auth import me
        tmp = tempfile.mktemp(suffix=".db")
        init_db(f"sqlite:///{tmp}")
        try:
            db = get_session()
            u = User(username="test_me", password_hash="h", display_name="Test Me")
            db.add(u)
            db.commit()
            db.refresh(u)
            info = me(u)
            assert info.username == "test_me"
            assert info.display_name == "Test Me"
        finally:
            close_db()
            Path(tmp).unlink(missing_ok=True)

    def test_refresh_token(self):
        from notebooklm.server.routes.auth import refresh_token
        tmp = tempfile.mktemp(suffix=".db")
        init_db(f"sqlite:///{tmp}")
        try:
            db = get_session()
            u = User(username="refresh_me", password_hash="h")
            db.add(u)
            db.commit()
            db.refresh(u)
            ref = create_refresh_token(u.id)
            resp = refresh_token({"refresh_token": ref}, db)
            assert resp.access_token is not None
            assert resp.refresh_token is not None
        finally:
            close_db()
            Path(tmp).unlink(missing_ok=True)
