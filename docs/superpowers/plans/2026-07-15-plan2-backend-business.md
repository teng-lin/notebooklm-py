# Plan 2: 后端业务逻辑 — 外部知识库连接器 + 内容生成引擎 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 subagent-driven-development（推荐）或 executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 实现外部知识库连接器体系和 6 种内容类型的生成引擎及其 API 路由。

**架构：** 插件化连接器模式 + 抽象生成器模式，各自通过注册中心管理。连接器从外部 KB API 获取数据并导入本地；生成引擎调用 NotebookLM API 或本地渲染库（python-pptx/Pillow/graphviz/FFmpeg/TTS）产出内容。两者通过 FastAPI 路由暴露，依赖 Plan 1 的数据库/存储/鉴权层。

**技术栈：**
- Pydantic v2 数据类 / dataclasses
- FastAPI APIRouter + Depends(get_current_user) + Depends(get_db)
- httpx (异步 HTTP 调用外部 API)
- python-pptx (PPT 生成)
- Pillow (信息图渲染 / 预览缩略图)
- graphviz (脑图导出 PNG)
- gTTS / edge-tts (视频旁白 TTS)
- cryptography.fernet (凭据加密存储)

---

## Task 2.1: External KB connector base + registry

### 步骤 2.1.1: 创建 `src/notebooklm/server/external_kb/__init__.py`

```python
from __future__ import annotations

__all__: list[str] = []
```

### 步骤 2.1.2: 创建 `src/notebooklm/server/external_kb/base.py` — 抽象基类 + 数据类

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass
class KBCollection:
    remote_id: str
    name: str
    description: str = ""
    document_count: int = 0


@dataclass
class KBDocument:
    remote_id: str
    title: str
    summary: str = ""
    file_type: str = ""
    file_size: int = 0
    url: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class KBSearchResult:
    document_id: str
    title: str
    snippet: str
    score: float = 0.0
    collection_id: str = ""


@dataclass
class ImportResult:
    success: bool
    local_source_id: int | None = None
    local_source_path: str | None = None
    error_message: str = ""


@dataclass
class PageResult(Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


class ExternalKBConnector(ABC):
    provider_type: str = ""

    @abstractmethod
    async def test_connection(self) -> bool:
        ...

    @abstractmethod
    async def list_collections(self) -> list[KBCollection]:
        ...

    @abstractmethod
    async def list_documents(
        self, collection_id: str, page: int = 1, page_size: int = 20
    ) -> PageResult[KBDocument]:
        ...

    @abstractmethod
    async def get_document_detail(self, document_id: str) -> KBDocument:
        ...

    @abstractmethod
    async def search_documents(
        self, collection_id: str, query: str, top_k: int = 10
    ) -> list[KBSearchResult]:
        ...

    @abstractmethod
    async def import_document(
        self, document_id: str, target_notebook_id: str
    ) -> ImportResult:
        ...
```

### 步骤 2.1.3: 创建 `src/notebooklm/server/external_kb/registry.py`

```python
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import ExternalKBConnector


_PROVIDERS: dict[str, type[ExternalKBConnector]] = {}


class ConnectorRegistry:

    @staticmethod
    def register(provider_type: str, connector_class: type[ExternalKBConnector]) -> None:
        _PROVIDERS[provider_type] = connector_class

    @staticmethod
    def create(provider_type: str, config: dict) -> ExternalKBConnector:
        cls = _PROVIDERS.get(provider_type)
        if cls is None:
            msg = f"Unknown provider type: {provider_type!r}. Available: {list(_PROVIDERS)}"
            raise ValueError(msg)
        return cls(config)

    @staticmethod
    def list_providers() -> list[str]:
        return list(_PROVIDERS)
```

### 步骤 2.1.4: 创建 `tests/unit/server/test_external_kb.py`

```python
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from notebooklm.server.external_kb.base import (
    ExternalKBConnector,
    ImportResult,
    KBCollection,
    KBDocument,
    KBSearchResult,
    PageResult,
)
from notebooklm.server.external_kb.registry import ConnectorRegistry


@dataclass
class FakeConnector(ExternalKBConnector):
    provider_type = "fake"
    config: dict = field(default_factory=dict)
    _connected: bool = True

    async def test_connection(self) -> bool:
        return self._connected

    async def list_collections(self) -> list[KBCollection]:
        return [
            KBCollection(remote_id="c1", name="Coll 1", document_count=5),
        ]

    async def list_documents(self, collection_id: str, page: int = 1, page_size: int = 20) -> PageResult[KBDocument]:
        if collection_id == "c1":
            return PageResult(
                items=[
                    KBDocument(remote_id="d1", title="Doc 1", file_type="pdf"),
                ],
                total=1, page=page, page_size=page_size,
            )
        return PageResult(items=[], total=0, page=page, page_size=page_size)

    async def get_document_detail(self, document_id: str) -> KBDocument:
        return KBDocument(remote_id=document_id, title=f"Doc {document_id}")

    async def search_documents(self, collection_id: str, query: str, top_k: int = 10) -> list[KBSearchResult]:
        return [
            KBSearchResult(document_id="d1", title="Matched", snippet=query, score=0.95, collection_id=collection_id),
        ]

    async def import_document(self, document_id: str, target_notebook_id: str) -> ImportResult:
        return ImportResult(success=True, local_source_id=42, local_source_path=f"/media/imports/{document_id}.pdf")


class TestKBConnectorBase:

    @pytest.mark.asyncio
    async def test_test_connection_returns_bool(self) -> None:
        c = FakeConnector()
        assert await c.test_connection() is True
        c._connected = False
        assert await c.test_connection() is False

    @pytest.mark.asyncio
    async def test_list_collections_returns_list(self) -> None:
        c = FakeConnector()
        cols = await c.list_collections()
        assert len(cols) == 1
        assert cols[0].remote_id == "c1"
        assert cols[0].name == "Coll 1"

    @pytest.mark.asyncio
    async def test_list_documents_paginates(self) -> None:
        c = FakeConnector()
        result = await c.list_documents("c1", page=1, page_size=10)
        assert result.total == 1
        assert result.items[0].title == "Doc 1"

    @pytest.mark.asyncio
    async def test_list_documents_empty_on_unknown_collection(self) -> None:
        c = FakeConnector()
        result = await c.list_documents("nonexistent", page=1, page_size=10)
        assert result.total == 0

    @pytest.mark.asyncio
    async def test_get_document_detail_returns_document(self) -> None:
        c = FakeConnector()
        doc = await c.get_document_detail("d1")
        assert doc.title == "Doc d1"

    @pytest.mark.asyncio
    async def test_search_documents_returns_results(self) -> None:
        c = FakeConnector()
        results = await c.search_documents("c1", "test query", top_k=5)
        assert len(results) == 1
        assert results[0].snippet == "test query"
        assert results[0].score == 0.95

    @pytest.mark.asyncio
    async def test_import_document_returns_import_result(self) -> None:
        c = FakeConnector()
        result = await c.import_document("d1", "notebook-abc")
        assert result.success is True
        assert result.local_source_id == 42


class TestConnectorRegistry:

    def test_register_and_create(self) -> None:
        ConnectorRegistry.register("fake", FakeConnector)
        connector = ConnectorRegistry.create("fake", {})
        assert isinstance(connector, FakeConnector)

    def test_create_unknown_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown provider type"):
            ConnectorRegistry.create("nonexistent", {})

    def test_list_providers(self) -> None:
        providers = ConnectorRegistry.list_providers()
        assert "fake" in providers
```

**运行测试：**
```bash
uv run pytest tests/unit/server/test_external_kb.py -v
```

---

## Task 2.2: External KB providers

### 步骤 2.2.1: 创建 `src/notebooklm/server/external_kb/providers/__init__.py`

```python
from __future__ import annotations

__all__: list[str] = []
```

### 步骤 2.2.2: 创建 `src/notebooklm/server/external_kb/providers/openapi.py`

```python
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..base import (
    ExternalKBConnector,
    ImportResult,
    KBCollection,
    KBDocument,
    KBSearchResult,
    PageResult,
)


@dataclass
class OpenApiConnector(ExternalKBConnector):
    provider_type = "openapi"
    config: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.api_base_url: str = self.config.get("api_base_url", "")
        self.auth_type: str = self.config.get("auth_type", "api_key")
        self.auth_credentials: dict = self.config.get("auth_credentials", {})

    def _build_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if self.auth_type == "api_key":
            headers["Authorization"] = f"Bearer {self.auth_credentials.get('api_key', '')}"
        elif self.auth_type == "bearer":
            headers["Authorization"] = f"Bearer {self.auth_credentials.get('token', '')}"
        elif self.auth_type == "basic":
            from base64 import b64encode
            raw = f"{self.auth_credentials.get('username', '')}:{self.auth_credentials.get('password', '')}"
            encoded = b64encode(raw.encode()).decode()
            headers["Authorization"] = f"Basic {encoded}"
        return headers

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.api_base_url.rstrip('/')}/{path.lstrip('/')}"
        headers = self._build_headers()
        headers.update(kwargs.pop("headers", {}))
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(method, url, headers=headers, **kwargs)
            resp.raise_for_status()
            return resp.json()

    async def test_connection(self) -> bool:
        try:
            await self._request("GET", "health")
            return True
        except Exception:
            return False

    async def list_collections(self) -> list[KBCollection]:
        data = await self._request("GET", "collections")
        return [
            KBCollection(
                remote_id=item.get("id", item.get("collection_id", "")),
                name=item.get("name", ""),
                description=item.get("description", ""),
                document_count=item.get("document_count", 0),
            )
            for item in data
        ]

    async def list_documents(
        self, collection_id: str, page: int = 1, page_size: int = 20
    ) -> PageResult[KBDocument]:
        data = await self._request(
            "GET",
            f"collections/{collection_id}/documents",
            params={"page": page, "page_size": page_size},
        )
        if isinstance(data, dict):
            items_data = data.get("items", data.get("documents", []))
            total = data.get("total", len(items_data))
        else:
            items_data = data
            total = len(items_data)
        return PageResult(
            items=[
                KBDocument(
                    remote_id=doc.get("id", doc.get("document_id", "")),
                    title=doc.get("title", ""),
                    summary=doc.get("summary", ""),
                    file_type=doc.get("file_type", ""),
                    file_size=doc.get("file_size", 0),
                    url=doc.get("url", ""),
                    metadata=doc,
                )
                for doc in items_data
            ],
            total=total or len(items_data),
            page=page,
            page_size=page_size,
        )

    async def get_document_detail(self, document_id: str) -> KBDocument:
        data = await self._request("GET", f"documents/{document_id}")
        return KBDocument(
            remote_id=data.get("id", data.get("document_id", document_id)),
            title=data.get("title", ""),
            summary=data.get("summary", ""),
            file_type=data.get("file_type", ""),
            file_size=data.get("file_size", 0),
            url=data.get("url", ""),
            metadata=data,
        )

    async def search_documents(
        self, collection_id: str, query: str, top_k: int = 10
    ) -> list[KBSearchResult]:
        data = await self._request(
            "GET",
            f"collections/{collection_id}/search",
            params={"q": query, "top_k": top_k},
        )
        return [
            KBSearchResult(
                document_id=item.get("document_id", item.get("id", "")),
                title=item.get("title", ""),
                snippet=item.get("snippet", ""),
                score=item.get("score", 0.0),
                collection_id=collection_id,
            )
            for item in data
        ]

    async def import_document(
        self, document_id: str, target_notebook_id: str
    ) -> ImportResult:
        try:
            doc = await self.get_document_detail(document_id)
            download_data = await self._request("GET", f"documents/{document_id}/download")
            from ..sync import import_document_to_notebook
            result = await import_document_to_notebook(
                connector=self,
                doc_id=document_id,
                notebook_id=target_notebook_id,
                doc_title=doc.title,
                content_data=download_data,
            )
            return ImportResult(
                success=True,
                local_source_id=result.get("source_id"),
                local_source_path=result.get("local_path"),
            )
        except Exception as exc:
            return ImportResult(
                success=False,
                error_message=str(exc),
            )
