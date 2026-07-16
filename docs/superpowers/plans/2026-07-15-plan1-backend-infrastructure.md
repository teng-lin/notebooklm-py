# Plan 1: Backend Infrastructure 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 subagent-driven-development（推荐）或 executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 为有道宝库克隆系统构建后端基础设施 — 数据库模型、文件存储、用户认证和请求日志中间件。

**架构：** 在现有的 notebooklm-py FastAPI REST 服务器（`src/notebooklm/server/`）之上增量构建。数据层使用 SQLAlchemy ORM + SQLite，文件存储在 `MEDIA_ROOT` 下，认证使用 JWT + bcrypt，所有请求/响应均记录到 `request_logs` 表。

**技术栈：** Python 3.10+, FastAPI, SQLAlchemy 2.0, SQLite, passlib[bcrypt], python-jose[cryptography], cryptography (Fernet), pytest, ruff

---
## 初始准备

### P0: 添加依赖并创建目录结构

- [ ] 将所需依赖添加到 `pyproject.toml` 的 `server` extra 中以使它们对服务器可用：

```toml
# 在 [project.optional-dependencies] server 部分追加：
server = [
    "fastapi>=0.118,<1",
    "uvicorn[standard]>=0.34,<1",
    "python-multipart>=0.0.20,<1",
    "sqlalchemy>=2.0.0,<3",
    "passlib[bcrypt]>=1.7.4,<2",
    "python-jose[cryptography]>=3.3.0,<4",
    "cryptography>=41.0.0,<44",
]
```

- [ ] 运行 `uv sync --frozen --extra server` 安装新依赖。

- [ ] 创建目录结构：

```bash
mkdir -p src/notebooklm/server/routes
mkdir -p tests/unit/server
```

---

## 任务 1.1：数据库模型 — `database.py` + `models.py`

### 文件

| 文件 | 操作 |
|---|---|
| `src/notebooklm/server/database.py` | 创建 |
| `src/notebooklm/server/models.py` | 创建 |
| `tests/unit/server/test_models.py` | 创建 |

### 步骤

#### 1.1.1 编写 `database.py` — 引擎、会话、Base

- [ ] 创建 `src/notebooklm/server/database.py`：

```python
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = os.path.expanduser("~/.notebooklm/data/baoku.db")
NOTEBOOKLM_DB_URL = os.environ.get(
    "NOTEBOOKLM_DATABASE_URL",
    f"sqlite:///{DEFAULT_DB_PATH}",
)

engine: Any = None
SessionLocal: sessionmaker[Session] | None = None


class Base(DeclarativeBase):
    pass


def get_db_path() -> str:
    if NOTEBOOKLM_DB_URL.startswith("sqlite:///"):
        return NOTEBOOKLM_DB_URL[len("sqlite:///"):]
    return NOTEBOOKLM_DB_URL


def init_db(db_url: str | None = None) -> None:
    global engine, SessionLocal
    url = db_url or NOTEBOOKLM_DB_URL
    if url.startswith("sqlite"):
        db_path = url[len("sqlite:///"):]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, echo=False, future=True)
    SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    from .models import ALL_MODELS
    Base.metadata.create_all(bind=engine)
    logger.info("Database initialized at %s", url)


def get_session() -> Session:
    if SessionLocal is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return SessionLocal()


def close_db() -> None:
    global engine, SessionLocal
    if engine is not None:
        engine.dispose()
    engine = None
    SessionLocal = None
```

#### 1.1.2 编写 `models.py` — 所有 SQLAlchemy 模型

- [ ] 创建 `src/notebooklm/server/models.py`：

