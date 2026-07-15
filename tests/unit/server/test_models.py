from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from notebooklm.server.database import close_db, get_session, init_db
from notebooklm.server.models import (
    ChatMessage,
    ChatSession,
    ExternalImport,
    ExternalKBCollection,
    ExternalKBConnection,
    ExternalKBDocument,
    GeneratedContent,
    Notebook,
    RequestLog,
    Source,
    User,
    UserSession,
)


@pytest.fixture(autouse=True)
def _db():
    tmp = tempfile.mktemp(suffix=".db")
    init_db(f"sqlite:///{tmp}")
    yield
    close_db()
    Path(tmp).unlink(missing_ok=True)


def _count_rows(model):
    return get_session().query(model).count()


class TestModels:
    def test_create_user(self):
        session = get_session()
        u = User(username="testuser", password_hash="abc123", display_name="Test")
        session.add(u)
        session.commit()
        assert u.id is not None
        assert _count_rows(User) == 1

    def test_create_notebook(self):
        session = get_session()
        nb = Notebook(remote_id="nb123", title="My Notebook")
        session.add(nb)
        session.commit()
        assert nb.id is not None

    def test_create_source(self):
        session = get_session()
        nb = Notebook(remote_id="nb1", title="T")
        session.add(nb)
        session.flush()
        src = Source(
            notebook_id=nb.id,
            remote_id="src1",
            filename="doc.pdf",
            file_type="pdf",
            file_size=1024,
        )
        session.add(src)
        session.commit()
        assert src.id is not None

    def test_create_generated_content(self):
        session = get_session()
        nb = Notebook(remote_id="nb3", title="T")
        session.add(nb)
        session.flush()
        gc = GeneratedContent(
            notebook_id=nb.id, content_type="ppt", title="My PPT", engine="local"
        )
        session.add(gc)
        session.commit()
        assert gc.id is not None

    def test_create_request_log(self):
        session = get_session()
        rl = RequestLog(
            endpoint="/api/test", method="POST", response_status=200, latency_ms=42
        )
        session.add(rl)
        session.commit()
        assert rl.id is not None

    def test_create_external_kb_connection(self):
        session = get_session()
        conn = ExternalKBConnection(
            name="My KB",
            provider_type="dify",
            api_base_url="https://example.com",
        )
        session.add(conn)
        session.commit()
        assert conn.id is not None

    def test_create_external_kb_collection(self):
        session = get_session()
        conn = ExternalKBConnection(
            name="C", provider_type="dify", api_base_url="https://ex.com"
        )
        session.add(conn)
        session.flush()
        col = ExternalKBCollection(
            connection_id=conn.id, remote_id="coll1", name="Documents"
        )
        session.add(col)
        session.commit()
        assert col.id is not None

    def test_create_external_kb_document(self):
        session = get_session()
        conn = ExternalKBConnection(
            name="C", provider_type="dify", api_base_url="https://ex.com"
        )
        session.add(conn)
        session.flush()
        col = ExternalKBCollection(
            connection_id=conn.id, remote_id="coll1", name="Docs"
        )
        session.add(col)
        session.flush()
        doc = ExternalKBDocument(
            collection_id=col.id,
            connection_id=conn.id,
            remote_id="doc1",
            title="Doc 1",
        )
        session.add(doc)
        session.commit()
        assert doc.id is not None

    def test_create_external_import(self):
        session = get_session()
        nb = Notebook(remote_id="nb4", title="T")
        session.add(nb)
        session.flush()
        imp = ExternalImport(target_notebook_id=nb.id, status="completed")
        session.add(imp)
        session.commit()
        assert imp.id is not None

    def test_create_user_session(self):
        session = get_session()
        u = User(username="u1", password_hash="h")
        session.add(u)
        session.flush()
        from datetime import datetime, timedelta

        us = UserSession(
            user_id=u.id,
            token="tok123",
            expires_at=datetime.utcnow() + timedelta(days=1),
        )
        session.add(us)
        session.commit()
        assert us.id is not None

    def test_create_chat_session_and_message(self):
        session = get_session()
        nb = Notebook(remote_id="nb2", title="T")
        session.add(nb)
        session.flush()
        cs = ChatSession(notebook_id=nb.id, title="Chat 1")
        session.add(cs)
        session.flush()
        msg = ChatMessage(session_id=cs.id, role="user", content="Hello")
        session.add(msg)
        session.commit()
        assert cs.id is not None
        assert msg.id is not None
