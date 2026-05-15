"""Regression tests for T7.D3 — TOCTOU close + FD-exhaustion guard in add_file.

Audit §23: pre-fix, ``SourcesAPI.add_file`` opened the source file twice — once
implicitly for validation via ``Path.stat()`` / ``Path.is_file()`` at the top
of the method, and again later inside ``_upload_file_streaming`` via
``open(file_path, "rb")``. Between those two moments, the path could be
swapped (intentionally by a hostile/racy caller, or unintentionally by a
periodic rotator) and the upload would silently stream the *replacement*
file's bytes instead of the validated one.

A second, independent concern: nothing bounded the number of concurrent
``add_file`` calls. A caller fanning out 100 ``add_file`` invocations could
hold 100 file descriptors simultaneously, exhausting the per-process FD
limit (default 1024 on macOS, configurable but rarely > 65535).

Post-fix (T7.D3):

  1. ``add_file`` opens the file ONCE under a ``try``/``with`` block, derives
     the size via ``os.fstat(fd.fileno()).st_size`` (operating on the FD
     itself rather than the path), and passes the live ``file_obj`` through
     to ``_upload_file_streaming``. The path is not re-opened. A test that
     swaps the path between validation and upload-open must observe either:
        - the bytes of the *validated* file in the captured upload body, OR
        - a clear error (FileNotFoundError, ValidationError, OSError) raised
          before the upload completes.
     What it must NOT observe is the bytes of the *replacement* file
     appearing in a successful upload.

  2. ``NotebookLMClient`` accepts ``max_concurrent_uploads: int | None`` (
     default 4) and ``add_file``'s upload section runs under an
     ``asyncio.Semaphore(max_concurrent_uploads)``. The semaphore is
     per-instance and intentionally separate from the RPC-pool sizing
     (``max_concurrent_rpcs`` / ``max_connections``) because uploads use
     their own ``httpx.AsyncClient`` and don't share the RPC pool.

This module asserts both halves:

  * ``test_add_file_holds_validated_fd_across_swap`` — swap the path between
    validation and upload-open; assert the upload streams the *validated*
    content (not the replacement). The check is performed by intercepting
    the captured upload body in the mock transport.

  * ``test_add_file_bounds_concurrent_open_fds`` — fan 100 ``add_file``
    calls out under ``max_concurrent_uploads=4`` and assert the peak number
    of FDs concurrently held by ``add_file`` never exceeds 4. The peak is
    measured by wrapping ``Path.open`` so we count only the opens the fix
    is responsible for, and ignore unrelated FDs (loop pipe, log file,
    etc.) that would otherwise make the assertion flaky across platforms.
"""

from __future__ import annotations

import asyncio
import builtins
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from notebooklm import NotebookLMClient

# T8.D11 — mock-transport concurrency tests; no HTTP, no cassette. Opt out
# of the tier-enforcement hook in tests/integration/conftest.py.
pytestmark = pytest.mark.allow_no_vcr

