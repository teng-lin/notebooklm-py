"""Notebook management service."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..api_client import NotebookLMClient


@dataclass
class Notebook:
    """Represents a NotebookLM notebook."""

    id: str
    title: str
    created_at: Optional[datetime] = None
    sources_count: int = 0
    is_owner: bool = True

    @classmethod
    def from_api_response(cls, data: list[Any]) -> "Notebook":
        raw_title = data[0] if len(data) > 0 and isinstance(data[0], str) else ""
        title = raw_title.replace("thought\n", "").strip()
        notebook_id = data[2] if len(data) > 2 and isinstance(data[2], str) else ""

        created_at = None
        if len(data) > 5 and isinstance(data[5], list) and len(data[5]) > 5:
            ts_data = data[5][5]
            if isinstance(ts_data, list) and len(ts_data) > 0:
                try:
                    created_at = datetime.fromtimestamp(ts_data[0])
                except (TypeError, ValueError):
                    pass

        # Extract ownership - data[5][1] = False means owner, True means shared
        is_owner = True
        if len(data) > 5 and isinstance(data[5], list) and len(data[5]) > 1:
            is_owner = data[5][1] is False

        return cls(id=notebook_id, title=title, created_at=created_at, is_owner=is_owner)


class NotebookService:
    """High-level service for notebook operations."""

    def __init__(self, client: "NotebookLMClient"):
        self._client = client

    async def list(self) -> list[Notebook]:
        result = await self._client.list_notebooks()
        return [Notebook.from_api_response(nb) for nb in result]

    async def create(self, title: str) -> Notebook:
        result = await self._client.create_notebook(title)
        return Notebook.from_api_response(result)

    async def get(self, notebook_id: str) -> Notebook:
        result = await self._client.get_notebook(notebook_id)
        return Notebook.from_api_response(result)

    async def delete(self, notebook_id: str) -> bool:
        result = await self._client.delete_notebook(notebook_id)
        return result is not None

    async def rename(self, notebook_id: str, new_title: str) -> Notebook:
        """Rename a notebook."""
        result = await self._client.rename_notebook(notebook_id, new_title)
        return Notebook.from_api_response(result)
