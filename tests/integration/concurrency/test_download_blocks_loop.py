"""Regression test for T7.D4 — download paths must not block the event loop.

Audit item #30 (`thread-safety-concurrency-audit.md` §30):

> `_download_urls_batch()` and `_download_url()` call `load_httpx_cookies()`
> (synchronous JSON read) directly from `async def`. `download_report()`
> and `download_mind_map()` call `Path.write_text()` directly on the loop.
> Slow storage / large payloads stall every other concurrent task.

This module pins the post-fix invariant: each blocking sync call site
must execute via ``asyncio.to_thread`` (or an equivalent offload) so a
slow filesystem cannot freeze sibling coroutines for the duration of
the call.

Methodology — heartbeat-gap detection
-------------------------------------
We do NOT try to prove the call is *exactly* on a thread; we prove the
**observable consequence** — that a 200 ms blocking sync stub does not
stall a concurrent heartbeat coroutine for more than ``MAX_GAP_MS``.

A heartbeat task fires roughly every 10 ms via ``asyncio.sleep(0.01)``
for the duration of the download. If the download blocked the loop,
the gap between consecutive heartbeat timestamps spikes to >= the
stub's sleep duration. With the fix in place the stub runs in a worker
thread and the heartbeat keeps ticking at ~10 ms intervals.

Important detail — post-block settling
......................................
The heartbeat records ``time.monotonic()`` *before* its ``await
asyncio.sleep(0.01)``. When the loop is blocked by a sync sleep, no
new samples are recorded *during* the block; the very next sample
appears only when the loop wakes and the heartbeat schedules its next
iteration. We therefore yield (``await asyncio.sleep(SETTLE_S)``)
*after* the download completes and *before* setting the stop event so
the heartbeat has a fair chance to record a post-block sample. Without
that yield the heartbeat could be terminated before it ever recorded
the "after" timestamp, and the gap detector would see only pre-block
samples and report a misleadingly small gap.

``MAX_GAP_MS`` is set to 50 ms (5x the nominal heartbeat). CI scheduling
jitter can produce occasional 20-40 ms gaps; a 200 ms blocking stub
produces gaps >> 100 ms. The gap between those two regimes is wide.

Tightness note: the spec named 50 ms as an example bound; on a busy
runner an occasional 30-40 ms gap from the asyncio scheduler is normal
even with no blocking call, so 50 ms is the floor below which we'd see
false positives. We assert ``max_gap_ms < 50`` rather than a percentile
because a single >= 100 ms gap is the signal we're hunting.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notebooklm._artifacts import ArtifactsAPI
from notebooklm.types import ArtifactDownloadError

# A "slow filesystem" or "slow auth store" simulation. 200 ms is well
# above any plausible scheduler hiccup on CI and far above the 50 ms
# assertion bound, so a regression where the call moves back onto the
# loop is unambiguous.
BLOCKING_SLEEP_S = 0.2
MAX_GAP_MS = 50.0
# Time we yield to the loop after the download completes so the heartbeat
# can record at least one post-block timestamp before we stop it. Must be
# (a) larger than the heartbeat's 10 ms tick interval and (b) much smaller
# than ``BLOCKING_SLEEP_S`` so it can't be confused with the block itself.
SETTLE_S = 0.05


async def _heartbeat(stop: asyncio.Event, samples: list[float]) -> None:
    """Tick every 10 ms until ``stop`` is set; record monotonic timestamps.

    Each ``asyncio.sleep(0.01)`` is a yield point. If anything monopolizes
    the loop for >>10 ms between yields, the gap between consecutive
    ``samples`` entries will reflect the stall.
    """
    while not stop.is_set():
        samples.append(time.monotonic())
        await asyncio.sleep(0.01)


def _max_gap_ms(samples: list[float]) -> float:
    """Return the largest gap between consecutive heartbeat timestamps, in ms.

    Returns 0.0 if fewer than two samples were recorded (the download
    finished before the heartbeat could tick; treat as no observable
    stall — failing the test on that would be a false positive).
    """
    if len(samples) < 2:
        return 0.0
    gaps_ms = [(samples[i] - samples[i - 1]) * 1000.0 for i in range(1, len(samples))]
    return max(gaps_ms)


@pytest.fixture
def mock_artifacts_api() -> tuple[ArtifactsAPI, MagicMock]:
    """``ArtifactsAPI`` wired to a mock ``ClientCore``.

    Same shape as the unit-test fixture in ``tests/unit/test_artifact_downloads.py``
    so future readers can cross-reference the protocol shaping. We keep
    a local copy here because importing across the unit/integration
    boundary in pytest is fragile when both define ``mock_artifacts_api``
    at module scope.
    """
    mock_core = MagicMock()
    mock_core.rpc_call = AsyncMock()
    mock_core.get_source_ids = AsyncMock(return_value=[])
    api = ArtifactsAPI(mock_core)
    return api, mock_core


@pytest.mark.asyncio
async def test_download_report_does_not_block_event_loop(
    mock_artifacts_api: tuple[ArtifactsAPI, MagicMock],
    tmp_path: Path,
) -> None:
    """``download_report`` must offload its ``write_text`` to a thread.

    We patch ``Path.write_text`` to sleep ``BLOCKING_SLEEP_S`` seconds
    synchronously. Pre-fix, that ``time.sleep`` runs on the event-loop
    thread and freezes the heartbeat for ~200 ms. Post-fix the write
    runs via ``asyncio.to_thread`` so the loop stays responsive and
    the maximum heartbeat gap is well under ``MAX_GAP_MS``.
    """
    api, _ = mock_artifacts_api
    output_path = tmp_path / "report.md"

    # Minimal "completed report" shape that `_select_artifact` will accept.
    # See ``tests/unit/test_artifact_downloads.py::TestDownloadReport`` for
    # the canonical structure; index 7 is the markdown payload.
    report_artifact_list = [
        [
            "report_001",  # id
            "Report Title",  # title
            2,  # type code: REPORT
            None,
            3,  # status: COMPLETED
            None,
            None,
            ["# Test Report\n\nT7.D4 regression body."],  # markdown content
        ]
    ]

    original_write_text = Path.write_text

    def slow_write_text(self: Path, *args: object, **kwargs: object) -> int:
        # Simulate slow disk. ``time.sleep`` is the right primitive here:
        # pre-fix this runs on the loop and stalls every concurrent task;
        # post-fix it runs in a worker thread (``asyncio.to_thread``) and
        # the loop's heartbeat keeps ticking.
        time.sleep(BLOCKING_SLEEP_S)
        return original_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    samples: list[float] = []
    stop = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(stop, samples))

    # Warm-up: yield to the heartbeat so it records pre-block samples
    # before the download starts. Without this, a fast download could
    # finish before the heartbeat ticked once, leaving fewer than 2
    # samples and tripping the "treat as no observable stall" branch
    # in ``_max_gap_ms`` — a false negative.
    await asyncio.sleep(SETTLE_S)

    try:
        with (
            patch.object(api, "_list_raw", new_callable=AsyncMock) as mock_list,
            patch.object(Path, "write_text", slow_write_text),
        ):
            mock_list.return_value = report_artifact_list
            result = await api.download_report("nb_t7d4", str(output_path))
        # Yield so the heartbeat records at least one post-download
        # timestamp before we stop it; see module docstring "post-block
        # settling" note.
        await asyncio.sleep(SETTLE_S)
    finally:
        stop.set()
        await heartbeat

    assert result == str(output_path)
    assert output_path.exists(), "download_report should still produce the file"

    gap_ms = _max_gap_ms(samples)
    assert gap_ms < MAX_GAP_MS, (
        f"download_report blocked the event loop for {gap_ms:.1f} ms "
        f"(threshold {MAX_GAP_MS} ms). "
        f"The Path.write_text call must be wrapped in asyncio.to_thread."
    )


@pytest.mark.asyncio
async def test_download_mind_map_does_not_block_event_loop(
    mock_artifacts_api: tuple[ArtifactsAPI, MagicMock],
    tmp_path: Path,
) -> None:
    """``download_mind_map`` must offload its ``write_text`` to a thread.

    Same heartbeat methodology as the report test. Mind map JSON is
    serialized in-memory (cheap) and then written to disk; only the
    write needs to be offloaded.
    """
    api, _ = mock_artifacts_api
    output_path = tmp_path / "mindmap.json"

    json_content = json.dumps({"name": "Root", "children": [{"name": "T7.D4"}]})
    # Shape matches the canonical mind-map row used elsewhere in the test
    # suite: index 1 holds the [meta, content_str] pair.
    mind_map_rows = [
        [
            "mindmap_001",  # mm[0] = id
            [None, json_content],  # mm[1][1] = JSON string
            None,
            None,
            "Mind Map Title",  # mm[4] = title
        ]
    ]

    original_write_text = Path.write_text

    def slow_write_text(self: Path, *args: object, **kwargs: object) -> int:
        time.sleep(BLOCKING_SLEEP_S)
        return original_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    samples: list[float] = []
    stop = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(stop, samples))

    # Warm-up: yield to the heartbeat so it records pre-block samples
    # before the download starts. Without this, a fast download could
    # finish before the heartbeat ticked once, leaving fewer than 2
    # samples and tripping the "treat as no observable stall" branch
    # in ``_max_gap_ms`` — a false negative.
    await asyncio.sleep(SETTLE_S)

    try:
        with (
            patch(
                "notebooklm._artifacts._mind_map.list_mind_maps",
                new=AsyncMock(return_value=mind_map_rows),
            ),
            patch.object(Path, "write_text", slow_write_text),
        ):
            result = await api.download_mind_map("nb_t7d4", str(output_path))
        await asyncio.sleep(SETTLE_S)  # let heartbeat record a post-block sample
    finally:
        stop.set()
        await heartbeat

    assert result == str(output_path)
    assert output_path.exists(), "download_mind_map should still produce the file"

    gap_ms = _max_gap_ms(samples)
    assert gap_ms < MAX_GAP_MS, (
        f"download_mind_map blocked the event loop for {gap_ms:.1f} ms "
        f"(threshold {MAX_GAP_MS} ms). "
        f"The Path.write_text call must be wrapped in asyncio.to_thread."
    )


@pytest.mark.asyncio
async def test_concurrent_downloads_keep_loop_responsive(
    mock_artifacts_api: tuple[ArtifactsAPI, MagicMock],
    tmp_path: Path,
) -> None:
    """End-to-end fan-out: report + mind-map concurrently must not block.

    This is the integration-flavored cousin of the two single-call tests
    above. It fans out one ``download_report`` and one ``download_mind_map``
    against the same heartbeat. With the fix in place neither one steals
    the loop and the heartbeat stays smooth across both calls.

    A single overall ``MAX_GAP_MS`` bound is asserted: even with two
    concurrent slow-stubbed writes the loop must not stall.
    """
    api, _ = mock_artifacts_api
    report_path = tmp_path / "report.md"
    mindmap_path = tmp_path / "mindmap.json"

    report_artifact_list = [
        [
            "report_002",
            "Report Title",
            2,
            None,
            3,
            None,
            None,
            ["# Fanout Report\n\nT7.D4 concurrent body."],
        ]
    ]
    mind_map_rows = [
        [
            "mindmap_002",
            [None, json.dumps({"name": "FanoutRoot"})],
            None,
            None,
            "Fanout Mind Map",
        ]
    ]

    original_write_text = Path.write_text

    def slow_write_text(self: Path, *args: object, **kwargs: object) -> int:
        time.sleep(BLOCKING_SLEEP_S)
        return original_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    samples: list[float] = []
    stop = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(stop, samples))

    # Warm-up: yield to the heartbeat so it records pre-block samples
    # before the download starts. Without this, a fast download could
    # finish before the heartbeat ticked once, leaving fewer than 2
    # samples and tripping the "treat as no observable stall" branch
    # in ``_max_gap_ms`` — a false negative.
    await asyncio.sleep(SETTLE_S)

    try:
        with (
            patch.object(api, "_list_raw", new_callable=AsyncMock) as mock_list,
            patch(
                "notebooklm._artifacts._mind_map.list_mind_maps",
                new=AsyncMock(return_value=mind_map_rows),
            ),
            patch.object(Path, "write_text", slow_write_text),
        ):
            mock_list.return_value = report_artifact_list
            report_result, mindmap_result = await asyncio.gather(
                api.download_report("nb_t7d4", str(report_path)),
                api.download_mind_map("nb_t7d4", str(mindmap_path)),
            )
        await asyncio.sleep(SETTLE_S)  # let heartbeat record a post-block sample
    finally:
        stop.set()
        await heartbeat

    assert report_result == str(report_path)
    assert mindmap_result == str(mindmap_path)
    assert report_path.exists()
    assert mindmap_path.exists()

    gap_ms = _max_gap_ms(samples)
    assert gap_ms < MAX_GAP_MS, (
        f"Concurrent download_report + download_mind_map blocked the event loop "
        f"for {gap_ms:.1f} ms (threshold {MAX_GAP_MS} ms). "
        f"Both write_text calls must be wrapped in asyncio.to_thread."
    )


@pytest.mark.asyncio
async def test_download_urls_batch_cookie_load_does_not_block_event_loop(
    mock_artifacts_api: tuple[ArtifactsAPI, MagicMock],
    tmp_path: Path,
) -> None:
    """``_download_urls_batch`` must offload its ``load_httpx_cookies`` call.

    Mirror of the ``_download_url`` test, but exercises the batch
    sibling. The batch helper is reachable from ``download_audio`` /
    ``download_video`` / ``download_image`` so its cookie-load path
    also needs the offload. We patch the imported symbol to sleep
    ``BLOCKING_SLEEP_S`` seconds and assert the heartbeat stayed
    alive while the cookies were "loading".

    The batch then proceeds into the HTTP path. We pass an EMPTY URL
    list so the per-URL loop exits immediately and the test stays
    sealed from the network — the only work between the cookie load
    and the return is opening + closing an ``httpx.AsyncClient``,
    which doesn't touch the network until the first request.

    Added in response to PR #579 review feedback from claude[bot]:
    each of the four T7.D4 wrap sites should have a direct regression
    test, not just three of them.
    """
    api, _ = mock_artifacts_api
    api._storage_path = tmp_path / "fake_storage_state.json"

    def slow_load_httpx_cookies(path: object = None) -> dict:
        time.sleep(BLOCKING_SLEEP_S)
        return {}

    samples: list[float] = []
    stop = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(stop, samples))

    # Warm-up: let the heartbeat record pre-block samples.
    await asyncio.sleep(SETTLE_S)

    try:
        with patch(
            "notebooklm._artifacts.load_httpx_cookies",
            side_effect=slow_load_httpx_cookies,
        ):
            # Empty URL list: the cookie-load runs (the thing we're
            # measuring), then the per-URL ``for`` loop exits without
            # ever issuing a request.
            result = await api._download_urls_batch([])
        await asyncio.sleep(SETTLE_S)  # let heartbeat record a post-block sample
    finally:
        stop.set()
        await heartbeat

    # Sanity: empty input → empty result, no failures fabricated.
    assert result.succeeded == []
    assert result.failed == []

    gap_ms = _max_gap_ms(samples)
    assert gap_ms < MAX_GAP_MS, (
        f"_download_urls_batch's load_httpx_cookies blocked the event loop "
        f"for {gap_ms:.1f} ms (threshold {MAX_GAP_MS} ms). "
        f"The load_httpx_cookies call must be wrapped in asyncio.to_thread."
    )


@pytest.mark.asyncio
async def test_download_url_cookie_load_does_not_block_event_loop(
    mock_artifacts_api: tuple[ArtifactsAPI, MagicMock],
    tmp_path: Path,
) -> None:
    """``_download_url`` must offload its ``load_httpx_cookies`` call.

    Pre-fix, the synchronous ``load_httpx_cookies(path=...)`` call —
    which can do a real ``Path.read_text`` + ``json.loads`` on the
    storage-state file — runs on the loop thread before the streaming
    download begins. We patch the imported symbol to sleep
    ``BLOCKING_SLEEP_S`` seconds and confirm the heartbeat is not
    starved while the cookies are "loading".

    We don't have to actually complete a download here — the cookie
    load happens before any HTTP work — so we let the subsequent
    streaming call raise (an ``ArtifactDownloadError``) and just
    assert the heartbeat stayed alive through the cookie-load phase.
    """
    api, _ = mock_artifacts_api
    api._storage_path = tmp_path / "fake_storage_state.json"
    output_path = tmp_path / "download.bin"

    def slow_load_httpx_cookies(path: object = None) -> dict:
        # Simulate a slow storage-state read. Returning {} is fine —
        # the test never reaches the HTTP transport.
        time.sleep(BLOCKING_SLEEP_S)
        return {}

    samples: list[float] = []
    stop = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(stop, samples))

    # Warm-up: yield to the heartbeat so it records pre-block samples
    # before the download starts. Without this, a fast download could
    # finish before the heartbeat ticked once, leaving fewer than 2
    # samples and tripping the "treat as no observable stall" branch
    # in ``_max_gap_ms`` — a false negative.
    await asyncio.sleep(SETTLE_S)

    try:
        # Use an obviously-unreachable but trusted-domain URL so the
        # request scheme + domain checks pass and we get all the way
        # to the cookie load; the subsequent network call fails with
        # ``ArtifactDownloadError`` (transport error), but that's fine —
        # we only need the heartbeat data from the pre-network phase.
        with (
            patch(
                "notebooklm._artifacts.load_httpx_cookies",
                side_effect=slow_load_httpx_cookies,
            ),
            pytest.raises(ArtifactDownloadError),
        ):
            await api._download_url(
                "https://storage.googleapis.com/never-resolved-t7d4.bin",
                str(output_path),
            )
        await asyncio.sleep(SETTLE_S)  # let heartbeat record a post-block sample
    finally:
        stop.set()
        await heartbeat

    gap_ms = _max_gap_ms(samples)
    assert gap_ms < MAX_GAP_MS, (
        f"_download_url's load_httpx_cookies blocked the event loop for "
        f"{gap_ms:.1f} ms (threshold {MAX_GAP_MS} ms). "
        f"The load_httpx_cookies call must be wrapped in asyncio.to_thread."
    )