```python
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.orm import relationship

from .database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(Text, unique=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    display_name = Column(Text, nullable=True)
    avatar_url = Column(Text, nullable=True)
    google_token = Column(Text, nullable=True)
    google_token_expires_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.current_timestamp())
    updated_at = Column(DateTime, server_default=func.current_timestamp(), onupdate=func.current_timestamp())

    sessions = relationship("UserSession", back_populates="user", cascade="all, delete-orphan")
    notebooks = relationship("Notebook", back_populates="user", cascade="all, delete-orphan")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    token = Column(Text, unique=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, server_default=func.current_timestamp())

    user = relationship("User", back_populates="sessions")


class Notebook(Base):
    __tablename__ = "notebooks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    remote_id = Column(Text, unique=True, nullable=False)
    title = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    source_count = Column(Integer, default=0)
    chat_count = Column(Integer, default=0)
    last_synced_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.current_timestamp())
    updated_at = Column(DateTime, server_default=func.current_timestamp(), onupdate=func.current_timestamp())

    user = relationship("User", back_populates="notebooks")
    sources = relationship("Source", back_populates="notebook", cascade="all, delete-orphan")
    chat_sessions = relationship("ChatSession", back_populates="notebook", cascade="all, delete-orphan")
    generated_contents = relationship("GeneratedContent", back_populates="notebook", cascade="all, delete-orphan")


Index("idx_notebooks_user", Notebook.user_id)
Index("idx_notebooks_remote", Notebook.remote_id)


class Source(Base):
    __tablename__ = "sources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    notebook_id = Column(Integer, ForeignKey("notebooks.id"), nullable=True)
    remote_id = Column(Text, unique=True, nullable=False)
    filename = Column(Text, nullable=False)
    original_filename = Column(Text, nullable=True)
    file_type = Column(Text, nullable=True)
    file_size = Column(Integer, nullable=True)
    page_count = Column(Integer, nullable=True)
    local_path = Column(Text, nullable=True)
    source_url = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)
    status = Column(Text, default="active")
    created_at = Column(DateTime, server_default=func.current_timestamp())
    updated_at = Column(DateTime, server_default=func.current_timestamp(), onupdate=func.current_timestamp())

    notebook = relationship("Notebook", back_populates="sources")


Index("idx_sources_notebook", Source.notebook_id)
Index("idx_sources_user", Source.user_id)


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    notebook_id = Column(Integer, ForeignKey("notebooks.id"), nullable=True)
    title = Column(Text, nullable=True)
    message_count = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.current_timestamp())
    updated_at = Column(DateTime, server_default=func.current_timestamp(), onupdate=func.current_timestamp())

    notebook = relationship("Notebook", back_populates="chat_sessions")
    messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan")


Index("idx_chat_sessions_notebook", ChatSession.notebook_id)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("chat_sessions.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    role = Column(Text, nullable=False)
    content = Column(Text, nullable=False)
    citations = Column(Text, nullable=True)
    request_body = Column(Text, nullable=True)
    response_body = Column(Text, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    token_count = Column(Integer, nullable=True)
    status = Column(Text, default="success")
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.current_timestamp())

    session = relationship("ChatSession", back_populates="messages")


Index("idx_chat_messages_session", ChatMessage.session_id)
Index("idx_chat_messages_user", ChatMessage.user_id)


class GeneratedContent(Base):
    __tablename__ = "generated_contents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    notebook_id = Column(Integer, ForeignKey("notebooks.id"), nullable=True)
    content_type = Column(Text, nullable=False)
    title = Column(Text, nullable=True)
    prompt = Column(Text, nullable=True)
    engine = Column(Text, default="notebooklm")
    content = Column(Text, nullable=True)
    local_file_path = Column(Text, nullable=True)
    file_size = Column(Integer, nullable=True)
    thumbnail_path = Column(Text, nullable=True)
    status = Column(Text, default="processing")
    error_message = Column(Text, nullable=True)
    ppt_page_count = Column(Integer, nullable=True)
    ppt_template = Column(Text, nullable=True)
    ppt_json = Column(Text, nullable=True)
    ppt_preview_images = Column(Text, nullable=True)
    mindmap_data = Column(Text, nullable=True)
    mindmap_layout = Column(Text, default="tree")
    infographic_template = Column(Text, nullable=True)
    infographic_blocks = Column(Text, nullable=True)
    audio_file_path = Column(Text, nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    audio_speakers = Column(Text, nullable=True)
    audio_transcript = Column(Text, nullable=True)
    video_file_path = Column(Text, nullable=True)
    video_duration_seconds = Column(Integer, nullable=True)
    video_resolution = Column(Text, nullable=True)
    video_scenes = Column(Text, nullable=True)
    video_narration = Column(Text, nullable=True)
    video_bg_music = Column(Text, nullable=True)
    doc_page_count = Column(Integer, nullable=True)
    doc_sections = Column(Text, nullable=True)
    doc_format = Column(Text, default="markdown")
    request_body = Column(Text, nullable=True)
    response_body = Column(Text, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, server_default=func.current_timestamp())

    notebook = relationship("Notebook", back_populates="generated_contents")


Index("idx_generated_notebook", GeneratedContent.notebook_id)
Index("idx_generated_user", GeneratedContent.user_id)


class RequestLog(Base):
    __tablename__ = "request_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    endpoint = Column(Text, nullable=False)
    method = Column(Text, nullable=False)
    request_headers = Column(Text, nullable=True)
    request_body = Column(Text, nullable=True)
    response_status = Column(Integer, nullable=True)
    response_headers = Column(Text, nullable=True)
    response_body = Column(Text, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    client_ip = Column(Text, nullable=True)
    user_agent = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.current_timestamp())


Index("idx_request_logs_user", RequestLog.user_id)
Index("idx_request_logs_endpoint", RequestLog.endpoint)
Index("idx_request_logs_created", RequestLog.created_at)


class ExternalKBConnection(Base):
    __tablename__ = "external_kb_connections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    name = Column(Text, nullable=False)
    provider_type = Column(Text, nullable=False)
    api_base_url = Column(Text, nullable=False)
    auth_type = Column(Text, default="api_key")
    auth_credentials = Column(Text, nullable=True)
    extra_config = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    last_sync_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.current_timestamp())
    updated_at = Column(DateTime, server_default=func.current_timestamp(), onupdate=func.current_timestamp())


Index("idx_ext_kb_conn_user", ExternalKBConnection.user_id)


class ExternalKBCollection(Base):
    __tablename__ = "external_kb_collections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    connection_id = Column(Integer, ForeignKey("external_kb_connections.id"), nullable=True)
    remote_id = Column(Text, nullable=False)
    name = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    document_count = Column(Integer, default=0)
    last_fetched_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.current_timestamp())

    __table_args__ = (
        Index("idx_ext_kb_coll_conn", "connection_id"),
    )


class ExternalKBDocument(Base):
    __tablename__ = "external_kb_documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    collection_id = Column(Integer, ForeignKey("external_kb_collections.id"), nullable=True)
    connection_id = Column(Integer, ForeignKey("external_kb_connections.id"), nullable=True)
    remote_id = Column(Text, nullable=False)
    title = Column(Text, nullable=False)
    summary = Column(Text, nullable=True)
    file_type = Column(Text, nullable=True)
    file_size = Column(Integer, nullable=True)
    url = Column(Text, nullable=True)
    metadata = Column(Text, nullable=True)
    last_fetched_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.current_timestamp())

    __table_args__ = (
        Index("idx_ext_kb_docs_coll", "collection_id"),
        Index("idx_ext_kb_docs_conn", "connection_id"),
    )


class ExternalImport(Base):
    __tablename__ = "external_imports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    connection_id = Column(Integer, ForeignKey("external_kb_connections.id"), nullable=True)
    source_document_id = Column(Integer, ForeignKey("external_kb_documents.id"), nullable=True)
    target_notebook_id = Column(Integer, ForeignKey("notebooks.id"), nullable=True)
    target_source_id = Column(Integer, ForeignKey("sources.id"), nullable=True)
    status = Column(Text, default="pending")
    error_message = Column(Text, nullable=True)
    imported_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.current_timestamp())


Index("idx_ext_imports_user", ExternalImport.user_id)
Index("idx_ext_imports_notebook", ExternalImport.target_notebook_id)


ALL_MODELS = [
    User,
    UserSession,
    Notebook,
    Source,
    ChatSession,
    ChatMessage,
    GeneratedContent,
    RequestLog,
    ExternalKBConnection,
    ExternalKBCollection,
    ExternalKBDocument,
    ExternalImport,
]
```

