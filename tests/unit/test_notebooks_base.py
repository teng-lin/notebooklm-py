"""Transport-neutral tests for the notebooks base class."""

from __future__ import annotations

import builtins
from typing import Any

import pytest

from notebooklm._notebooks import NotebooksAPI
from notebooklm.exceptions import NetworkError
from notebooklm.types import Notebook, NotebookDescription, PromptSuggestion


class _EmptySourceLister:
    async def list(self, notebook_id: str, *, strict: bool = False) -> list[Any]:
        return []


class _FakeNotebooksAPI(NotebooksAPI):
    """Minimal backend proving shared orchestration needs only ``_send_create``."""

    _create_method_id = "fake.CreateNotebook"

    def __init__(
        self,
        *,
        list_results: list[list[Notebook]],
        create_results: list[Notebook | Exception],
    ) -> None:
        super().__init__(_EmptySourceLister())
        self._list_results = list_results
        self._create_results = create_results
        self.sent_titles: list[str] = []

    async def _send_create(self, title: str) -> Notebook:
        self.sent_titles.append(title)
        result = self._create_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def list(self) -> list[Notebook]:
        return self._list_results.pop(0)

    async def get_source_ids(self, notebook_id: str) -> builtins.list[str]:
        raise NotImplementedError

    async def suggest_prompts(
        self,
        notebook_id: str,
        *,
        source_ids: builtins.list[str] | None = None,
        mode: int = 4,
        query: str | None = None,
    ) -> builtins.list[PromptSuggestion]:
        raise NotImplementedError

    async def suggest_next_steps(
        self,
        notebook_id: str,
        *,
        source_ids: builtins.list[str] | None = None,
    ) -> builtins.list[Any]:
        raise NotImplementedError

    async def get(self, notebook_id: str) -> Notebook:
        raise NotImplementedError

    async def copy(self, notebook_id: str, title: str) -> Notebook:
        raise NotImplementedError

    async def delete(self, notebook_id: str) -> None:
        raise NotImplementedError

    async def update(
        self,
        notebook_id: str,
        *,
        title: str | None = None,
        emoji: str | None = None,
    ) -> Notebook:
        raise NotImplementedError

    async def get_summary(self, notebook_id: str) -> str:
        raise NotImplementedError

    async def get_description(self, notebook_id: str) -> NotebookDescription:
        raise NotImplementedError

    async def remove_from_recent(self, notebook_id: str) -> None:
        raise NotImplementedError

    async def get_raw(self, notebook_id: str) -> Any:
        raise NotImplementedError


@pytest.mark.asyncio
async def test_create_recovers_through_transport_neutral_hook_and_probe() -> None:
    created = Notebook(id="nb-created", title="Base orchestration")
    api = _FakeNotebooksAPI(
        list_results=[[], [created]],
        create_results=[NetworkError("response lost")],
    )

    result = await api.create("Base orchestration")

    assert result is created
    assert api.sent_titles == ["Base orchestration"]
    assert api._take_created_chat_session_id(created.id) is None
