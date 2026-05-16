"""Regression test for the shield upload finalize + Scotty cancel cleanup.

Audit item §9: pre-fix, a `CancelledError` arriving mid-`_upload_file_streaming`
abandoned the in-flight `"upload, finalize"` POST without waiting for the
server's 200 (or its error). The server-side resumable upload session
either:
  - Completed silently (the server processed all bytes but the client
    never learned), leaving a dangling registered source with no
    title-update applied, OR
  - Remained half-finalized, the resumable URL still alive, holding
    quota until Scotty's GC timeout expired.

Post-fix:
  - The finalize POST is wrapped in ``asyncio.shield`` so a cancel
    mid-POST lets the request complete; the cancel then propagates after
    the response is received (or after the inner Task wins the race).
  - If the cancel arrives BEFORE the finalize POST is dispatched, the
    handler fires a best-effort Scotty cancel
    (``X-Goog-Upload-Command: cancel``) to the same resumable upload URL
    via ``asyncio.create_task`` so the re-raise is never blocked.

This module asserts both branches:

1. ``test_cancel_after_finalize_started_shield_completes_request``
   Holds the mock transport handler until the test releases it. Cancels
   AFTER the handler has started → the shielded inner Task keeps the
   POST in flight, the handler returns 200, and the captured request
   list shows a completed ``"upload, finalize"`` POST.

2. ``test_cancel_before_finalize_fires_scotty_cleanup``
   Blocks ``httpx.AsyncClient.__aenter__`` on an event that the test
   never releases for the finalize client, so the cancel hits BEFORE
   the finalize POST is dispatched. Asserts a separate POST with
   ``X-Goog-Upload-Command: cancel`` lands on the same upload URL.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from notebooklm import NotebookLMClient

from .helpers import with_simulated_cancel

# mock-transport cancellation tests; no HTTP, no cassette. Opt out
# of the tier-enforcement hook in tests/integration/conftest.py.
pytestmark = pytest.mark.allow_no_vcr

UPLOAD_URL = "https://example.test/scotty/upload-session-abc123"


# ---------------------------------------------------------------------------
# Test 1 — cancel AFTER finalize POST starts
# ---------------------------------------------------------------------------


async def test_cancel_after_finalize_started_shield_completes_request(
    auth_tokens,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel mid-finalize → shield keeps the POST alive; finalize completes.

    The mock transport sets ``finalize_arrived`` once the handler runs and
    then awaits ``release`` before returning 200. The test waits for
    ``finalize_arrived`` (so the POST is genuinely in flight), cancels the
    upload task, then releases the handler. Because the finalize POST is
    shielded, the inner Task is not cancelled — it observes the 200 and
    completes. The captured request list confirms the finalize POST landed.
    """
    file_path = tmp_path / "doc.pdf"
    file_path.write_bytes(b"x" * 128)

    finalize_arrived = asyncio.Event()
    release = asyncio.Event()
    captured: list[httpx.Request] = []
    handler_completed: list[bool] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        finalize_arrived.set()
        # Block here so the test can deterministically cancel the caller
        # while the POST is in flight. On the un-shielded baseline, httpx
        # propagates the outer cancel into this coroutine and we never
        # reach the response-construction line below — that absence is
        # what the shield assertion catches.
        try:
            await release.wait()
        except BaseException:
            # Don't swallow — let httpx see the cancel — but record that
            # the handler did NOT run to completion.
            raise
        handler_completed.append(True)
        return httpx.Response(200, content=b"")

    transport = httpx.MockTransport(handler)
    _patch_async_client_transport(monkeypatch, transport)

    async with NotebookLMClient(auth_tokens) as client:
        upload_task = asyncio.create_task(
            client.sources._upload_file_streaming(UPLOAD_URL, file_path),
        )

        # Wait until the mock transport has the request — i.e., the
        # finalize POST is definitely in flight.
        await asyncio.wait_for(finalize_arrived.wait(), timeout=2.0)

        # Cancel the outer task. With the shield in place, the inner POST
        # keeps running; without the shield, the request is abandoned.
        upload_task.cancel()

        # Release the handler so the inner request can finish.
        # Give the cancel a beat to propagate first, mirroring the real
        # Ctrl-C interleave.
        await asyncio.sleep(0)
        release.set()

        # The outer task will see CancelledError (shield re-raises after
        # the inner POST completes — or immediately, with the inner Task
        # still scheduled). Either way, the captured POST must include
        # the finalize command.
        with pytest.raises(asyncio.CancelledError):
            await upload_task

        # Drain pending callbacks so the shielded background task lands
        # — we need the handler to reach `handler_completed.append(True)`
        # AFTER its `release` wait returns.
        for _ in range(100):
            if handler_completed:
                break
            await asyncio.sleep(0.01)

    finalize_seen = [
        r for r in captured if r.headers.get("x-goog-upload-command") == "upload, finalize"
    ]
    assert finalize_seen, (
        "shield failure: finalize POST was abandoned on cancel "
        f"(captured commands: "
        f"{[r.headers.get('x-goog-upload-command') for r in captured]})"
    )
    # The strong shield assertion: without the shield, httpx cancels the
    # underlying transport coroutine when the outer task is cancelled,
    # and the handler is interrupted before `handler_completed.append`
    # runs. With the shield, the handler observes `release.set()` and
    # returns 200 — `handler_completed` is non-empty.
    assert handler_completed, (
        "shield failure: the in-flight finalize POST was interrupted by "
        "cancel — the mock handler never reached its response line. "
        "asyncio.shield should keep the POST alive through the cancel."
    )
    # The shield protects the in-flight finalize; the cancel POST is for the
    # pre-finalize branch only — confirm we did NOT also fire a redundant
    # cancel here, otherwise the patched code is shielding AND cleaning up.
    cancel_seen = [r for r in captured if r.headers.get("x-goog-upload-command") == "cancel"]
    assert not cancel_seen, (
        "shield+cleanup both fired; cancel cleanup must be gated on pre-finalize state"
    )