# ---------------------------------------------------------------------------
# Test 1 — TOCTOU: validated FD must survive a path-swap between validation
# and upload-open.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "POSIX rename-over-open-FD semantics. The test swaps the path via "
        "os.replace while a validated FD is held; on Windows that raises "
        "PermissionError because Python opens files without FILE_SHARE_DELETE, "
        "so the file with an open handle cannot be the rename target. The "
        "TOCTOU attack vector this guards against is also POSIX-specific — "
        "Windows itself blocks the rename. POSIX matrix entries cover the "
        "regression."
    ),
)
async def test_add_file_holds_validated_fd_across_swap(
    auth_tokens,
    tmp_path: Path,
) -> None:
    """If the path is swapped between validation and upload-open, the upload
    streams the *validated* bytes — not the replacement.

    Strategy: stub out ``_register_file_source`` and ``_start_resumable_upload``
    so we can probe the upload section deterministically. Patch
    ``_upload_file_streaming`` to record what it actually streams. While the
    coroutine is suspended between those steps (the stubs yield via
    ``asyncio.sleep(0)``), a sibling task overwrites the path with the
    *replacement* content. If the fix holds the validated FD across the
    suspend points, the captured stream contains the original bytes.
    """
    file_path = tmp_path / "doc.pdf"
    validated_content = b"VALIDATED-CONTENT" * 64
    replacement_content = b"REPLACEMENT-ATTACK" * 64
    file_path.write_bytes(validated_content)

    captured_streams: list[bytes] = []

    register_arrived = asyncio.Event()
    upload_started = asyncio.Event()
    swap_done = asyncio.Event()

    async def stub_register(notebook_id: str, filename: str) -> str:
        register_arrived.set()
        # Wait until the test has swapped the file out from under us.
        await swap_done.wait()
        return "src_id_001"

    async def stub_start_upload(*args: object, **kwargs: object) -> str:
        return "https://upload.example.test/sess/abc"

    async def stub_streaming(
        upload_url: str, file_obj: Any, *args: object, **kwargs: object
    ) -> None:
        upload_started.set()
        # Drain the FD into ``captured_streams`` so the test can assert what
        # the upload pipeline actually saw.
        chunks: list[bytes] = []
        # The fix may pass either an open file-like or (legacy fallback)
        # a Path. Discriminate so the test continues to fail loudly on the
        # path-based pre-fix shape.
        if hasattr(file_obj, "read"):
            while True:
                chunk = file_obj.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        else:
            # Pre-fix shape: the caller is passing a Path; opening here is
            # the second open() that the fix is supposed to eliminate. The
            # test should still capture the bytes-as-streamed so the
            # assertion below catches the TOCTOU.
            with open(file_obj, "rb") as f:  # type: ignore[arg-type]
                chunks.append(f.read())
        captured_streams.append(b"".join(chunks))

    async with NotebookLMClient(auth_tokens) as client:
        with (
            patch.object(
                client.sources,
                "_register_file_source",
                side_effect=stub_register,
            ),
            patch.object(
                client.sources,
                "_start_resumable_upload",
                side_effect=stub_start_upload,
            ),
            patch.object(
                client.sources,
                "_upload_file_streaming",
                side_effect=stub_streaming,
            ),
        ):
            add_file_task = asyncio.create_task(client.sources.add_file("nb_123", file_path))

            async def swap_file() -> None:
                await register_arrived.wait()
                # Atomically substitute the path with a new inode. A
                # ``write_bytes`` would truncate-and-rewrite the same
                # inode, which both the pre-fix path-based re-open and
                # the post-fix held-FD see identically on POSIX (the FD
                # tracks the inode and the inode's bytes just changed).
                # ``os.replace`` performs an atomic rename, swapping the
                # path target to a *different inode* — the held FD keeps
                # pointing at the original inode (the validated bytes),
                # while a fresh ``open(path)`` would land on the new
                # inode (the replacement bytes). That's the discriminator
                # the fix has to satisfy.
                replacement = file_path.with_name(file_path.name + ".repl")
                replacement.write_bytes(replacement_content)
                os.replace(replacement, file_path)
                swap_done.set()

            swapper = asyncio.create_task(swap_file())
            try:
                await asyncio.wait_for(add_file_task, timeout=5.0)
            finally:
                await swapper

    # The fix must have streamed the validated bytes (the FD was held
    # across the swap). A pre-fix run would stream the replacement bytes.
    assert captured_streams, "upload streaming was not invoked"
    streamed = captured_streams[0]
    assert streamed == validated_content, (
        "TOCTOU regression: upload streamed the replacement bytes "
        f"({streamed[:32]!r}...) instead of the validated bytes "
        f"({validated_content[:32]!r}...). The fix must hold the FD opened "
        "during validation across the upload-open boundary."
    )


# ---------------------------------------------------------------------------
# Test 2 — FD-exhaustion guard: max_concurrent_uploads bounds the peak FD count.
# ---------------------------------------------------------------------------