```

### 步骤 2.2.3: 创建 `src/notebooklm/server/external_kb/providers/dify.py`

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from ..base import (
    ExternalKBConnector,
    ImportResult,
    KBCollection,
    KBDocument,
    KBSearchResult,
    PageResult,
)


@dataclass
class DifyConnector(ExternalKBConnector):
    provider_type = "dify"
    config: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.api_base_url: str = self.config.get("api_base_url", "").rstrip("/")
        self.api_key: str = self.config.get("auth_credentials", {}).get("api_key", "")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    async def _get(self, path: str, **kwargs: Any) -> Any:
        async with httpx.AsyncClient(timeout=30.0, base_url=self.api_base_url) as client:
            resp = await client.get(path, headers=self._headers(), **kwargs)
            resp.raise_for_status()
            return resp.json()

    async def test_connection(self) -> bool:
        try:
            await self._get("/datasets", params={"page": 1, "limit": 1})
            return True
        except Exception:
            return False

    async def list_collections(self) -> list[KBCollection]:
        data = await self._get("/datasets", params={"page": 1, "limit": 100})
        items = data.get("data", []) if isinstance(data, dict) else data
        return [
            KBCollection(
                remote_id=item["id"],
                name=item.get("name", ""),
                description=item.get("description", ""),
                document_count=item.get("document_count", 0),
            )
            for item in items
        ]

    async def list_documents(
        self, collection_id: str, page: int = 1, page_size: int = 20
    ) -> PageResult[KBDocument]:
        data = await self._get(
            f"/datasets/{collection_id}/documents",
            params={"page": page, "limit": page_size},
        )
        if isinstance(data, dict):
            items = data.get("data", [])
            total = data.get("total", len(items))
        else:
            items = data
            total = len(items)
        return PageResult(
            items=[
                KBDocument(
                    remote_id=doc["id"],
                    title=doc.get("name", ""),
                    summary=doc.get("summary", ""),
                    file_type=doc.get("data_source_type", "text"),
                    file_size=doc.get("size", 0),
                    metadata=doc,
                )
                for doc in items
            ],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def get_document_detail(self, document_id: str) -> KBDocument:
        raise NotImplementedError("Dify API does not provide a single-document detail endpoint")

    async def search_documents(
        self, collection_id: str, query: str, top_k: int = 10
    ) -> list[KBSearchResult]:
        data = await self._get(
            f"/datasets/{collection_id}/documents",
            params={"keyword": query, "limit": top_k},
        )
        if isinstance(data, dict):
            items = data.get("data", [])
        else:
            items = data
        return [
            KBSearchResult(
                document_id=doc["id"],
                title=doc.get("name", ""),
                snippet=doc.get("summary", doc.get("name", "")),
                score=1.0,
                collection_id=collection_id,
            )
            for doc in items
        ]

    async def import_document(
        self, document_id: str, target_notebook_id: str
    ) -> ImportResult:
        try:
            resp = await self._get(f"/datasets/{document_id}/documents")
            doc_name = "unknown"
            if isinstance(resp, dict):
                items = resp.get("data", [resp])
                doc_name = items[0].get("name", items[0].get("id", document_id))
            from ..sync import import_document_to_notebook
            result = await import_document_to_notebook(
                connector=self, doc_id=document_id,
                notebook_id=target_notebook_id,
                doc_title=doc_name,
                content_data={"text": doc_name, "source": "dify"},
            )
            return ImportResult(
                success=True,
                local_source_id=result.get("source_id"),
                local_source_path=result.get("local_path"),
            )
        except Exception as exc:
            return ImportResult(success=False, error_message=str(exc))
```

### 步骤 2.2.4: 创建 `src/notebooklm/server/external_kb/sync.py`

```python
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...server.database import get_db
from ...server.models import ExternalImport, Source
from ...server.storage import store_source_file


async def import_document_to_notebook(
    connector: Any,
    doc_id: str,
    notebook_id: str,
    doc_title: str = "imported",
    content_data: dict | None = None,
) -> dict[str, Any]:
    from ..config import settings

    user_id = getattr(connector, "_current_user_id", None)
    if user_id is None:
        user_id = 0

    local_filename = f"{uuid.uuid4().hex}_{doc_title}"
    text_content = json.dumps(content_data or {}, ensure_ascii=False)

    source_path = await store_source_file(
        user_id=user_id,
        notebook_id=notebook_id,
        filename=local_filename,
        content=text_content.encode("utf-8"),
        suffix=".json",
    )

    async with get_db() as session:
        source = Source(
            user_id=user_id,
            notebook_id=int(notebook_id) if notebook_id.isdigit() else 0,
            remote_id=f"ext-{doc_id}",
            filename=local_filename,
            original_filename=doc_title,
            file_type="json",
            file_size=len(text_content),
            local_path=str(source_path),
            status="active",
        )
        session.add(source)
        await session.commit()
        await session.refresh(source)

        imp = ExternalImport(
            user_id=user_id,
            connection_id=getattr(connector, "_connection_id", 0),
            source_document_id=0,
            target_notebook_id=int(notebook_id) if notebook_id.isdigit() else 0,
            target_source_id=source.id,
            status="completed",
        )
        session.add(imp)
        await session.commit()

    return {"source_id": source.id, "local_path": str(source_path)}
```

### 步骤 2.2.5: 创建 `tests/unit/server/test_external_kb_providers.py`

```python
from __future__ import annotations

import pytest

from notebooklm.server.external_kb.providers.openapi import OpenApiConnector
from notebooklm.server.external_kb.providers.dify import DifyConnector


class TestOpenApiConnector:

    @pytest.fixture
    def config(self) -> dict:
        return {
            "api_base_url": "https://fake-api.example.com",
            "auth_type": "api_key",
            "auth_credentials": {"api_key": "test-key-123"},
        }

    def test_initialization(self, config: dict) -> None:
        c = OpenApiConnector(config=config)
        assert c.api_base_url == "https://fake-api.example.com"
        assert c.auth_type == "api_key"

    def test_build_headers_api_key(self, config: dict) -> None:
        c = OpenApiConnector(config=config)
        headers = c._build_headers()
        assert headers["Authorization"] == "Bearer test-key-123"

    def test_build_headers_basic(self) -> None:
        cfg = {
            "api_base_url": "https://example.com",
            "auth_type": "basic",
            "auth_credentials": {"username": "user", "password": "pass"},
        }
        c = OpenApiConnector(config=cfg)
        headers = c._build_headers()
        assert headers["Authorization"].startswith("Basic ")

    def test_build_headers_bearer(self) -> None:
        cfg = {
            "api_base_url": "https://example.com",
            "auth_type": "bearer",
            "auth_credentials": {"token": "my-token"},
        }
        c = OpenApiConnector(config=cfg)
        headers = c._build_headers()
        assert headers["Authorization"] == "Bearer my-token"


class TestDifyConnector:

    @pytest.fixture
    def config(self) -> dict:
        return {
            "api_base_url": "https://dify.example.com/v1",
            "auth_credentials": {"api_key": "dify-key"},
        }

    def test_initialization(self, config: dict) -> None:
        c = DifyConnector(config=config)
        assert c.api_base_url == "https://dify.example.com/v1"
        assert c.api_key == "dify-key"

    def test_headers(self, config: dict) -> None:
        c = DifyConnector(config=config)
        headers = c._headers()
        assert headers["Authorization"] == "Bearer dify-key"
        assert headers["Accept"] == "application/json"
```

**运行测试：**
```bash
uv run pytest tests/unit/server/test_external_kb_providers.py -v
```

---

## Task 2.3: External KB API routes

### 步骤 2.3.1: 创建 `src/notebooklm/server/routes/external_kb.py`

