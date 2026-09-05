"""Transport-neutral tests for the notebooks base class."""

from __future__ import annotations

import asyncio
import builtins
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

from notebooklm._client_metrics import ClientMetrics
from notebooklm._notebooks import NotebooksAPI
from notebooklm._runtime.call_supervisor import AdmissionState, CallSupervisor
from notebooklm._runtime.lifecycle import ClientLifecycle
from notebooklm.exceptions import (
    NetworkError,
    RateLimitError,
    RPCError,
    ServerError,
    ValidationError,
)
from notebooklm.types import Notebook, NotebookDescription, PromptSuggestion
from tests._fixtures.fake_core import declared_spawn_child


class _EmptySourceLister:
    async def list(self, notebook_id: str, *, strict: bool = False) -> list[Any]:
        return []


class _FakeNotebooksAPI(NotebooksAPI):
    """Minimal backend proving shared orchestration needs only narrow send hooks."""

    _create_method_id = "fake.CreateNotebook"
    _copy_method_id = "fake.CopyNotebook"
    _copy_failure_chain = "explicit"

    @contextlib.asynccontextmanager
    async def _operation_scope(self, label: str) -> AsyncIterator[None]:
        """Declare that this transport-free fake intentionally skips admission."""
        del label
        yield None

    def __init__(
        self,
        *,
        list_results: list[list[Notebook]],
        create_results: list[Notebook | Exception],
        copy_results: list[Notebook | Exception] | None = None,
    ) -> None:
        super().__init__(_EmptySourceLister(), spawn_child=declared_spawn_child)
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


class _ConcurrentCreateService:
    """Stateful fake service that lets B commit before A's lost-response probe."""

    def __init__(self) -> None:
        self.a_send_started = asyncio.Event()
        self.b_committed = asyncio.Event()
        self.visible: list[Notebook] = []
        self.a_error = NetworkError("A lost its create response")
        self.b_notebook = Notebook(id="nb-b", title="Shared title")

    async def list(self) -> list[Notebook]:
        return list(self.visible)

    async def create(self, caller: str) -> Notebook:
        if caller == "A":
            self.a_send_started.set()
            await self.b_committed.wait()
            raise self.a_error
        assert caller == "B"
        self.visible.append(self.b_notebook)
        self.b_committed.set()
        return self.b_notebook


class _ConcurrentNotebooksAPI(_FakeNotebooksAPI):
    def __init__(self, service: _ConcurrentCreateService, caller: str) -> None:
        super().__init__(list_results=[], create_results=[])
        self._service = service
        self._caller = caller

    async def list(self) -> list[Notebook]:
        return await self._service.list()

    async def _send_create(self, title: str) -> Notebook:
        assert title == "Shared title"
        self.sent_titles.append(title)
        return await self._service.create(self._caller)


class _StaleCreateService:
    """Stateful fake whose committed first create is absent from one stale probe."""

    def __init__(self) -> None:
        self.error = NetworkError("first create committed, response lost")
        self.committed: list[Notebook] = []
        self.create_calls = 0
        self.list_calls = 0

    async def list(self) -> list[Notebook]:
        self.list_calls += 1
        if self.list_calls <= 2:
            return []
        return list(self.committed)

    async def create(self) -> Notebook:
        self.create_calls += 1
        created = Notebook(id=f"nb-{self.create_calls}", title="Stale title")
        self.committed.append(created)
        if self.create_calls == 1:
            raise self.error
        return created


class _StaleNotebooksAPI(_FakeNotebooksAPI):
    def __init__(self, service: _StaleCreateService) -> None:
        super().__init__(list_results=[], create_results=[])
        self._service = service

    async def list(self) -> list[Notebook]:
        return await self._service.list()

    async def _send_create(self, title: str) -> Notebook:
        assert title == "Stale title"
        self.sent_titles.append(title)
        return await self._service.create()


