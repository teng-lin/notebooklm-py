"""Service-layer tests for ``cli/services/research.py``.

These tests exercise :class:`ResearchWaitPlan` + :func:`execute_research_wait`
DIRECTLY — no Click ``CliRunner``, no Rich console capture, no asyncio
``CancelledError`` rituals. The Click handler in ``cli/research_cmd.py`` is
exercised separately by ``tests/unit/cli/test_research*.py``.

The service is intentionally I/O-free: it never calls ``console.print``,
``click.echo``, or ``exit_with_code``. The Click handler owns rendering and
exit codes; the service owns the polling loop, the P1.T2 task-id pinning,
and the (optional) import call.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from notebooklm.cli.research_import import ResearchImportResult
from notebooklm.cli.services.research import (
    ResearchWaitPlan,
    ResearchWaitResult,
    execute_research_wait,
)

# ---------------------------------------------------------------------------
# Fixtures: a fake notebook client with only the surface the service touches
# ---------------------------------------------------------------------------


class _FakeResearchAPI:
    """Records poll calls; returns canned values."""

    def __init__(self, *, side_effect: Any) -> None:
        self.poll = AsyncMock(side_effect=side_effect)


class _FakeClient:
    def __init__(self, *, poll_side_effect: Any) -> None:
        self.research = _FakeResearchAPI(side_effect=poll_side_effect)


async def _fake_resolve(client, notebook_id, *, json_output: bool = False) -> str:  # noqa: ARG001
    """Pass-through resolver — service should not touch ID resolution."""
    return notebook_id


@pytest.fixture
def base_plan() -> ResearchWaitPlan:
    return ResearchWaitPlan(
        notebook_id="nb_123",
        timeout=5,
        interval=1,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_returns_outcome_with_sources(base_plan):
    """First poll completes; service returns completed result without import."""
    client = _FakeClient(
        poll_side_effect=[
            {
                "status": "completed",
                "task_id": "task_abc",
                "query": "AI research",
                "sources": [{"title": "S", "url": "http://example.com"}],
                "report": "REPORT",
            }
        ]
    )

    result = await execute_research_wait(base_plan, client=client, resolve_id=_fake_resolve)

    assert isinstance(result, ResearchWaitResult)
    assert result.outcome == "completed"
    assert result.notebook_id == "nb_123"
    assert result.task_id == "task_abc"
    assert result.query == "AI research"
    assert result.sources == [{"title": "S", "url": "http://example.com"}]
    assert result.sources_count == 1
    assert result.report == "REPORT"
    assert result.import_result is None  # import_all=False default
    # Exactly one poll call; task_id=None on the first poll.
    assert client.research.poll.await_count == 1
    first_call = client.research.poll.await_args_list[0]
    assert first_call.args == ("nb_123",)
    assert first_call.kwargs == {"task_id": None}


# ---------------------------------------------------------------------------
# Timeout path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_returns_outcome_without_completion(monkeypatch):
    """A persistent in_progress status produces outcome='timeout'."""
    # Eliminate real sleep so the timeout fires on the first interval boundary.
    from notebooklm.cli.services import polling

    clock = {"t": 0.0}

    async def fake_sleep(delay: float) -> None:
        clock["t"] += delay

    monkeypatch.setattr(polling.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(polling.asyncio, "sleep", fake_sleep)

    async def _poll(nb_id, *, task_id=None):  # noqa: ARG001
        return {"status": "in_progress", "query": "AI"}

    plan = ResearchWaitPlan(notebook_id="nb_123", timeout=1, interval=1)
    client = _FakeClient(poll_side_effect=_poll)

    result = await execute_research_wait(plan, client=client, resolve_id=_fake_resolve)

    assert result.outcome == "timeout"
    assert result.timeout == 1
    assert result.import_result is None


# ---------------------------------------------------------------------------
# No-research path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_research_returns_outcome_without_exit(base_plan):
    """no_research is a terminal poll value; service returns it (no SystemExit)."""
    client = _FakeClient(poll_side_effect=[{"status": "no_research"}])

    # The service must NOT raise SystemExit — that's the handler's job.
    result = await execute_research_wait(base_plan, client=client, resolve_id=_fake_resolve)

    assert result.outcome == "no_research"
    assert result.notebook_id == "nb_123"
    assert result.sources == []
    assert result.import_result is None


# ---------------------------------------------------------------------------
# P1.T2 task-id pinning regression — service-layer test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_id_pinned_after_first_discovery(monkeypatch):
    """The first non-empty task_id pins every subsequent poll's discriminator.

    Service-layer mirror of the CLI characterization test
    ``TestTaskIdPinning::test_task_id_pinned_after_first_discovery``. This
    test does NOT use the Click runner — it asserts directly on
    ``client.research.poll.await_args_list``.
    """
    polls = [
        {"status": "in_progress", "task_id": "task_pinned", "query": "AI"},
        {
            "status": "completed",
            "task_id": "task_pinned",
            "query": "AI",
            "sources": [{"title": "S", "url": "http://example.com"}],
            "report": "R",
        },
    ]
    client = _FakeClient(poll_side_effect=polls)
    plan = ResearchWaitPlan(notebook_id="nb_123", timeout=10, interval=1)

    # No-op sleep so the inter-poll wait doesn't slow the test.
    from notebooklm.cli.services import polling

    async def _no_sleep(delay):  # noqa: ARG001
        return None

    monkeypatch.setattr(polling.asyncio, "sleep", _no_sleep)

    result = await execute_research_wait(plan, client=client, resolve_id=_fake_resolve)

    assert result.outcome == "completed"
    calls = client.research.poll.await_args_list
    assert len(calls) == 2
    # First call: task_id=None (nothing pinned yet).
    assert calls[0].kwargs.get("task_id") is None
    # Second call: pinned to the value from the first poll.
    assert calls[1].kwargs.get("task_id") == "task_pinned"


@pytest.mark.asyncio
async def test_task_id_never_set_when_polls_never_return_one():
    """If poll responses never include a task_id, the discriminator stays None."""
    client = _FakeClient(
        poll_side_effect=[{"status": "completed", "query": "X", "sources": [], "report": ""}]
    )
    plan = ResearchWaitPlan(notebook_id="nb_123", timeout=5, interval=1)

    result = await execute_research_wait(plan, client=client, resolve_id=_fake_resolve)

    assert result.outcome == "completed"
    assert result.task_id is None
    # The single poll call passed task_id=None.
    assert client.research.poll.await_args_list[0].kwargs.get("task_id") is None


# ---------------------------------------------------------------------------
# Import-all path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_all_invokes_importer_with_pinned_task_id():
    """When import_all=True and a task_id was pinned, importer is invoked."""
    plan = ResearchWaitPlan(
        notebook_id="nb_123",
        timeout=300,
        interval=5,
        import_all=True,
    )
    client = _FakeClient(
        poll_side_effect=[
            {
                "status": "completed",
                "task_id": "task_abc",
                "query": "AI research",
                "sources": [{"title": "S", "url": "http://example.com"}],
                "report": "R",
            }
        ]
    )
    import_mock = AsyncMock(
        return_value=ResearchImportResult(
            imported=[{"id": "src_1", "title": "S"}],
            sources=[{"title": "S", "url": "http://example.com"}],
            cited_selection=None,
        )
    )

    result = await execute_research_wait(
        plan,
        client=client,
        resolve_id=_fake_resolve,
        import_sources=import_mock,
    )

    assert result.outcome == "completed"
    assert result.import_result is not None
    assert result.import_result.imported == [{"id": "src_1", "title": "S"}]
    import_mock.assert_awaited_once_with(
        client,
        "nb_123",
        "task_abc",
        [{"title": "S", "url": "http://example.com"}],
        report="R",
        cited_only=False,
        max_elapsed=300,
        status_message="Importing sources...",
    )


@pytest.mark.asyncio
async def test_import_all_passes_json_output_flag():
    """In JSON mode, importer receives json_output=True instead of status_message."""
    plan = ResearchWaitPlan(
        notebook_id="nb_123",
        timeout=300,
        interval=5,
        import_all=True,
        json_output=True,
    )
    client = _FakeClient(
        poll_side_effect=[
            {
                "status": "completed",
                "task_id": "task_abc",
                "query": "AI",
                "sources": [{"title": "S", "url": "http://example.com"}],
                "report": "R",
            }
        ]
    )
    import_mock = AsyncMock(
        return_value=ResearchImportResult(
            imported=[{"id": "src_1", "title": "S"}],
            sources=[{"title": "S", "url": "http://example.com"}],
            cited_selection=None,
        )
    )

    await execute_research_wait(
        plan,
        client=client,
        resolve_id=_fake_resolve,
        import_sources=import_mock,
    )

    call_kwargs = import_mock.await_args.kwargs
    assert call_kwargs.get("json_output") is True
    assert "status_message" not in call_kwargs


@pytest.mark.asyncio
async def test_import_all_skipped_when_no_task_id():
    """No task_id => importer is NOT invoked (handler-parity guard)."""
    plan = ResearchWaitPlan(
        notebook_id="nb_123",
        timeout=300,
        interval=5,
        import_all=True,
    )
    client = _FakeClient(
        poll_side_effect=[
            {
                "status": "completed",
                "query": "AI",
                "sources": [{"title": "S", "url": "http://example.com"}],
                "report": "R",
                # NO task_id key.
            }
        ]
    )
    import_mock = AsyncMock()

    result = await execute_research_wait(
        plan,
        client=client,
        resolve_id=_fake_resolve,
        import_sources=import_mock,
    )

    assert result.outcome == "completed"
    assert result.import_result is None
    import_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_import_all_skipped_when_no_sources():
    """Empty sources list => importer is NOT invoked."""
    plan = ResearchWaitPlan(
        notebook_id="nb_123",
        timeout=300,
        interval=5,
        import_all=True,
    )
    client = _FakeClient(
        poll_side_effect=[
            {
                "status": "completed",
                "task_id": "task_abc",
                "query": "AI",
                "sources": [],
                "report": "R",
            }
        ]
    )
    import_mock = AsyncMock()

    result = await execute_research_wait(
        plan,
        client=client,
        resolve_id=_fake_resolve,
        import_sources=import_mock,
    )

    assert result.outcome == "completed"
    assert result.import_result is None
    import_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_import_all_passes_cited_only_flag():
    """plan.cited_only flows through to the importer's cited_only kwarg."""
    plan = ResearchWaitPlan(
        notebook_id="nb_123",
        timeout=300,
        interval=5,
        import_all=True,
        cited_only=True,
    )
    client = _FakeClient(
        poll_side_effect=[
            {
                "status": "completed",
                "task_id": "task_abc",
                "query": "AI",
                "sources": [{"title": "S", "url": "http://example.com"}],
                "report": "R cites http://example.com",
            }
        ]
    )
    import_mock = AsyncMock(
        return_value=ResearchImportResult(
            imported=[{"id": "src_1", "title": "S"}],
            sources=[{"title": "S", "url": "http://example.com"}],
            cited_selection=None,
        )
    )

    await execute_research_wait(
        plan,
        client=client,
        resolve_id=_fake_resolve,
        import_sources=import_mock,
    )

    assert import_mock.await_args.kwargs.get("cited_only") is True


