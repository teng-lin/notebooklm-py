"""Direct service-layer tests for extracted ``cli/services/source_*`` modules."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from notebooklm.cli import source_cmd
from notebooklm.cli.services import source_mutations, source_research
from notebooklm.cli.services.source_content import (
    SourceFulltextPlan,
    SourceGuidePlan,
    execute_source_fulltext,
    execute_source_guide,
)
from notebooklm.cli.services.source_mutations import (
    SourceDeletePlan,
    SourceMutationError,
    SourceRenamePlan,
    execute_source_delete,
    execute_source_rename,
)
from notebooklm.cli.services.source_research import (
    SourceAddResearchPlan,
    SourceAddResearchResult,
    execute_source_add_research,
)
from notebooklm.cli.services.source_wait import (
    SourceWaitPlan,
    SourceWaitTimeout,
    execute_source_wait,
)
from notebooklm.types import Source, SourceFulltext, SourceTimeoutError


@pytest.mark.asyncio
async def test_source_delete_json_without_yes_uses_structured_confirmation_error() -> None:
    client = SimpleNamespace(
        sources=SimpleNamespace(
            list=AsyncMock(return_value=[Source(id="src_abcdef", title="Paper")]),
            delete=AsyncMock(),
        )
    )
    plan = SourceDeletePlan(
        notebook_id="nb_1",
        source_id="src_abc",
        yes=False,
        json_output=True,
    )

    with pytest.raises(SourceMutationError) as exc_info:
        await execute_source_delete(
            client,
            plan,
            confirmer=lambda message: pytest.fail(f"unexpected confirmation: {message}"),
        )

    assert exc_info.value.message == "Pass --yes to confirm destructive operation in --json mode"
    assert exc_info.value.code == "CONFIRM_REQUIRED"
    assert exc_info.value.extra == {
        "action": "delete",
        "source_id": "src_abcdef",
        "notebook_id": "nb_1",
    }
    assert exc_info.value.status_message == "[dim]Matched: src_abcdef... (Paper)[/dim]"
    client.sources.delete.assert_not_called()


@pytest.mark.asyncio
async def test_source_rename_returns_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        source_mutations,
        "resolve_source_id",
        AsyncMock(return_value="src_full"),
    )
    client = SimpleNamespace(
        sources=SimpleNamespace(rename=AsyncMock(return_value=Source(id="src_full", title="New")))
    )

    result = await execute_source_rename(
        client,
        SourceRenamePlan(
            notebook_id="nb_1",
            source_id="src",
            new_title="New",
            json_output=True,
        ),
    )

    client.sources.rename.assert_awaited_once_with("nb_1", "src_full", "New")
    assert result.payload == {
        "action": "rename",
        "source_id": "src_full",
        "notebook_id": "nb_1",
        "title": "New",
        "status": "renamed",
    }


@pytest.mark.asyncio
async def test_source_fulltext_service_returns_fetched_content() -> None:
    client = SimpleNamespace(
        sources=SimpleNamespace(
            get_fulltext=AsyncMock(
                return_value=SourceFulltext(
                    source_id="src_1",
                    title="Paper",
                    content="full content",
                    char_count=12,
                )
            )
        )
    )

    result = await execute_source_fulltext(
        client,
        SourceFulltextPlan(
            notebook_id="nb_1",
            source_id="src_1",
            output_format="text",
        ),
    )

    assert result.fulltext.source_id == "src_1"
    assert result.fulltext.title == "Paper"
    assert result.fulltext.content == "full content"
    assert result.fulltext.char_count == 12


@pytest.mark.asyncio
async def test_source_guide_service_normalizes_untrusted_backend_payload() -> None:
    client = SimpleNamespace(
        sources=SimpleNamespace(
            get_guide=AsyncMock(
                return_value={
                    "summary": 42,
                    "keywords": [" alpha ", 7, "", "   ", None, "beta"],
                }
            )
        )
    )

    result = await execute_source_guide(
        client,
        SourceGuidePlan(notebook_id="nb_1", source_id="src_1"),
    )

    assert result.source_id == "src_1"
    assert result.summary == ""
    assert result.keywords == ("alpha", "beta")


@pytest.mark.asyncio
async def test_source_guide_service_treats_non_mapping_payload_as_empty() -> None:
    client = SimpleNamespace(sources=SimpleNamespace(get_guide=AsyncMock(return_value=None)))

    result = await execute_source_guide(
        client,
        SourceGuidePlan(notebook_id="nb_1", source_id="src_1"),
    )

    assert result.source_id == "src_1"
    assert result.is_empty


@pytest.mark.asyncio
async def test_source_add_research_waits_with_started_task_id_and_imports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported = SimpleNamespace(imported=["src_1"], cited_selection=None, sources=[])
    import_research_sources = AsyncMock(return_value=imported)
    monkeypatch.setattr(source_research, "import_research_sources", import_research_sources)
    client = SimpleNamespace(
        research=SimpleNamespace(
            start=AsyncMock(return_value={"task_id": "task_123"}),
            wait_for_completion=AsyncMock(
                return_value={
                    "status": "completed",
                    "task_id": "task_123",
                    "sources": [{"title": "Result"}],
                    "report": "Report",
                }
            ),
        )
    )

    result = await execute_source_add_research(
        client,
        SourceAddResearchPlan(
            notebook_id="nb_1",
            query="topic",
            search_source="web",
            mode="deep",
            import_all=True,
            cited_only=True,
            no_wait=False,
            timeout=30,
        ),
    )

    assert result.outcome == "completed"
    assert result.poll_task_id == "task_123"
    assert result.sources == [{"title": "Result"}]
    assert result.report == "Report"
    assert result.import_result is imported
    client.research.start.assert_awaited_once_with("nb_1", "topic", "web", "deep")
    client.research.wait_for_completion.assert_awaited_once_with(
        "nb_1",
        task_id="task_123",
        timeout=30.0,
        interval=5.0,
    )
    import_research_sources.assert_awaited_once_with(
        client,
        "nb_1",
        "task_123",
        [{"title": "Result"}],
        report="Report",
        cited_only=True,
        max_elapsed=30,
    )


@pytest.mark.asyncio
async def test_source_add_research_json_import_stays_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported = SimpleNamespace(imported=["src_1"], cited_selection=None, sources=[])
    import_research_sources = AsyncMock(return_value=imported)
    monkeypatch.setattr(source_research, "import_research_sources", import_research_sources)
    client = SimpleNamespace(
        research=SimpleNamespace(
            start=AsyncMock(return_value={"task_id": "task_123"}),
            wait_for_completion=AsyncMock(
                return_value={
                    "status": "completed",
                    "task_id": "task_123",
                    "sources": [{"title": "Result"}],
                    "report": "Report",
                }
            ),
        )
    )

    result = await execute_source_add_research(
        client,
        SourceAddResearchPlan(
            notebook_id="nb_1",
            query="topic",
            search_source="web",
            mode="fast",
            import_all=True,
            cited_only=True,
            no_wait=False,
            timeout=30,
            json_output=True,
        ),
    )

    assert result.outcome == "completed"
    import_research_sources.assert_awaited_once_with(
        client,
        "nb_1",
        "task_123",
        [{"title": "Result"}],
        report="Report",
        cited_only=True,
        max_elapsed=30,
        json_output=True,
    )


def test_render_add_research_started_no_wait_json_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads: list[dict[str, object]] = []
    monkeypatch.setattr(source_cmd, "json_output_response", payloads.append)

    source_cmd._render_add_research_result(
        SourceAddResearchResult(
            outcome="started_no_wait",
            plan=SourceAddResearchPlan(
                notebook_id="nb_1",
                query="topic",
                search_source="web",
                mode="deep",
                import_all=False,
                cited_only=False,
                no_wait=True,
                timeout=30,
                json_output=True,
            ),
            start_task_id="task_123",
            poll_task_id="report_456",
        ),
        json_output=True,
    )

    assert payloads == [
        {
            "status": "started",
            "task_id": "task_123",
            "poll_task_id": "report_456",
        }
    ]


def test_render_add_research_completed_json_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads: list[dict[str, object]] = []
    monkeypatch.setattr(source_cmd, "json_output_response", payloads.append)
    import_result = SimpleNamespace(
        imported=[{"id": "src_1", "title": "Result"}],
        cited_selection=SimpleNamespace(used_fallback=False),
        sources=[{"title": "Result"}],
    )

    source_cmd._render_add_research_result(
        SourceAddResearchResult(
            outcome="completed",
            plan=SourceAddResearchPlan(
                notebook_id="nb_1",
                query="topic",
                search_source="web",
                mode="fast",
                import_all=True,
                cited_only=True,
                no_wait=False,
                timeout=30,
                json_output=True,
            ),
            start_task_id="task_123",
            poll_task_id="task_123",
            sources=[{"title": "Result"}],
            report="Report",
            import_result=import_result,
        ),
        json_output=True,
    )

    assert payloads == [
        {
            "status": "completed",
            "task_id": "task_123",
            "sources_found": 1,
            "sources": [{"title": "Result"}],
            "report": "Report",
            "cited_only": True,
            "cited_sources_selected": 1,
            "cited_only_fallback": False,
            "imported": 1,
            "imported_sources": [{"id": "src_1", "title": "Result"}],
        }
    ]


def test_render_add_research_completed_text_keeps_task_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed: list[object] = []
    monkeypatch.setattr(
        source_cmd.console, "print", lambda message="", *_, **__: printed.append(message)
    )
    monkeypatch.setattr(source_cmd, "display_research_sources", lambda sources: None)
    monkeypatch.setattr(source_cmd, "display_report", lambda report, json_hint=False: None)

    source_cmd._render_add_research_result(
        SourceAddResearchResult(
            outcome="completed",
            plan=SourceAddResearchPlan(
                notebook_id="nb_1",
                query="topic",
                search_source="web",
                mode="deep",
                import_all=False,
                cited_only=False,
                no_wait=False,
                timeout=30,
            ),
            start_task_id="task_123",
            poll_task_id="report_456",
            sources=[{"title": "Result"}],
            report="Report",
        ),
        json_output=False,
    )

    assert "[dim]Task ID: task_123[/dim]" in printed
    assert "[dim]Poll ID: report_456[/dim]" in printed


@pytest.mark.parametrize("status", ["failed", "timeout"])
@pytest.mark.asyncio
async def test_source_add_research_failed_or_timeout_returns_terminal_outcome(
    status: str,
) -> None:
    client = SimpleNamespace(
        research=SimpleNamespace(
            start=AsyncMock(return_value={"task_id": "task_123"}),
            wait_for_completion=AsyncMock(return_value={"status": status, "task_id": "task_123"}),
        )
    )

    result = await execute_source_add_research(
        client,
        SourceAddResearchPlan(
            notebook_id="nb_1",
            query="topic",
            search_source="web",
            mode="deep",
            import_all=False,
            cited_only=False,
            no_wait=False,
            timeout=30,
        ),
    )

    # status="failed" -> outcome="failed"; status="timeout" from the wait_for_completion
    # return (not the TimeoutError branch) is treated as the same terminal class.
    assert result.outcome == status
    assert result.start_task_id == "task_123"
    assert result.poll_task_id == "task_123"


@pytest.mark.asyncio
async def test_source_add_research_start_failed_returns_outcome() -> None:
    """``research.start`` returning falsy maps to ``outcome='start_failed'``."""
    client = SimpleNamespace(
        research=SimpleNamespace(
            start=AsyncMock(return_value=None),
            wait_for_completion=AsyncMock(),
        )
    )

    result = await execute_source_add_research(
        client,
        SourceAddResearchPlan(
            notebook_id="nb_1",
            query="topic",
            search_source="web",
            mode="fast",
            import_all=False,
            cited_only=False,
            no_wait=False,
            timeout=30,
        ),
    )

    assert result.outcome == "start_failed"
    assert result.start_task_id is None
    client.research.wait_for_completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_add_research_timeout_error_maps_to_timeout_outcome() -> None:
    """``wait_for_completion`` raising :class:`TimeoutError` maps to ``timeout``."""
    client = SimpleNamespace(
        research=SimpleNamespace(
            start=AsyncMock(return_value={"task_id": "task_123"}),
            wait_for_completion=AsyncMock(side_effect=TimeoutError("budget exhausted")),
        )
    )

    result = await execute_source_add_research(
        client,
        SourceAddResearchPlan(
            notebook_id="nb_1",
            query="topic",
            search_source="web",
            mode="fast",
            import_all=False,
            cited_only=False,
            no_wait=False,
            timeout=30,
        ),
    )

    assert result.outcome == "timeout"
    assert result.poll_task_id == "task_123"


@pytest.mark.asyncio
async def test_source_add_research_unknown_status_returns_unknown_outcome() -> None:
    client = SimpleNamespace(
        research=SimpleNamespace(
            start=AsyncMock(return_value={"task_id": "task_123"}),
            wait_for_completion=AsyncMock(
                return_value={"status": "cancelled", "task_id": "task_123"}
            ),
        )
    )

    result = await execute_source_add_research(
        client,
        SourceAddResearchPlan(
            notebook_id="nb_1",
            query="topic",
            search_source="web",
            mode="deep",
            import_all=False,
            cited_only=False,
            no_wait=False,
            timeout=30,
        ),
    )

    assert result.outcome == "unknown_status"
    assert result.status == "cancelled"


@pytest.mark.asyncio
async def test_source_add_research_no_wait_returns_early_outcome() -> None:
    """``--no-wait`` skips the wait loop and returns ``started_no_wait``."""
    client = SimpleNamespace(
        research=SimpleNamespace(
            start=AsyncMock(return_value={"task_id": "task_123"}),
            wait_for_completion=AsyncMock(),
        )
    )

    result = await execute_source_add_research(
        client,
        SourceAddResearchPlan(
            notebook_id="nb_1",
            query="topic",
            search_source="web",
            mode="fast",
            import_all=False,
            cited_only=False,
            no_wait=True,
            timeout=30,
        ),
    )

    assert result.outcome == "started_no_wait"
    assert result.start_task_id == "task_123"
    client.research.wait_for_completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_add_research_delegates_timeout_budget_to_research_api() -> None:
    client = SimpleNamespace(
        research=SimpleNamespace(
            start=AsyncMock(return_value={"task_id": "task_123"}),
            wait_for_completion=AsyncMock(
                return_value={
                    "status": "completed",
                    "task_id": "task_123",
                    "sources": [],
                    "report": "",
                }
            ),
        )
    )

    result = await execute_source_add_research(
        client,
        SourceAddResearchPlan(
            notebook_id="nb_1",
            query="topic",
            search_source="web",
            mode="fast",
            import_all=False,
            cited_only=False,
            no_wait=False,
            timeout=6,
        ),
    )

    assert result.outcome == "completed"
    client.research.wait_for_completion.assert_awaited_once_with(
        "nb_1",
        task_id="task_123",
        timeout=6.0,
        interval=5.0,
    )


@pytest.mark.asyncio
async def test_source_wait_timeout_returns_typed_outcome() -> None:
    @contextlib.asynccontextmanager
    async def fake_status_with_elapsed(*args: object, **kwargs: object) -> AsyncIterator[None]:
        yield

    timeout_exc = SourceTimeoutError("src_1", 10.0, 2)
    client = SimpleNamespace(
        sources=SimpleNamespace(wait_until_ready=AsyncMock(side_effect=timeout_exc))
    )

    outcome = await execute_source_wait(
        client,
        SourceWaitPlan(
            notebook_id="nb_1",
            source_id="src_1",
            timeout=10.0,
            interval=0.5,
            json_output=True,
        ),
        wait_context=lambda: fake_status_with_elapsed(),
    )

    # Service now returns a typed outcome; presentation + exit code are the
    # caller's responsibility (covered by tests/unit/cli/test_source.py).
    assert isinstance(outcome, SourceWaitTimeout)
    assert outcome.error is timeout_exc
    client.sources.wait_until_ready.assert_awaited_once_with(
        "nb_1",
        "src_1",
        timeout=10.0,
        initial_interval=0.5,
    )
