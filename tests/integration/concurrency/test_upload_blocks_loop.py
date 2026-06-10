"""Regression test for the upload chunk reads must not block the event loop.

Audit item §22 (``thread-safety-concurrency-audit.md`` §22):

    ``_upload_file_streaming`` streams the upload body via an
    ``async def file_stream()`` generator that does
    ``while chunk := f.read(65536):  yield chunk``. The ``f.read(...)``
    call is a *synchronous* filesystem syscall executed directly on the
    event-loop thread. For each 64 KiB chunk, slow storage (a FUSE
    mount, a network filesystem under contention, an encrypted home,
    or a large PDF served from a cold cache) stalls every other
    concurrent task — auth refresh, the cancellation watchdog, the
    sibling RPC the caller fired in parallel — for the full read
    latency.

Post-fix — each chunk read wraps the synchronous ``f.read``
with ``await asyncio.to_thread(f.read, 65536)`` so the blocking
syscall runs on the default thread executor and the loop keeps
ticking sibling tasks. Stdlib only — no ``aiofiles``.

Methodology — heartbeat-gap detection
-------------------------------------
We do NOT try to prove the call is *exactly* on a thread; we prove
the **observable consequence** — that a slow synchronous chunk read
does not stall a concurrent heartbeat coroutine.

A heartbeat task fires roughly every 10 ms via ``asyncio.sleep(0.01)``
for the duration of the upload. The upload's first chunk read takes
``SLEEP_S`` seconds (we inject a ``time.sleep`` inside the file-like
object's ``.read``). If the upload blocked the loop, the gap between
consecutive heartbeat timestamps spikes to ``>= SLEEP_S * 1000`` ms.
With the fix in place the read runs on a worker thread and the
heartbeat keeps ticking at ~10 ms intervals.

This module pins TWO call sites — both must remain off-loop:

1. ``test_upload_file_streaming_fd_path_does_not_block_event_loop``
   The production path: ``add_file`` opens the FD once, transfers
   ownership to ``_upload_file_streaming`` via the ``file_obj``
   argument, and the helper drives ``file_obj.read(65536)`` from the
   generator. We pass a file-like object whose ``.read`` does
   ``time.sleep`` so the regression signal is unmistakable.

2. ``test_upload_file_streaming_path_fallback_does_not_block_event_loop``
   The legacy direct-call path retained for the existing
   ``test_sources_upload.py`` unit tests. ``_upload_file_streaming``
   accepts a ``Path``, opens it itself, and reads via
   ``f.read(65536)`` from the ``with open(...)`` block. We
   monkey-patch ``builtins.open`` (scoped to our temp file) so the
   returned file object's ``.read`` is the slow one; the bare
   ``open()`` call inside the helper falls through to the patch.

Both branches must be wrapped; a regression on either path fails
this suite.
"""

from __future__ import annotations

import asyncio
import builtins
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notebooklm._source.upload import SourceUploadPipeline
from notebooklm._sources import SourcesAPI
from tests._fixtures.fake_core import FakeSession, make_fake_core

# mock-based loop-blocking detection tests; no HTTP, no cassette.
# Opt out of the tier-enforcement hook in tests/integration/conftest.py.
pytestmark = pytest.mark.allow_no_vcr

# How long the injected synchronous ``.read`` blocks for. 200 ms is well
# above the asyncio scheduler resolution and comfortably above CI jitter,
# but short enough to keep the test fast.
SLEEP_S = 0.2

# Heartbeat interval. ~20 ticks fit inside the SLEEP_S window, giving us
# plenty of headroom over the ``MIN_HEARTBEATS`` threshold.
HEARTBEAT_INTERVAL_S = 0.01

# Minimum heartbeat samples we must observe during the slow read. Pre-fix
# the synchronous sleep freezes the loop and we typically see 0–1
# samples. Post-fix the read runs on a worker thread and we see ~20.
# 5 leaves a 4x margin over the regression signal while staying clearly
# below the post-fix expectation.
MIN_HEARTBEATS = 5


def _make_sources_api() -> tuple[SourcesAPI, FakeSession]:
    """Build a SourcesAPI via constructor-injection over ``make_fake_core``.

    Mirrors ``tests/unit/test_sources_upload.py``'s ``mock_core`` /
    ``sources_api`` fixture pair, which themselves build on
    ``make_fake_core`` — the sanctioned ADR-0007 substrate. The fake bundles
    every narrow collaborator the upload pipeline needs:

    - ``rpc_executor.rpc_call`` (RpcCaller) — injected fresh per call so a
      test can set ``.return_value`` without the default ``side_effect``
      shadowing it,
    - ``operation_scope`` (the ``drain`` slot) and ``assert_bound_loop``
      (the ``lifecycle`` slot) used by ``add_file``,
    - ``kernel`` (live cookie jar) and ``auth`` (authuser routing) consumed
      by the streaming/start paths,
    - ``record_upload_queue_wait`` for the upload-metrics path.

    Returning the fake lets callers configure ``core.rpc_executor.rpc_call``
    and assert it was awaited (the Form-1 bite-check for ADR-0007).
    """
    core = make_fake_core(rpc_call=AsyncMock())
    uploader = SourceUploadPipeline(
        rpc=core.rpc_executor,
        drain=core,
        lifecycle=core,
        kernel=core.kernel,
        auth=core.auth,
        record_upload_queue_wait=core.record_upload_queue_wait,
    )
    return SourcesAPI(core.rpc_executor, uploader=uploader), core


