from __future__ import annotations

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


class FakeConnector(ExternalKBConnector):
    provider_type = "fake"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config or {})
        self._connected: bool = True

    async def test_connection(self) -> bool:
        return self._connected

    async def list_collections(self) -> list[KBCollection]:
        return [
            KBCollection(remote_id="c1", name="Coll 1", document_count=5),
        ]

    async def list_documents(
        self, collection_id: str, page: int = 1, page_size: int = 20
    ) -> PageResult[KBDocument]:
        if collection_id == "c1":
            return PageResult(
                items=[
                    KBDocument(remote_id="d1", title="Doc 1", file_type="pdf"),
                ],
                total=1,
                page=page,
                page_size=page_size,
            )
        return PageResult(items=[], total=0, page=page, page_size=page_size)

    async def get_document_detail(self, document_id: str) -> KBDocument:
        return KBDocument(remote_id=document_id, title=f"Doc {document_id}")

    async def search_documents(
        self, collection_id: str, query: str, top_k: int = 10
    ) -> list[KBSearchResult]:
        return [
            KBSearchResult(
                document_id="d1",
                title="Matched",
                snippet=query,
                score=0.95,
                collection_id=collection_id,
            ),
        ]

    async def import_document(self, document_id: str, target_notebook_id: str) -> ImportResult:
        return ImportResult(
            success=True, local_source_id=42, local_source_path=f"/media/imports/{document_id}.pdf"
        )


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