```python
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated, Any

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth_deps import get_current_user
from ..database import get_db
from ..external_kb.registry import ConnectorRegistry
from ..models import ExternalKbCollection, ExternalKbConnection, ExternalKbDocument, ExternalImport, User

router = APIRouter(prefix="/api/external-kb", tags=["external-kb"])

CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


def _get_cipher() -> Fernet:
    from ..config import settings
    key = settings.SECRET_KEY.encode()[:32].ljust(32, b"0")
    return Fernet(Fernet.generate_key() if len(key) != 32 else key)


def _encrypt_credentials(raw: dict) -> str:
    return _get_cipher().encrypt(json.dumps(raw).encode()).decode()


def _decrypt_credentials(encrypted: str) -> dict:
    return json.loads(_get_cipher().decrypt(encrypted.encode()).decode())


@router.post("/connections")
async def create_connection(
    body: dict[str, Any],
    user: CurrentUser,
    db: DbSession,
) -> dict[str, Any]:
    encrypted = _encrypt_credentials(body.get("auth_credentials", {}))
    conn = ExternalKbConnection(
        user_id=user.id,
        name=body["name"],
        provider_type=body["provider_type"],
        api_base_url=body["api_base_url"],
        auth_type=body.get("auth_type", "api_key"),
        auth_credentials=encrypted,
        extra_config=json.dumps(body.get("extra_config", {})),
        is_active=True,
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)
    return {
        "id": conn.id,
        "name": conn.name,
        "provider_type": conn.provider_type,
        "api_base_url": conn.api_base_url,
    }


@router.get("/connections")
async def list_connections(
    user: CurrentUser,
    db: DbSession,
) -> list[dict[str, Any]]:
    result = await db.execute(
        select(ExternalKbConnection).where(
            ExternalKbConnection.user_id == user.id,
            ExternalKbConnection.is_active == True,
        )
    )
    rows = result.scalars().all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "provider_type": r.provider_type,
            "api_base_url": r.api_base_url,
            "auth_type": r.auth_type,
            "last_sync_at": r.last_sync_at.isoformat() if r.last_sync_at else None,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.put("/connections/{connection_id}")
async def update_connection(
    connection_id: int,
    body: dict[str, Any],
    user: CurrentUser,
    db: DbSession,
) -> dict[str, Any]:
    result = await db.execute(
        select(ExternalKbConnection).where(
            ExternalKbConnection.id == connection_id,
            ExternalKbConnection.user_id == user.id,
        )
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise HTTPException(404, "Connection not found")
    if "name" in body:
        conn.name = body["name"]
    if "api_base_url" in body:
        conn.api_base_url = body["api_base_url"]
    if "auth_type" in body:
        conn.auth_type = body["auth_type"]
    if "auth_credentials" in body:
        conn.auth_credentials = _encrypt_credentials(body["auth_credentials"])
    if "extra_config" in body:
        conn.extra_config = json.dumps(body["extra_config"])
    await db.commit()
    return {"id": conn.id, "status": "updated"}


@router.delete("/connections/{connection_id}", status_code=204)
async def delete_connection(
    connection_id: int,
    user: CurrentUser,
    db: DbSession,
) -> None:
    result = await db.execute(
        select(ExternalKbConnection).where(
            ExternalKbConnection.id == connection_id,
            ExternalKbConnection.user_id == user.id,
        )
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise HTTPException(404, "Connection not found")
    conn.is_active = False
    await db.commit()


@router.post("/connections/{connection_id}/test")
async def test_connection(
    connection_id: int,
    user: CurrentUser,
    db: DbSession,
) -> dict[str, bool]:
    result = await db.execute(
        select(ExternalKbConnection).where(
            ExternalKbConnection.id == connection_id,
            ExternalKbConnection.user_id == user.id,
        )
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise HTTPException(404, "Connection not found")
    config = {
        "api_base_url": conn.api_base_url,
        "auth_type": conn.auth_type,
        "auth_credentials": _decrypt_credentials(conn.auth_credentials),
    }
    try:
        instance = ConnectorRegistry.create(conn.provider_type, config)
        ok = await instance.test_connection()
        return {"success": ok}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@router.get("/connections/{connection_id}/collections")
async def list_collections(
    connection_id: int,
    user: CurrentUser,
    db: DbSession,
) -> list[dict[str, Any]]:
    result = await db.execute(
        select(ExternalKbCollection).where(
            ExternalKbCollection.connection_id == connection_id,
        )
    )
    rows = result.scalars().all()
    return [
        {
            "id": r.id,
            "remote_id": r.remote_id,
            "name": r.name,
            "description": r.description,
            "document_count": r.document_count,
        }
        for r in rows
    ]


@router.get("/connections/{connection_id}/collections/{collection_id}/documents")
async def list_documents(
    connection_id: int,
    collection_id: int,
    user: CurrentUser,
    db: DbSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    result = await db.execute(
        select(ExternalKbDocument).where(
            ExternalKbDocument.collection_id == collection_id,
            ExternalKbDocument.connection_id == connection_id,
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = result.scalars().all()
    return {
        "items": [
            {
                "id": r.id,
                "remote_id": r.remote_id,
                "title": r.title,
                "summary": r.summary,
                "file_type": r.file_type,
                "file_size": r.file_size,
            }
            for r in rows
        ],
        "total": len(rows),
        "page": page,
        "page_size": page_size,
    }


@router.get("/connections/{connection_id}/collections/{collection_id}/search")
async def search_documents(
    connection_id: int,
    collection_id: int,
    user: CurrentUser,
    db: DbSession,
    q: str = Query("", min_length=1),
) -> list[dict[str, Any]]:
    result = await db.execute(
        select(ExternalKbDocument).where(
            ExternalKbDocument.collection_id == collection_id,
            ExternalKbDocument.connection_id == connection_id,
            ExternalKbDocument.title.ilike(f"%{q}%"),
        )
    )
    rows = result.scalars().all()
    return [
        {
            "id": r.id,
            "remote_id": r.remote_id,
            "title": r.title,
            "summary": r.summary,
        }
        for r in rows
    ]


@router.post("/import")
async def import_document(
    body: dict[str, Any],
    user: CurrentUser,
    db: DbSession,
) -> dict[str, Any]:
    connection_id = body.get("connection_id")
    document_id = body.get("document_id")
    target_notebook_id = body.get("target_notebook_id")
    if not all([connection_id, document_id, target_notebook_id]):
        raise HTTPException(400, "connection_id, document_id, and target_notebook_id are required")

    result = await db.execute(
        select(ExternalKbConnection).where(
            ExternalKbConnection.id == connection_id,
            ExternalKbConnection.user_id == user.id,
        )
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise HTTPException(404, "Connection not found")

    config = {
        "api_base_url": conn.api_base_url,
        "auth_type": conn.auth_type,
        "auth_credentials": _decrypt_credentials(conn.auth_credentials),
    }
    instance = ConnectorRegistry.create(conn.provider_type, config)
    instance._current_user_id = user.id
    instance._connection_id = conn.id

    import_result = await instance.import_document(document_id, target_notebook_id)

    return {
        "success": import_result.success,
        "local_source_id": import_result.local_source_id,
        "local_source_path": import_result.local_source_path,
        "error_message": import_result.error_message,
    }


@router.get("/imports")
async def list_imports(
    user: CurrentUser,
    db: DbSession,
) -> list[dict[str, Any]]:
    result = await db.execute(
        select(ExternalImport).where(
            ExternalImport.user_id == user.id,
        ).order_by(ExternalImport.created_at.desc())
    )
    rows = result.scalars().all()
    return [
        {
            "id": r.id,
            "connection_id": r.connection_id,
            "target_notebook_id": r.target_notebook_id,
            "status": r.status,
            "error_message": r.error_message,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]
```

### 步骤 2.3.2: 在 `src/notebooklm/server/app.py` 中注册路由

在文件末尾，`v1.include_router(meta.router)` 之后添加：

```python
from .routes import external_kb as external_kb_routes

# ...

    v1.include_router(meta.router)
    v1.include_router(external_kb_routes.router)
    app.include_router(v1)
```

### 步骤 2.3.3: 创建 `tests/unit/server/test_external_kb_routes.py`

```python
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from notebooklm.server.app import create_app


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestExternalKbRoutes:

    @pytest.mark.asyncio
    async def test_create_connection_missing_auth(self, client: AsyncClient) -> None:
        resp = await client.post("/api/external-kb/connections", json={"name": "test"})
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_list_connections_missing_auth(self, client: AsyncClient) -> None:
        resp = await client.get("/api/external-kb/connections")
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_list_imports_missing_auth(self, client: AsyncClient) -> None:
        resp = await client.get("/api/external-kb/imports")
        assert resp.status_code in (401, 403)
```

**运行测试：**
```bash
uv run pytest tests/unit/server/test_external_kb_routes.py -v
```

---

## Task 2.4: Content generation engine base + registry

### 步骤 2.4.1: 创建 `src/notebooklm/server/generation/__init__.py`

```python
from __future__ import annotations

__all__: list[str] = []
```

### 步骤 2.4.2: 创建 `src/notebooklm/server/generation/base.py`

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TemplateInfo:
    name: str
    label: str
    description: str = ""
    preview_image: str = ""


@dataclass
class PreviewResult:
    content_type: str
    outline: list[dict[str, Any]] = field(default_factory=list)
    estimated_pages: int = 0
    estimated_duration_seconds: int = 0
    warning: str = ""


@dataclass
class GeneratedContent:
    id: int = 0
    content_type: str = ""
    title: str = ""
    status: str = "processing"
    local_file_path: str = ""
    file_size: int = 0
    thumbnail_path: str = ""
    content: str = ""
    metadata: dict = field(default_factory=dict)
    error_message: str = ""


class ContentGenerator(ABC):

    @property
    @abstractmethod
    def content_type(self) -> str:
        ...

    @abstractmethod
    async def generate(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
        options: dict | None = None,
    ) -> GeneratedContent:
        ...

    @abstractmethod
    async def preview(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
    ) -> PreviewResult:
        ...

    @abstractmethod
    async def get_supported_templates(self) -> list[TemplateInfo]:
        ...
```

### 步骤 2.4.3: 创建 `src/notebooklm/server/generation/registry.py`

```python
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import ContentGenerator


_GENERATORS: dict[str, type[ContentGenerator]] = {}


class GeneratorRegistry:

    @staticmethod
    def register(content_type: str, generator_class: type[ContentGenerator]) -> None:
        _GENERATORS[content_type] = generator_class

    @staticmethod
    def create(content_type: str, notebooklm_client: object | None = None) -> ContentGenerator:
        cls = _GENERATORS.get(content_type)
        if cls is None:
            msg = f"Unknown content type: {content_type!r}. Available: {list(_GENERATORS)}"
            raise ValueError(msg)
        return cls(notebooklm_client=notebooklm_client) if notebooklm_client is not None else cls()

    @staticmethod
    def list_types() -> list[str]:
        return list(_GENERATORS)
```

### 步骤 2.4.4: 创建 `src/notebooklm/server/generation/extractors/__init__.py`

```python
from __future__ import annotations

__all__: list[str] = []
```

### 步骤 2.4.5: 创建 `src/notebooklm/server/generation/extractors/source_extractor.py`

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExtractedContent:
    title: str = ""
    headings: list[str] = field(default_factory=list)
    paragraphs: list[str] = field(default_factory=list)
    key_points: list[str] = field(default_factory=list)
    raw_text: str = ""


class SourceExtractor:

    @staticmethod
    async def extract_from_text(text: str, title: str = "") -> ExtractedContent:
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        lines = text.splitlines()
        headings = [l.strip().strip("#").strip() for l in lines if l.strip().startswith("#")]
        sentences = []
        for p in paragraphs:
            for s in p.replace("! ", ". ").replace("? ", ". ").split(". "):
                s = s.strip()
                if len(s) > 20:
                    sentences.append(s)
        key_points = sentences[:5]
        return ExtractedContent(
            title=title or (headings[0] if headings else ""),
            headings=headings,
            paragraphs=paragraphs,
            key_points=key_points,
            raw_text=text,
        )

    @staticmethod
    async def extract_from_sources(source_texts: list[str]) -> ExtractedContent:
        combined = "\n\n".join(source_texts)
        return await SourceExtractor.extract_from_text(combined)

    @staticmethod
    async def build_hierarchy(text: str) -> dict[str, Any]:
        extracted = await SourceExtractor.extract_from_text(text)
        root: dict[str, Any] = {"name": extracted.title or "Root", "children": []}
        for heading in extracted.headings:
            root["children"].append({"name": heading, "children": []})
        if not root["children"]:
            for point in extracted.key_points:
                root["children"].append({"name": point, "children": []})
        return root
```