class _SlowReadFile:
    """File-like object whose ``.read`` does a real synchronous sleep.

    Behaves like ``io.BytesIO`` over a fixed payload but injects
    ``time.sleep(SLEEP_S)`` on the FIRST call to ``.read``. Subsequent
    reads return promptly (we only need ONE slow read to demonstrate
    the loop is blocked / not-blocked; further sleeps would just make
    the test slower without sharpening the signal).
    """

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._pos = 0
        self._first_read = True

    def read(self, size: int = -1) -> bytes:
        # The first read is the one whose duration the heartbeat
        # measures. Subsequent reads (and the empty terminator read at
        # EOF that closes the generator) skip the sleep so we don't pay
        # for them.
        if self._first_read:
            self._first_read = False
            time.sleep(SLEEP_S)
        if size < 0:
            chunk = self._payload[self._pos :]
            self._pos = len(self._payload)
            return chunk
        chunk = self._payload[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    def close(self) -> None:
        # The FD-branch done-callback closes the file; provide a no-op
        # so it doesn't error.
        pass

    # Context-manager support so the Path branch's
    # ``with open(path_fallback, "rb") as f:`` accepts the slow stub
    # returned by the monkey-patched ``builtins.open``.
    def __enter__(self) -> _SlowReadFile:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        self.close()


async def _run_with_heartbeat(coro_factory):
    """Drive ``coro_factory()`` while a heartbeat counts loop ticks.

    Returns the heartbeat count observed during the awaited coroutine.
    A pre-fix synchronous sleep inside the coroutine produces a count
    near zero; a post-fix off-loop sleep lets the heartbeat tick at
    its nominal cadence.
    """
    heartbeats = 0
    stop = asyncio.Event()

    async def _heartbeat() -> None:
        nonlocal heartbeats
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_S)
            except asyncio.TimeoutError:
                heartbeats += 1

    heartbeat_task = asyncio.create_task(_heartbeat())
    try:
        # Yield once so the heartbeat task has a chance to start before
        # the upload kicks off.
        await asyncio.sleep(0)
        await coro_factory()
    finally:
        stop.set()
        await heartbeat_task

    return heartbeats


def _patch_async_client(mock_client_cls: MagicMock) -> MagicMock:
    """Wire a ``patch('httpx.AsyncClient')`` mock to consume the body generator.

    The fix lives inside the ``file_stream`` generator, but the
    generator is only consumed when the mocked POST iterates the
    ``content=...`` kwarg. The default ``AsyncMock`` mock just records
    the call and never touches ``content``, which means the generator
    is never driven and our slow ``read`` is never hit. We replace the
    POST with a side-effect that fully consumes the generator before
    returning a 200 response — that's what httpx does in production
    and it's what we need to surface the regression signal.
    """
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    async def fake_post(*args: object, **kwargs: object) -> MagicMock:
        content = kwargs.get("content")
        if content is not None and hasattr(content, "__aiter__"):
            async for _chunk in content:
                # Drain the generator so every chunk read is exercised.
                pass
        response = MagicMock()
        response.raise_for_status = MagicMock()
        return response

    mock_client.post.side_effect = fake_post
    mock_client_cls.return_value = mock_client
    return mock_client


@pytest.mark.asyncio
async def test_upload_file_streaming_fd_path_does_not_block_event_loop() -> None:
    """FD branch: ``file_obj.read(65536)`` must run via ``asyncio.to_thread``.

    Pre-fix: the synchronous ``time.sleep`` inside ``_SlowReadFile.read``
    executes on the event-loop thread and freezes the heartbeat for
    ``SLEEP_S`` seconds, so the count comes back at or near 0.

    Post-fix: the read is dispatched to the default thread executor
    via ``await asyncio.to_thread(file_obj.read, 65536)``; the
    heartbeat keeps ticking and we observe ``>= MIN_HEARTBEATS``
    samples.
    """
    sources_api, _core = _make_sources_api()
    file_obj = _SlowReadFile(b"x" * 4096)

    with patch("httpx.AsyncClient") as mock_client_cls:
        _patch_async_client(mock_client_cls)

        async def _upload() -> None:
            await sources_api._upload_file_streaming(
                "https://notebooklm.google.com/upload/_/?upload_id=session",
                file_obj,
                filename="slow.bin",
            )

        heartbeats = await _run_with_heartbeat(_upload)

    assert heartbeats >= MIN_HEARTBEATS, (
        f"Event loop was blocked during _upload_file_streaming FD-branch read: "
        f"only {heartbeats} heartbeats fired during a {SLEEP_S}s synchronous read "
        f"(expected >= {MIN_HEARTBEATS}). The synchronous f.read(65536) is "
        f"still running on the event-loop thread — wrap it with "
        f"asyncio.to_thread."
    )


