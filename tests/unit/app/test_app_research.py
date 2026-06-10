"""Tests for ``notebooklm._app.research`` (transport-neutral research core).

Net-new **direct** coverage of the Click-free ``research status`` / ``research
wait`` core: flag validation, the single-poll
:func:`poll_and_classify` → :class:`ResearchStatusResult` classification, and
the :func:`execute_research_wait` outcome ladder (``no_research`` / ``timeout``
/ ``failed`` / ``completed`` + the import-gating guards). Driven with a
``MagicMock`` client and injected resolver / importer / wait-context — no Click
/ ``CliRunner``. The ``--json`` envelope projection + exit-code mapping stay in
``tests/unit/cli/test_research.py`` and ``test_research_characterization.py``.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._app.research import (
    ResearchStatusResult,
    ResearchWaitPlan,
    ResearchWaitResult,
    execute_research_wait,
    poll_and_classify,
    validate_research_wait_flags,
)
from notebooklm.exceptions import ValidationError
from notebooklm.types import ResearchSource, ResearchStatus, ResearchTask


def _task(
    *,
    status: ResearchStatus,
    task_id: str = "",
    query: str = "",
    sources: list[dict[str, Any]] | None = None,
    summary: str = "",
    report: str = "",
) -> ResearchTask:
    coerced = tuple(ResearchSource.from_public_dict(s) for s in (sources or []))
    return ResearchTask(
        task_id=task_id,
        status=status,
        query=query,
        sources=coerced,
        summary=summary,
        report=report,
    )


def _client(*, poll: ResearchTask | None = None, wait: ResearchTask | None = None) -> MagicMock:
    client = MagicMock()
    client.research = MagicMock()
    if poll is not None:
        client.research.poll = AsyncMock(return_value=poll)
    if wait is not None:
        client.research.wait_for_completion = AsyncMock(return_value=wait)
    return client


async def _resolve_passthrough(client: Any, nb_id: str, *, json_output: bool = False) -> str:
    return nb_id


# ===========================================================================
# validate_research_wait_flags
# ===========================================================================


def test_validate_flags_cited_only_requires_import_all() -> None:
    with pytest.raises(ValidationError) as caught:
        validate_research_wait_flags(import_all=False, cited_only=True)
    assert "--cited-only requires --import-all" in str(caught.value)


def test_validate_flags_cited_only_with_import_all_ok() -> None:
    # No raise.
    validate_research_wait_flags(import_all=True, cited_only=True)


def test_validate_flags_import_all_alone_ok() -> None:
    validate_research_wait_flags(import_all=True, cited_only=False)


def test_validate_flags_neither_flag_ok() -> None:
    validate_research_wait_flags(import_all=False, cited_only=False)


# ===========================================================================
# poll_and_classify
# ===========================================================================


async def test_poll_classifies_no_research() -> None:
    client = _client(poll=_task(status=ResearchStatus.NO_RESEARCH))
    result = await poll_and_classify(client, "nb_1")

    assert isinstance(result, ResearchStatusResult)
    assert result.kind == "no_research"
    assert result.status == "no_research"
    client.research.poll.assert_awaited_once_with("nb_1", None)


async def test_poll_classifies_in_progress_carries_query() -> None:
    client = _client(poll=_task(status=ResearchStatus.IN_PROGRESS, query="AI research"))
    result = await poll_and_classify(client, "nb_1")

    assert result.kind == "in_progress"
    assert result.query == "AI research"


async def test_poll_classifies_completed_serializes_sources_and_public_dict() -> None:
    client = _client(
        poll=_task(
            status=ResearchStatus.COMPLETED,
            query="AI research",
            sources=[{"title": "Source 1", "url": "http://example.com/1"}],
            summary="A summary",
            report="# Report",
        )
    )
    result = await poll_and_classify(client, "nb_1")

    assert result.kind == "completed"
    assert result.status == "completed"
    assert result.summary == "A summary"
    assert result.report == "# Report"
    # Sources serialize back to the canonical public-dict shape.
    assert result.sources == [
        {"url": "http://example.com/1", "title": "Source 1", "result_type": 1}
    ]
    # public_dict is the verbatim --json payload the CLI emits.
    assert result.public_dict["status"] == "completed"
    assert result.public_dict["sources"] == result.sources


async def test_poll_classifies_failed_as_other() -> None:
    """``failed`` is a terminal status the CLI renders via the generic branch."""
    client = _client(poll=_task(status=ResearchStatus.FAILED, query="AI research"))
    result = await poll_and_classify(client, "nb_1")

    assert result.kind == "other"
    assert result.status == "failed"


# ===========================================================================
# execute_research_wait — outcome classification
# ===========================================================================


async def test_wait_completed_without_import() -> None:
    completed = _task(
        status=ResearchStatus.COMPLETED,
        task_id="task_1",
        query="AI research",
        sources=[{"title": "S1", "url": "http://example.com"}],
        report="# Report",
    )
    client = _client(wait=completed)
    plan = ResearchWaitPlan(notebook_id="nb_1", timeout=300, interval=5)

    result = await execute_research_wait(plan, client=client, resolve_id=_resolve_passthrough)

    assert isinstance(result, ResearchWaitResult)
    assert result.outcome == "completed"
    assert result.task_id == "task_1"
    assert result.query == "AI research"
    assert result.sources_count == 1
    assert result.report == "# Report"
    assert result.import_result is None


async def test_wait_no_research_outcome() -> None:
    client = _client(wait=_task(status=ResearchStatus.NO_RESEARCH))
    plan = ResearchWaitPlan(notebook_id="nb_1", timeout=300, interval=5)

    result = await execute_research_wait(plan, client=client, resolve_id=_resolve_passthrough)

    assert result.outcome == "no_research"


async def test_wait_failed_outcome_carries_query_and_sources() -> None:
    failed = _task(
        status=ResearchStatus.FAILED,
        task_id="task_1",
        query="AI research",
        sources=[{"title": "S1", "url": "http://example.com"}],
        report="# Partial",
    )
    client = _client(wait=failed)
    plan = ResearchWaitPlan(notebook_id="nb_1", timeout=300, interval=5)

    result = await execute_research_wait(plan, client=client, resolve_id=_resolve_passthrough)

    assert result.outcome == "failed"
    assert result.query == "AI research"
    assert result.sources_count == 1
    assert result.report == "# Partial"


async def test_wait_timeout_outcome() -> None:
    client = _client()
    client.research.wait_for_completion = AsyncMock(side_effect=TimeoutError)
    plan = ResearchWaitPlan(notebook_id="nb_1", timeout=42, interval=5)

    result = await execute_research_wait(plan, client=client, resolve_id=_resolve_passthrough)

    assert result.outcome == "timeout"
    assert result.timeout == 42


async def test_wait_resolves_notebook_id_through_injected_resolver() -> None:
    client = _client(wait=_task(status=ResearchStatus.NO_RESEARCH))
    resolver = AsyncMock(return_value="nb_resolved")
    plan = ResearchWaitPlan(notebook_id="nb_partial", timeout=300, interval=5, json_output=True)

    result = await execute_research_wait(plan, client=client, resolve_id=resolver)

    resolver.assert_awaited_once_with(client, "nb_partial", json_output=True)
    assert result.notebook_id == "nb_resolved"


# ===========================================================================
# execute_research_wait — import gating
# ===========================================================================


async def test_wait_import_invoked_when_completed_with_sources_and_task_id() -> None:
    completed = _task(
        status=ResearchStatus.COMPLETED,
        task_id="task_1",
        query="AI research",
        sources=[{"title": "S1", "url": "http://example.com"}],
        report="# Report",
    )
    client = _client(wait=completed)
    import_result = MagicMock(imported=[{"id": "src_1"}], sources=[], cited_selection=None)
    importer = AsyncMock(return_value=import_result)
    plan = ResearchWaitPlan(notebook_id="nb_1", timeout=300, interval=5, import_all=True)

    result = await execute_research_wait(
        plan,
        client=client,
        resolve_id=_resolve_passthrough,
        import_sources=importer,
    )

    assert result.outcome == "completed"
    assert result.import_result is import_result
    importer.assert_awaited_once()
    awaited = importer.await_args
    assert awaited is not None
    # Positional: client, nb_id, task_id, sources.
    assert awaited.args[2] == "task_1"
    assert awaited.kwargs["cited_only"] is False
    assert awaited.kwargs["max_elapsed"] == 300
    # Text mode (json_output False) routes a status message rather than json_output.
    assert awaited.kwargs["status_message"] == "Importing sources..."
    assert "json_output" not in awaited.kwargs


async def test_wait_import_json_mode_passes_json_output_flag() -> None:
    completed = _task(
        status=ResearchStatus.COMPLETED,
        task_id="task_1",
        sources=[{"title": "S1", "url": "http://example.com"}],
    )
    client = _client(wait=completed)
    importer = AsyncMock(return_value=MagicMock(imported=[], sources=[], cited_selection=None))
    plan = ResearchWaitPlan(
        notebook_id="nb_1", timeout=120, interval=5, import_all=True, json_output=True
    )

    await execute_research_wait(
        plan,
        client=client,
        resolve_id=_resolve_passthrough,
        import_sources=importer,
    )

    awaited = importer.await_args
    assert awaited is not None
    assert awaited.kwargs["json_output"] is True
    assert "status_message" not in awaited.kwargs


async def test_wait_import_skipped_when_no_task_id() -> None:
    """No task_id means the importer has nothing to verify against — skip it."""
    completed = _task(
        status=ResearchStatus.COMPLETED,
        task_id="",
        sources=[{"title": "S1", "url": "http://example.com"}],
    )
    client = _client(wait=completed)
    importer = AsyncMock()
    plan = ResearchWaitPlan(notebook_id="nb_1", timeout=300, interval=5, import_all=True)

    result = await execute_research_wait(
        plan,
        client=client,
        resolve_id=_resolve_passthrough,
        import_sources=importer,
    )

    assert result.outcome == "completed"
    assert result.import_result is None
    importer.assert_not_awaited()


async def test_wait_import_skipped_when_no_sources() -> None:
    completed = _task(status=ResearchStatus.COMPLETED, task_id="task_1", sources=[])
    client = _client(wait=completed)
    importer = AsyncMock()
    plan = ResearchWaitPlan(notebook_id="nb_1", timeout=300, interval=5, import_all=True)

    result = await execute_research_wait(
        plan,
        client=client,
        resolve_id=_resolve_passthrough,
        import_sources=importer,
    )

    assert result.import_result is None
    importer.assert_not_awaited()


async def test_wait_import_skipped_when_import_all_false() -> None:
    completed = _task(
        status=ResearchStatus.COMPLETED,
        task_id="task_1",
        sources=[{"title": "S1", "url": "http://example.com"}],
    )
    client = _client(wait=completed)
    importer = AsyncMock()
    plan = ResearchWaitPlan(notebook_id="nb_1", timeout=300, interval=5, import_all=False)

    result = await execute_research_wait(
        plan,
        client=client,
        resolve_id=_resolve_passthrough,
        import_sources=importer,
    )

    assert result.import_result is None
    importer.assert_not_awaited()


async def test_wait_runs_inside_injected_wait_context() -> None:
    """The polling loop is wrapped by the injected wait-context factory."""
    events: list[str] = []

    @contextlib.asynccontextmanager
    async def tracking_context() -> AsyncIterator[None]:
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    client = _client(wait=_task(status=ResearchStatus.NO_RESEARCH))
    plan = ResearchWaitPlan(notebook_id="nb_1", timeout=300, interval=5)

    await execute_research_wait(
        plan,
        client=client,
        resolve_id=_resolve_passthrough,
        wait_context=tracking_context,
    )

    assert events == ["enter", "exit"]
