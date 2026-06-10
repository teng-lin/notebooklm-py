"""Regression test for the download paths must not block the event loop.

Audit item #30 (`thread-safety-concurrency-audit.md` §30):

> `_download_urls_batch()` and `_download_url()` call `load_httpx_cookies()`
> (synchronous JSON read) directly from `async def`. `download_report()`
> and `download_mind_map()` call `Path.write_text()` directly on the loop.
> Slow storage / large payloads stall every other concurrent task.

This module pins the post-fix invariant: each blocking sync call site
must execute via ``asyncio.to_thread`` (or an equivalent offload) so a
slow filesystem cannot freeze sibling coroutines for the duration of
the call.

Assertion methodology — thread-id capture
-----------------------------------------

Each test patches the production call site (either the ``to_thread``
target itself or a method called from inside the ``to_thread``
closure) with a recording stub that captures ``threading.get_ident()``.
After the download runs, the test asserts the captured thread id
differs from the loop thread id. If the production wrap
(``await asyncio.to_thread(...)``) is in place, the stub runs on the
default ThreadPoolExecutor and the ids differ. If a regression removes
the wrap, the stub runs on the loop thread and the ids match.

Why not measure scheduler responsiveness directly (heartbeat-gap)
.................................................................

An earlier version of these tests fired a 10 ms heartbeat coroutine
during the download and asserted the max gap between heartbeat ticks
stayed below a threshold. That pattern proved flaky on shared CI:

* macOS 3.14:    ~55 ms green-run jitter (the original tuning point).
* Ubuntu 3.11:   170.8 ms green-run jitter (PR #621 run 25928433246).
* Windows 3.10-14: 170-220 ms typical, 2594 ms outlier on Win 3.11.

A 200 ms regression signal sits inside the 170-220 ms baseline noise
on Linux + Windows runners — no single threshold can discriminate
"offloaded" from "regressed." Widening the stub trades wall time for
the same problem at the next jitter level.

The thread-id check is a *positive* assertion of the property the
heartbeat-gap was inferring. It needs no threshold, no scheduler
timing, and survives every matrix entry uniformly.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notebooklm._artifacts import ArtifactsAPI
from notebooklm.types import ArtifactDownloadError
from tests._fixtures.fake_core import FakeSession, make_fake_core

# mock-based loop-blocking detection tests; no HTTP, no cassette.
# Opt out of the tier-enforcement hook in tests/integration/conftest.py.
pytestmark = pytest.mark.allow_no_vcr


def _assert_offloaded_to_worker_thread(
    captured_thread_id: int | None,
    loop_thread_id: int,
    *,
    call_site: str,
    wrap_target: str,
) -> None:
    """Assert ``captured_thread_id`` came from a worker thread, not the loop.

    Args:
        captured_thread_id: The thread id observed inside the patched stub,
            or ``None`` if the stub never ran.
        loop_thread_id: ``threading.get_ident()`` captured on the loop
            thread before the download was awaited.
        call_site: Human-readable name of the production async function
            being tested (e.g. ``"_download_url"``), used in the failure
            message so the diagnostic points at the right code.
        wrap_target: Human-readable name of the synchronous call that
            must be offloaded (e.g. ``"load_httpx_cookies"``).
    """
    assert captured_thread_id is not None, (
        f"{call_site}'s {wrap_target} stub never ran — check the patch target."
    )
    assert captured_thread_id != loop_thread_id, (
        f"{call_site} ran {wrap_target} on the event-loop thread "
        f"(thread id {captured_thread_id}). It must be wrapped in "
        "asyncio.to_thread so slow synchronous I/O cannot stall "
        "concurrent tasks."
    )


@pytest.fixture
def mock_artifacts_api(tmp_path: Path) -> tuple[ArtifactsAPI, FakeSession]:
    """``ArtifactsAPI`` wired to a constructor-injected fake core.

    Same shape as the unit-test fixture in ``tests/unit/test_artifact_downloads.py``
    so future readers can cross-reference the protocol shaping. We keep
    a local copy here because importing across the unit/integration
    boundary in pytest is fragile when both define ``mock_artifacts_api``
    at module scope.

    The core is built via ``make_fake_core(...)`` (ADR-0007 constructor
    injection) rather than mutating a ``MagicMock``'s ``rpc_call`` after
    construction: the fake exposes the shared ``RpcCaller`` surface on
    ``mock_core`` (and its ``rpc_executor`` mirror) plus the drain /
    lifecycle capability stubs the API reads, so no post-hoc
    ``AsyncMock`` attribute assignment is needed.
    """
    from notebooklm._mind_map import NoteBackedMindMapService
    from notebooklm._note_service import NoteService

    mock_core = make_fake_core(
        rpc_call=AsyncMock(),
        get_source_ids=AsyncMock(return_value=[]),
    )
    note_service = NoteService(mock_core)
    mind_maps = NoteBackedMindMapService(note_service)
    api = ArtifactsAPI(
        rpc=mock_core,
        drain=mock_core,
        lifecycle=mock_core,
        notebooks=MagicMock(),
        mind_maps=mind_maps,
        note_service=note_service,
        storage_path=tmp_path / "fake_storage_state.json",
    )
    return api, mock_core


@pytest.mark.asyncio
async def test_download_report_runs_write_off_loop_thread(
    mock_artifacts_api: tuple[ArtifactsAPI, FakeSession],
    tmp_path: Path,
) -> None:
    """``download_report`` must offload its ``Path.write_text`` to a thread.

    The production path wraps ``output.write_text(...)`` inside an
    ``asyncio.to_thread(_write_markdown)`` closure. We patch
    ``Path.write_text`` with a recording stub that captures the thread
    id on which it runs and still performs the real write (so the
    file-exists sanity check at the end stays meaningful). If a
    regression removes the wrap, the recording stub runs on the loop
    thread and the assertion fires.
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

    loop_thread_id = threading.get_ident()
    original_write_text = Path.write_text
    captured: list[int] = []

    def recording_write_text(self: Path, *args: object, **kwargs: object) -> int:
        captured.append(threading.get_ident())
        return original_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    with (
        patch.object(api._downloads, "_list_raw", new_callable=AsyncMock) as mock_list,
        patch.object(Path, "write_text", recording_write_text),
    ):
        mock_list.return_value = report_artifact_list
        result = await api.download_report("nb_t7d4", str(output_path))

    assert result == str(output_path)
    assert output_path.exists(), "download_report should still produce the file"

    _assert_offloaded_to_worker_thread(
        captured[0] if captured else None,
        loop_thread_id,
        call_site="download_report",
        wrap_target="Path.write_text",
    )