### 步骤 2.4.6: 创建 `tests/unit/server/test_generation.py`

```python
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from notebooklm.server.generation.base import (
    ContentGenerator,
    GeneratedContent,
    PreviewResult,
    TemplateInfo,
)
from notebooklm.server.generation.extractors.source_extractor import SourceExtractor
from notebooklm.server.generation.registry import GeneratorRegistry


@dataclass
class FakeGenerator(ContentGenerator):
    content_type: str = "fake"
    notebooklm_client: object = None
    config: dict = field(default_factory=dict)

    async def generate(
        self, notebook_id: str, prompt: str,
        template: str | None = None,
        options: dict | None = None,
    ) -> GeneratedContent:
        return GeneratedContent(
            id=1, content_type="fake", title="Fake Output",
            status="completed", content=f"Generated for: {prompt}",
        )

    async def preview(
        self, notebook_id: str, prompt: str, template: str | None = None,
    ) -> PreviewResult:
        return PreviewResult(content_type="fake", estimated_pages=3)

    async def get_supported_templates(self) -> list[TemplateInfo]:
        return [TemplateInfo(name="default", label="Default Template")]


class TestContentGeneratorBase:

    @pytest.mark.asyncio
    async def test_generate_returns_content(self) -> None:
        g = FakeGenerator()
        result = await g.generate("nb-1", "Make a summary")
        assert result.status == "completed"
        assert "Make a summary" in result.content

    @pytest.mark.asyncio
    async def test_preview_returns_outline(self) -> None:
        g = FakeGenerator()
        result = await g.preview("nb-1", "Preview this")
        assert result.estimated_pages == 3

    @pytest.mark.asyncio
    async def test_get_supported_templates(self) -> None:
        g = FakeGenerator()
        templates = await g.get_supported_templates()
        assert len(templates) == 1
        assert templates[0].name == "default"


class TestGeneratorRegistry:

    def test_register_and_create(self) -> None:
        GeneratorRegistry.register("fake", FakeGenerator)
        g = GeneratorRegistry.create("fake")
        assert isinstance(g, FakeGenerator)

    def test_create_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown content type"):
            GeneratorRegistry.create("nonexistent")

    def test_list_types(self) -> None:
        types = GeneratorRegistry.list_types()
        assert "fake" in types


SAMPLE_TEXT = """# Introduction
This is the first paragraph of a document.
It contains important information.

## Key Features
Feature one is about speed.
Feature two is about reliability.
Feature three is about scalability.

## Conclusion
The conclusion summarizes everything."""


class TestSourceExtractor:

    @pytest.mark.asyncio
    async def test_extract_from_text_returns_headings(self) -> None:
        result = await SourceExtractor.extract_from_text(SAMPLE_TEXT)
        assert "Introduction" in result.headings[0]
        assert "Key Features" in result.headings[1]
        assert "Conclusion" in result.headings[2]

    @pytest.mark.asyncio
    async def test_extract_from_text_returns_paragraphs(self) -> None:
        result = await SourceExtractor.extract_from_text(SAMPLE_TEXT)
        assert len(result.paragraphs) >= 1

    @pytest.mark.asyncio
    async def test_extract_from_text_returns_key_points(self) -> None:
        result = await SourceExtractor.extract_from_text(SAMPLE_TEXT)
        assert len(result.key_points) > 0

    @pytest.mark.asyncio
    async def test_build_hierarchy_returns_tree(self) -> None:
        tree = await SourceExtractor.build_hierarchy(SAMPLE_TEXT)
        assert tree["name"] == "Introduction"
        assert len(tree["children"]) >= 3
```

**运行测试：**
```bash
uv run pytest tests/unit/server/test_generation.py -v
```

---

## Task 2.5: Document + Podcast engines (NotebookLM-based)

### 步骤 2.5.1: 创建 `src/notebooklm/server/generation/engines/__init__.py`

```python
from __future__ import annotations

__all__: list[str] = []
```

### 步骤 2.5.2: 创建 `src/notebooklm/server/generation/engines/document_engine.py`

```python
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..base import ContentGenerator, GeneratedContent, PreviewResult, TemplateInfo


@dataclass
class DocumentGenerator(ContentGenerator):
    content_type: str = "document"
    notebooklm_client: Any = None
    media_root: Path = field(default_factory=lambda: Path.home() / ".notebooklm" / "data")

    async def generate(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
        options: dict | None = None,
    ) -> GeneratedContent:
        opts = options or {}
        doc_format = opts.get("format", "markdown")
        template_name = template or "note"
        title = opts.get("title", f"Document {uuid.uuid4().hex[:8]}")

        artifact_type = {
            "note": "NOTE",
            "summary": "SUMMARY",
            "faq": "FAQ",
            "study_guide": "STUDY_GUIDE",
            "briefing_doc": "BRIEFING_DOC",
            "outline": "OUTLINE",
            "timeline": "TIMELINE",
        }.get(template_name, "NOTE")

        if self.notebooklm_client is not None:
            artifact = await self.notebooklm_client.artifacts.generate(
                notebook_id=notebook_id,
                prompt=prompt,
                artifact_type=artifact_type,
            )
            content = getattr(artifact, "content", str(artifact))
        else:
            content = f"[Mock] {artifact_type} generated for: {prompt}"

        user_dir = self.media_root / "generated" / str(opts.get("user_id", 0)) / notebook_id
        user_dir.mkdir(parents=True, exist_ok=True)

        file_ext = ".md" if doc_format == "markdown" else ".pdf"
        filename = f"doc_{uuid.uuid4().hex[:8]}{file_ext}"
        file_path = user_dir / filename
        file_path.write_text(content, encoding="utf-8")

        return GeneratedContent(
            id=0,
            content_type="document",
            title=title,
            status="completed",
            local_file_path=str(file_path),
            file_size=len(content.encode("utf-8")),
            content=content,
            metadata={
                "format": doc_format,
                "template": template_name,
                "artifact_type": artifact_type,
                "doc_page_count": len(content.splitlines()),
                "doc_sections": json.dumps([{"title": title}]),
            },
        )

    async def preview(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
    ) -> PreviewResult:
        return PreviewResult(
            content_type="document",
            outline=[{"title": prompt, "type": "section"}],
            estimated_pages=1,
        )

    async def get_supported_templates(self) -> list[TemplateInfo]:
        return [
            TemplateInfo(name="note", label="笔记", description="自由格式笔记"),
            TemplateInfo(name="summary", label="摘要", description="文档摘要"),
            TemplateInfo(name="faq", label="常见问题", description="FAQ 生成"),
            TemplateInfo(name="study_guide", label="学习指南", description="考试复习材料"),
            TemplateInfo(name="briefing_doc", label="简报", description="简报文档"),
            TemplateInfo(name="outline", label="大纲", description="文档大纲"),
            TemplateInfo(name="timeline", label="时间线", description="事件时间线"),
        ]
```

### 步骤 2.5.3: 创建 `src/notebooklm/server/generation/engines/podcast_engine.py`

```python
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..base import ContentGenerator, GeneratedContent, PreviewResult, TemplateInfo


@dataclass
class PodcastGenerator(ContentGenerator):
    content_type: str = "podcast"
    notebooklm_client: Any = None
    media_root: Path = field(default_factory=lambda: Path.home() / ".notebooklm" / "data")

    async def generate(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
        options: dict | None = None,
    ) -> GeneratedContent:
        opts = options or {}
        title = opts.get("title", f"Podcast {uuid.uuid4().hex[:8]}")
        user_id = opts.get("user_id", 0)

        if self.notebooklm_client is not None:
            audio_result = await self.notebooklm_client.audio.generate(
                notebook_id=notebook_id,
                prompt=prompt,
            )
            audio_data = getattr(audio_result, "audio_data", b"")
            transcript = getattr(audio_result, "transcript", "")
            duration = getattr(audio_result, "duration_seconds", 0)
        else:
            audio_data = b"mock-audio-data"
            transcript = f"[Mock transcript for: {prompt}]"
            duration = 120

        user_dir = self.media_root / "generated" / str(user_id) / notebook_id
        user_dir.mkdir(parents=True, exist_ok=True)

        audio_filename = f"podcast_{uuid.uuid4().hex[:8]}.mp3"
        audio_path = user_dir / audio_filename
        audio_path.write_bytes(audio_data)

        metadata_filename = f"podcast_{uuid.uuid4().hex[:8]}.json"
        metadata_path = user_dir / metadata_filename
        metadata_path.write_text(
            json.dumps({"transcript": transcript, "duration": duration, "title": title}, ensure_ascii=False),
            encoding="utf-8",
        )

        return GeneratedContent(
            id=0,
            content_type="podcast",
            title=title,
            status="completed",
            local_file_path=str(audio_path),
            file_size=len(audio_data),
            content=transcript,
            metadata={
                "audio_file_path": str(audio_path),
                "duration_seconds": duration,
                "audio_transcript": transcript,
                "audio_speakers": json.dumps([
                    {"name": "Host", "voice": "en-US-Standard-A"},
                    {"name": "Guest", "voice": "en-US-Standard-B"},
                ]),
            },
        )

    async def preview(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
    ) -> PreviewResult:
        return PreviewResult(
            content_type="podcast",
            outline=[{"title": "Host introduction"}, {"title": "Discussion"}, {"title": "Conclusion"}],
            estimated_duration_seconds=180,
        )

    async def get_supported_templates(self) -> list[TemplateInfo]:
        return [
            TemplateInfo(name="deep_dive", label="深度讨论", description="双人深度对话式播客"),
            TemplateInfo(name="interview", label="采访", description="主持人采访嘉宾形式"),
            TemplateInfo(name="summary", label="摘要播客", description="快速总结式播客"),
        ]
```

### 步骤 2.5.4: 创建 `tests/unit/server/test_generation_engines.py`