# ---------------------------------------------------------------------------
# Test 2 — cancel BEFORE finalize POST is dispatched
# ---------------------------------------------------------------------------


async def test_cancel_before_finalize_fires_scotty_cleanup(
    auth_tokens,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel before the finalize POST → best-effort cancel POST is fired.

    Strategy: wrap ``httpx.AsyncClient`` so the finalize client blocks
    inside ``__aenter__`` on an event the test never releases. A
    ``with_simulated_cancel`` cancels the upload task while it is still
    waiting to acquire the finalize client. The fix must observe this
    pre-finalize cancel and fire a separate ``X-Goog-Upload-Command: cancel``
    POST (via ``asyncio.create_task`` so the re-raise is not blocked) on
    a NEW client whose transport we observe.
    """
    file_path = tmp_path / "doc.pdf"
    file_path.write_bytes(b"x" * 128)

    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"")

    cleanup_transport = httpx.MockTransport(handler)
    aenter_call_count = {"n": 0}
    finalize_aenter_blocker = asyncio.Event()

    # Discriminate by call order: the FIRST AsyncClient constructed inside
    # _upload_file_streaming is the finalize client (we block its
    # __aenter__ so the cancel lands before the POST). Subsequent
    # constructions (e.g. the cleanup client created via
    # asyncio.create_task) pass through, and we observe their requests on
    # `cleanup_transport`.
    class _BlockingFinalizeClient(httpx.AsyncClient):
        async def __aenter__(self) -> httpx.AsyncClient:
            aenter_call_count["n"] += 1
            if aenter_call_count["n"] == 1:
                await finalize_aenter_blocker.wait()
            return await super().__aenter__()

    def _client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs.setdefault("transport", cleanup_transport)
        return _BlockingFinalizeClient(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("notebooklm._sources.httpx.AsyncClient", _client_factory)

    async with NotebookLMClient(auth_tokens) as client:
        # with_simulated_cancel cancels the inner task after `delay`
        # seconds. The finalize client blocks indefinitely on
        # finalize_aenter_blocker, so the cancel is guaranteed to land
        # BEFORE the POST is dispatched.
        result = await with_simulated_cancel(
            client.sources._upload_file_streaming(UPLOAD_URL, file_path),
            delay=0.05,
            label="upload-pre-finalize-cancel",
        )
        # The outer call must surface CancelledError (re-raise after
        # scheduling the cleanup task).
        assert isinstance(result, asyncio.CancelledError), (
            f"expected CancelledError, got {type(result).__name__}: {result!r}"
        )

        # Let the cleanup task (created via asyncio.create_task) run.
        for _ in range(50):
            if any(r.headers.get("x-goog-upload-command") == "cancel" for r in captured):
                break
            await asyncio.sleep(0.01)

        # Release the finalize __aenter__ so no stray task lives past the
        # test. (The shielded inner finalize task — if any — completes
        # immediately because we never sent the POST.)
        finalize_aenter_blocker.set()
        await asyncio.sleep(0)

    cancel_posts = [
        r
        for r in captured
        if r.method == "POST"
        and r.headers.get("x-goog-upload-command") == "cancel"
        and str(r.url) == UPLOAD_URL
    ]
    assert cancel_posts, (
        "pre-finalize cancel: expected a best-effort 'X-Goog-Upload-Command: "
        "cancel' POST to the resumable upload URL; captured: "
        f"{[(r.method, str(r.url), dict(r.headers)) for r in captured]}"
    )


# ---------------------------------------------------------------------------
# Patch helper
# ---------------------------------------------------------------------------


def _patch_async_client_transport(
    monkeypatch: pytest.MonkeyPatch,
    transport: httpx.MockTransport,
) -> None:
    """Force every ``httpx.AsyncClient`` constructed inside ``_sources`` to
    use the given ``MockTransport``.

    ``_upload_file_streaming`` instantiates its own ``httpx.AsyncClient``
    (it doesn't route through the shared core client), so we replace the
    constructor in the ``_sources`` module namespace with a wrapper that
    pre-binds the transport kwarg.
    """

    # Capture the unpatched AsyncClient before monkeypatch swaps the
    # attribute. `monkeypatch.setattr("notebooklm._sources.httpx.AsyncClient", ...)`
    # mutates the shared `httpx` module object, so calling `httpx.AsyncClient`
    # inside the factory would recurse into the factory itself.
    original_async_client = httpx.AsyncClient

    def _factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_async_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("notebooklm._sources.httpx.AsyncClient", _factory)
