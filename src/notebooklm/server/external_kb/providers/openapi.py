from __future__ import annotations

import json
from base64 import b64encode
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
