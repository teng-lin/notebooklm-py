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
                connector=self,
                doc_id=document_id,
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