async def test_add_file_bounds_concurrent_open_fds(
    auth_tokens,
    tmp_path: Path,
) -> None:
    """100 concurrent ``add_file`` calls with ``max_concurrent_uploads=4``
    → peak FD count never exceeds 4.

    We measure FDs by wrapping ``builtins.open`` and counting opens that
    target files under ``tmp_path``. Counting only test-fixture paths
    avoids platform-specific noise (loop pipes on Linux, log handles on
    macOS, the import-machinery's own opens on CI) that would otherwise
    make the absolute-FD-count assertion flaky.
    """
    n_concurrent = 100
    max_uploads = 4

    inflight = {"current": 0, "peak": 0}
    real_open = builtins.open

    def counting_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        fd = real_open(file, *args, **kwargs)
        # Track only opens of the test-fixture files. Path-comparison via
        # ``str(...).startswith(...)`` because ``file`` may be a Path or a
        # str; ``fspath()`` would canonicalize but we want a string compare
        # that doesn't go through the filesystem (no extra opens).
        try:
            file_str = str(file)
        except Exception:  # noqa: BLE001 — defensive
            file_str = ""
        if file_str.startswith(str(tmp_path)):
            inflight["current"] += 1
            if inflight["current"] > inflight["peak"]:
                inflight["peak"] = inflight["current"]
            original_close = fd.close

            def tracking_close() -> None:
                inflight["current"] -= 1
                return original_close()

            fd.close = tracking_close  # type: ignore[method-assign]
        return fd

    # Files for the fan-out. Each call gets its own path so a per-path
    # collision can't artificially throttle the test.
    file_paths = [tmp_path / f"doc_{i}.pdf" for i in range(n_concurrent)]
    for fp in file_paths:
        fp.write_bytes(b"x" * 4096)

    # Mock the upload pipeline so the test doesn't make network calls.
    # The mocked _upload_file_streaming MUST hold the FD open for a beat
    # so the peak-inflight assertion is meaningful — if the upload is
    # instantaneous, no FD overlap ever occurs and the assertion is
    # vacuous.

    async def stub_register(*args: object, **kwargs: object) -> str:
        await asyncio.sleep(0)
        return "src_id"

    async def stub_start(*args: object, **kwargs: object) -> str:
        await asyncio.sleep(0)
        return "https://upload.example.test/sess"

    async def stub_streaming(
        upload_url: str, file_obj: Any, *args: object, **kwargs: object
    ) -> None:
        # A small delay so concurrent calls stack at this await point,
        # holding their FDs open. The semaphore is what limits the peak.
        await asyncio.sleep(0.02)
        # Honor the FD-ownership contract: the real
        # ``_upload_file_streaming`` takes ownership of ``file_obj`` and
        # closes it via the shielded finalize task's done-callback. A
        # stub that doesn't close would leak the FD and break the
        # peak-FD assertion below (which counts opens minus closes).
        if hasattr(file_obj, "close"):
            file_obj.close()

    async with NotebookLMClient(auth_tokens, max_concurrent_uploads=max_uploads) as client:
        with (
            patch.object(
                client.sources,
                "_register_file_source",
                side_effect=stub_register,
            ),
            patch.object(
                client.sources,
                "_start_resumable_upload",
                side_effect=stub_start,
            ),
            patch.object(
                client.sources,
                "_upload_file_streaming",
                side_effect=stub_streaming,
            ),
            patch.object(builtins, "open", counting_open),
        ):
            await asyncio.gather(*(client.sources.add_file("nb_123", fp) for fp in file_paths))

    assert inflight["peak"] <= max_uploads, (
        f"FD-exhaustion guard failure: peak concurrent FDs={inflight['peak']} "
        f"with max_concurrent_uploads={max_uploads} over {n_concurrent} "
        "concurrent add_file calls. The semaphore must bound the FDs held "
        "for upload."
    )
    # Sanity: the test should also confirm at least *some* concurrency
    # occurred — a peak of 1 would mean the gather serialised for some
    # unrelated reason and the assertion above is vacuous.
    assert inflight["peak"] >= 2, (
        "test setup degenerate: no FD overlap observed; the assertion "
        f"that peak<=4 is vacuous. Inflight peak={inflight['peak']}."
    )