#### 1.1.3 编写测试 `tests/unit/server/test_models.py`

- [ ] 创建 `tests/unit/server/test_models.py`：

```python
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from notebooklm.server.database import Base, close_db, get_session, init_db
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
        fetched = session.query(User).filter_by(username="testuser").first()
        assert fetched is not None
        assert fetched.display_name == "Test"

    def test_create_user_session(self):
        session = get_session()
        u = User(username="u1", password_hash="h")
        session.add(u)
        session.flush()
        from datetime import datetime, timedelta

        us = UserSession(user_id=u.id, token="tok123", expires_at=datetime.utcnow() + timedelta(days=1))
        session.add(us)
        session.commit()
        assert us.id is not None
        assert _count_rows(UserSession) == 1

    def test_create_notebook(self):
        session = get_session()
        nb = Notebook(remote_id="nb123", title="My Notebook")
        session.add(nb)
        session.commit()
        assert nb.id is not None
        assert _count_rows(Notebook) == 1

    def test_create_source(self):
        session = get_session()
        nb = Notebook(remote_id="nb1", title="T")
        session.add(nb)
        session.flush()
        src = Source(notebook_id=nb.id, user_id=None, remote_id="src1", filename="doc.pdf", file_type="pdf", file_size=1024)
        session.add(src)
        session.commit()
        assert src.id is not None
        assert _count_rows(Source) == 1

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
        assert _count_rows(ChatMessage) == 1

    def test_create_generated_content(self):
        session = get_session()
        nb = Notebook(remote_id="nb3", title="T")
        session.add(nb)
        session.flush()
        gc = GeneratedContent(notebook_id=nb.id, content_type="ppt", title="My PPT", engine="local")
        session.add(gc)
        session.commit()
        assert gc.id is not None
        assert _count_rows(GeneratedContent) == 1

    def test_create_request_log(self):
        session = get_session()
        rl = RequestLog(endpoint="/api/test", method="POST", response_status=200, latency_ms=42)
        session.add(rl)
        session.commit()
        assert rl.id is not None
        assert _count_rows(RequestLog) == 1

    def test_create_external_kb_connection(self):
        session = get_session()
        conn = ExternalKBConnection(name="My KB", provider_type="dify", api_base_url="https://example.com")
        session.add(conn)
        session.commit()
        assert conn.id is not None
        assert _count_rows(ExternalKBConnection) == 1

    def test_create_external_kb_collection(self):
        session = get_session()
        conn = ExternalKBConnection(name="C", provider_type="dify", api_base_url="https://ex.com")
        session.add(conn)
        session.flush()
        col = ExternalKBCollection(connection_id=conn.id, remote_id="coll1", name="Documents")
        session.add(col)
        session.commit()
        assert col.id is not None
        assert _count_rows(ExternalKBCollection) == 1

    def test_create_external_kb_document(self):
        session = get_session()
        conn = ExternalKBConnection(name="C", provider_type="dify", api_base_url="https://ex.com")
        session.add(conn)
        session.flush()
        col = ExternalKBCollection(connection_id=conn.id, remote_id="coll1", name="Docs")
        session.add(col)
        session.flush()
        doc = ExternalKBDocument(collection_id=col.id, connection_id=conn.id, remote_id="doc1", title="Doc 1")
        session.add(doc)
        session.commit()
        assert doc.id is not None
        assert _count_rows(ExternalKBDocument) == 1

    def test_create_external_import(self):
        session = get_session()
        nb = Notebook(remote_id="nb4", title="T")
        session.add(nb)
        session.flush()
        imp = ExternalImport(
            user_id=None,
            connection_id=None,
            source_document_id=None,
            target_notebook_id=nb.id,
            status="completed",
        )
        session.add(imp)
        session.commit()
        assert imp.id is not None
        assert _count_rows(ExternalImport) == 1
```