@pytest.mark.asyncio
async def test_download_mind_map_runs_write_off_loop_thread(
    mock_artifacts_api: tuple[ArtifactsAPI, FakeSession],
    tmp_path: Path,
) -> None:
    """``download_mind_map`` must offload its JSON write to a thread.

    The production path wraps ``json.dump(...)`` inside an
    ``asyncio.to_thread(_write_json)`` closure. A legacy alternative
    that used ``Path.write_text`` is also patched, so a refactor that
    rewrites the write API in either direction is still covered. We
    require AT LEAST ONE of the two write APIs to fire (the production
    path uses ``json.dump``; ``Path.write_text`` is the fallback) and
    that the firing site ran on a worker thread, not the loop.

    Originally pointed out by coderabbit on PR #579: patching only
    ``Path.write_text`` would silently miss the production ``json.dump``
    path.
    """
    import notebooklm._artifact.downloads as artifact_downloads

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

    loop_thread_id = threading.get_ident()
    original_json_dump = json.dump
    original_write_text = Path.write_text
    captured_json: list[int] = []
    captured_write: list[int] = []

    def recording_json_dump(*args: object, **kwargs: object) -> None:
        captured_json.append(threading.get_ident())
        return original_json_dump(*args, **kwargs)  # type: ignore[arg-type]

    def recording_write_text(self: Path, *args: object, **kwargs: object) -> int:
        captured_write.append(threading.get_ident())
        return original_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    with (
        patch.object(
            api._mind_maps,
            "list_mind_maps",
            new=AsyncMock(return_value=mind_map_rows),
        ),
        # Patch the `json` module as imported by `_artifact.downloads` so the
        # closure inside `download_mind_map` resolves to the stub.
        patch.object(artifact_downloads.json, "dump", recording_json_dump),
        # Cover the legacy ``Path.write_text``-based path too so a
        # rewrite either direction is caught by this test.
        patch.object(Path, "write_text", recording_write_text),
    ):
        result = await api.download_mind_map("nb_t7d4", str(output_path))

    assert result == str(output_path)
    assert output_path.exists(), "download_mind_map should still produce the file"

    # Require the firing write site to have run off-loop. The production
    # code uses json.dump; if a future refactor swaps to Path.write_text
    # the other capture list catches it.
    captured = captured_json or captured_write
    wrap_target = "json.dump" if captured_json else "Path.write_text"
    _assert_offloaded_to_worker_thread(
        captured[0] if captured else None,
        loop_thread_id,
        call_site="download_mind_map",
        wrap_target=wrap_target,
    )