# ---------------------------------------------------------------------------
# Test 3 — max_concurrent_uploads normalization: None → default; <=0 rejected.
# ---------------------------------------------------------------------------


async def test_max_concurrent_uploads_rejects_non_positive(auth_tokens) -> None:
    """``max_concurrent_uploads`` must be positive when supplied.

    ``None`` is normalized to the default (4) per the wave-3 spec
    ("MUST NOT allow unbounded concurrent uploads"). Zero or negative
    values are caller bugs that should fail fast at construction.
    """
    # None is allowed; resolves to default.
    NotebookLMClient(auth_tokens, max_concurrent_uploads=None)
    # Positive int is allowed.
    NotebookLMClient(auth_tokens, max_concurrent_uploads=1)
    NotebookLMClient(auth_tokens, max_concurrent_uploads=100)
    # Zero and negative are rejected.
    with pytest.raises(ValueError, match="max_concurrent_uploads"):
        NotebookLMClient(auth_tokens, max_concurrent_uploads=0)
    with pytest.raises(ValueError, match="max_concurrent_uploads"):
        NotebookLMClient(auth_tokens, max_concurrent_uploads=-1)


# ---------------------------------------------------------------------------
# Test 4 — file-not-found at upload-open is surfaced as a clear error.
# ---------------------------------------------------------------------------


async def test_add_file_missing_path_raises_clear_error(
    auth_tokens,
    tmp_path: Path,
) -> None:
    """``add_file`` against a non-existent path must raise ``FileNotFoundError``
    before any RPC fires — the pre-check happens at the same scope as the
    open(), so there's no window where a "found at validation, missing at
    upload" inconsistency can hide.
    """
    missing = tmp_path / "does-not-exist.pdf"

    register_calls: list[tuple[Any, ...]] = []

    async def stub_register(*args: Any, **kwargs: Any) -> str:
        register_calls.append(args)
        return "should_not_get_here"

    async with NotebookLMClient(auth_tokens) as client:
        with patch.object(
            client.sources,
            "_register_file_source",
            side_effect=stub_register,
        ):
            with pytest.raises(FileNotFoundError):
                await client.sources.add_file("nb_123", missing)
    assert not register_calls, (
        "FileNotFoundError must be raised before any RPC fires; got "
        f"{len(register_calls)} register call(s)."
    )


# ---------------------------------------------------------------------------
# Test 5 — FD ownership transfers to ``_upload_file_streaming``: the FD is
# only closed when the streaming helper's shielded task finishes, NOT when
# ``add_file``'s scope exits. This guards the T7.C3 dangling-session
# invariant: a post-finalize cancel keeps the shielded POST running in the
# background, and that background task must still be able to read the FD.
# ---------------------------------------------------------------------------