- [ ] 运行测试以验证失败（依赖未安装）：

```bash
cd /Users/xinghen/projects/notebooklm-py && uv run pytest tests/unit/server/test_models.py -v 2>&1 || true
```

预期结果：由于缺少 `sqlalchemy`，出现 `ModuleNotFoundError`（或 `ImportError`）。

- [ ] 将依赖添加到 `pyproject.toml`（参见 P0），运行 `uv sync --frozen --extra server`。

- [ ] 再次运行测试：

```bash
cd /Users/xinghen/projects/notebooklm-py && uv run pytest tests/unit/server/test_models.py -v
```

预期结果：全部通过。

- [ ] 添加 `init-db` CLI 命令。在 `src/notebooklm/server/__main__.py` 中添加 `init-db` 子解析器：

在 `_build_parser` 中，在 `--log-level` 参数之后添加：

```python
    parser.add_argument(
        "--init-db",
        action="store_true",
        default=False,
        help="Initialize the database tables and exit.",
    )
```

在 `main` 中，在 `_reject_argv_token` 调用之后和 `_check_token_configured` 之前添加：

```python
    if args.init_db:
        from .database import init_db as _init_db
        _init_db()
        print("Database initialized successfully.")
        return
```

- [ ] 在 `src/notebooklm/cli/` 下为 `notebooklm server init-db` 添加一个 Click 命令（可选，但作为首选方式）。