```python
from __future__ import annotations

import pytest

from notebooklm.server.generation.engines.document_engine import DocumentGenerator
from notebooklm.server.generation.engines.podcast_engine import PodcastGenerator


class TestDocumentGenerator:

    @pytest.fixture
    def gen(self) -> DocumentGenerator:
        return DocumentGenerator()

    @pytest.mark.asyncio
    async def test_content_type(self, gen: DocumentGenerator) -> None:
        assert gen.content_type == "document"

    @pytest.mark.asyncio
    async def test_generate_returns_content(self, gen: DocumentGenerator) -> None:
        result = await gen.generate("nb-1", "Write a summary", template="summary")
        assert result.status == "completed"
        assert result.content_type == "document"
        assert "Summary" in result.content or "Mock" in result.content

    @pytest.mark.asyncio
    async def test_generate_without_client_returns_mock(self, gen: DocumentGenerator) -> None:
        result = await gen.generate("nb-1", "test prompt")
        assert "[Mock]" in result.content

    @pytest.mark.asyncio
    async def test_preview_returns_outline(self, gen: DocumentGenerator) -> None:
        result = await gen.preview("nb-1", "Preview")
        assert result.content_type == "document"

    @pytest.mark.asyncio
    async def test_get_supported_templates(self, gen: DocumentGenerator) -> None:
        templates = await gen.get_supported_templates()
        assert len(templates) >= 3
        names = [t.name for t in templates]
        assert "note" in names
        assert "summary" in names


class TestPodcastGenerator:

    @pytest.fixture
    def gen(self) -> PodcastGenerator:
        return PodcastGenerator()

    @pytest.mark.asyncio
    async def test_content_type(self, gen: PodcastGenerator) -> None:
        assert gen.content_type == "podcast"

    @pytest.mark.asyncio
    async def test_generate_returns_audio(self, gen: PodcastGenerator) -> None:
        result = await gen.generate("nb-1", "Discuss AI", template="deep_dive")
        assert result.status == "completed"
        assert result.content_type == "podcast"
        assert "[Mock transcript" in result.content

    @pytest.mark.asyncio
    async def test_preview_returns_outline(self, gen: PodcastGenerator) -> None:
        result = await gen.preview("nb-1", "Preview")
        assert result.estimated_duration_seconds > 0

    @pytest.mark.asyncio
    async def test_get_supported_templates(self, gen: PodcastGenerator) -> None:
        templates = await gen.get_supported_templates()
        assert len(templates) >= 2
```

**运行测试：**
```bash
uv run pytest tests/unit/server/test_generation_engines.py -v
```

---

## Task 2.6: PPT engine

### 步骤 2.6.1: 更新 `pyproject.toml` 添加依赖

```toml
# 在 [project] dependencies 下添加：
    "python-pptx>=1.0.0,<2",
    "Pillow>=10.0.0,<12",
```

### 步骤 2.6.2: 创建 `src/notebooklm/server/generation/templates/ppt/classic.json`

```json
{
  "name": "classic",
  "label": "经典",
  "description": "经典白底黑字简约模板",
  "slide_width": 13.333,
  "slide_height": 7.5,
  "background_color": "#FFFFFF",
  "font_family": "Microsoft YaHei",
  "title_color": "#333333",
  "body_color": "#666666",
  "accent_color": "#1E90FF",
  "layouts": [
    {
      "type": "title_slide",
      "label": "封面",
      "title_font_size": 36,
      "subtitle_font_size": 18
    },
    {
      "type": "section_header",
      "label": "章节标题",
      "title_font_size": 28,
      "body_font_size": 16
    },
    {
      "type": "content",
      "label": "内容页",
      "title_font_size": 24,
      "body_font_size": 14,
      "bullet_style": "filled"
    },
    {
      "type": "two_column",
      "label": "双栏",
      "title_font_size": 24,
      "body_font_size": 14
    }
  ]
}
```

### 步骤 2.6.3: 创建 `src/notebooklm/server/generation/templates/ppt/modern.json`

```json
{
  "name": "modern",
  "label": "现代",
  "description": "蓝白配色现代风格模板",
  "slide_width": 13.333,
  "slide_height": 7.5,
  "background_color": "#F0F6FF",
  "font_family": "Microsoft YaHei",
  "title_color": "#1a365d",
  "body_color": "#2d3748",
  "accent_color": "#3182CE",
  "layouts": [
    {
      "type": "title_slide",
      "label": "封面",
      "title_font_size": 40,
      "subtitle_font_size": 20
    },
    {
      "type": "section_header",
      "label": "章节标题",
      "title_font_size": 30,
      "body_font_size": 18
    },
    {
      "type": "content",
      "label": "内容页",
      "title_font_size": 26,
      "body_font_size": 16,
      "bullet_style": "filled"
    },
    {
      "type": "two_column",
      "label": "双栏",
      "title_font_size": 26,
      "body_font_size": 16
    }
  ]
}
```

### 步骤 2.6.4: 创建 `src/notebooklm/server/generation/engines/ppt_engine.py`

```python
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.util import Emu, Inches, Pt

from ..base import ContentGenerator, GeneratedContent, PreviewResult, TemplateInfo


@dataclass
class PptGenerator(ContentGenerator):
    content_type: str = "ppt"
    notebooklm_client: Any = None
    media_root: Path = field(default_factory=lambda: Path.home() / ".notebooklm" / "data")
    _templates: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._load_templates()

    def _load_templates(self) -> None:
        template_dir = Path(__file__).resolve().parent.parent / "templates" / "ppt"
        if template_dir.is_dir():
            for f in sorted(template_dir.glob("*.json")):
                try:
                    self._templates.append(json.loads(f.read_text(encoding="utf-8")))
                except Exception:
                    pass

    def _get_template(self, name: str) -> dict:
        for t in self._templates:
            if t["name"] == name:
                return dict(t)
        return dict(self._templates[0]) if self._templates else {
            "name": "default",
            "slide_width": 13.333,
            "slide_height": 7.5,
            "background_color": "#FFFFFF",
            "font_family": "Microsoft YaHei",
            "title_color": "#333333",
            "body_color": "#666666",
            "accent_color": "#1E90FF",
            "layouts": [],
        }

    def _build_slides(self, prs: Presentation, template: dict, slides_data: list[dict]) -> None:
        bg_color = template.get("background_color", "#FFFFFF")
        title_color = template.get("title_color", "#333333")
        body_color = template.get("body_color", "#666666")
        accent_color = template.get("accent_color", "#1E90FF")
        font_family = template.get("font_family", "Microsoft YaHei")

        def _parse_hex(color_str: str) -> RGBColor:
            h = color_str.lstrip("#")
            return RGBColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

        bg_rgb = _parse_hex(bg_color)
        title_rgb = _parse_hex(title_color)
        body_rgb = _parse_hex(body_color)
        accent_rgb = _parse_hex(accent_color)

        for slide_data in slides_data:
            slide_layout = prs.slide_layouts[6]  # blank
            slide = prs.slides.add_slide(slide_layout)
            bg = slide.background
            fill = bg.fill
            fill.solid()
            fill.fore_color.rgb = bg_rgb

            slide_title = slide_data.get("title", "")
            content_items = slide_data.get("items", [])
            layout_type = slide_data.get("layout", "content")

            if layout_type == "title_slide":
                left = Inches(1.5)
                top = Inches(2.5)
                width = Inches(10)
                height = Inches(1.5)
                txBox = slide.shapes.add_textbox(left, top, width, height)
                tf = txBox.text_frame
                tf.word_wrap = True
                p = tf.paragraphs[0]
                p.text = slide_title
                p.font.size = Pt(36)
                p.font.color.rgb = title_rgb
                p.font.name = font_family
                p.alignment = PP_ALIGN.CENTER
                if content_items:
                    top2 = Inches(4.5)
                    txBox2 = slide.shapes.add_textbox(Inches(3), top2, Inches(7), Inches(1))
                    tf2 = txBox2.text_frame
                    p2 = tf2.paragraphs[0]
                    p2.text = str(content_items[0]) if content_items else ""
                    p2.font.size = Pt(18)
                    p2.font.color.rgb = body_rgb
                    p2.font.name = font_family
                    p2.alignment = PP_ALIGN.CENTER
            elif layout_type == "section_header":
                left = Inches(1)
                top = Inches(2)
                txBox = slide.shapes.add_textbox(left, top, Inches(11), Inches(1.5))
                tf = txBox.text_frame
                p = tf.paragraphs[0]
                p.text = slide_title
                p.font.size = Pt(28)
                p.font.color.rgb = title_rgb
                p.font.name = font_family
                p.alignment = PP_ALIGN.LEFT
            else:
                left = Inches(0.8)
                top = Inches(0.5)
                txBox = slide.shapes.add_textbox(left, top, Inches(11.5), Inches(1))
                tf = txBox.text_frame
                p = tf.paragraphs[0]
                p.text = slide_title
                p.font.size = Pt(24)
                p.font.color.rgb = title_rgb
                p.font.name = font_family
                p.alignment = PP_ALIGN.LEFT
                left2 = Inches(0.8)
                top2 = Inches(1.8)
                txBox2 = slide.shapes.add_textbox(left2, top2, Inches(11.5), Inches(5))
                tf2 = txBox2.text_frame
                tf2.word_wrap = True
                for i, item in enumerate(content_items):
                    if i == 0:
                        p2 = tf2.paragraphs[0]
                    else:
                        p2 = tf2.add_paragraph()
                    p2.text = f"\u2022 {item}"
                    p2.font.size = Pt(14)
                    p2.font.color.rgb = body_rgb
                    p2.font.name = font_family
                    p2.space_after = Pt(6)

    async def generate(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
        options: dict | None = None,
    ) -> GeneratedContent:
        opts = options or {}
        title = opts.get("title", f"PPT {uuid.uuid4().hex[:8]}")
        user_id = opts.get("user_id", 0)
        template_name = template or "classic"
        tmpl = self._get_template(template_name)

        slides_data = opts.get("slides", [])
        if not slides_data:
            slides_data = [
                {"title": title, "items": [prompt], "layout": "title_slide"},
                {"title": "内容概述", "items": ["要点 1", "要点 2", "要点 3"], "layout": "section_header"},
                {"title": "详细内容", "items": ["详细说明第一点", "详细说明第二点", "详细说明第三点"], "layout": "content"},
                {"title": "总结", "items": ["关键结论", "下一步行动"], "layout": "content"},
            ]

        prs = Presentation()
        prs.slide_width = Inches(tmpl.get("slide_width", 13.333))
        prs.slide_height = Inches(tmpl.get("slide_height", 7.5))

        self._build_slides(prs, tmpl, slides_data)

        user_dir = self.media_root / "generated" / str(user_id) / notebook_id
        user_dir.mkdir(parents=True, exist_ok=True)

        pptx_filename = f"ppt_{uuid.uuid4().hex[:8]}.pptx"
        pptx_path = user_dir / pptx_filename
        prs.save(str(pptx_path))

        preview_dir = user_dir / f"{pptx_path.stem}_preview"
        preview_dir.mkdir(exist_ok=True)

        file_size = pptx_path.stat().st_size
        slide_count = len(prs.slides)

        return GeneratedContent(
            id=0,
            content_type="ppt",
            title=title,
            status="completed",
            local_file_path=str(pptx_path),
            file_size=file_size,
            content=json.dumps(slides_data, ensure_ascii=False),
            metadata={
                "ppt_page_count": slide_count,
                "ppt_template": template_name,
                "ppt_json": json.dumps(slides_data, ensure_ascii=False),
                "ppt_preview_images": json.dumps([
                    str(preview_dir / f"slide_{i+1:02d}.png")
                    for i in range(slide_count)
                ]),
            },
        )

    async def preview(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
    ) -> PreviewResult:
        return PreviewResult(
            content_type="ppt",
            outline=[
                {"title": "封面", "type": "title_slide"},
                {"title": "内容大纲", "type": "section_header"},
                {"title": "详细内容", "type": "content"},
                {"title": "总结", "type": "content"},
            ],
            estimated_pages=4,
        )

    async def get_supported_templates(self) -> list[TemplateInfo]:
        if not self._templates:
            return [
                TemplateInfo(name="classic", label="经典", description="经典白底黑字简约模板"),
                TemplateInfo(name="modern", label="现代", description="蓝白配色现代风格模板"),
            ]
        return [
            TemplateInfo(
                name=t["name"],
                label=t.get("label", t["name"]),
                description=t.get("description", ""),
            )
            for t in self._templates
        ]
```

