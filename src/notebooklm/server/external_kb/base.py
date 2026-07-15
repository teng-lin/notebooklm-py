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

    def __init__(self, config: dict) -> None:
        self.config = config

    @abstractmethod
    async def test_connection(self) -> bool: ...

    @abstractmethod
    async def list_collections(self) -> list[KBCollection]: ...

    @abstractmethod
    async def list_documents(
        self, collection_id: str, page: int = 1, page_size: int = 20
    ) -> PageResult[KBDocument]: ...

    @abstractmethod
    async def get_document_detail(self, document_id: str) -> KBDocument: ...

    @abstractmethod
    async def search_documents(
        self, collection_id: str, query: str, top_k: int = 10
    ) -> list[KBSearchResult]: ...

    @abstractmethod
    async def import_document(self, document_id: str, target_notebook_id: str) -> ImportResult: ...
