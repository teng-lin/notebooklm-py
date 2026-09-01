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
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._app.research import (
    ResearchStatusResult,
    ResearchWaitPlan,
    ResearchWaitResult,
    cancel_research,
    classify_research_task,
    discover_and_classify,
    execute_research_wait,
    poll_and_classify,
    poll_importable_research,
    poll_sources_for_import,
    validate_research_wait_flags,
)
from notebooklm.exceptions import ValidationError
from notebooklm.types import DiscoveryMode, ResearchSource, ResearchStatus, ResearchTask


def _task(
    *,
    status: ResearchStatus,
    task_id: str = "",
    query: str = "",
    sources: list[dict[str, Any]] | None = None,
    summary: str = "",
    report: str = "",
    status_code: int | None = None,
    source_type: int | None = None,
    discovery_mode: DiscoveryMode | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> ResearchTask:
    coerced = tuple(ResearchSource.from_public_dict(s) for s in (sources or []))
    return ResearchTask(
        task_id=task_id,
        status=status,
        query=query,
        sources=coerced,
        summary=summary,
        report=report,
        status_code=status_code,
        source_type=source_type,
        discovery_mode=discovery_mode,
        created_at=created_at,
        updated_at=updated_at,
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
# cancel_research
# ===========================================================================


async def test_cancel_research_forwards_run_id_and_returns_none() -> None:
    client = _client()
    client.research.cancel = AsyncMock(return_value=None)

    result = await cancel_research(client, "nb_1", "run_9")

    assert result is None
    client.research.cancel.assert_awaited_once_with("nb_1", "run_9")


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
# poll_importable_research / poll_sources_for_import
# ===========================================================================


async def test_poll_importable_returns_sources_and_report() -> None:
    client = _client(
        poll=_task(
            status=ResearchStatus.COMPLETED,
            sources=[{"title": "S", "url": "http://example.com/1"}],
            report="# Report",
        )
    )
    sources, report = await poll_importable_research(client, "nb_1", "run_1")
    assert report == "# Report"
    assert sources[0]["url"] == "http://example.com/1"
    # The pinned run id is threaded through poll as the discriminator.
    client.research.poll.assert_awaited_once_with("nb_1", "run_1")


async def test_poll_sources_for_import_drops_report() -> None:
    """The thin wrapper returns just the sources (REST import path)."""
    client = _client(
        poll=_task(
            status=ResearchStatus.COMPLETED,
            sources=[{"title": "S", "url": "http://example.com/1"}],
            report="# Report",
        )
    )
    sources = await poll_sources_for_import(client, "nb_1", "run_1")
    assert sources[0]["url"] == "http://example.com/1"


@pytest.mark.parametrize(
    "status",
    [
        ResearchStatus.NOT_FOUND,
        ResearchStatus.FAILED,
        ResearchStatus.IN_PROGRESS,
        ResearchStatus.NO_RESEARCH,
    ],
)
async def test_poll_importable_refuses_non_completed(status: ResearchStatus) -> None:
    client = _client(
        poll=_task(status=status, sources=[{"title": "S", "url": "http://example.com/1"}])
    )
    with pytest.raises(ValidationError):
        await poll_importable_research(client, "nb_1", "run_1")


async def test_poll_importable_refuses_completed_empty() -> None:
    client = _client(poll=_task(status=ResearchStatus.COMPLETED, sources=[]))
    with pytest.raises(ValidationError):
        await poll_importable_research(client, "nb_1", "run_1")


# ---------------------------------------------------------------------------
# Differentiated termination reasons (#1964)
# ---------------------------------------------------------------------------


async def test_poll_and_classify_surfaces_reason_message_and_hint() -> None:
    """An empty Drive search reaches the adapter with a reason and remediation,
    not a bare ``failed``."""
    client = _client(
        poll=_task(
            status=ResearchStatus.FAILED,
            query="Example Document.md",
            status_code=3,
            source_type=2,
        )
    )
    result = await poll_and_classify(client, "nb_1", "run_1")
    assert result.status == "failed"
    assert result.status_code == 3
    assert result.termination_reason == "no_results"
    assert "no matches" in result.reason_message
    assert "document id" in result.hint


async def test_poll_and_classify_leaves_reason_fields_none_on_success() -> None:
    client = _client(poll=_task(status=ResearchStatus.COMPLETED, status_code=2, source_type=1))
    result = await poll_and_classify(client, "nb_1", "run_1")
    assert result.termination_reason == "completed"
    assert result.reason_message is None
    assert result.hint is None


async def test_poll_and_classify_reason_none_without_status_code() -> None:
    client = _client(poll=_task(status=ResearchStatus.NO_RESEARCH))
    result = await poll_and_classify(client, "nb_1", "run_1")
    assert result.termination_reason is None
    assert result.reason_message is None


async def test_poll_importable_empty_drive_search_explains_instead_of_misdirecting() -> None:
    """Regression for #1964: the old message told the caller to 'start a new
    research session', which is the wrong remediation for a query that simply
    matched nothing."""
    client = _client(
        poll=_task(
            status=ResearchStatus.FAILED,
            query="Example Document.md",
            status_code=3,
            source_type=2,
        )
    )
    with pytest.raises(ValidationError) as excinfo:
        await poll_importable_research(client, "nb_1", "run_1")
    message = str(excinfo.value)
    assert "no matches" in message
    assert "document id" in message
    assert "start a new research session" not in message


async def test_poll_importable_cancelled_run_says_it_was_cancelled() -> None:
    client = _client(
        poll=_task(status=ResearchStatus.FAILED, query="q", status_code=4, source_type=1)
    )
    with pytest.raises(ValidationError) as excinfo:
        await poll_importable_research(client, "nb_1", "run_1")
    assert "cancelled" in str(excinfo.value)


async def test_poll_importable_unnameable_failure_keeps_historical_message() -> None:
    """A FAILED task carrying no status code keeps the original wording.

    Defensive rather than observed: the parser only produces FAILED from a
    non-null code, so this pairing should not arise from a real poll. It pins
    the branch against a hand-built or future task that reaches it.
    """
    client = _client(poll=_task(status=ResearchStatus.FAILED, query="q"))
    with pytest.raises(ValidationError) as excinfo:
        await poll_importable_research(client, "nb_1", "run_1")
    assert "start a new research session" in str(excinfo.value)


async def test_poll_importable_unknown_code_keeps_terminal_guidance() -> None:
    """An unrecognised code is coarsened to FAILED and treated as terminal, so
    "it will not complete" is still the right advice — the first cut sent every
    code-carrying failure down the differentiated path and silently dropped it
    for genuinely-broken runs.
    """
    client = _client(
        poll=_task(status=ResearchStatus.FAILED, query="q", status_code=7, source_type=1)
    )
    with pytest.raises(ValidationError) as excinfo:
        await poll_importable_research(client, "nb_1", "run_1")
    message = str(excinfo.value)
    assert "it will not complete" in message
    assert "start a new research session" in message
    # ...and it still reports what was actually observed.
    assert "unrecognised backend status code (7)" in message


async def test_poll_importable_cancelled_run_with_partial_sources_wording() -> None:
    """A cancelled run can carry partially-discovered sources, so the refusal
    must not assert it "has no sources"."""
    client = _client(
        poll=_task(
            status=ResearchStatus.FAILED,
            query="q",
            sources=[{"title": "Partial", "url": "http://example.com/1"}],
            status_code=4,
            source_type=1,
        )
    )
    with pytest.raises(ValidationError) as excinfo:
        await poll_importable_research(client, "nb_1", "run_1")
    message = str(excinfo.value)
    assert "cannot be imported" in message
    assert "has no sources" not in message


@pytest.mark.parametrize(
    ("status_code", "expected_reason"),
    [(1, "in_progress"), (2, "completed"), (3, "no_results"), (4, "cancelled"), (7, "unknown")],
)
async def test_poll_and_classify_converts_every_reason_to_its_string(
    status_code: int, expected_reason: str
) -> None:
    """The enum→str narrowing at the _app boundary must hold for every reason,
    not just the two the happy-path tests exercise."""
    client = _client(
        poll=_task(status=ResearchStatus.FAILED, query="q", status_code=status_code, source_type=1)
    )
    result = await poll_and_classify(client, "nb_1", "run_1")
    assert result.termination_reason == expected_reason
    assert isinstance(result.termination_reason, str)


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


async def test_wait_threads_an_explicit_task_id_without_changing_default_calls() -> None:
    client = _client(wait=_task(status=ResearchStatus.COMPLETED, task_id="task_exact"))
    plan = ResearchWaitPlan(
        notebook_id="nb_1",
        timeout=300,
        interval=5,
        task_id="task_exact",
    )

    await execute_research_wait(plan, client=client, resolve_id=_resolve_passthrough)

    client.research.wait_for_completion.assert_awaited_once_with(
        "nb_1",
        "task_exact",
        timeout=300.0,
        initial_interval=5.0,
    )


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


# ---------------------------------------------------------------------------
# import_research_sources (#1961 idempotent import wrapper)
# ---------------------------------------------------------------------------


async def test_import_research_sources_reports_already_present() -> None:
    from notebooklm._app.research import ResearchImportOutcome, import_research_sources
    from notebooklm._research import _imported_result

    client = MagicMock()
    client.research = MagicMock()
    client.research.import_sources_with_verification = AsyncMock(
        return_value=_imported_result(
            [{"id": "new_1", "title": "New"}],
            [{"id": "old_1", "title": "Old", "url": "https://old.example.com"}],
        )
    )

    outcome = await import_research_sources(
        client, "nb_1", "task_1", [{"url": "https://new.example.com", "title": "New"}]
    )

    assert isinstance(outcome, ResearchImportOutcome)
    assert outcome.newly_imported == [{"id": "new_1", "title": "New"}]
    assert outcome.already_present == [
        {"id": "old_1", "title": "Old", "url": "https://old.example.com"}
    ]
    assert outcome.newly_imported_count == 1
    assert outcome.already_present_count == 1
    # First three args are positional (MCP tests assert args[0]/args[1]); the
    # opt-out threads through as a keyword.
    args, kwargs = client.research.import_sources_with_verification.await_args
    assert args[0] == "nb_1"
    assert args[1] == "task_1"
    assert args[2] == [{"url": "https://new.example.com", "title": "New"}]
    assert kwargs == {"allow_duplicate": False}


async def test_import_research_sources_plain_list_return_has_empty_already_present() -> None:
    from notebooklm._app.research import import_research_sources

    client = MagicMock()
    client.research = MagicMock()
    client.research.import_sources_with_verification = AsyncMock(
        return_value=[{"id": "new_1", "title": "New"}]
    )

    outcome = await import_research_sources(client, "nb_1", "task_1", [], allow_duplicate=True)

    assert outcome.newly_imported == [{"id": "new_1", "title": "New"}]
    assert outcome.already_present == []
    _, kwargs = client.research.import_sources_with_verification.await_args
    assert kwargs == {"allow_duplicate": True}


# ===========================================================================
# Run metadata carried onto the shared MCP/REST view (#2122)
# ===========================================================================

_CREATED = datetime(2026, 8, 13, 11, 12, 58, tzinfo=timezone.utc)
_UPDATED = datetime(2026, 8, 13, 11, 13, 5, tzinfo=timezone.utc)


async def test_poll_projects_run_metadata_for_the_transports() -> None:
    client = _client(
        poll=_task(
            status=ResearchStatus.COMPLETED,
            query="AI research",
            discovery_mode=DiscoveryMode.DEEP_RESEARCH,
            created_at=_CREATED,
            updated_at=_UPDATED,
        )
    )
    result = await poll_and_classify(client, "nb_1")

    # The LABEL, not the raw 5 — an agent must not have to know the enum.
    assert result.discovery_mode == "deep_research"
    assert result.created_at == "2026-08-13T11:12:58+00:00"
    assert result.updated_at == "2026-08-13T11:13:05+00:00"
    assert result.duration_seconds == 7.0


async def test_poll_reports_absent_run_metadata_as_none() -> None:
    """Most of these are optional on the wire; absence must not fabricate."""
    client = _client(poll=_task(status=ResearchStatus.IN_PROGRESS, query="q"))
    result = await poll_and_classify(client, "nb_1")

    assert result.discovery_mode is None
    assert result.created_at is None
    assert result.updated_at is None
    assert result.duration_seconds is None


async def test_run_metadata_stays_off_the_byte_stable_public_dict() -> None:
    """``public_dict`` is the CLI ``--json`` payload emitted verbatim, so the
    task-level additions deliberately do not appear there (matching
    ``status_code`` / ``source_type``)."""
    client = _client(
        poll=_task(
            status=ResearchStatus.COMPLETED,
            discovery_mode=DiscoveryMode.DEFAULT_LLM_SEARCH,
            created_at=_CREATED,
            updated_at=_UPDATED,
        )
    )
    result = await poll_and_classify(client, "nb_1")

    assert "discovery_mode" not in result.public_dict
    assert "created_at" not in result.public_dict
    assert "updated_at" not in result.public_dict


async def test_source_hint_does_reach_the_public_source_dicts() -> None:
    """Unlike the task-level fields, the per-source hint rides the existing
    conditional-key convention (``source_ordinal`` / ``report_markdown``), so
    it reaches the CLI, MCP and REST source payloads alike."""
    client = _client(
        poll=_task(
            status=ResearchStatus.COMPLETED,
            sources=[
                {"url": "http://example.com/1", "title": "S1", "hint": "why this one"},
                {"url": "http://example.com/2", "title": "S2"},
            ],
        )
    )
    result = await poll_and_classify(client, "nb_1")

    assert result.sources[0]["hint"] == "why this one"
    assert "hint" not in result.sources[1]


async def test_unmappable_discovery_mode_reaches_the_transports_as_unknown() -> None:
    """The adapter's UNKNOWN-vs-None distinction has to survive projection.

    ``None`` means "the poll made no mode claim"; ``"unknown"`` means "the
    backend named a mode this client cannot read" — i.e. Google added a mode
    and the enum needs updating. If both projected to ``null`` the drift signal
    would die at the transport boundary, one layer short of the operator.
    """
    client = _client(
        poll=_task(status=ResearchStatus.COMPLETED, discovery_mode=DiscoveryMode.UNKNOWN)
    )
    result = await poll_and_classify(client, "nb_1")
    assert result.discovery_mode == "unknown"


# ===========================================================================
# discover_and_classify
# ===========================================================================


@pytest.mark.asyncio
async def test_discover_and_classify_projects_the_completed_task_like_a_poll() -> None:
    task = _task(
        status=ResearchStatus.COMPLETED,
        task_id="job_001",
        query="history of the transistor",
        sources=[{"title": "Source 1", "url": "http://example.com/1", "hint": "why"}],
        summary="An overview.",
        status_code=2,
        source_type=1,
        discovery_mode=DiscoveryMode.RAW_SEARCH,
    )
    client = MagicMock()
    client.research = MagicMock()
    client.research.discover = AsyncMock(return_value=task)

    result = await discover_and_classify(client, "nb_1", "history of the transistor", "raw")

    client.research.discover.assert_awaited_once_with(
        "nb_1", "history of the transistor", mode="raw"
    )
    assert result == classify_research_task(task)
    assert result.kind == "completed" and result.task_id == "job_001"
    assert result.summary == "An overview." and result.report == ""
    assert result.sources == [
        {"url": "http://example.com/1", "title": "Source 1", "result_type": 1, "hint": "why"}
    ]
    assert result.public_dict == task.to_public_dict()
    assert result.status_code == 2 and result.discovery_mode == "raw_search"
    assert result.termination_reason == "completed"
    assert result.reason_message is None and result.hint is None


@pytest.mark.asyncio
async def test_discover_and_classify_defaults_to_the_default_mode() -> None:
    client = MagicMock()
    client.research = MagicMock()
    client.research.discover = AsyncMock(return_value=_task(status=ResearchStatus.COMPLETED))
    await discover_and_classify(client, "nb_1", "q")
    client.research.discover.assert_awaited_once_with("nb_1", "q", mode="default")