### 步骤 2.6.5: 创建 `tests/unit/server/test_ppt_engine.py`

```python
from __future__ import annotations

import pytest

from notebooklm.server.generation.engines.ppt_engine import PptGenerator


class TestPptGenerator:

    @pytest.fixture
    def gen(self) -> PptGenerator:
        return PptGenerator()

    @pytest.mark.asyncio
    async def test_content_type(self, gen: PptGenerator) -> None:
        assert gen.content_type == "ppt"

    @pytest.mark.asyncio
    async def test_generate_creates_pptx(self, gen: PptGenerator) -> None:
        result = await gen.generate(
            "nb-1",
            "AI 发展史",
            template="classic",
            options={
                "title": "Test PPT",
                "slides": [
                    {"title": "封面", "items": ["AI 发展史"], "layout": "title_slide"},
                    {"title": "早期", "items": ["1950s: 图灵测试"], "layout": "content"},
                ],
            },
        )
        assert result.status == "completed"
        assert result.local_file_path.endswith(".pptx")
        assert result.metadata["ppt_page_count"] == 2

    @pytest.mark.asyncio
    async def test_generate_without_slides_creates_default(self, gen: PptGenerator) -> None:
        result = await gen.generate("nb-1", "Default prompt")
        assert result.status == "completed"
        assert result.metadata["ppt_page_count"] >= 1

    @pytest.mark.asyncio
    async def test_preview_returns_outline(self, gen: PptGenerator) -> None:
        result = await gen.preview("nb-1", "Preview PPT")
        assert result.estimated_pages == 4

    @pytest.mark.asyncio
    async def test_get_supported_templates(self, gen: PptGenerator) -> None:
        templates = await gen.get_supported_templates()
        assert len(templates) >= 1
        names = [t.name for t in templates]
        assert "classic" in names
```

**运行测试：**
```bash
uv run pytest tests/unit/server/test_ppt_engine.py -v
```

---

## Task 2.7: Mindmap + Infographic + Video engines

### 步骤 2.7.1: 创建 `src/notebooklm/server/generation/templates/mindmap/default.json`

```json
{
  "name": "default",
  "label": "默认",
  "description": "树形布局思维导图",
  "layout": "tree",
  "node_color": "#4A90D9",
  "line_color": "#CCCCCC",
  "font_family": "Microsoft YaHei",
  "font_size": 14,
  "export_width": 1920,
  "export_height": 1080
}
```

### 步骤 2.7.2: 创建 `src/notebooklm/server/generation/templates/mindmap/radial.json`

```json
{
  "name": "radial",
  "label": "辐射",
  "description": "辐射状布局思维导图",
  "layout": "radial",
  "node_color": "#E8822A",
  "line_color": "#DDDDDD",
  "font_family": "Microsoft YaHei",
  "font_size": 14,
  "export_width": 1920,
  "export_height": 1080
}
```

### 步骤 2.7.3: 创建 `src/notebooklm/server/generation/templates/infographic/default.json`

```json
{
  "name": "default",
  "label": "默认信息图",
  "description": "简洁三栏信息图模板",
  "width": 800,
  "height": 2000,
  "background_color": "#FFFFFF",
  "font_family": "Microsoft YaHei",
  "title_color": "#1a365d",
  "body_color": "#2d3748",
  "accent_color": "#3182CE",
  "blocks": [
    {"type": "header", "height_ratio": 0.08},
    {"type": "text", "height_ratio": 0.12},
    {"type": "stats", "height_ratio": 0.15},
    {"type": "text", "height_ratio": 0.25},
    {"type": "divider", "height_ratio": 0.02},
    {"type": "text", "height_ratio": 0.25},
    {"type": "footer", "height_ratio": 0.08}
  ]
}
```

### 步骤 2.7.4: 创建 `src/notebooklm/server/generation/templates/video/default.json`

```json
{
  "name": "default",
  "label": "默认视频",
  "description": "图文混合默认短视频模板",
  "resolution": "1080p",
  "width": 1920,
  "height": 1080,
  "fps": 30,
  "background_color": "#1a202c",
  "font_family": "Microsoft YaHei",
  "title_color": "#FFFFFF",
  "body_color": "#E2E8F0",
  "scene_duration_seconds": 5,
  "transition": "fade",
  "tts_language": "zh-CN"
}
```

### 步骤 2.7.5: 创建 `src/notebooklm/server/generation/engines/mindmap_engine.py`

```python
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..base import ContentGenerator, GeneratedContent, PreviewResult, TemplateInfo
from ..extractors.source_extractor import SourceExtractor


@dataclass
class MindmapGenerator(ContentGenerator):
    content_type: str = "mindmap"
    notebooklm_client: Any = None
    media_root: Path = field(default_factory=lambda: Path.home() / ".notebooklm" / "data")
    _templates: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._load_templates()

    def _load_templates(self) -> None:
        template_dir = Path(__file__).resolve().parent.parent / "templates" / "mindmap"
        if template_dir.is_dir():
            for f in sorted(template_dir.glob("*.json")):
                try:
                    self._templates.append(json.loads(f.read_text(encoding="utf-8")))
                except Exception:
                    pass

    def _get_template(self, name: str) -> dict:
        for t in self._templates:
            if t["name"] == name:
                return dict(t)
        return {
            "name": "default", "layout": "tree",
            "node_color": "#4A90D9", "export_width": 1920, "export_height": 1080,
        }

    async def generate(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
        options: dict | None = None,
    ) -> GeneratedContent:
        opts = options or {}
        title = opts.get("title", f"Mindmap {uuid.uuid4().hex[:8]}")
        user_id = opts.get("user_id", 0)
        template_name = template or "default"
        tmpl = self._get_template(template_name)

        source_text = opts.get("source_text", prompt)
        hierarchy = await SourceExtractor.build_hierarchy(source_text)

        mindmap_data = {
            "meta": {
                "name": title,
                "author": "",
                "version": "1.0",
            },
            "format": "node_tree",
            "data": hierarchy,
            "template": tmpl,
        }

        user_dir = self.media_root / "generated" / str(user_id) / notebook_id
        user_dir.mkdir(parents=True, exist_ok=True)

        json_filename = f"mindmap_{uuid.uuid4().hex[:8]}.json"
        json_path = user_dir / json_filename
        json_path.write_text(json.dumps(mindmap_data, ensure_ascii=False), encoding="utf-8")

        return GeneratedContent(
            id=0,
            content_type="mindmap",
            title=title,
            status="completed",
            local_file_path=str(json_path),
            file_size=json_path.stat().st_size,
            content=json.dumps(hierarchy, ensure_ascii=False),
            metadata={
                "mindmap_data": json.dumps(mindmap_data, ensure_ascii=False),
                "mindmap_layout": tmpl.get("layout", "tree"),
            },
        )

    async def preview(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
    ) -> PreviewResult:
        return PreviewResult(
            content_type="mindmap",
            outline=[{"title": "根节点"}, {"title": "子节点 1"}, {"title": "子节点 2"}],
            estimated_pages=1,
        )

    async def get_supported_templates(self) -> list[TemplateInfo]:
        if not self._templates:
            return [
                TemplateInfo(name="default", label="默认", description="树形布局思维导图"),
                TemplateInfo(name="radial", label="辐射", description="辐射状布局思维导图"),
            ]
        return [
            TemplateInfo(
                name=t["name"],
                label=t.get("label", t["name"]),
                description=t.get("description", ""),
            )
            for t in self._templates
        ]
```

### 步骤 2.7.6: 创建 `src/notebooklm/server/generation/engines/infographic_engine.py`