@pytest.mark.asyncio
async def test_concurrent_downloads_both_offload_writes(
    mock_artifacts_api: tuple[ArtifactsAPI, FakeSession],
    tmp_path: Path,
) -> None:
    """End-to-end fan-out: report + mind-map concurrently must both offload.

    Integration-flavored cousin of the two single-call tests above. We
    fan out one ``download_report`` and one ``download_mind_map`` under
    ``asyncio.gather`` and require BOTH write sites — ``Path.write_text``
    (report) and ``json.dump`` (mind-map) — to have run on a worker
    thread. A regression on either path leaves its capture matching the
    loop thread and fails the assertion.
    """
    import notebooklm._artifact.downloads as artifact_downloads

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

    loop_thread_id = threading.get_ident()
    original_write_text = Path.write_text
    original_json_dump = json.dump
    captured_write: list[int] = []
    captured_json: list[int] = []

    def recording_write_text(self: Path, *args: object, **kwargs: object) -> int:
        captured_write.append(threading.get_ident())
        return original_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    def recording_json_dump(*args: object, **kwargs: object) -> None:
        captured_json.append(threading.get_ident())
        return original_json_dump(*args, **kwargs)  # type: ignore[arg-type]

    with (
        patch.object(api._downloads, "_list_raw", new_callable=AsyncMock) as mock_list,
        patch.object(
            api._mind_maps,
            "list_mind_maps",
            new=AsyncMock(return_value=mind_map_rows),
        ),
        patch.object(Path, "write_text", recording_write_text),
        patch.object(artifact_downloads.json, "dump", recording_json_dump),
    ):
        mock_list.return_value = report_artifact_list
        report_result, mindmap_result = await asyncio.gather(
            api.download_report("nb_t7d4", str(report_path)),
            api.download_mind_map("nb_t7d4", str(mindmap_path)),
        )

    assert report_result == str(report_path)
    assert mindmap_result == str(mindmap_path)
    assert report_path.exists()
    assert mindmap_path.exists()

    _assert_offloaded_to_worker_thread(
        captured_write[0] if captured_write else None,
        loop_thread_id,
        call_site="download_report (concurrent)",
        wrap_target="Path.write_text",
    )
    _assert_offloaded_to_worker_thread(
        captured_json[0] if captured_json else None,
        loop_thread_id,
        call_site="download_mind_map (concurrent)",
        wrap_target="json.dump",
    )


@pytest.mark.asyncio
async def test_download_urls_batch_cookie_load_runs_off_loop_thread(
    mock_artifacts_api: tuple[ArtifactsAPI, FakeSession],
    tmp_path: Path,
) -> None:
    """``_download_urls_batch`` must offload its ``load_httpx_cookies`` call.

    Empty URL list keeps the test sealed from the network — the only
    work between the cookie load and the return is opening + closing
    an ``httpx.AsyncClient``, which doesn't touch the network until
    the first request.
    """
    import notebooklm._artifact.downloads as artifact_downloads

    api, _ = mock_artifacts_api

    loop_thread_id = threading.get_ident()
    captured: list[int] = []

    def recording_load_httpx_cookies(path: object = None) -> dict:
        captured.append(threading.get_ident())
        return {}

    # Object-form patch against the locally-imported downloads module
    # (ADR-0007): the seam is the ``load_httpx_cookies`` name bound *in*
    # ``_artifact.downloads`` (imported there from ``..auth``), which the
    # ``asyncio.to_thread(_load_httpx_cookies, ...)`` offload resolves.
    with patch.object(
        artifact_downloads,
        "load_httpx_cookies",
        new=recording_load_httpx_cookies,
    ):
        result = await api._download_urls_batch([])

    # Sanity: empty input → empty result, no failures fabricated.
    assert result.succeeded == []
    assert result.failed == []

    _assert_offloaded_to_worker_thread(
        captured[0] if captured else None,
        loop_thread_id,
        call_site="_download_urls_batch",
        wrap_target="load_httpx_cookies",
    )