# ---------------------------------------------------------------------------
# Wait-context injection (spinner seam)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_context_is_entered_and_exited(base_plan):
    """The handler-injected wait_context wraps the poll loop."""
    enter_count = {"n": 0}
    exit_count = {"n": 0}

    @contextlib.asynccontextmanager
    async def tracking_context() -> AsyncIterator[None]:
        enter_count["n"] += 1
        try:
            yield
        finally:
            exit_count["n"] += 1

    client = _FakeClient(
        poll_side_effect=[
            {
                "status": "completed",
                "task_id": "task_abc",
                "query": "AI",
                "sources": [],
                "report": "",
            }
        ]
    )

    await execute_research_wait(
        base_plan,
        client=client,
        resolve_id=_fake_resolve,
        wait_context=tracking_context,
    )

    assert enter_count["n"] == 1
    assert exit_count["n"] == 1


@pytest.mark.asyncio
async def test_default_wait_context_is_noop(base_plan):
    """The default null context lets the service run without any spinner."""
    client = _FakeClient(
        poll_side_effect=[
            {
                "status": "completed",
                "task_id": "task_abc",
                "query": "AI",
                "sources": [],
                "report": "",
            }
        ]
    )

    # No wait_context passed; default must work.
    result = await execute_research_wait(base_plan, client=client, resolve_id=_fake_resolve)
    assert result.outcome == "completed"