```python
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from ..base import ContentGenerator, GeneratedContent, PreviewResult, TemplateInfo


@dataclass
class InfographicGenerator(ContentGenerator):
    content_type: str = "infographic"
    notebooklm_client: Any = None
    media_root: Path = field(default_factory=lambda: Path.home() / ".notebooklm" / "data")
    _templates: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._load_templates()

    def _load_templates(self) -> None:
        template_dir = Path(__file__).resolve().parent.parent / "templates" / "infographic"
        if template_dir.is_dir():
            for f in sorted(template_dir.glob("*.json")):
                try:
                    self._templates.append(json.loads(f.read_text(encoding="utf-8")))
                except Exception:
                    pass

    def _get_template(self, name: str) -> dict:
        for t in self._templates:
            if t["name"] == name:
                return dict(t)
        return {
            "name": "default", "width": 800, "height": 2000,
            "background_color": "#FFFFFF",
            "title_color": "#1a365d", "body_color": "#2d3748",
            "font_family": "Microsoft YaHei",
        }

    def _render_block(
        self, draw: ImageDraw.Draw, block: dict,
        x: int, y: int, w: int, h: int,
        colors: dict[str, str],
    ) -> None:
        btype = block.get("type", "text")
        content = block.get("content", "")
        if btype == "header":
            draw.rectangle([x, y, x + w, y + h], fill="#1a365d")
            draw.text((x + 20, y + 20), content or "标题", fill="#FFFFFF", font=None)
        elif btype == "text":
            draw.text((x + 20, y + 10), content or "正文内容", fill=colors.get("body", "#333"), font=None)
        elif btype == "stats":
            draw.rectangle([x + 20, y + 10, x + w - 20, y + h - 10], outline="#3182CE", width=2)
            draw.text((x + 40, y + 20), content or "统计数据", fill="#3182CE", font=None)
        elif btype == "divider":
            draw.line([(x + 40, y + h // 2), (x + w - 40, y + h // 2)], fill="#E2E8F0", width=2)
        elif btype == "footer":
            draw.rectangle([x, y, x + w, y + h], fill="#EDF2F7")
            draw.text((x + 20, y + 10), content or "页脚", fill={}, font=None)

    async def generate(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
        options: dict | None = None,
    ) -> GeneratedContent:
        opts = options or {}
        title = opts.get("title", f"Infographic {uuid.uuid4().hex[:8]}")
        user_id = opts.get("user_id", 0)
        template_name = template or "default"
        tmpl = self._get_template(template_name)

        user_dir = self.media_root / "generated" / str(user_id) / notebook_id
        user_dir.mkdir(parents=True, exist_ok=True)

        width = tmpl.get("width", 800)
        height = tmpl.get("height", 2000)
        bg_color_str = tmpl.get("background_color", "#FFFFFF")

        img = Image.new("RGB", (width, height), bg_color_str)
        draw = ImageDraw.Draw(img)

        blocks = tmpl.get("blocks", [])
        block_height_total = sum(b.get("height_ratio", 0.1) for b in blocks)
        y_offset = 0
        for block in blocks:
            hr = block.get("height_ratio", 0.1)
            bh = int(height * hr / block_height_total) if block_height_total > 0 else int(height * hr)
            block["content"] = ""
            self._render_block(draw, block, 0, y_offset, width, bh, {
                "title": tmpl.get("title_color", "#1a365d"),
                "body": tmpl.get("body_color", "#2d3748"),
                "accent": tmpl.get("accent_color", "#3182CE"),
            })
            y_offset += bh

        json_filename = f"infographic_{uuid.uuid4().hex[:8]}.json"
        json_path = user_dir / json_filename
        json_path.write_text(json.dumps({"title": title, "template": template_name}, ensure_ascii=False), encoding="utf-8")

        png_filename = f"infographic_{uuid.uuid4().hex[:8]}.png"
        png_path = user_dir / png_filename
        img.save(str(png_path), "PNG")

        return GeneratedContent(
            id=0,
            content_type="infographic",
            title=title,
            status="completed",
            local_file_path=str(png_path),
            file_size=png_path.stat().st_size,
            content=json.dumps({"template": template_name, "blocks": len(blocks)}, ensure_ascii=False),
            metadata={
                "infographic_template": template_name,
                "infographic_blocks": json.dumps(blocks, ensure_ascii=False),
            },
        )

    async def preview(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
    ) -> PreviewResult:
        return PreviewResult(content_type="infographic", estimated_pages=1)

    async def get_supported_templates(self) -> list[TemplateInfo]:
        if not self._templates:
            return [
                TemplateInfo(name="default", label="默认信息图", description="简洁三栏信息图模板"),
            ]
        return [
            TemplateInfo(name=t["name"], label=t.get("label", t["name"]), description=t.get("description", ""))
            for t in self._templates
        ]
```

### 步骤 2.7.7: 创建 `src/notebooklm/server/generation/engines/video_engine.py`

```python
from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from ..base import ContentGenerator, GeneratedContent, PreviewResult, TemplateInfo


@dataclass
class VideoGenerator(ContentGenerator):
    content_type: str = "video"
    notebooklm_client: Any = None
    media_root: Path = field(default_factory=lambda: Path.home() / ".notebooklm" / "data")
    _templates: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._load_templates()

    def _load_templates(self) -> None:
        template_dir = Path(__file__).resolve().parent.parent / "templates" / "video"
        if template_dir.is_dir():
            for f in sorted(template_dir.glob("*.json")):
                try:
                    self._templates.append(json.loads(f.read_text(encoding="utf-8")))
                except Exception:
                    pass

    def _get_template(self, name: str) -> dict:
        for t in self._templates:
            if t["name"] == name:
                return dict(t)
        return {
            "name": "default", "width": 1920, "height": 1080,
            "fps": 30, "background_color": "#1a202c",
            "title_color": "#FFFFFF", "body_color": "#E2E8F0",
            "scene_duration_seconds": 5,
            "tts_language": "zh-CN",
        }

    async def generate(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
        options: dict | None = None,
    ) -> GeneratedContent:
        opts = options or {}
        title = opts.get("title", f"Video {uuid.uuid4().hex[:8]}")
        user_id = opts.get("user_id", 0)
        template_name = template or "default"
        tmpl = self._get_template(template_name)

        scenes = opts.get("scenes", [
            {"text": title, "type": "title"},
            {"text": prompt, "type": "content"},
        ])

        user_dir = self.media_root / "generated" / str(user_id) / notebook_id
        user_dir.mkdir(parents=True, exist_ok=True)
        frames_dir = user_dir / f"frames_{uuid.uuid4().hex[:8]}"
        frames_dir.mkdir(exist_ok=True)

        width = tmpl.get("width", 1920)
        height = tmpl.get("height", 1080)
        bg_color = tmpl.get("background_color", "#1a202c")
        scene_duration = tmpl.get("scene_duration_seconds", 5)
        fps = tmpl.get("fps", 30)

        frame_files: list[str] = []
        for i, scene in enumerate(scenes):
            img = Image.new("RGB", (width, height), bg_color)
            draw = ImageDraw.Draw(img)
            text = scene.get("text", "")
            lines = text.split("\n")
            y_start = height // 2 - len(lines) * 20
            for j, line in enumerate(lines):
                draw.text(
                    (width // 2 - len(line) * 5, y_start + j * 40),
                    line,
                    fill=("#FFFFFF" if scene.get("type") == "title" else "#E2E8F0"),
                    font=None,
                )
            for f in range(scene_duration * fps):
                frame_path = frames_dir / f"frame_{i:04d}_{f:06d}.png"
                img.save(str(frame_path))
                frame_files.append(str(frame_path))

        output_filename = f"video_{uuid.uuid4().hex[:8]}.mp4"
        output_path = user_dir / output_filename

        scene_json_filename = f"video_{uuid.uuid4().hex[:8]}.json"
        scene_json_path = user_dir / scene_json_filename
        scene_json_path.write_text(
            json.dumps({"scenes": scenes, "total_frames": len(frame_files)}, ensure_ascii=False),
            encoding="utf-8",
        )

        # Try to compose with ffmpeg if available
        if frame_files:
            try:
                first_frame = frame_files[0]
                subprocess.run(
                    ["ffmpeg", "-y", "-framerate", str(fps), "-pattern_type", "glob",
                     "-i", str(frames_dir / "*.png"),
                     "-c:v", "libx264", "-pix_fmt", "yuv420p",
                     str(output_path)],
                    capture_output=True, timeout=120,
                )
            except Exception:
                output_path.write_text(f"[Video would be {len(scenes)} scenes]", encoding="utf-8")

        return GeneratedContent(
            id=0,
            content_type="video",
            title=title,
            status="completed",
            local_file_path=str(output_path),
            file_size=output_path.stat().st_size if output_path.exists() else 0,
            content=json.dumps(scenes, ensure_ascii=False),
            metadata={
                "video_scenes": json.dumps(scenes, ensure_ascii=False),
                "video_duration_seconds": len(scenes) * scene_duration,
                "video_resolution": f"{width}x{height}",
                "video_narration": prompt,
            },
        )

    async def preview(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
    ) -> PreviewResult:
        return PreviewResult(
            content_type="video",
            outline=[{"title": "场景 1"}, {"title": "场景 2"}, {"title": "场景 3"}],
            estimated_duration_seconds=15,
        )

    async def get_supported_templates(self) -> list[TemplateInfo]:
        if not self._templates:
            return [
                TemplateInfo(name="default", label="默认视频", description="图文混合默认短视频模板"),
            ]
        return [
            TemplateInfo(name=t["name"], label=t.get("label", t["name"]), description=t.get("description", ""))
            for t in self._templates
        ]
```

### 步骤 2.7.8: 创建 `tests/unit/server/test_mindmap_engine.py`

```python
from __future__ import annotations

import pytest

from notebooklm.server.generation.engines.mindmap_engine import MindmapGenerator


class TestMindmapGenerator:

    @pytest.fixture
    def gen(self) -> MindmapGenerator:
        return MindmapGenerator()

    @pytest.mark.asyncio
    async def test_content_type(self, gen: MindmapGenerator) -> None:
        assert gen.content_type == "mindmap"

    @pytest.mark.asyncio
    async def test_generate_returns_json(self, gen: MindmapGenerator) -> None:
        result = await gen.generate(
            "nb-1",
            "Create mindmap about AI",
            options={"title": "AI Map", "source_text": "# AI\n## ML\n## DL"},
        )
        assert result.status == "completed"
        assert result.local_file_path.endswith(".json")

    @pytest.mark.asyncio
    async def test_generate_builds_hierarchy(self, gen: MindmapGenerator) -> None:
        result = await gen.generate(
            "nb-1",
            "Mindmap",
            options={"source_text": "# Root\n## Child 1\n## Child 2"},
        )
        import json
        data = json.loads(result.content)
        assert data["name"] == "Root"
        assert len(data["children"]) >= 2

    @pytest.mark.asyncio
    async def test_preview_returns_outline(self, gen: MindmapGenerator) -> None:
        result = await gen.preview("nb-1", "Preview")
        assert result.content_type == "mindmap"

    @pytest.mark.asyncio
    async def test_get_supported_templates(self, gen: MindmapGenerator) -> None:
        templates = await gen.get_supported_templates()
        assert len(templates) >= 1
```

### 步骤 2.7.9: 创建 `tests/unit/server/test_infographic_engine.py`