class _DrainingWebWorkflowAPI(_FakeNotebooksAPI):
    """Web-shaped workflow using the real supervisor scope."""

    def __init__(self, supervisor: CallSupervisor) -> None:
        super().__init__(list_results=[], create_results=[])
        self._supervisor = supervisor
        self.between_calls = asyncio.Event()
        self.resume_create = asyncio.Event()

    def _operation_scope(self, label: str):
        return self._supervisor.operation_scope(label)

    async def list(self) -> list[Notebook]:
        async with self._supervisor.call_scope("web.list", "ListNotebooks", None):
            return []

    async def _send_create(self, title: str) -> Notebook:
        self.between_calls.set()
        await self.resume_create.wait()
        async with self._supervisor.call_scope("web.create", "CreateNotebook", None):
            return Notebook(id="nb-created", title=title)


@pytest.mark.asyncio
async def test_create_surfaces_transport_loss_without_probe_or_replay() -> None:
    created = Notebook(id="nb-created", title="Base orchestration")
    api = _FakeNotebooksAPI(
        list_results=[[], [created]],
        create_results=[NetworkError("response lost")],
    )

    with pytest.raises(NetworkError) as raised:
        await api.create("Base orchestration")

    assert getattr(raised.value, "unconfirmed", False) is True
    assert api.sent_titles == ["Base orchestration"]
    assert api._list_results == [[], [created]]


@pytest.mark.asyncio
async def test_e5_concurrent_same_title_create_does_not_return_the_other_callers_id() -> None:
    service = _ConcurrentCreateService()
    caller_a = _ConcurrentNotebooksAPI(service, "A")
    caller_b = _ConcurrentNotebooksAPI(service, "B")

    a_task = asyncio.create_task(caller_a.create("Shared title"))
    await service.a_send_started.wait()
    b_task = asyncio.create_task(caller_b.create("Shared title"))
    a_result, b_result = await asyncio.gather(a_task, b_task, return_exceptions=True)

    assert a_result is service.a_error
    assert b_result is service.b_notebook
    assert caller_a.sent_titles == ["Shared title"]
    assert caller_b.sent_titles == ["Shared title"]


@pytest.mark.asyncio
async def test_e5_stale_probe_after_committed_create_does_not_send_a_second_create() -> None:
    service = _StaleCreateService()
    api = _StaleNotebooksAPI(service)

    with pytest.raises(NetworkError) as raised:
        await api.create("Stale title")

    assert raised.value is service.error
    assert service.create_calls == 1
    assert [notebook.id for notebook in service.committed] == ["nb-1"]


@pytest.mark.asyncio
async def test_e6_web_create_holds_real_lifecycle_admission_across_drain() -> None:
    supervisor = CallSupervisor(
        metrics=ClientMetrics(),
        max_concurrent_rpcs=None,
    )
    lifecycle = ClientLifecycle(
        supervisor=supervisor,
        transports=(),
        loop_participants=(supervisor,),
    )
    await lifecycle.open()
    api = _DrainingWebWorkflowAPI(supervisor)
    create_task = asyncio.create_task(api.create("Drain-safe"))
    await api.between_calls.wait()

    draining = asyncio.Event()
    stop_accepting = supervisor.stop_accepting

    async def observed_stop_accepting(epoch: int) -> None:
        await stop_accepting(epoch)
        draining.set()

    supervisor.stop_accepting = observed_stop_accepting  # type: ignore[method-assign]
    drain_task = asyncio.create_task(lifecycle.drain())
    await draining.wait()
    assert supervisor._current is not None
    assert supervisor._current.state is AdmissionState.DRAINING
    try:
        assert not drain_task.done()
    finally:
        api.resume_create.set()
        created_result, drain_result = await asyncio.gather(
            create_task,
            drain_task,
            return_exceptions=True,
        )
        await lifecycle.close(drain=False)

    assert drain_result is None
    assert isinstance(created_result, Notebook)
    assert created_result.id == "nb-created"


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