@pytest.mark.asyncio
async def test_download_url_cookie_load_runs_off_loop_thread(
    mock_artifacts_api: tuple[ArtifactsAPI, FakeSession],
    tmp_path: Path,
    httpx_mock,
) -> None:
    """``_download_url`` must offload its ``load_httpx_cookies`` call.

    The subsequent HTTP request is intercepted by ``httpx_mock`` with a
    404 so ``_download_url`` raises ``ArtifactDownloadError`` (and the
    test stays sealed from the real network — the URL must still match
    the production trusted-domain whitelist to clear validation, but no
    bytes hit the wire). The thread-id capture has already happened by
    the time the HTTP step runs.
    """
    import notebooklm._artifact.downloads as artifact_downloads

    api, _ = mock_artifacts_api
    output_path = tmp_path / "download.bin"

    # URL must clear the production trusted-domain check
    # (``.googleapis.com``) BEFORE ``load_httpx_cookies`` runs — the
    # validation happens first, and a rejected URL would raise
    # ``ArtifactDownloadError("Untrusted download domain")`` before the
    # cookie load and leave ``captured`` empty (turning this test into
    # a false negative). The path is arbitrary because ``httpx_mock``
    # intercepts the request before it leaves the process.
    url = "https://storage.googleapis.com/never-resolved-t7d4.bin"
    httpx_mock.add_response(url=url, status_code=404)

    loop_thread_id = threading.get_ident()
    captured: list[int] = []

    def recording_load_httpx_cookies(path: object = None) -> dict:
        captured.append(threading.get_ident())
        return {}

    with (
        # Object-form patch against the locally-imported downloads module
        # (ADR-0007) — patches the ``load_httpx_cookies`` name bound in
        # ``_artifact.downloads`` that the cookie-load offload resolves.
        patch.object(
            artifact_downloads,
            "load_httpx_cookies",
            new=recording_load_httpx_cookies,
        ),
        pytest.raises(ArtifactDownloadError),
    ):
        await api._download_url(url, str(output_path))

    _assert_offloaded_to_worker_thread(
        captured[0] if captured else None,
        loop_thread_id,
        call_site="_download_url",
        wrap_target="load_httpx_cookies",
    )