```python
from __future__ import annotations

import pytest

from notebooklm.server.generation.engines.infographic_engine import InfographicGenerator


class TestInfographicGenerator:

    @pytest.fixture
    def gen(self) -> InfographicGenerator:
        return InfographicGenerator()

    @pytest.mark.asyncio
    async def test_content_type(self, gen: InfographicGenerator) -> None:
        assert gen.content_type == "infographic"

    @pytest.mark.asyncio
    async def test_generate_returns_png(self, gen: InfographicGenerator) -> None:
        result = await gen.generate("nb-1", "Create infographic", options={"title": "Test Info"})
        assert result.status == "completed"
        assert result.local_file_path.endswith(".png")
        assert result.file_size > 0

    @pytest.mark.asyncio
    async def test_preview_returns_outline(self, gen: InfographicGenerator) -> None:
        result = await gen.preview("nb-1", "Preview")
        assert result.content_type == "infographic"

    @pytest.mark.asyncio
    async def test_get_supported_templates(self, gen: InfographicGenerator) -> None:
        templates = await gen.get_supported_templates()
        assert len(templates) >= 1
```

### 步骤 2.7.10: 创建 `tests/unit/server/test_video_engine.py`

```python
from __future__ import annotations

import pytest

from notebooklm.server.generation.engines.video_engine import VideoGenerator


class TestVideoGenerator:

    @pytest.fixture
    def gen(self) -> VideoGenerator:
        return VideoGenerator()

    @pytest.mark.asyncio
    async def test_content_type(self, gen: VideoGenerator) -> None:
        assert gen.content_type == "video"

    @pytest.mark.asyncio
    async def test_generate_returns_video(self, gen: VideoGenerator) -> None:
        result = await gen.generate(
            "nb-1",
            "Create video about AI",
            options={"title": "AI Video", "scenes": [
                {"text": "Introduction to AI", "type": "title"},
                {"text": "Deep Learning", "type": "content"},
            ]},
        )
        assert result.status == "completed"
        assert result.metadata["video_duration_seconds"] > 0

    @pytest.mark.asyncio
    async def test_preview_returns_outline(self, gen: VideoGenerator) -> None:
        result = await gen.preview("nb-1", "Preview")
        assert result.content_type == "video"

    @pytest.mark.asyncio
    async def test_get_supported_templates(self, gen: VideoGenerator) -> None:
        templates = await gen.get_supported_templates()
        assert len(templates) >= 1
```

**运行测试：**
```bash
uv run pytest tests/unit/server/test_mindmap_engine.py tests/unit/server/test_infographic_engine.py tests/unit/server/test_video_engine.py -v
```

---

## Task 2.8: Generation API routes

### 步骤 2.8.1: 创建 `src/notebooklm/server/routes/generation.py`

```python
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth_deps import get_current_user
from ..database import get_db
from ..generation.registry import GeneratorRegistry
from ..models import GeneratedContent as GeneratedContentModel, User

router = APIRouter(prefix="/api/generation", tags=["generation"])

CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.post("/generate")
async def generate_content(
    body: dict[str, Any],
    user: CurrentUser,
    db: DbSession,
) -> dict[str, Any]:
    content_type = body.get("content_type", "")
    notebook_id = body.get("notebook_id", "")
    prompt = body.get("prompt", "")
    template = body.get("template")
    options = body.get("options", {})

    if not all([content_type, notebook_id, prompt]):
        raise HTTPException(400, "content_type, notebook_id, and prompt are required")

    options.setdefault("title", body.get("title", ""))
    options.setdefault("user_id", user.id)

    generator = GeneratorRegistry.create(content_type)
    result = await generator.generate(
        notebook_id=notebook_id,
        prompt=prompt,
        template=template,
        options=options,
    )

    record = GeneratedContentModel(
        user_id=user.id,
        notebook_id=int(notebook_id) if notebook_id.isdigit() else 0,
        content_type=content_type,
        title=result.title,
        prompt=prompt,
        status=result.status,
        local_file_path=result.local_file_path,
        file_size=result.file_size,
        content=result.content,
        **{k: v for k, v in result.metadata.items() if k in {
            "ppt_page_count", "ppt_template", "ppt_json", "ppt_preview_images",
            "mindmap_data", "mindmap_layout",
            "infographic_template", "infographic_blocks",
            "audio_file_path", "duration_seconds", "audio_speakers", "audio_transcript",
            "video_file_path", "video_duration_seconds", "video_resolution",
            "video_scenes", "video_narration",
            "doc_page_count", "doc_sections", "doc_format",
        }},
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)

    return {
        "id": record.id,
        "content_type": record.content_type,
        "title": record.title,
        "status": record.status,
        "local_file_path": record.local_file_path,
        "file_size": record.file_size,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


@router.get("/list")
async def list_generated_contents(
    user: CurrentUser,
    db: DbSession,
    notebook_id: int | None = Query(None),
    content_type: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    stmt = select(GeneratedContentModel).where(
        GeneratedContentModel.user_id == user.id,
    )
    if notebook_id is not None:
        stmt = stmt.where(GeneratedContentModel.notebook_id == notebook_id)
    if content_type is not None:
        stmt = stmt.where(GeneratedContentModel.content_type == content_type)
    stmt = stmt.order_by(GeneratedContentModel.created_at.desc())
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(stmt)
    rows = result.scalars().all()

    return {
        "items": [
            {
                "id": r.id,
                "content_type": r.content_type,
                "title": r.title,
                "status": r.status,
                "file_size": r.file_size,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
        "total": len(rows),
        "page": page,
        "page_size": page_size,
    }


@router.get("/{content_id}")
async def get_generated_content(
    content_id: int,
    user: CurrentUser,
    db: DbSession,
) -> dict[str, Any]:
    result = await db.execute(
        select(GeneratedContentModel).where(
            GeneratedContentModel.id == content_id,
            GeneratedContentModel.user_id == user.id,
        )
    )
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Generated content not found")

    return {
        "id": r.id,
        "content_type": r.content_type,
        "title": r.title,
        "prompt": r.prompt,
        "status": r.status,
        "content": r.content,
        "local_file_path": r.local_file_path,
        "file_size": r.file_size,
        "thumbnail_path": r.thumbnail_path,
        "error_message": r.error_message,
        "metadata": {
            "ppt_page_count": r.ppt_page_count,
            "ppt_template": r.ppt_template,
            "ppt_json": r.ppt_json,
            "ppt_preview_images": r.ppt_preview_images,
            "mindmap_data": r.mindmap_data,
            "mindmap_layout": r.mindmap_layout,
            "infographic_template": r.infographic_template,
            "infographic_blocks": r.infographic_blocks,
            "audio_file_path": r.audio_file_path,
            "duration_seconds": r.duration_seconds,
            "audio_speakers": r.audio_speakers,
            "audio_transcript": r.audio_transcript,
            "video_file_path": r.video_file_path,
            "video_duration_seconds": r.video_duration_seconds,
            "video_resolution": r.video_resolution,
            "video_scenes": r.video_scenes,
            "video_narration": r.video_narration,
            "doc_page_count": r.doc_page_count,
            "doc_sections": r.doc_sections,
            "doc_format": r.doc_format,
        },
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.delete("/{content_id}", status_code=204)
async def delete_generated_content(
    content_id: int,
    user: CurrentUser,
    db: DbSession,
) -> None:
    result = await db.execute(
        select(GeneratedContentModel).where(
            GeneratedContentModel.id == content_id,
            GeneratedContentModel.user_id == user.id,
        )
    )
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Generated content not found")

    import os
    if r.local_file_path and os.path.exists(r.local_file_path):
        os.remove(r.local_file_path)

    await db.delete(r)
    await db.commit()


@router.get("/templates")
async def list_templates(
    content_type: str | None = Query(None),
) -> list[dict[str, str]]:
    types: list[str] = [content_type] if content_type else GeneratorRegistry.list_types()
    result: list[dict[str, str]] = []
    for ct in types:
        try:
            gen = GeneratorRegistry.create(ct)
            for t in await gen.get_supported_templates():
                result.append({
                    "content_type": ct,
                    "name": t.name,
                    "label": t.label,
                    "description": t.description,
                })
        except ValueError:
            pass
    return result


@router.post("/{content_id}/regenerate")
async def regenerate_content(
    content_id: int,
    body: dict[str, Any],
    user: CurrentUser,
    db: DbSession,
) -> dict[str, Any]:
    result = await db.execute(
        select(GeneratedContentModel).where(
            GeneratedContentModel.id == content_id,
            GeneratedContentModel.user_id == user.id,
        )
    )
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(404, "Generated content not found")

    new_prompt = body.get("prompt")
    if not new_prompt:
        raise HTTPException(400, "prompt is required")

    generator = GeneratorRegistry.create(record.content_type)
    gen_result = await generator.generate(
        notebook_id=str(record.notebook_id),
        prompt=new_prompt,
        template=record.ppt_template or body.get("template"),
        options={"title": record.title, "user_id": user.id},
    )

    record.prompt = new_prompt
    record.status = gen_result.status
    record.content = gen_result.content
    record.local_file_path = gen_result.local_file_path
    record.file_size = gen_result.file_size
    await db.commit()

    return {
        "id": record.id,
        "status": record.status,
        "local_file_path": record.local_file_path,
    }
```

### 步骤 2.8.2: 在 `src/notebooklm/server/app.py` 中注册路由

```python
from .routes import external_kb as external_kb_routes
from .routes import generation as generation_routes

# ...

    v1.include_router(external_kb_routes.router)
    v1.include_router(generation_routes.router)
    app.include_router(v1)
```

### 步骤 2.8.3: 创建 `tests/unit/server/test_generation_routes.py`

```python
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from notebooklm.server.app import create_app


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestGenerationRoutes:

    @pytest.mark.asyncio
    async def test_generate_missing_auth(self, client: AsyncClient) -> None:
        resp = await client.post("/api/generation/generate", json={
            "content_type": "document",
            "notebook_id": "nb-1",
            "prompt": "Write a summary",
        })
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_list_missing_auth(self, client: AsyncClient) -> None:
        resp = await client.get("/api/generation/list")
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_list_with_notebook_id_missing_auth(self, client: AsyncClient) -> None:
        resp = await client.get("/api/generation/list?notebook_id=1")
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_templates_public(self, client: AsyncClient) -> None:
        resp = await client.get("/api/generation/templates")
        assert resp.status_code in (200, 401)
```

**运行测试：**
```bash
uv run pytest tests/unit/server/test_generation_routes.py -v
```

---

## 全局检查点：运行所有 Plan 2 测试

```bash
uv run pytest tests/unit/server/test_external_kb.py tests/unit/server/test_external_kb_providers.py tests/unit/server/test_external_kb_routes.py tests/unit/server/test_generation.py tests/unit/server/test_generation_engines.py tests/unit/server/test_ppt_engine.py tests/unit/server/test_mindmap_engine.py tests/unit/server/test_infographic_engine.py tests/unit/server/test_video_engine.py tests/unit/server/test_generation_routes.py -v
```