@pytest.mark.asyncio
async def test_wait_context_exits_before_import_runs():
    """Spinner-vs-import ordering: import MUST run after the wait context exits.

    The pre-extraction handler kept the wait spinner open ONLY around the poll
    loop, and called the importer (which has its own spinner) after the wait
    spinner closed. This ordering matters because two live Rich spinners
    overlap badly; verifying it directly guards the most subtle refactor risk
    flagged in review.
    """
    events: list[str] = []

    @contextlib.asynccontextmanager
    async def tracking_context() -> AsyncIterator[None]:
        events.append("wait_enter")
        try:
            yield
        finally:
            events.append("wait_exit")

    async def tracking_import(*_args, **_kwargs):
        events.append("import")
        return ResearchImportResult(
            imported=[{"id": "src_1", "title": "S"}],
            sources=[{"title": "S", "url": "http://example.com"}],
            cited_selection=None,
        )

    async def tracking_resolve(client, notebook_id, *, json_output=False):  # noqa: ARG001
        events.append("resolve")
        return notebook_id

    plan = ResearchWaitPlan(
        notebook_id="nb_123",
        timeout=300,
        interval=5,
        import_all=True,
    )
    client = _FakeClient(
        poll_side_effect=[
            {
                "status": "completed",
                "task_id": "task_abc",
                "query": "AI",
                "sources": [{"title": "S", "url": "http://example.com"}],
                "report": "R",
            }
        ]
    )

    await execute_research_wait(
        plan,
        client=client,
        wait_context=tracking_context,
        resolve_id=tracking_resolve,
        import_sources=tracking_import,
    )

    # The exact lifecycle ordering we depend on for spinner-non-overlap.
    assert events == ["resolve", "wait_enter", "wait_exit", "import"]


# ---------------------------------------------------------------------------
# Dataclass invariants
# ---------------------------------------------------------------------------


class TestResearchWaitPlan:
    def test_defaults(self):
        plan = ResearchWaitPlan(notebook_id="nb", timeout=10, interval=1)
        assert plan.import_all is False
        assert plan.cited_only is False
        assert plan.json_output is False

    def test_is_frozen(self):
        from dataclasses import FrozenInstanceError

        plan = ResearchWaitPlan(notebook_id="nb", timeout=10, interval=1)
        with pytest.raises(FrozenInstanceError):
            plan.notebook_id = "other"  # type: ignore[misc]


class TestResearchWaitResult:
    def test_sources_count(self):
        result = ResearchWaitResult(
            outcome="completed",
            notebook_id="nb",
            timeout=10,
            sources=[{"a": 1}, {"b": 2}],
        )
        assert result.sources_count == 2

    def test_defaults(self):
        result = ResearchWaitResult(outcome="no_research", notebook_id="nb", timeout=10)
        assert result.task_id is None
        assert result.query == ""
        assert result.sources == []
        assert result.report == ""
        assert result.import_result is None
