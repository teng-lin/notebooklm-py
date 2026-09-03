"""Transport-neutral tests for the notebooks base class."""

from __future__ import annotations

import builtins
from typing import Any

import pytest

from notebooklm._notebooks import NotebooksAPI
from notebooklm.exceptions import (
    NetworkError,
    RateLimitError,
    RPCError,
    ServerError,
    ValidationError,
)
from notebooklm.types import Notebook, NotebookDescription, PromptSuggestion


class _EmptySourceLister:
    async def list(self, notebook_id: str, *, strict: bool = False) -> list[Any]:
        return []


class _FakeNotebooksAPI(NotebooksAPI):
    """Minimal backend proving shared orchestration needs only narrow send hooks."""

    _create_method_id = "fake.CreateNotebook"
    _copy_method_id = "fake.CopyNotebook"
    _copy_failure_chain = "explicit"

    def __init__(
        self,
        *,
        list_results: list[list[Notebook]],
        create_results: list[Notebook | Exception],
        copy_results: list[Notebook | Exception] | None = None,
    ) -> None:
        super().__init__(_EmptySourceLister())
        self._list_results = list_results
        self._create_results = create_results
        self._copy_results = copy_results or []
        self.sent_titles: list[str] = []
        self.sent_copies: list[tuple[str, str]] = []

    async def _send_create(self, title: str) -> Notebook:
        self.sent_titles.append(title)
        result = self._create_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def _send_copy(self, notebook_id: str, title: str) -> Notebook:
        self.sent_copies.append((notebook_id, title))
        result = self._copy_results.pop(0)
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


def test_created_chat_session_hint_storage_is_owned_and_consumed_by_base() -> None:
    api = _FakeNotebooksAPI(list_results=[], create_results=[])
    api._created_chat_session_ids["notebook"] = "session"

    assert api._take_created_chat_session_id("notebook") == "session"
    assert api._take_created_chat_session_id("notebook") is None


@pytest.mark.asyncio
async def test_copy_validates_then_returns_the_single_hook_result() -> None:
    copied = Notebook(id="nb-copy", title="  Copy  ")
    api = _FakeNotebooksAPI(
        list_results=[],
        create_results=[],
        copy_results=[copied],
    )

    assert await api.copy("nb-source", "  Copy  ") is copied
    assert api.sent_copies == [("nb-source", "  Copy  ")]

    for notebook_id, title in (("", "Copy"), ("nb-source", ""), ("nb-source", "  ")):
        with pytest.raises(ValidationError):
            await api.copy(notebook_id, title)
    assert api.sent_copies == [("nb-source", "  Copy  ")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "rpc_code"),
    [
        (NetworkError("lost"), None),
        (RateLimitError("limited", rpc_code=8), 8),
        (ServerError("unavailable", rpc_code=14), 14),
    ],
)
async def test_copy_replaces_transient_failure_with_chained_unconfirmed_error(
    failure: NetworkError | RateLimitError | ServerError,
    rpc_code: int | None,
) -> None:
    api = _FakeNotebooksAPI(
        list_results=[],
        create_results=[],
        copy_results=[failure],
    )

    with pytest.raises(RPCError, match="list notebooks.*manually") as raised:
        await api.copy("nb-source", "Copy")

    assert raised.value is not failure
    assert raised.value.__cause__ is failure
    assert getattr(raised.value, "unconfirmed", False) is True
    assert raised.value.method_id == "fake.CopyNotebook"
    assert raised.value.rpc_code == rpc_code
    assert api.sent_copies == [("nb-source", "Copy")]
