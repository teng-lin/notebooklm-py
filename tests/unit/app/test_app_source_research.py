"""Unit tests for the transport-neutral ``notebooklm._app.source_research`` core.

These pin the relocated ``source add-research`` business logic at the ``_app``
boundary (independent of the Click adapter):

* :func:`validate_add_research_flags` — the two rejected flag combinations
  (``--cited-only`` without ``--import-all``; ``--no-wait`` with ``--import-all``),
  both raising the public :class:`ValidationError`.
* :func:`execute_source_add_research` — the start → wait → optional-import
  workflow and the discriminated outcome it returns for every terminal state
  (``start_failed`` / ``started_no_wait`` / ``completed`` / ``no_research`` /
  ``failed`` / ``timeout`` / ``unknown_status``), with the importer injected.

Pure-service tests (no Click / CliRunner): the command-layer rendering +
exit-code policy is exercised in ``tests/unit/cli/test_source.py`` and
``tests/unit/cli/test_source_characterization.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._app.source_research import (
    SourceAddResearchPlan,
    execute_source_add_research,
    validate_add_research_flags,
)
from notebooklm.exceptions import ValidationError
from notebooklm.types import ResearchStatus


def _plan(**overrides: Any) -> SourceAddResearchPlan:
    base: dict[str, Any] = {
        "notebook_id": "nb_1",
        "query": "ml",
        "search_source": "web",
        "mode": "fast",
        "import_all": False,
        "cited_only": False,
        "no_wait": False,
        "timeout": 60,
    }
    base.update(overrides)
    return SourceAddResearchPlan(**base)


def _start(*, task_id: str = "task_1", report_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(task_id=task_id, report_id=report_id)


def _source(url: str = "http://ex.com", title: str = "S") -> SimpleNamespace:
    return SimpleNamespace(to_public_dict=lambda: {"url": url, "title": title, "result_type": 1})


def _status(
    status: ResearchStatus, *, sources: list[Any] | None = None, report: str = ""
) -> SimpleNamespace:
    return SimpleNamespace(status=status, sources=sources or [], report=report)


def _client() -> MagicMock:
    client = MagicMock()
    client.research = MagicMock()
    return client


# ===========================================================================
# validate_add_research_flags
# ===========================================================================


class TestValidateAddResearchFlags:
    def test_valid_combinations_pass(self) -> None:
        # No exception for sane combos.
        validate_add_research_flags(import_all=False, cited_only=False, no_wait=False)
        validate_add_research_flags(import_all=True, cited_only=True, no_wait=False)
        validate_add_research_flags(import_all=False, cited_only=False, no_wait=True)

    def test_cited_only_without_import_all_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            validate_add_research_flags(import_all=False, cited_only=True, no_wait=False)
        assert "--cited-only requires --import-all" in str(exc.value)

    def test_no_wait_with_import_all_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            validate_add_research_flags(import_all=True, cited_only=False, no_wait=True)
        assert "--import-all requires" in str(exc.value)


# ===========================================================================
# execute_source_add_research — terminal outcomes
# ===========================================================================


@pytest.mark.asyncio
async def test_start_failed_when_start_returns_empty() -> None:
    client = _client()
    client.research.start = AsyncMock(return_value=None)
    importer = AsyncMock()
    result = await execute_source_add_research(client, _plan(), import_sources=importer)
    assert result.outcome == "start_failed"
    client.research.wait_for_completion.assert_not_called()
    importer.assert_not_called()


@pytest.mark.asyncio
async def test_no_wait_returns_after_start() -> None:
    client = _client()
    client.research.start = AsyncMock(return_value=_start(task_id="task_1"))
    client.research.wait_for_completion = AsyncMock()
    importer = AsyncMock()
    result = await execute_source_add_research(client, _plan(no_wait=True), import_sources=importer)
    assert result.outcome == "started_no_wait"
    assert result.start_task_id == "task_1"
    assert result.poll_task_id == "task_1"
    client.research.wait_for_completion.assert_not_called()
    importer.assert_not_called()


@pytest.mark.asyncio
async def test_deep_mode_polls_with_report_id() -> None:
    client = _client()
    client.research.start = AsyncMock(return_value=_start(task_id="task_1", report_id="report_9"))
    client.research.wait_for_completion = AsyncMock(return_value=_status(ResearchStatus.COMPLETED))
    result = await execute_source_add_research(
        client, _plan(mode="deep"), import_sources=AsyncMock()
    )
    # Deep research polls under the report id, not the start task id.
    _, kwargs = client.research.wait_for_completion.call_args
    assert kwargs["task_id"] == "report_9"
    assert result.poll_task_id == "report_9"


@pytest.mark.asyncio
async def test_completed_without_import() -> None:
    client = _client()
    client.research.start = AsyncMock(return_value=_start())
    client.research.wait_for_completion = AsyncMock(
        return_value=_status(ResearchStatus.COMPLETED, sources=[_source()], report="# R")
    )
    importer = AsyncMock()
    result = await execute_source_add_research(client, _plan(), import_sources=importer)
    assert result.outcome == "completed"
    assert result.sources == [{"url": "http://ex.com", "title": "S", "result_type": 1}]
    assert result.report == "# R"
    assert result.import_result is None
    # No import without --import-all.
    importer.assert_not_called()


@pytest.mark.asyncio
async def test_completed_with_import_all_invokes_importer() -> None:
    client = _client()
    client.research.start = AsyncMock(return_value=_start(task_id="task_1"))
    client.research.wait_for_completion = AsyncMock(
        return_value=_status(ResearchStatus.COMPLETED, sources=[_source()], report="# R")
    )
    import_outcome = SimpleNamespace(imported=[{"id": "s"}], sources=[], cited_selection=None)
    importer = AsyncMock(return_value=import_outcome)

    result = await execute_source_add_research(
        client, _plan(import_all=True, cited_only=True), import_sources=importer
    )
    assert result.outcome == "completed"
    assert result.import_result is import_outcome
    args, kwargs = importer.call_args
    assert args[0] is client
    assert args[1] == "nb_1"
    assert args[2] == "task_1"
    assert kwargs["cited_only"] is True
    assert kwargs["report"] == "# R"
    assert kwargs["max_elapsed"] == 60


@pytest.mark.asyncio
async def test_completed_import_all_but_no_sources_skips_importer() -> None:
    client = _client()
    client.research.start = AsyncMock(return_value=_start())
    client.research.wait_for_completion = AsyncMock(
        return_value=_status(ResearchStatus.COMPLETED, sources=[], report="")
    )
    importer = AsyncMock()
    result = await execute_source_add_research(
        client, _plan(import_all=True), import_sources=importer
    )
    assert result.outcome == "completed"
    assert result.import_result is None
    importer.assert_not_called()


@pytest.mark.asyncio
async def test_json_output_threaded_to_importer() -> None:
    client = _client()
    client.research.start = AsyncMock(return_value=_start())
    client.research.wait_for_completion = AsyncMock(
        return_value=_status(ResearchStatus.COMPLETED, sources=[_source()])
    )
    importer = AsyncMock(
        return_value=SimpleNamespace(imported=[], sources=[], cited_selection=None)
    )
    await execute_source_add_research(
        client, _plan(import_all=True, json_output=True), import_sources=importer
    )
    _, kwargs = importer.call_args
    assert kwargs["json_output"] is True


@pytest.mark.asyncio
async def test_no_research_outcome() -> None:
    client = _client()
    client.research.start = AsyncMock(return_value=_start())
    client.research.wait_for_completion = AsyncMock(
        return_value=_status(ResearchStatus.NO_RESEARCH)
    )
    result = await execute_source_add_research(client, _plan(), import_sources=AsyncMock())
    assert result.outcome == "no_research"


@pytest.mark.asyncio
async def test_failed_outcome() -> None:
    client = _client()
    client.research.start = AsyncMock(return_value=_start())
    client.research.wait_for_completion = AsyncMock(
        return_value=_status(ResearchStatus.FAILED, sources=[_source()], report="# R")
    )
    result = await execute_source_add_research(client, _plan(), import_sources=AsyncMock())
    assert result.outcome == "failed"
    assert result.report == "# R"


@pytest.mark.asyncio
async def test_timeout_outcome_on_timeout_error() -> None:
    client = _client()
    client.research.start = AsyncMock(return_value=_start())
    client.research.wait_for_completion = AsyncMock(side_effect=TimeoutError)
    result = await execute_source_add_research(client, _plan(), import_sources=AsyncMock())
    assert result.outcome == "timeout"


@pytest.mark.asyncio
async def test_unknown_status_preserves_raw_value() -> None:
    client = _client()
    client.research.start = AsyncMock(return_value=_start())
    # An unexpected status string the discriminator does not recognise.
    client.research.wait_for_completion = AsyncMock(
        return_value=_status(ResearchStatus.IN_PROGRESS)
    )
    result = await execute_source_add_research(client, _plan(), import_sources=AsyncMock())
    assert result.outcome == "unknown_status"
    assert result.status == "in_progress"