@pytest.mark.asyncio
async def test_download_url_uses_single_writer_thread_for_all_chunks(
    mock_artifacts_api: tuple[ArtifactsAPI, FakeSession],
    tmp_path: Path,
) -> None:
    """``_download_url`` must drive ALL chunks through ONE writer thread.

    Pre-fix the streaming loop wrapped every 64 KiB chunk in its own
    ``asyncio.to_thread(f.write, chunk)`` call. For multi-GB downloads
    that adds up to thousands of fresh thread-pool jobs — allocation
    churn and contention on the default executor.

    Post-fix the producer pushes chunks onto a bounded queue drained by
    a single dedicated writer thread. The writer runs on a dedicated
    ``threading.Thread`` (NOT ``asyncio.to_thread``) so it does not tie
    up a slot in asyncio's default executor pool — addresses the
    gemini-code-assist HIGH-severity finding about default-executor
    saturation under many concurrent downloads.

    Methodology
    -----------
    Two layers of evidence:

    1. Patch ``threading.Thread`` with a recorder that counts
       instantiations whose ``target`` is the ``_writer_loop`` closure.
       Exactly one writer thread must be instantiated.

    2. Patch ``builtins.open`` on the temp-file path so the returned
       handle records ``threading.get_ident()`` for every ``write()``
       call. All recorded ids must be identical AND must match the
       writer thread's ``ident``. CodeRabbit nitpick on PR #981: thread
       construction alone doesn't prove every chunk was written on
       that thread; recording the call site closes that gap.

    A regression that goes back to per-chunk ``to_thread(f.write, ...)``
    fails layer 1 (thread count > 1) AND layer 2 (writes happen across
    multiple thread idents).
    """
    api, _ = mock_artifacts_api
    output_path = tmp_path / "many_chunks.bin"

    import builtins
    import threading as real_threading

    import httpx as real_httpx

    import notebooklm._artifact.downloads as artifact_downloads

    # 32 chunks of 64 KiB. Anything > ~2 demonstrates the regression
    # signal cleanly; 32 keeps the test fast while making the
    # per-chunk-pre-fix count visually obvious in failure output.
    chunks = [b"x" * 65536 for _ in range(32)]

    async def mock_aiter_bytes(chunk_size: int = 8192):
        for chunk in chunks:
            yield chunk

    mock_response = MagicMock()
    mock_response.headers = {"content-type": "video/mp4"}
    mock_response.raise_for_status = MagicMock()
    mock_response.aiter_bytes = mock_aiter_bytes
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    mock_client = AsyncMock()
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    original_thread_cls = real_threading.Thread
    real_open = builtins.open
    writer_threads = 0
    writer_thread_idents: list[int] = []
    write_thread_ids: list[int] = []

    class _RecordingThread(original_thread_cls):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal writer_threads
            target = kwargs.get("target") or (args[1] if len(args) > 1 else None)
            name = getattr(target, "__qualname__", "") or getattr(target, "__name__", "")
            self._is_writer_loop_thread = name.endswith("_writer_loop")
            if self._is_writer_loop_thread:
                writer_threads += 1
            super().__init__(*args, **kwargs)

        def start(self) -> None:
            super().start()
            # ``ident`` is only valid after ``start()``. Capture it
            # here so the test can compare it against the per-write
            # thread ids recorded by ``_RecordingFile``.
            if self._is_writer_loop_thread:
                writer_thread_idents.append(self.ident)  # type: ignore[arg-type]

    class _RecordingFile:
        """Forwards every attribute to the real handle but instruments
        ``write`` to record the calling thread's ident."""

        def __init__(self, fh) -> None:  # type: ignore[no-untyped-def]
            self._fh = fh

        def write(self, data: bytes) -> int:
            write_thread_ids.append(real_threading.get_ident())
            return self._fh.write(data)

        def __enter__(self) -> _RecordingFile:
            return self

        def __exit__(self, *exc) -> None:  # type: ignore[no-untyped-def]
            self._fh.close()

        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            return getattr(self._fh, name)

    def _patched_open(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            target_path = Path(file).resolve()
            in_tmp_dir = target_path.parent == tmp_path.resolve()
        except (TypeError, ValueError):
            in_tmp_dir = False
        if in_tmp_dir and "w" in mode and "b" in mode:
            return _RecordingFile(real_open(file, mode, *args, **kwargs))
        return real_open(file, mode, *args, **kwargs)

    with (
        patch.object(real_httpx, "AsyncClient", return_value=mock_client),
        # Object-form patches against the locally-imported downloads module
        # (ADR-0007): the cookie-load seam and the module's ``threading``
        # reference whose ``Thread`` the writer-thread construction uses.
        patch.object(artifact_downloads, "load_httpx_cookies", return_value=MagicMock()),
        patch.object(artifact_downloads.threading, "Thread", new=_RecordingThread),
        patch.object(builtins, "open", new=_patched_open),
    ):
        result = await api._download_url(
            "https://storage.googleapis.com/many_chunks.bin", str(output_path)
        )

    assert result == str(output_path)
    assert output_path.exists()
    assert output_path.stat().st_size == sum(len(c) for c in chunks), (
        "The temp-file → final-file atomic replace must preserve all bytes."
    )
    assert writer_threads == 1, (
        f"_download_url created {writer_threads} writer threads for a "
        f"{len(chunks)}-chunk download. The fix requires exactly ONE long-lived "
        "writer thread fed via a bounded queue, not a fresh thread per chunk."
    )
    # Layer 2: prove every chunk was written on the same single thread,
    # and that thread is the dedicated writer.
    assert len(write_thread_ids) == len(chunks), (
        f"recorded {len(write_thread_ids)} writes for a {len(chunks)}-chunk "
        "download — production code must call ``fh.write`` exactly once per "
        "chunk on the writer thread."
    )
    distinct_write_idents = set(write_thread_ids)
    assert len(distinct_write_idents) == 1, (
        f"chunks were written across {len(distinct_write_idents)} distinct "
        f"threads ({sorted(distinct_write_idents)}). A regression that mixes "
        "the dedicated writer with per-chunk ``to_thread(f.write, ...)`` calls "
        "would surface here."
    )
    assert writer_thread_idents, "writer thread ident was not captured"
    assert distinct_write_idents == {writer_thread_idents[0]}, (
        f"writes happened on thread {distinct_write_idents.pop()} but the "
        f"dedicated _writer_loop thread had ident {writer_thread_idents[0]}. "
        "Writes must run on the writer thread, not a sibling executor slot."
    )


@pytest.mark.asyncio
async def test_download_url_writer_failure_does_not_deadlock_producer(
    mock_artifacts_api: tuple[ArtifactsAPI, FakeSession],
    tmp_path: Path,
) -> None:
    """A failing writer thread must not deadlock the producer.

    Regression: with a bounded queue, a producer parked in ``q.put``
    on a full queue hangs forever if the writer raises mid-stream
    (the worker thread holding the put only releases when a consumer
    takes, and we are the only consumer). The fix has the writer's
    ``finally`` drain the queue so blocked puts can complete and the
    producer can observe ``writer_task.done()`` on the next iteration.

    Determinism strategy — engineer the race so the queue is GUARANTEED
    full when the writer dies:

    1. Patch ``builtins.open`` so the writer's handle defers the
       OSError until a ``threading.Event`` is set. This holds the
       writer at its first ``fh.write`` indefinitely, letting the
       producer fill the queue.
    2. The producer yields many chunks. Each ``q.put`` succeeds until
       the bounded queue (size 8) is full; the next put parks on a
       worker thread.
    3. From the test we wait until the queue is observed full, then
       set the event. The writer's first write raises OSError; with
       the fix, the writer's ``finally`` drains the queue and the
       parked producer wakes; without the fix, the parked producer
       hangs and ``asyncio.wait_for`` times out.
    """
    import builtins  # local import keeps the module's import list lean
    import threading

    import httpx as real_httpx

    import notebooklm._artifact.downloads as artifact_downloads

    api, _ = mock_artifacts_api
    output_path = tmp_path / "doomed.bin"

    # ``aiter_bytes`` yields chunks indefinitely so the queue can keep
    # being filled regardless of how many slots the writer has cleared.
    async def mock_aiter_bytes(chunk_size: int = 8192):
        idx = 0
        while True:
            yield b"x" * 65536
            idx += 1
            # Yield to the loop so the producer doesn't spin without
            # giving the writer thread CPU time.
            await asyncio.sleep(0)

    mock_response = MagicMock()
    mock_response.headers = {"content-type": "video/mp4"}
    mock_response.raise_for_status = MagicMock()
    mock_response.aiter_bytes = mock_aiter_bytes
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    mock_client = AsyncMock()
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    real_open = builtins.open
    write_blocked = threading.Event()
    release_writer = threading.Event()

    class _DeferredExplodingHandle:
        """File handle whose first ``write`` blocks until released, then raises."""

        def write(self, data: bytes) -> int:
            write_blocked.set()
            release_writer.wait(timeout=5.0)
            raise OSError("simulated disk full")

        def close(self) -> None:  # noqa: D401
            pass

        def __enter__(self) -> _DeferredExplodingHandle:
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
            self.close()

    def _patched_open(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            target = Path(file).resolve()
            same_dir = target.parent == tmp_path.resolve()
        except (TypeError, ValueError):
            same_dir = False
        if same_dir and "w" in mode and "b" in mode:
            return _DeferredExplodingHandle()
        return real_open(file, mode, *args, **kwargs)

    async def _release_writer_after_queue_fills() -> None:
        # Wait until the writer thread has entered its (blocked)
        # ``write`` call. That guarantees one chunk has been
        # dequeued, AND the writer is no longer consuming. By the
        # time the producer's next ``q.put`` parks, we know the
        # queue is full.
        await asyncio.to_thread(write_blocked.wait, 2.0)
        # Give the producer time to fill the queue and park.
        await asyncio.sleep(0.2)
        # Now release the writer — its ``write`` raises OSError.
        # The post-fix ``finally`` block must drain the queue so the
        # parked producer can wake.
        release_writer.set()

    with (
        patch.object(real_httpx, "AsyncClient", return_value=mock_client),
        # Object-form patch against the locally-imported downloads module
        # (ADR-0007) — patches the cookie-load seam bound in
        # ``_artifact.downloads``.
        patch.object(artifact_downloads, "load_httpx_cookies", return_value=MagicMock()),
        patch.object(builtins, "open", new=_patched_open),
    ):
        download_coro = api._download_url(
            "https://storage.googleapis.com/doomed.bin", str(output_path)
        )
        release_coro = _release_writer_after_queue_fills()
        # Run both concurrently. Pre-fix the download hangs in
        # ``q.put`` forever and ``wait_for`` fires before either coro
        # makes progress. Post-fix the writer's ``finally`` drains the
        # queue, the producer unblocks, ``await writer_task`` raises
        # OSError, the outer ``except`` cleans up the temp file, and
        # the OSError propagates to the test.
        with pytest.raises(OSError, match="simulated disk full"):
            await asyncio.wait_for(
                asyncio.gather(download_coro, release_coro),
                timeout=5.0,
            )

    # Final file must NOT exist (atomic replace never ran).
    assert not output_path.exists()
    # Temp file must NOT be left behind.
    assert list(tmp_path.glob("doomed.bin.*.tmp")) == []
