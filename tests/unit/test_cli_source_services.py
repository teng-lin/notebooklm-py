"""Direct service-layer tests for extracted ``cli/services/source_*`` modules."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from notebooklm.cli.services import source_content, source_mutations, source_research, source_wait
from notebooklm.cli.services.source_content import SourceFulltextPlan, execute_source_fulltext
from notebooklm.cli.services.source_mutations import (
    SourceDeletePlan,
    SourceRenamePlan,
    execute_source_delete,
    execute_source_rename,
)
from notebooklm.cli.services.source_research import (
    SourceAddResearchPlan,
    execute_source_add_research,
)
from notebooklm.cli.services.source_wait import SourceWaitPlan, execute_source_wait
from notebooklm.types import Source, SourceFulltext, SourceTimeoutError


@pytest.mark.asyncio
async def test_source_delete_json_without_yes_uses_structured_confirmation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_output_error(
        message: str,
        code: str,
        json_output: bool,
        exit_code: int,
        *,
        extra: dict[str, object] | None = None,
    ) -> None:
        calls.append(
            {
                "message": message,
                "code": code,
                "json_output": json_output,
                "exit_code": exit_code,
                "extra": extra,
            }
        )
        raise SystemExit(exit_code)

    monkeypatch.setattr(source_mutations, "output_error", fake_output_error)
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

    with pytest.raises(SystemExit) as exc_info:
        await execute_source_delete(client, plan)

    assert exc_info.value.code == 1
    assert calls == [
        {
            "message": "Pass --yes to confirm destructive operation in --json mode",
            "code": "CONFIRM_REQUIRED",
            "json_output": True,
            "exit_code": 1,
            "extra": {
                "action": "delete",
                "source_id": "src_abcdef",
                "notebook_id": "nb_1",
            },
        }
    ]
    client.sources.delete.assert_not_called()


@pytest.mark.asyncio
async def test_source_rename_json_emits_service_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads: list[dict[str, object]] = []
    monkeypatch.setattr(source_mutations, "json_output_response", payloads.append)
    monkeypatch.setattr(
        source_mutations,
        "resolve_source_id",
        AsyncMock(return_value="src_full"),
    )
    client = SimpleNamespace(
        sources=SimpleNamespace(rename=AsyncMock(return_value=Source(id="src_full", title="New")))
    )

    await execute_source_rename(
        client,
        SourceRenamePlan(
            notebook_id="nb_1",
            source_id="src",
            new_title="New",
            json_output=True,
        ),
    )

    client.sources.rename.assert_awaited_once_with("nb_1", "src_full", "New")
    assert payloads == [
        {
            "action": "rename",
            "source_id": "src_full",
            "notebook_id": "nb_1",
            "title": "New",
            "status": "renamed",
        }
    ]


@pytest.mark.asyncio
async def test_source_fulltext_json_output_writes_file_and_emits_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads: list[dict[str, object]] = []
    monkeypatch.setattr(source_content, "json_output_response", payloads.append)
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
    output_path = tmp_path / "source.txt"

    await execute_source_fulltext(
        client,
        SourceFulltextPlan(
            notebook_id="nb_1",
            source_id="src_1",
            json_output=True,
            output=str(output_path),
            output_format="text",
        ),
    )

    assert output_path.read_text(encoding="utf-8") == "full content"
    assert payloads == [
        {
            "path": str(output_path),
            "bytes": len(b"full content"),
            "source_id": "src_1",
            "title": "Paper",
        }
    ]


@pytest.mark.asyncio
async def test_source_add_research_pins_task_id_and_imports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported = SimpleNamespace(imported=["src_1"])
    import_research_sources = AsyncMock(return_value=imported)
    monkeypatch.setattr(source_research, "import_research_sources", import_research_sources)
    monkeypatch.setattr(source_research.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(source_research.console, "print", lambda *args, **kwargs: None)
    monkeypatch.setattr(source_research, "display_research_sources", lambda sources: None)
    monkeypatch.setattr(source_research, "display_report", lambda report, json_hint=False: None)
    client = SimpleNamespace(
        research=SimpleNamespace(
            start=AsyncMock(return_value={"task_id": "task_123"}),
            poll=AsyncMock(
                return_value={
                    "status": "completed",
                    "sources": [{"title": "Result"}],
                    "report": "Report",
                }
            ),
        )
    )

    await execute_source_add_research(
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

    client.research.start.assert_awaited_once_with("nb_1", "topic", "web", "deep")
    client.research.poll.assert_awaited_once_with("nb_1", task_id="task_123")
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
async def test_source_wait_timeout_maps_to_json_envelope_and_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads: list[dict[str, object]] = []

    @contextlib.asynccontextmanager
    async def fake_status_with_elapsed(*args: object, **kwargs: object) -> AsyncIterator[None]:
        yield

    def fake_exit_with_code(code: int) -> None:
        raise SystemExit(code)

    monkeypatch.setattr(source_wait, "status_with_elapsed", fake_status_with_elapsed)
    monkeypatch.setattr(source_wait, "json_output_response", payloads.append)
    monkeypatch.setattr(source_wait, "exit_with_code", fake_exit_with_code)
    client = SimpleNamespace(
        sources=SimpleNamespace(
            wait_until_ready=AsyncMock(side_effect=SourceTimeoutError("src_1", 10.0, 2))
        )
    )

    with pytest.raises(SystemExit) as exc_info:
        await execute_source_wait(
            client,
            SourceWaitPlan(
                notebook_id="nb_1",
                source_id="src_1",
                timeout=10.0,
                interval=0.5,
                json_output=True,
            ),
        )

    assert exc_info.value.code == 2
    assert payloads == [
        {
            "source_id": "src_1",
            "status": "timeout",
            "last_status_code": 2,
            "timeout_seconds": 10,
            "error": "Source src_1 not ready after 10.0s (last status: 2)",
        }
    ]
    client.sources.wait_until_ready.assert_awaited_once_with(
        "nb_1",
        "src_1",
        timeout=10.0,
        initial_interval=0.5,
    )