@pytest.mark.asyncio
async def test_upload_file_streaming_path_fallback_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path branch: ``f.read(65536)`` inside ``with open(...)`` must offload too.

    The legacy ``Path`` overload (kept for the direct-call unit tests
    in ``tests/unit/test_sources_upload.py``) opens the file inside
    the helper. We monkey-patch ``builtins.open`` so that opening our
    specific test file returns a ``_SlowReadFile`` — every other
    ``open()`` call passes through untouched.
    """
    sources_api, _core = _make_sources_api()
    test_file = tmp_path / "slow.bin"
    test_file.write_bytes(b"x" * 4096)

    real_open = builtins.open

    def _patched_open(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        # Match by resolved path so partial-path / cwd-relative
        # spellings inside other code (logging, etc.) don't get
        # accidentally diverted to the slow stub.
        try:
            same = Path(file).resolve() == test_file.resolve()
        except (TypeError, ValueError):
            same = False
        if same and "b" in mode:
            return _SlowReadFile(test_file.read_bytes())
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _patched_open)

    with patch("httpx.AsyncClient") as mock_client_cls:
        _patch_async_client(mock_client_cls)

        async def _upload() -> None:
            await sources_api._upload_file_streaming(
                "https://notebooklm.google.com/upload/_/?upload_id=session",
                test_file,
            )

        heartbeats = await _run_with_heartbeat(_upload)

    assert heartbeats >= MIN_HEARTBEATS, (
        f"Event loop was blocked during _upload_file_streaming Path-branch read: "
        f"only {heartbeats} heartbeats fired during a {SLEEP_S}s synchronous read "
        f"(expected >= {MIN_HEARTBEATS}). The synchronous f.read(65536) is "
        f"still running on the event-loop thread — wrap it with "
        f"asyncio.to_thread."
    )


@pytest.mark.asyncio
async def test_add_file_open_runs_off_loop_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``add_file``'s ``open()`` + ``fstat()`` must run off the event loop.

    Pre-fix the ``open()`` and ``os.fstat`` calls inside ``add_file``
    execute as synchronous syscalls directly on the loop thread. On a
    slow network mount that stalls every other concurrent coroutine for
    the full ``open`` latency. The fix wraps the pair in
    ``await asyncio.to_thread(...)``.

    Methodology mirrors ``test_download_blocks_loop.py``: we monkey-patch
    ``builtins.open`` so it captures ``threading.get_ident()`` on the
    very first call for our test file. If the wrap is in place the
    captured id differs from the loop thread id; a regression that
    removes the wrap leaves them equal.
    """
    sources_api, _core = _make_sources_api()
    test_file = tmp_path / "to_open.bin"
    test_file.write_bytes(b"payload")

    real_open = builtins.open
    loop_thread_id = threading.get_ident()
    captured: list[int] = []

    def _recording_open(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        # Only record when the open is for our test file in binary mode
        # — log handlers, .pyc caches, etc. can also call builtins.open
        # and would otherwise pollute the capture list.
        try:
            same = Path(file).resolve() == test_file.resolve()
        except (TypeError, ValueError):
            same = False
        if same and "b" in mode:
            captured.append(threading.get_ident())
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _recording_open)

    # Mock the registration RPC so we never hit the wire. Two RPC calls
    # land before ``add_file`` returns: GET_NOTEBOOK (baseline list) and
    # ADD_SOURCE_FILE (register). The "[[[['src_t1']]]]" shape feeds the
    # standard SOURCE_ID walker in ``_extract_register_file_source_id``.
    _core.rpc_executor.rpc_call.return_value = [[[["src_t1"]]]]

    mock_start_response = MagicMock()
    mock_start_response.headers = {
        "x-goog-upload-url": "https://notebooklm.google.com/upload/_/?upload_id=session"
    }
    mock_upload_response = MagicMock()
    mock_upload_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.post.side_effect = [mock_start_response, mock_upload_response]
        mock_client_cls.return_value = mock_client

        await sources_api.add_file("nb_t1", str(test_file))

    # The first `open` on our test file is the production ``open()``
    # inside the upload-semaphore block. Subsequent `open`s (e.g. the
    # finalize path) are not under test here.
    assert captured, (
        "builtins.open was never called for the test file — the patch target "
        "or the production code path may have changed."
    )
    assert captured[0] != loop_thread_id, (
        f"add_file's open() ran on the event-loop thread (thread id {captured[0]}). "
        "It must be wrapped in asyncio.to_thread so a slow filesystem cannot stall "
        "concurrent tasks."
    )
    # Form-1 bite-check (ADR-0007): prove the constructor-injected RPC
    # collaborator from ``make_fake_core`` is the one ``add_file`` drove,
    # so the migration off the monkeypatch allowlist is wired, not a no-op.
    _core.rpc_executor.rpc_call.assert_awaited()