在 `src/notebooklm/cli/doctor_cmd.py` 末尾（或新建 `src/notebooklm/cli/server_cmd.py`）添加：

```python
@click.group()
def server():
    """Server management commands."""


@server.command("init-db")
@handle_errors()
def init_db():
    """Initialize the database tables."""
    from notebooklm.server.database import init_db as _init_db
    _init_db(callback=lambda: click.echo("Database initialized successfully."))
```

然后在 `notebooklm_cli.py` 中导入并注册：

```python
from .cli.server_cmd import server
cli.add_command(server)
```

---

## 任务 1.2：文件存储模块 — `storage.py`

### 文件

| 文件 | 操作 |
|---|---|
| `src/notebooklm/server/storage.py` | 创建 |
| `tests/unit/server/test_storage.py` | 创建 |

### 步骤

#### 1.2.1 编写 `storage.py`

- [ ] 创建 `src/notebooklm/server/storage.py`：

```python
from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


def get_media_root() -> Path:
    raw = os.environ.get("NOTEBOOKLM_MEDIA_ROOT", "")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home() / ".notebooklm" / "data"


MEDIA_ROOT = get_media_root()


class StorageManager:
    def __init__(self, media_root: str | Path | None = None) -> None:
        self._root = Path(media_root).expanduser().resolve() if media_root else MEDIA_ROOT

    @property
    def root(self) -> Path:
        return self._root

    def save_source_file(
        self,
        user_id: int | str,
        notebook_id: int | str,
        source_id: int | str,
        filename: str,
        content: bytes,
    ) -> str:
        dest_dir = self._root / "sources" / str(user_id) / str(notebook_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{source_id}_{filename}"
        dest.write_bytes(content)
        return str(dest.relative_to(self._root))

    def save_generated_file(
        self,
        user_id: int | str,
        notebook_id: int | str,
        content_type: str,
        content_id: int | str,
        ext: str,
        content: bytes,
    ) -> str:
        dest_dir = self._root / "generated" / str(user_id) / str(notebook_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{content_type}_{content_id}.{ext}"
        dest.write_bytes(content)
        return str(dest.relative_to(self._root))

    def get_file(self, relative_path: str) -> bytes:
        full = self._resolve(relative_path)
        return full.read_bytes()

    def get_file_path(self, relative_path: str) -> Path:
        return self._resolve(relative_path)

    def get_file_url(self, relative_path: str) -> str:
        return f"/media/{relative_path}"

    def delete_file(self, relative_path: str) -> None:
        full = self._resolve(relative_path)
        if full.exists():
            full.unlink()

    def file_exists(self, relative_path: str) -> bool:
        return self._resolve(relative_path).exists()

    def _resolve(self, relative_path: str) -> Path:
        # Security: prevent path traversal
        sanitized = Path(relative_path).as_posix()
        full = (self._root / sanitized).resolve()
        if not str(full).startswith(str(self._root.resolve())):
            raise ValueError(f"Path traversal detected: {relative_path}")
        return full
```

#### 1.2.2 编写测试 `tests/unit/server/test_storage.py`

- [ ] 创建 `tests/unit/server/test_storage.py`：

