"""Unit tests for the extracted source polling service."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notebooklm._source.polling import SourcePoller
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
async def test_wait_until_ready_clamps_sleep_to_remaining_timeout(
    poller: SourcePoller,
    logger: logging.Logger,
) -> None:
    processing = Source(id="src_1", status=SourceStatus.PROCESSING)
    get_source = AsyncMock(return_value=processing)
    sleeps: list[float] = []
    clock = 0.0

    def monotonic() -> float:
        return clock

    async def sleep(seconds: float) -> None:
        nonlocal clock
        sleeps.append(seconds)
        clock += seconds

    with pytest.raises(SourceTimeoutError) as exc_info:
        await poller.wait_until_ready(
            "nb_1",
            "src_1",
            timeout=1.0,
            initial_interval=10.0,
            get_source=get_source,
            sleep=sleep,
            monotonic=monotonic,
            logger=logger,
        )

    assert exc_info.value.last_status == SourceStatus.PROCESSING
    assert get_source.await_count == 1
    assert sleeps == [1.0]
    assert clock == 1.0


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
async def test_wait_until_registered_clamps_sleep_to_remaining_timeout(
    poller: SourcePoller,
    logger: logging.Logger,
) -> None:
    get_source = AsyncMock(return_value=None)
    sleeps: list[float] = []
    clock = 0.0

    def monotonic() -> float:
        return clock

    async def sleep(seconds: float) -> None:
        nonlocal clock
        sleeps.append(seconds)
        clock += seconds

    with pytest.raises(SourceTimeoutError) as exc_info:
        await poller.wait_until_registered(
            "nb_1",
            "src_1",
            timeout=1.0,
            initial_interval=10.0,
            get_source=get_source,
            sleep=sleep,
            monotonic=monotonic,
            logger=logger,
        )

    assert exc_info.value.last_status is None
    assert get_source.await_count == 1
    assert sleeps == [1.0]
    assert clock == 1.0


def _sequenced_list_sources(snapshots: list[list[Source]]) -> AsyncMock:
    """Return an ``AsyncMock`` yielding one snapshot per call (last one repeats).

    ``await_count`` is then the number of poll TICKS — the invariant the
    single-snapshot loop must uphold: ONE whole-notebook list per tick,
    regardless of how many sources are still pending (#1870).
    """
    calls = {"n": 0}

    async def _list(_notebook_id: str) -> list[Source]:
        index = min(calls["n"], len(snapshots) - 1)
        calls["n"] += 1
        return snapshots[index]

    return AsyncMock(side_effect=_list)


@pytest.mark.asyncio
async def test_wait_all_until_ready_polls_notebook_once_per_tick(
    poller: SourcePoller,
    logger: logging.Logger,
) -> None:
    """The heart of #1870: with four sources pending, each tick issues EXACTLY ONE
    list_sources call (not one per source), and results come back in input order."""
    ready0 = Source(id="s0", status=SourceStatus.READY)
    err2 = Source(id="s2", status=SourceStatus.ERROR, _type_code=3)  # terminal (pdf)
    proc3 = Source(id="s3", status=SourceStatus.PROCESSING)
    ready3 = Source(id="s3", status=SourceStatus.READY)
    # s1 is never in any snapshot → not found. Tick 1 resolves s0/s1/s2; s3 lags.
    list_sources = _sequenced_list_sources([[ready0, err2, proc3], [ready0, err2, ready3]])

    results = await poller.wait_all_until_ready(
        "nb_1",
        ["s0", "s1", "s2", "s3"],
        timeout=10.0,
        initial_interval=0.25,
        list_sources=list_sources,
        sleep=AsyncMock(),
        monotonic=MagicMock(return_value=0.0),
        logger=logger,
    )

    # Two ticks total (s3 lagged one tick) — ONE list per tick, NOT one per source.
    assert list_sources.await_count == 2
    # Input order preserved, one neutral result per id.
    assert results[0] is ready0
    assert isinstance(results[1], SourceNotFoundError) and results[1].source_id == "s1"
    assert isinstance(results[2], SourceProcessingError) and results[2].source_id == "s2"
    assert results[3] is ready3


@pytest.mark.asyncio
async def test_wait_all_until_ready_tolerates_transient_error_across_ticks(
    poller: SourcePoller,
    logger: logging.Logger,
) -> None:
    """A transient (audio) ERROR keeps the source pending across ticks, exactly like
    the single-source loop, until it settles READY."""
    transient = Source(id="s0", status=SourceStatus.ERROR, _type_code=10)
    ready = Source(id="s0", status=SourceStatus.READY, _type_code=10)
    list_sources = _sequenced_list_sources([[transient], [ready]])

    results = await poller.wait_all_until_ready(
        "nb_1",
        ["s0"],
        timeout=10.0,
        initial_interval=0.25,
        list_sources=list_sources,
        sleep=AsyncMock(),
        monotonic=MagicMock(return_value=0.0),
        logger=logger,
    )

    assert results == [ready]
    assert list_sources.await_count == 2


@pytest.mark.asyncio
async def test_wait_all_until_ready_timeout_reports_each_sources_own_last_status(
    poller: SourcePoller,
    logger: logging.Logger,
) -> None:
    """On timeout every still-pending source gets its OWN last observed status — the
    per-index ``last_status`` dict, so a scalar can't leak one source's status into
    another's ``SourceTimeoutError`` (binding correction)."""
    processing = Source(id="s0", status=SourceStatus.PROCESSING)
    transient_err = Source(id="s1", status=SourceStatus.ERROR, _type_code=10)  # audio, stays
    list_sources = _sequenced_list_sources([[processing, transient_err]])

    sleeps: list[float] = []
    clock = 0.0

    def monotonic() -> float:
        return clock

    async def sleep(seconds: float) -> None:
        nonlocal clock
        sleeps.append(seconds)
        clock += seconds

    results = await poller.wait_all_until_ready(
        "nb_1",
        ["s0", "s1"],
        timeout=1.0,
        initial_interval=10.0,  # clamped to the remaining 1.0s
        list_sources=list_sources,
        sleep=sleep,
        monotonic=monotonic,
        logger=logger,
    )

    assert list_sources.await_count == 1  # one snapshot, then the clock expires
    assert sleeps == [1.0]
    assert isinstance(results[0], SourceTimeoutError)
    assert results[0].source_id == "s0"
    assert results[0].last_status == SourceStatus.PROCESSING
    assert isinstance(results[1], SourceTimeoutError)
    assert results[1].source_id == "s1"
    # Distinct per-source last_status — NOT s0's PROCESSING.
    assert results[1].last_status == SourceStatus.ERROR


@pytest.mark.asyncio
async def test_wait_all_until_ready_bounds_overall_duration_regardless_of_source_count(
    poller: SourcePoller,
    logger: logging.Logger,
) -> None:
    """Regression for #1878: the OVERALL multi-source wait is bounded by ONE
    ``timeout``, not ``N * timeout``.

    The issue was filed against the pre-#1870 bounded-concurrency wait pool, where
    each queued source got the full ``timeout`` so the overall wait grew with N
    (~``ceil(N / workers) * timeout``). #1870 rewrote this into a single-snapshot
    loop over one shared :class:`RuntimeDeadline`, so N never multiplies the wait.

    Drive 25 sources that ALL stay PROCESSING forever (``list_sources`` always
    returns the same PROCESSING snapshot) against an injected fake clock whose only
    time source is the loop's own clamped sleeps. Assert the total *simulated*
    elapsed time is bounded by ~one ``timeout`` (NOT 25 * timeout), that all 25 come
    back as :class:`SourceTimeoutError`, and that each carries its own last status.
    """
    source_count = 25
    timeout = 5.0
    ids = [f"s{i}" for i in range(source_count)]
    # Every source is stuck PROCESSING and never leaves that state.
    stuck = [Source(id=sid, status=SourceStatus.PROCESSING) for sid in ids]
    # The same PROCESSING snapshot on every tick — nothing ever becomes ready.
    list_sources = _sequenced_list_sources([stuck])

    sleeps: list[float] = []
    clock = 0.0

    def monotonic() -> float:
        return clock

    async def sleep(seconds: float) -> None:
        nonlocal clock
        sleeps.append(seconds)
        clock += seconds

    results = await poller.wait_all_until_ready(
        "nb_1",
        ids,
        timeout=timeout,
        initial_interval=1.0,
        max_interval=10.0,
        list_sources=list_sources,
        sleep=sleep,
        monotonic=monotonic,
        logger=logger,
    )

    # (a) The overall wait is bounded by ~one timeout, NOT source_count * timeout.
    # Every tick resolves ALL still-pending sources against ONE shared deadline, so
    # the accumulated sleep budget can never exceed the single ``timeout`` — with 25
    # sources the old per-source pool would have burned up to 25 * timeout here.
    assert sum(sleeps) <= timeout
    assert clock <= timeout
    assert clock < source_count * timeout
    # One whole-notebook snapshot per tick (never one-per-source): a handful of
    # ticks under backoff, not 25 independent per-source waits.
    assert list_sources.await_count <= 10

    # (b) All 25 stuck sources come back as timeouts, in input order.
    assert len(results) == source_count
    assert all(isinstance(result, SourceTimeoutError) for result in results)
    # (c) Each timeout carries its OWN id and last observed status (PROCESSING).
    for sid, result in zip(ids, results, strict=True):
        assert isinstance(result, SourceTimeoutError)
        assert result.source_id == sid
        assert result.last_status == SourceStatus.PROCESSING


@pytest.mark.asyncio
async def test_wait_all_until_ready_empty_returns_empty(
    poller: SourcePoller,
    logger: logging.Logger,
) -> None:
    list_sources = AsyncMock()
    results = await poller.wait_all_until_ready(
        "nb_1",
        [],
        list_sources=list_sources,
        sleep=AsyncMock(),
        monotonic=MagicMock(return_value=0.0),
        logger=logger,
    )
    assert results == []
    list_sources.assert_not_awaited()  # nothing pending → no snapshot poll


@pytest.mark.asyncio
async def test_wait_all_until_ready_propagates_unexpected_list_error(
    poller: SourcePoller,
    logger: logging.Logger,
) -> None:
    """An UNEXPECTED error from the snapshot poll is not a per-source outcome — it
    propagates out of the single await (no siblings to leak)."""

    class Boom(Exception):
        pass

    list_sources = AsyncMock(side_effect=Boom())
    with pytest.raises(Boom):
        await poller.wait_all_until_ready(
            "nb_1",
            ["s0"],
            list_sources=list_sources,
            sleep=AsyncMock(),
            monotonic=MagicMock(return_value=0.0),
            logger=logger,
        )


@pytest.mark.asyncio
async def test_sources_api_wait_all_until_ready_delegates_with_list_seam() -> None:
    """The thin ``SourcesAPI`` delegate wires the poller with ``self.list`` (the
    single-snapshot source) and the module sleep/clock seams."""
    api = SourcesAPI(MagicMock(), uploader=MagicMock())
    ready = [Source(id="s0", status=SourceStatus.READY)]

    with patch.object(SourcePoller, "wait_all_until_ready", new_callable=AsyncMock) as delegate:
        delegate.return_value = ready
        result = await api.wait_all_until_ready("nb_1", ["s0"])

    assert result is ready
    args = delegate.await_args
    assert args.args == ("nb_1", ["s0"])
    kwargs = args.kwargs
    assert kwargs["timeout"] == 120.0
    assert kwargs["initial_interval"] == 1.0
    # Wired with the notebook-list seam (NOT get_or_none) — one list per tick.
    assert kwargs["list_sources"].__self__ is api
    assert kwargs["list_sources"].__func__ is SourcesAPI.list


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
    api = SourcesAPI(MagicMock(), uploader=MagicMock())
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
    # The poller is wired with get_or_none (not public get), so the readiness
    # poll never trips the get()-returns-None deprecation.
    assert kwargs["get_source"].__func__ is SourcesAPI.get_or_none


@pytest.mark.asyncio
async def test_sources_api_wait_until_ready_resolves_sources_sleep_and_monotonic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import notebooklm._sources as _sources

    api = SourcesAPI(MagicMock(), uploader=MagicMock())
    processing = Source(id="src_1", status=SourceStatus.PROCESSING)
    ready = Source(id="src_1", status=SourceStatus.READY)

    sleep = AsyncMock()
    monotonic = MagicMock(return_value=0.0)
    monkeypatch.setattr(api, "get_or_none", AsyncMock(side_effect=[processing, ready]))
    # Object-form patches against the locally-imported `_sources` seam alias:
    # the production code resolves `asyncio.sleep`/`monotonic` from this module
    # namespace (see `_sources.SourcesAPI.wait_until_ready`), so substituting them here
    # exercises that resolution without an import-string patch.
    # `_sources.monotonic` is a module-local alias (`from time import monotonic`),
    # so patching it is already isolated. For `asyncio.sleep` we swap the whole
    # `_sources.asyncio` binding for a mock wrapping the real module with only
    # `sleep` overridden, so we never mutate the shared stdlib `asyncio` object.
    fake_asyncio = MagicMock(wraps=_sources.asyncio)
    fake_asyncio.sleep = sleep
    monkeypatch.setattr(_sources, "asyncio", fake_asyncio)
    monkeypatch.setattr(_sources, "monotonic", monotonic)

    result = await api.wait_until_ready("nb_1", "src_1", initial_interval=0.75)

    assert result is ready
    sleep.assert_awaited_once_with(0.75)
    assert monotonic.call_count >= 4


@pytest.mark.asyncio
async def test_sources_api_wait_for_sources_uses_late_bound_wait_until_ready() -> None:
    api = SourcesAPI(MagicMock(), uploader=MagicMock())
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