async def test_add_file_transfers_fd_ownership_to_streaming_helper(
    auth_tokens,
    tmp_path: Path,
) -> None:
    """The FD opened by ``add_file`` is closed by ``_upload_file_streaming``,
    not by ``add_file`` itself.

    Why this matters (regression guard for the T7.C3 ↔ T7.D3 interaction):
    if ``add_file`` closed the FD on its own scope exit, a post-finalize
    cancel — which T7.C3 specifies "shields the in-flight POST so the
    server-side session reaches a known terminal state" — would leave a
    background task reading from a closed FD, breaking the dangling-session
    guarantee.

    Strategy: stub ``_upload_file_streaming`` to capture the FD reference
    WITHOUT closing it, and verify ``add_file`` returns with the FD still
    open. If ``add_file`` were closing the FD on its own (e.g. via
    ``with open(...) as file_obj``), the captured FD would be closed by
    the time ``add_file`` returns and this assertion would fail.
    """
    file_path = tmp_path / "doc.pdf"
    file_path.write_bytes(b"x" * 1024)

    captured_fd: list[Any] = []

    async def stub_register(*args: Any, **kwargs: Any) -> str:
        return "src_id"

    async def stub_start(*args: Any, **kwargs: Any) -> str:
        return "https://upload.example.test/sess"

    async def stub_streaming(upload_url: str, file_obj: Any, *args: Any, **kwargs: Any) -> None:
        # Capture but DO NOT close — simulate the production
        # ``_upload_file_streaming`` returning before its shielded
        # done-callback fires (the shielded task is still running in
        # the background and the FD must remain readable).
        captured_fd.append(file_obj)

    async with NotebookLMClient(auth_tokens) as client:
        with (
            patch.object(client.sources, "_register_file_source", side_effect=stub_register),
            patch.object(client.sources, "_start_resumable_upload", side_effect=stub_start),
            patch.object(client.sources, "_upload_file_streaming", side_effect=stub_streaming),
        ):
            await client.sources.add_file("nb_123", file_path)

    assert captured_fd, "_upload_file_streaming was not invoked"
    fd = captured_fd[0]
    # The FD must still be readable AFTER ``add_file`` returned. A
    # closed-by-add_file FD would raise ``ValueError: I/O operation
    # on closed file`` on ``.read()`` — that's the regression this
    # guards against.
    assert hasattr(fd, "closed"), "stub captured a non-file object"
    assert not fd.closed, (
        "T7.D3 ↔ T7.C3 regression: ``add_file`` closed the FD on its own "
        "scope exit. The shielded post-finalize background task would now "
        "be unable to read the FD, breaking the dangling-session guarantee. "
        "Ownership of the FD must transfer to ``_upload_file_streaming``, "
        "which closes it via the finalize task's done-callback."
    )
    # Cleanup: this test stub didn't honor the close contract, so close
    # manually to avoid leaking a real FD at test teardown.
    fd.close()


# ---------------------------------------------------------------------------
# Test 6 — FD is closed locally when an RPC raises BEFORE
# ``_upload_file_streaming`` runs. Counterpart to Test 5: the handoff is
# conditional on the streaming helper actually being invoked.
# ---------------------------------------------------------------------------


async def test_add_file_closes_fd_when_registration_fails(
    auth_tokens,
    tmp_path: Path,
) -> None:
    """If ``_register_file_source`` raises, the FD must close before
    ``add_file`` re-raises — the ownership handoff hasn't happened yet,
    so ``add_file`` is still on the hook for cleanup.
    """
    file_path = tmp_path / "doc.pdf"
    file_path.write_bytes(b"x" * 1024)

    captured_fd: list[Any] = []
    real_open = builtins.open

    def capturing_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        fd = real_open(file, *args, **kwargs)
        if isinstance(file, (str, os.PathLike)) and str(file).startswith(str(tmp_path)):
            captured_fd.append(fd)
        return fd

    class _RegistrationError(RuntimeError):
        pass

    async def failing_register(*args: Any, **kwargs: Any) -> str:
        raise _RegistrationError("registration boom")

    async with NotebookLMClient(auth_tokens) as client:
        with (
            patch.object(builtins, "open", capturing_open),
            patch.object(client.sources, "_register_file_source", side_effect=failing_register),
        ):
            with pytest.raises(_RegistrationError):
                await client.sources.add_file("nb_123", file_path)

    assert captured_fd, "add_file did not open the file"
    # No handoff happened (registration failed before
    # _upload_file_streaming was reached), so add_file must close the FD
    # itself before propagating the exception.
    assert captured_fd[0].closed, (
        "add_file leaked an open FD on a pre-handoff exception path. "
        "The ``finally`` branch with ``handed_off=False`` must close "
        "locally when ``_upload_file_streaming`` was never reached."
    )