```python
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from notebooklm.server.storage import StorageManager


@pytest.fixture
def tmp_media():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


class TestStorageManager:
    def test_save_source_file(self, tmp_media: Path):
        mgr = StorageManager(tmp_media)
        rel = mgr.save_source_file(1, 10, 100, "test.pdf", b"%PDF-content")
        assert rel == "sources/1/10/100_test.pdf"
        assert (tmp_media / rel).read_bytes() == b"%PDF-content"

    def test_save_generated_file(self, tmp_media: Path):
        mgr = StorageManager(tmp_media)
        rel = mgr.save_generated_file(1, 10, "ppt", 200, "pptx", b"%PPTX")
        assert rel == "generated/1/10/ppt_200.pptx"
        assert (tmp_media / rel).read_bytes() == b"%PPTX"

    def test_get_file(self, tmp_media: Path):
        mgr = StorageManager(tmp_media)
        rel = mgr.save_source_file(1, 10, 100, "doc.txt", b"hello")
        assert mgr.get_file(rel) == b"hello"

    def test_get_file_url(self, tmp_media: Path):
        mgr = StorageManager(tmp_media)
        assert mgr.get_file_url("sources/1/doc.txt") == "/media/sources/1/doc.txt"

    def test_delete_file(self, tmp_media: Path):
        mgr = StorageManager(tmp_media)
        rel = mgr.save_source_file(1, 10, 100, "tmp.txt", b"data")
        assert mgr.file_exists(rel)
        mgr.delete_file(rel)
        assert not mgr.file_exists(rel)

    def test_path_traversal_raises(self, tmp_media: Path):
        mgr = StorageManager(tmp_media)
        with pytest.raises(ValueError, match="Path traversal"):
            mgr._resolve("../../etc/passwd")

    def test_default_media_root(self):
        import os
        os.environ.pop("NOTEBOOKLM_MEDIA_ROOT", None)
        from notebooklm.server.storage import MEDIA_ROOT
        assert str(MEDIA_ROOT).endswith(".notebooklm/data")
```

- [ ] 运行测试：

```bash
cd /Users/xinghen/projects/notebooklm-py && uv run pytest tests/unit/server/test_storage.py -v
```

预期结果：全部通过。

---

## 任务 1.3：用户认证模块 — `routes/auth.py` + `auth_deps.py`

### 文件

| 文件 | 操作 |
|---|---|
| `src/notebooklm/server/routes/auth.py` | 创建 |
| `src/notebooklm/server/auth_deps.py` | 创建 |
| `tests/unit/server/test_auth.py` | 创建 |

### 步骤

#### 1.3.1 编写 `auth_deps.py` — `get_current_user` 依赖

- [ ] 创建 `src/notebooklm/server/auth_deps.py`：

```python
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from ..database import get_session
from ..models import User, UserSession

SECRET_KEY = os.environ.get("NOTEBOOKLM_JWT_SECRET", "change-me-in-production-use-a-real-secret")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours
REFRESH_TOKEN_EXPIRE_DAYS = 30

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(user_id: int, username: str) -> str:
    payload = {
        "sub": str(user_id),
        "username": username,
        "type": "access",
        "exp": datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        "iat": datetime.utcnow(),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(user_id: int) -> str:
    payload = {
        "sub": str(user_id),
        "type": "refresh",
        "exp": datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
        "iat": datetime.utcnow(),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_session),
) -> User:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_token(credentials.credentials)
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid token type")
    user_id = int(payload["sub"])
    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_session),
) -> User | None:
    if credentials is None:
        return None
    try:
        payload = decode_token(credentials.credentials)
    except HTTPException:
        return None
    if payload.get("type") != "access":
        return None
    user_id = int(payload["sub"])
    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.is_active:
        return None
    return user
```

#### 1.3.2 编写 `routes/auth.py` — 认证 API 路由

- [ ] 创建 `src/notebooklm/server/routes/auth.py`：

