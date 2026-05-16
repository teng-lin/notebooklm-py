"""Unit tests for the extracted source polling service."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notebooklm._source_polling import SourcePoller
from notebooklm._sources import SourcesAPI
from notebooklm.types import (
    Source,
    SourceNotFoundError,
    SourceProcessingError,
    SourceStatus,
    SourceTimeoutError,
)


@pytest.fixture
def poller() -> SourcePoller:
    return SourcePoller()


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("tests.source_polling")


@pytest.mark.asyncio
async def test_wait_until_ready_uses_injected_get_sleep_and_clock(
    poller: SourcePoller,
    logger: logging.Logger,
) -> None:
    processing = Source(id="src_1", status=SourceStatus.PROCESSING)
    ready = Source(id="src_1", status=SourceStatus.READY)
    get_source = AsyncMock(side_effect=[processing, ready])
    sleep = AsyncMock()
    monotonic = MagicMock(return_value=0.0)

    result = await poller.wait_until_ready(
        "nb_1",
        "src_1",
        timeout=10.0,
        initial_interval=0.25,
        max_interval=1.0,
        backoff_factor=2.0,
        get_source=get_source,
        sleep=sleep,
        monotonic=monotonic,
        logger=logger,
    )

    assert result is ready
    assert get_source.await_args_list[0].args == ("nb_1", "src_1")
    sleep.assert_awaited_once_with(0.25)
    assert monotonic.call_count >= 4


@pytest.mark.asyncio
async def test_wait_until_ready_checks_timeout_after_get(
    poller: SourcePoller,
    logger: logging.Logger,
) -> None:
    processing = Source(id="src_1", status=SourceStatus.PROCESSING)
    get_source = AsyncMock(return_value=processing)
    sleep = AsyncMock()
    monotonic = MagicMock(side_effect=[0.0, 0.5, 1.5])

    with pytest.raises(SourceTimeoutError) as exc_info:
        await poller.wait_until_ready(
            "nb_1",
            "src_1",
            timeout=1.0,
            get_source=get_source,
            sleep=sleep,
            monotonic=monotonic,
            logger=logger,
        )

    assert exc_info.value.last_status == SourceStatus.PROCESSING
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_wait_until_ready_raises_source_not_found_when_get_returns_none(
    poller: SourcePoller,
    logger: logging.Logger,
) -> None:
    get_source = AsyncMock(return_value=None)

    with pytest.raises(SourceNotFoundError) as exc_info:
        await poller.wait_until_ready(
            "nb_1",
            "src_missing",
            get_source=get_source,
            sleep=AsyncMock(),
            monotonic=MagicMock(return_value=0.0),
            logger=logger,
        )

    assert exc_info.value.source_id == "src_missing"


@pytest.mark.asyncio
async def test_wait_until_ready_raises_processing_error_for_terminal_error_type(
    poller: SourcePoller,
    logger: logging.Logger,
) -> None:
    terminal_error = Source(id="src_pdf", status=SourceStatus.ERROR, _type_code=3)
    get_source = AsyncMock(return_value=terminal_error)

    with pytest.raises(SourceProcessingError) as exc_info:
        await poller.wait_until_ready(
            "nb_1",
            "src_pdf",
            get_source=get_source,
            sleep=AsyncMock(),
            monotonic=MagicMock(return_value=0.0),
            logger=logger,
        )

    assert exc_info.value.source_id == "src_pdf"
    assert exc_info.value.status == SourceStatus.ERROR


@pytest.mark.asyncio
async def test_wait_until_ready_tolerates_transient_error_for_audio(
    poller: SourcePoller,
    logger: logging.Logger,
) -> None:
    transient_error = Source(id="src_audio", status=SourceStatus.ERROR, _type_code=10)
    ready = Source(id="src_audio", status=SourceStatus.READY, _type_code=10)
    get_source = AsyncMock(side_effect=[transient_error, ready])
    sleep = AsyncMock()

    result = await poller.wait_until_ready(
        "nb_1",
        "src_audio",
        timeout=10.0,
        initial_interval=0.25,
        get_source=get_source,
        sleep=sleep,
        monotonic=MagicMock(return_value=0.0),
        logger=logger,
    )

    assert result is ready
    sleep.assert_awaited_once_with(0.25)


@pytest.mark.asyncio
async def test_wait_until_registered_tolerates_transient_error(
    poller: SourcePoller,
    logger: logging.Logger,
) -> None:
    transient_error = Source(id="src_audio", status=SourceStatus.ERROR, _type_code=10)
    processing = Source(id="src_audio", status=SourceStatus.PROCESSING, _type_code=10)
    get_source = AsyncMock(side_effect=[transient_error, processing])
    sleep = AsyncMock()
    monotonic = MagicMock(return_value=0.0)

    result = await poller.wait_until_registered(
        "nb_1",
        "src_audio",
        timeout=10.0,
        initial_interval=0.5,
        get_source=get_source,
        sleep=sleep,
        monotonic=monotonic,
        logger=logger,
    )

    assert result is processing
    sleep.assert_awaited_once_with(0.5)


@pytest.mark.asyncio
async def test_wait_until_registered_raises_processing_error_for_terminal_type(
    poller: SourcePoller,
    logger: logging.Logger,
) -> None:
    terminal_error = Source(id="src_pdf", status=SourceStatus.ERROR, _type_code=3)
    get_source = AsyncMock(return_value=terminal_error)

    with pytest.raises(SourceProcessingError) as exc_info:
        await poller.wait_until_registered(
            "nb_1",
            "src_pdf",
            get_source=get_source,
            sleep=AsyncMock(),
            monotonic=MagicMock(return_value=0.0),
            logger=logger,
        )

    assert exc_info.value.source_id == "src_pdf"
    assert exc_info.value.status == SourceStatus.ERROR


@pytest.mark.asyncio
async def test_wait_until_registered_waits_while_source_is_none(
    poller: SourcePoller,
    logger: logging.Logger,
) -> None:
    processing = Source(id="src_1", status=SourceStatus.PROCESSING)
    get_source = AsyncMock(side_effect=[None, processing])
    sleep = AsyncMock()

    result = await poller.wait_until_registered(
        "nb_1",
        "src_1",
        timeout=10.0,
        initial_interval=0.5,
        get_source=get_source,
        sleep=sleep,
        monotonic=MagicMock(return_value=0.0),
        logger=logger,
    )

    assert result is processing
    sleep.assert_awaited_once_with(0.5)


@pytest.mark.asyncio
async def test_wait_until_registered_raises_timeout(
    poller: SourcePoller,
    logger: logging.Logger,
) -> None:
    get_source = AsyncMock(return_value=None)
    sleep = AsyncMock()
    monotonic = MagicMock(side_effect=[0.0, 0.5, 1.5])

    with pytest.raises(SourceTimeoutError) as exc_info:
        await poller.wait_until_registered(
            "nb_1",
            "src_1",
            timeout=1.0,
            get_source=get_source,
            sleep=sleep,
            monotonic=monotonic,
            logger=logger,
        )

    assert exc_info.value.last_status is None
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_wait_for_sources_catches_base_exception_and_drains_siblings(
    poller: SourcePoller,
    logger: logging.Logger,
) -> None:
    class PollStopped(BaseException):
        pass

    slow_entered = asyncio.Event()
    slow_cancelled = asyncio.Event()

    async def wait_until_ready(notebook_id: str, source_id: str, **kwargs: object) -> Source:
        if source_id == "bad":
            await slow_entered.wait()
            raise PollStopped()
        if source_id == "slow":
            slow_entered.set()
            try:
                await asyncio.sleep(60.0)
            except asyncio.CancelledError:
                slow_cancelled.set()
                raise
        return Source(id=source_id)

    with pytest.raises(PollStopped):
        await asyncio.wait_for(
            poller.wait_for_sources(
                "nb_1",
                ["bad", "slow"],
                wait_until_ready=wait_until_ready,
                logger=logger,
            ),
            timeout=1.0,
        )

    assert slow_entered.is_set()
    assert slow_cancelled.is_set()


@pytest.mark.asyncio
async def test_sources_api_wait_until_ready_delegates_with_call_time_dependencies() -> None:
    api = SourcesAPI(MagicMock())
    ready = Source(id="src_1", status=SourceStatus.READY)

    with patch.object(api._poller, "wait_until_ready", new_callable=AsyncMock) as delegate:
        delegate.return_value = ready
        result = await api.wait_until_ready("nb_1", "src_1")

    assert result is ready
    kwargs = delegate.await_args.kwargs
    assert kwargs["timeout"] == 120.0
    assert kwargs["initial_interval"] == 1.0
    assert kwargs["max_interval"] == 10.0
    assert kwargs["backoff_factor"] == 1.5
    assert kwargs["get_source"].__self__ is api
    assert kwargs["get_source"].__func__ is SourcesAPI.get


@pytest.mark.asyncio
async def test_sources_api_wait_until_ready_resolves_sources_sleep_and_monotonic() -> None:
    api = SourcesAPI(MagicMock())
    processing = Source(id="src_1", status=SourceStatus.PROCESSING)
    ready = Source(id="src_1", status=SourceStatus.READY)

    with (
        patch.object(api, "get", new_callable=AsyncMock, side_effect=[processing, ready]),
        patch("notebooklm._sources.asyncio.sleep", new_callable=AsyncMock) as sleep,
        patch("notebooklm._sources.monotonic", MagicMock(return_value=0.0)) as monotonic,
    ):
        result = await api.wait_until_ready("nb_1", "src_1", initial_interval=0.75)

    assert result is ready
    sleep.assert_awaited_once_with(0.75)
    assert monotonic.call_count >= 4


@pytest.mark.asyncio
async def test_sources_api_wait_for_sources_uses_late_bound_wait_until_ready() -> None:
    api = SourcesAPI(MagicMock())
    api.wait_until_ready = AsyncMock(
        side_effect=[
            Source(id="src_1", status=SourceStatus.READY),
            Source(id="src_2", status=SourceStatus.READY),
        ]
    )

    results = await api.wait_for_sources("nb_1", ["src_1", "src_2"], timeout=42.0)

    assert [source.id for source in results] == ["src_1", "src_2"]
    assert api.wait_until_ready.await_count == 2
    for call in api.wait_until_ready.await_args_list:
        assert call.kwargs["timeout"] == 42.0