```python
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ...models import User, UserSession
from ..auth_deps import (
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
    get_session,
    hash_password,
    verify_password,
)
from ..database import get_session as _get_session

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    username: str
    password: str
    display_name: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class GoogleBindRequest(BaseModel):
    google_token: str
    expires_at: str | None = None


class UserInfo(BaseModel):
    id: int
    username: str
    display_name: str | None
    avatar_url: str | None
    google_bound: bool
    created_at: datetime | None


@router.post("/register", response_model=TokenResponse)
def register(body: RegisterRequest, db: Session = Depends(_get_session)):
    existing = db.query(User).filter(User.username == body.username).first()
    if existing:
        raise HTTPException(status_code=409, detail="Username already exists")
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        display_name=body.display_name or body.username,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    access = create_access_token(user.id, user.username)
    refresh = create_refresh_token(user.id)
    return TokenResponse(access_token=access, refresh_token=refresh)


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(_get_session)):
    user = db.query(User).filter(User.username == body.username).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is inactive")
    access = create_access_token(user.id, user.username)
    refresh = create_refresh_token(user.id)
    return TokenResponse(access_token=access, refresh_token=refresh)


@router.post("/refresh", response_model=TokenResponse)
def refresh_token(body: dict[str, str], db: Session = Depends(_get_session)):
    refresh_token_str = body.get("refresh_token", "")
    payload = decode_token(refresh_token_str)
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    user_id = int(payload["sub"])
    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    access = create_access_token(user.id, user.username)
    refresh = create_refresh_token(user.id)
    return TokenResponse(access_token=access, refresh_token=refresh)


@router.get("/me", response_model=UserInfo)
def me(current_user: User = Depends(get_current_user)):
    return UserInfo(
        id=current_user.id,
        username=current_user.username,
        display_name=current_user.display_name,
        avatar_url=current_user.avatar_url,
        google_bound=bool(current_user.google_token),
        created_at=current_user.created_at,
    )


@router.post("/google/bind")
def google_bind(
    body: GoogleBindRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(_get_session),
):
    from cryptography.fernet import Fernet
    key = _get_fernet_key()
    cipher = Fernet(key)
    encrypted = cipher.encrypt(body.google_token.encode())
    current_user.google_token = encrypted.decode()
    if body.expires_at:
        try:
            current_user.google_token_expires_at = datetime.fromisoformat(body.expires_at)
        except ValueError:
            pass
    db.commit()
    return {"ok": True}


@router.delete("/logout")
def logout(current_user: User = Depends(get_current_user)):
    return {"ok": True}


@router.post("/logout")
def logout_post(current_user: User = Depends(get_current_user)):
    return {"ok": True}


def _get_fernet_key() -> bytes:
    import os
    import base64
    import hashlib
    raw = os.environ.get("NOTEBOOKLM_FERNET_KEY", SECRET_KEY)
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())
```

#### 1.3.3 编写测试 `tests/unit/server/test_auth.py`

- [ ] 创建 `tests/unit/server/test_auth.py`：

```python
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
        from notebooklm.server.routes.auth import register, RegisterRequest
        from notebooklm.server.database import init_db as _init
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
        from notebooklm.server.routes.auth import LoginRequest, RegisterRequest, login, register as reg
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
        from notebooklm.server.routes.auth import LoginRequest, RegisterRequest, login, register as reg
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
        from notebooklm.server.routes.auth import me
        from notebooklm.server.models import User
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
```

- [ ] 运行测试：

```bash
cd /Users/xinghen/projects/notebooklm-py && uv run pytest tests/unit/server/test_auth.py -v
```

预期结果：全部通过。

---

## 任务 1.4：请求日志中间件 — `routes/middleware.py`

### 文件

| 文件 | 操作 |
|---|---|
| `src/notebooklm/server/routes/middleware.py` | 创建 |
| `tests/unit/server/test_middleware.py` | 创建 |

### 步骤

#### 1.4.1 编写 `routes/middleware.py`

- [ ] 创建 `src/notebooklm/server/routes/middleware.py`：

```python
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from fastapi import Request, Response
from jose import JWTError, jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from ...models import RequestLog as RequestLogModel
from ..auth_deps import SECRET_KEY, ALGORITHM
from ..database import get_session

logger = logging.getLogger(__name__)

MAX_BODY_LOG_BYTES = 100 * 1024
CLEANUP_LOG_DAYS = 90


def _truncate(body: bytes) -> str:
    if len(body) > MAX_BODY_LOG_BYTES:
        return body[:MAX_BODY_LOG_BYTES].decode("utf-8", errors="replace") + "... (truncated)"
    return body.decode("utf-8", errors="replace")


def _extract_user_id(request: Request) -> int | None:
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[len("Bearer "):].strip()
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return int(payload.get("sub", 0)) or None
    except (JWTError, ValueError, TypeError):
        return None


class RequestLogMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable[[Request], Any]) -> Response:
        start = time.monotonic()
        user_id = _extract_user_id(request)
        client_ip = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent", "")
        method = request.method
        path = request.url.path
        endpoint = path

        try:
            body_bytes = await request.body()
        except Exception:
            body_bytes = b""

        response = await call_next(request)
        latency_ms = int((time.monotonic() - start) * 1000)

        response_body_bytes = b""
        if hasattr(response, "body_iterator"):
            chunks: list[bytes] = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
            response_body_bytes = b"".join(chunks)

        try:
            db = get_session()
            log_entry = RequestLogModel(
                user_id=user_id,
                endpoint=endpoint,
                method=method,
                request_headers=json.dumps(dict(request.headers)),
                request_body=_truncate(body_bytes),
                response_status=response.status_code,
                response_headers=json.dumps(dict(response.headers)),
                response_body=_truncate(response_body_bytes),
                latency_ms=latency_ms,
                client_ip=client_ip,
                user_agent=user_agent,
            )
            db.add(log_entry)
            db.commit()
        except Exception as exc:
            logger.warning("Failed to log request: %s", exc)

        return response


async def cleanup_old_logs() -> int:
    from datetime import datetime, timedelta
    try:
        db = get_session()
        cutoff = datetime.utcnow() - timedelta(days=CLEANUP_LOG_DAYS)
        deleted = db.query(RequestLogModel).filter(
            RequestLogModel.created_at < cutoff
        ).delete()
        db.commit()
        return deleted
    except Exception as exc:
        logger.warning("Failed to cleanup old logs: %s", exc)
        return 0
```

#### 1.4.2 编写测试 `tests/unit/server/test_middleware.py`

- [ ] 创建 `tests/unit/server/test_middleware.py`：

```python
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from notebooklm.server.database import Base, close_db, engine, get_session, init_db, SessionLocal
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
        from fastapi import Request
        import asyncio
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
        from fastapi.testclient import TestClient as TC
        client = TC(test_app.app, client=("192.168.1.1", 12345))
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
```

- [ ] 运行测试：

```bash
cd /Users/xinghen/projects/notebooklm-py && uv run pytest tests/unit/server/test_middleware.py -v
```

预期结果：全部通过。

---

## 任务 1.5：集成和验证

### 步骤

#### 1.5.1 将所有模块接入 `server.py`（`app.py`）

- [ ] 编辑 `src/notebooklm/server/app.py`，在导入路由后添加中间件和数据库初始化：

在 `create_app` 函数中，在 `install_exception_handlers(app)` 之后添加：

```python
    from .database import init_db as _init_server_db
    from .routes.middleware import RequestLogMiddleware, cleanup_old_logs

    try:
        _init_server_db()
    except Exception as exc:
        logger.warning("Failed to initialize database: %s", exc)

    app.add_middleware(RequestLogMiddleware)
```

同时，在 `lifespan` 函数的末尾添加日志清理：

在 `finally` 块中的 `set_active_profile(previous_profile)` 之前添加：

```python
                await asyncio.to_thread(cleanup_old_logs)
```

并在文件顶部添加 `import logging`（如尚未导入）：

```python
import logging
```

#### 1.5.2 运行所有单元测试

- [ ] 运行完整的服务器单元测试套件：

```bash
cd /Users/xinghen/projects/notebooklm-py && uv run pytest tests/unit/server/ -v
```

预期结果：全部通过。

#### 1.5.3 验证 `init-db` CLI 命令

- [ ] 运行 `init-db` 命令：

```bash
cd /Users/xinghen/projects/notebooklm-py && uv run python -m notebooklm.server.__main__ --init-db
```

预期结果：打印 "Database initialized successfully."，且在 `~/.notebooklm/data/baoku.db` 创建 SQLite 文件。

#### 1.5.4 运行 lint 检查

- [ ] 运行 ruff：

```bash
cd /Users/xinghen/projects/notebooklm-py && uv run ruff check src/notebooklm/server/ tests/unit/server/ --fix
```

- [ ] 运行 ruff format：

```bash
cd /Users/xinghen/projects/notebooklm-py && uv run ruff format src/notebooklm/server/ tests/unit/server/
```

- [ ] 最终提交：

```bash
cd /Users/xinghen/projects/notebooklm-py && git add -A && git commit -m "feat(server): backend infrastructure - DB models, file storage, auth, request logging"
```
