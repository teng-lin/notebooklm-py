"""Cancellation, lifecycle, and worker-boundary machinery of the Android upload pipeline.

The pure helpers (MIME resolution, HTML refusal, Drive metadata gates, timeout
projection) live in ``test_upload_helpers.py``. This module covers the moving
parts instead: the ``_settle_context_exit`` cancellation primitive, resource
settling, epoch fencing, the bearer destination allowlist, and the secret-owning
HTTP workers' exception and cleanup tails.
"""

from __future__ import annotations

import asyncio
import builtins
import inspect
import io
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, cast

import httpx
import pytest

from notebooklm._android import upload as upload_module
from notebooklm._android.auth import BearerCredential, BearerProvider
from notebooklm._android.session import AndroidSession
from notebooklm._android.upload import (
    UPLOAD_ORIGIN,
    AndroidUploadPipeline,
    _one_header,
    _RetiredEpochError,
    _settle_context_exit,
    _UploadState,
    validate_upload_session_url,
)
from notebooklm._source.drive import DriveRef
from notebooklm.exceptions import (
    AuthError,
    RateLimitError,
    ServerError,
    SourceAddError,
    ValidationError,
)

NOTEBOOK_ID = "00000000-0000-4000-8000-000000000300"
SOURCE_ID = "00000000-0000-4000-8000-000000000301"
SESSION_URL = (
    f"https://notebooklm-pa.googleapis.com/upload/upload/{NOTEBOOK_ID}"
    "?upload_id=session-capability&upload_protocol=resumable"
)
START_URL = f"{UPLOAD_ORIGIN}/upload/upload/{NOTEBOOK_ID}"
DRIVE_REF = DriveRef(file_id="A" * 28)


# --------------------------------------------------------------------------- #
# ``_settle_context_exit`` -- the cancellation-safety primitive.
# --------------------------------------------------------------------------- #


class _RecordingExit:
    """A minimal async context manager that only implements ``__aexit__``.

    ``_settle_context_exit`` never enters the manager -- it is handed one that is
    already entered -- so ``__aenter__`` is deliberately absent.
    """

    def __init__(self, *, outcome: str = "clean") -> None:
        self.outcome = outcome
        self.exits = 0
        self.exit_args: tuple[Any, ...] | None = None
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        self.exits += 1
        self.exit_args = (exc_type, exc, traceback)
        if self.outcome == "raise":
            raise ValueError("exit boom")
        if self.outcome == "cancel":
            raise asyncio.CancelledError()
        return None


async def _suspend_at_shield(manager: _RecordingExit) -> Any:
    """Start one ``_settle_context_exit`` and stop it on its ``shield`` await.

    Driving the coroutine by hand is the only way to place an interruption
    *deterministically* between "the exit task finished" and "our await was
    resumed" -- the exact race the primitive exists to survive.
    """

    coro = _settle_context_exit(manager, None, None, None)
    coro.send(None)
    return coro


@pytest.mark.asyncio
async def test_a_failing_context_exit_propagates_its_own_error() -> None:
    """A settle that fails must surface the exit's error, not swallow it."""

    manager = _RecordingExit(outcome="raise")

    with pytest.raises(ValueError, match="exit boom"):
        await _settle_context_exit(manager, ValueError, ValueError("upstream"), None)

    assert manager.exits == 1
    assert manager.exit_args is not None
    assert manager.exit_args[0] is ValueError


@pytest.mark.asyncio
async def test_repeated_cancellation_still_runs_the_exit_exactly_once() -> None:
    """Two cancellations while the exit is in flight must not abandon it.

    This is the whole point of the shield loop: a caller that spams ``cancel()``
    must not leave a half-closed HTTP stream behind. The first cancellation is
    the one re-raised.
    """

    manager = _RecordingExit()
    manager.release = asyncio.Event()
    task = asyncio.ensure_future(_settle_context_exit(manager, None, None, None))
    await manager.entered.wait()

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert manager.exits == 0

    manager.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert manager.exits == 1


@pytest.mark.asyncio
async def test_cancellation_delivered_after_a_clean_exit_is_still_reported() -> None:
    """Losing the race must not turn a cancelled settle into a silent success."""

    manager = _RecordingExit()
    coro = await _suspend_at_shield(manager)
    await asyncio.sleep(0)
    assert manager.exits == 1

    with pytest.raises(asyncio.CancelledError):
        coro.throw(asyncio.CancelledError())


@pytest.mark.asyncio
async def test_a_settled_exit_error_outranks_a_late_cancellation() -> None:
    """The exit's own failure is the more informative outcome, so it wins."""

    manager = _RecordingExit(outcome="raise")
    coro = await _suspend_at_shield(manager)
    await asyncio.sleep(0)

    with pytest.raises(ValueError, match="exit boom"):
        coro.throw(asyncio.CancelledError())


@pytest.mark.asyncio
async def test_an_exit_that_cancelled_itself_reports_that_cancellation() -> None:
    """``exit_task.result()`` raising ``CancelledError`` is a settled outcome.

    It must be re-raised from the settled result rather than mistaken for the
    caller's own cancellation.
    """

    manager = _RecordingExit(outcome="cancel")
    coro = await _suspend_at_shield(manager)
    await asyncio.sleep(0)

    with pytest.raises(asyncio.CancelledError):
        coro.throw(asyncio.CancelledError())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interrupt",
    [KeyboardInterrupt, SystemExit],
    ids=["keyboard-interrupt", "system-exit"],
)
async def test_an_interpreter_interrupt_outranks_a_recorded_cancellation(
    interrupt: type[BaseException],
) -> None:
    """Ctrl-C after a cancellation must keep tearing the interpreter down.

    The loop has already recorded a ``CancelledError`` to re-raise. Replaying
    that instead of the interrupt would let a stream teardown swallow Ctrl-C.
    """

    manager = _RecordingExit()
    manager.release = asyncio.Event()
    coro = await _suspend_at_shield(manager)
    await manager.entered.wait()

    # First a cancellation, which is recorded and re-shielded rather than raised.
    coro.throw(asyncio.CancelledError())

    with pytest.raises(interrupt):
        coro.throw(interrupt())

    # The exit task was already scheduled; let it finish so no task is orphaned.
    manager.release.set()
    await asyncio.sleep(0)
    assert manager.exits == 1


# --------------------------------------------------------------------------- #
# URL and header validation.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw_url",
    [
        pytest.param(
            f"https://notebooklm-pa.googleapis.com:notaport/upload/upload/{NOTEBOOK_ID}"
            "?upload_id=x&upload_protocol=resumable",
            id="unparseable-port",
        ),
        pytest.param(
            f"https://notebooklm-pa.googleapis.com/upload/upload/{NOTEBOOK_ID}"
            "?upload_id=x&other=resumable",
            id="wrong-query-key",
        ),
        pytest.param(
            f"https://notebooklm-pa.googleapis.com/upload/upload/{NOTEBOOK_ID}"
            "?upload_id=x&upload_id=y",
            id="duplicated-query-key",
        ),
    ],
)
def test_a_malformed_session_capability_is_refused(raw_url: str) -> None:
    """Every rejection collapses to one message so no capability text leaks."""

    with pytest.raises(ValidationError) as excinfo:
        validate_upload_session_url(raw_url, NOTEBOOK_ID)
    assert str(excinfo.value) == "Invalid Android upload session response"
    assert "upload_id" not in str(excinfo.value)


def test_a_plain_mapping_of_headers_is_read_without_get_list() -> None:
    """Not every client's response headers are an ``httpx.Headers`` multidict."""

    assert _one_header({"x-goog-upload-status": "active"}, "x-goog-upload-status") == "active"
    assert _one_header({}, "x-goog-upload-status") is None


# --------------------------------------------------------------------------- #
# Pipeline fixtures.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Lease:
    epoch: int


class FakeSession:
    def __init__(self) -> None:
        self.epoch = 7
        self.scopes: list[str] = []
        self.epoch_error: BaseException | None = None

    @asynccontextmanager
    async def operation_scope(self, label: str, **kwargs: Any) -> AsyncIterator[_Lease]:
        assert not kwargs
        self.scopes.append(label)
        yield _Lease(self.epoch)

    def assert_epoch(self, expected_epoch: int) -> None:
        if self.epoch_error is not None:
            raise self.epoch_error
        if expected_epoch != self.epoch:
            raise RuntimeError("retired fake epoch")


class FakeBearerProvider:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.invalidated: list[int] = []
        self.error: BaseException | None = None

    async def get(self, expected_epoch: int) -> BearerCredential:
        self.calls.append(expected_epoch)
        if self.error is not None:
            raise self.error
        generation = len(self.calls)
        return BearerCredential(token=f"bearer-secret-{generation}", generation=generation)

    def invalidate(self, generation: int) -> None:
        self.invalidated.append(generation)


async def _pipeline(
    *,
    factory: Any = None,
    upload_timeout: float = 5.0,
    record_upload_queue_wait: Any = None,
    max_concurrent_uploads: int = 1,
) -> tuple[FakeSession, FakeBearerProvider, AndroidUploadPipeline]:
    """Build one opened pipeline over fake collaborators.

    ``max_concurrent_uploads`` defaults to 1 so ``Semaphore.locked()`` is a
    faithful "the slot was leaked" probe after a single acquire.
    """

    session = FakeSession()
    bearer = FakeBearerProvider()
    pipeline = AndroidUploadPipeline(
        session=cast(AndroidSession, session),
        bearer_provider=cast(BearerProvider, bearer),
        upload_timeout=upload_timeout,
        max_concurrent_uploads=max_concurrent_uploads,
        async_client_factory=factory,
        record_upload_queue_wait=record_upload_queue_wait,
    )
    loop = asyncio.get_running_loop()
    pipeline.set_bound_loop(loop)
    pipeline.reset_after_open()
    await pipeline.open(loop, session.epoch)
    return session, bearer, pipeline


# --------------------------------------------------------------------------- #
# Lifecycle: binding, epoch fencing, and resource settling.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_opening_on_a_loop_the_lifecycle_did_not_bind_is_refused() -> None:
    """``open`` must never adopt a loop behind ``ClientLifecycle``'s back."""

    _session, _bearer, pipeline = await _pipeline()
    pipeline.set_bound_loop(None)

    with pytest.raises(RuntimeError, match="not bound by the client lifecycle"):
        await pipeline.open(asyncio.get_running_loop(), 1)


@pytest.mark.asyncio
async def test_preparing_to_close_an_unbound_pipeline_still_retires_the_epoch() -> None:
    """A pipeline closed before it was ever opened must not raise."""

    session = FakeSession()
    pipeline = AndroidUploadPipeline(
        session=cast(AndroidSession, session),
        bearer_provider=cast(BearerProvider, FakeBearerProvider()),
    )

    await pipeline.prepare_close()

    assert pipeline._bound_loop is None
    assert pipeline._active_epoch is None


@pytest.mark.asyncio
async def test_settling_resources_tolerates_every_uncooperative_holder() -> None:
    """Close must be best-effort: one bad client or file cannot strand the rest.

    Settling runs on the teardown path, so a client without ``aclose``, a client
    whose ``aclose`` explodes, and a file whose ``close`` explodes must all be
    stepped over while the well-behaved ones are still closed.
    """

    _session, _bearer, pipeline = await _pipeline()

    class _NotCloseable:
        """A tracked client that never grew an ``aclose`` (e.g. a bare session)."""

    class _ExplodingClient:
        def __init__(self) -> None:
            self.calls = 0

        async def aclose(self) -> None:
            self.calls += 1
            raise RuntimeError("aclose boom")

    class _GoodClient:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    class _ExplodingFile:
        def __init__(self) -> None:
            self.calls = 0

        def close(self) -> None:
            self.calls += 1
            raise OSError("close boom")

    exploding_client = _ExplodingClient()
    good_client = _GoodClient()
    exploding_file = _ExplodingFile()
    good_file = io.BytesIO(b"payload")
    pipeline._transport_clients.update({_NotCloseable(), exploding_client, good_client})
    pipeline._open_files.update({cast(IO[bytes], exploding_file), good_file})

    await pipeline.close_resources()

    assert exploding_client.calls == 1
    assert good_client.closed is True
    assert exploding_file.calls == 1
    assert good_file.closed is True
    assert pipeline._transport_clients == set()
    assert pipeline._open_files == set()


@pytest.mark.asyncio
async def test_a_retired_generation_is_fenced_before_any_credentialed_work() -> None:
    """Both fences -- our own epoch and the transport's -- raise the same signal."""

    session, _bearer, pipeline = await _pipeline()

    with pytest.raises(_RetiredEpochError, match=r"expected=999, active=7"):
        pipeline._assert_epoch(999)

    session.epoch_error = RuntimeError("transport retired")
    with pytest.raises(_RetiredEpochError) as excinfo:
        pipeline._assert_epoch(session.epoch)
    assert excinfo.value.__cause__ is None
    assert "transport retired" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_the_closing_flag_alone_fences_work_that_still_holds_a_live_epoch() -> None:
    """``prepare_close`` raises the flag *before* it clears the epoch.

    A task that samples the pipeline in between must already be refused;
    fencing on the epoch alone would let it start one more credentialed request.
    """

    session, _bearer, pipeline = await _pipeline()
    epoch = session.epoch
    pipeline._closing = True

    assert pipeline._active_epoch == epoch
    with pytest.raises(_RetiredEpochError):
        pipeline._assert_epoch(epoch)

    await pipeline.prepare_close()
    with pytest.raises(_RetiredEpochError):
        pipeline._assert_epoch(epoch)


@pytest.mark.asyncio
async def test_without_an_injected_factory_the_shared_transport_resolver_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upload must share the one TLS fingerprint chosen by the transport opt-in."""

    monkeypatch.delenv("NOTEBOOKLM_TRANSPORT", raising=False)
    _session, _bearer, pipeline = await _pipeline(factory=None)

    assert pipeline._client_factory() is httpx.AsyncClient


@pytest.mark.asyncio
async def test_upload_and_download_admission_are_separate_cached_semaphores() -> None:
    """Sharing one semaphore would let a staged download deadlock an upload."""

    _session, _bearer, pipeline = await _pipeline()

    upload_slot = pipeline._upload_slot()
    download_slot = pipeline._download_slot()

    assert pipeline._upload_slot() is upload_slot
    assert pipeline._download_slot() is download_slot
    assert upload_slot is not download_slot


# --------------------------------------------------------------------------- #
# Drive read path.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "expected", "match"),
    [
        pytest.param(401, AuthError, "reauthenticate the selected profile", id="401"),
        pytest.param(429, RateLimitError, "throttled the download", id="429"),
        pytest.param(503, ServerError, "retry later", id="503"),
        pytest.param(404, ValidationError, "confirm the file id", id="404"),
    ],
)
def test_each_drive_status_class_maps_to_its_own_public_error(
    status: int,
    expected: type[Exception],
    match: str,
) -> None:
    """Callers branch on the exception type, so the mapping must be exact.

    ``ServerError`` also has to carry the numeric status because the retry
    middleware reads it.
    """

    with pytest.raises(expected, match=match) as excinfo:
        AndroidUploadPipeline._map_drive_status(status, DRIVE_REF)
    if expected is ServerError:
        assert cast(ServerError, excinfo.value).status_code == status


def test_a_successful_drive_status_maps_to_nothing() -> None:
    """200 and 204 must fall through so the caller keeps reading the body."""

    assert AndroidUploadPipeline._map_drive_status(200, DRIVE_REF) is None
    assert AndroidUploadPipeline._map_drive_status(204, DRIVE_REF) is None


class _StubResponse:
    """A response double that is deliberately neither closeable nor abortable.

    Both ``aclose`` and ``abort`` are optional in the production code, so the
    base double omits them; the subclasses below opt back in.
    """

    def __init__(
        self,
        status_code: int = 200,
        *,
        headers: dict[str, str] | None = None,
        payload: Any = None,
        json_error: BaseException | None = None,
        chunks: tuple[bytes, ...] = (),
    ) -> None:
        self.status_code = status_code
        self.headers = {} if headers is None else headers
        self._payload = payload
        self._json_error = json_error
        self._chunks = chunks
        self.acloses = 0
        self.aborts = 0
        #: Chunks actually pulled from the body — lets a test assert that a
        #: header-only refusal never touched the stream.
        self.reads = 0

    def json(self) -> Any:
        if self._json_error is not None:
            raise self._json_error
        return self._payload

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            self.reads += 1
            yield chunk


class _ClosableStubResponse(_StubResponse):
    """A response the worker is expected to close, optionally unsuccessfully."""

    def __init__(
        self, *args: Any, aclose_error: BaseException | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self._aclose_error = aclose_error

    async def aclose(self) -> None:
        self.acloses += 1
        if self._aclose_error is not None:
            raise self._aclose_error


class _AbortableStubResponse(_StubResponse):
    """A streaming response that can tear its connection down mid-body."""

    def abort(self) -> None:
        self.aborts += 1


class _StubStream:
    def __init__(self, response: _StubResponse) -> None:
        self.response = response
        self.exit_args: tuple[Any, ...] | None = None
        self.exits = 0

    async def __aenter__(self) -> _StubResponse:
        return self.response

    async def __aexit__(self, *exc: Any) -> None:
        self.exits += 1
        self.exit_args = exc


class _StubDriveClient:
    def __init__(self, *, response: Any = None, stream: _StubStream | None = None) -> None:
        self._response = response
        self._stream = stream
        self.get_calls: list[tuple[str, dict[str, str]]] = []

    async def get(self, url: str, *, headers: Any, follow_redirects: bool) -> Any:
        self.get_calls.append((url, dict(headers)))
        return self._response

    def stream(self, method: str, url: str, *, headers: Any, follow_redirects: bool) -> _StubStream:
        assert self._stream is not None
        return self._stream


def _deadline(timeout: float = 5.0) -> Any:
    """One generous aggregate budget; the timeout paths are exercised separately."""

    return upload_module.RuntimeDeadline.start(timeout)


def _credential(generation: int = 1) -> BearerCredential:
    return BearerCredential(token=f"bearer-secret-{generation}", generation=generation)


@pytest.mark.asyncio
async def test_unparseable_drive_metadata_never_reaches_the_public_message() -> None:
    """A body that is not JSON must become one fixed, file-id-only diagnostic."""

    _session, _bearer, pipeline = await _pipeline()
    response = _ClosableStubResponse(200, json_error=ValueError("Expecting value: line 1 column 1"))
    client = _StubDriveClient(response=response)

    with pytest.raises(ValidationError) as excinfo:
        await pipeline._drive_metadata(
            client,
            DRIVE_REF,
            _credential(),
            _deadline(),
        )

    assert str(excinfo.value) == f"Drive returned malformed metadata for {DRIVE_REF.file_id}."
    assert "Expecting value" not in str(excinfo.value)
    assert response.acloses == 1


@pytest.mark.asyncio
async def test_a_rejected_bearer_on_the_media_leg_is_invalidated_before_raising(
    tmp_path: Path,
) -> None:
    """The metadata leg succeeded, so only the stream can retire this credential."""

    _session, bearer, pipeline = await _pipeline()
    stream = _StubStream(_StubResponse(401))
    client = _StubDriveClient(stream=stream)

    with pytest.raises(AuthError):
        await pipeline._stream_drive_media(
            client,
            DRIVE_REF,
            "document.pdf",
            _credential(generation=3),
            tmp_path / "document.pdf",
            _deadline(),
        )

    assert bearer.invalidated == [3]
    assert stream.exits == 1
    assert pipeline._open_files == set()


@pytest.mark.asyncio
async def test_an_unparseable_content_length_does_not_pre_reject_the_download(
    tmp_path: Path,
) -> None:
    """A junk ``Content-Length`` must fall back to the streamed byte count.

    Treating it as a cap violation would refuse downloads that are actually fine.
    """

    _session, _bearer, pipeline = await _pipeline()
    stream = _StubStream(
        _StubResponse(200, headers={"content-length": "not-a-number"}, chunks=(b"hello",))
    )
    client = _StubDriveClient(stream=stream)
    destination = tmp_path / "document.pdf"

    await pipeline._stream_drive_media(
        client,
        DRIVE_REF,
        "document.pdf",
        _credential(),
        destination,
        _deadline(),
    )

    assert destination.read_bytes() == b"hello"
    assert pipeline._open_files == set()


@pytest.mark.asyncio
async def test_a_declared_oversize_body_is_refused_before_a_single_byte_is_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap must be enforced from the header, and the stream exit still runs.

    The stub response has no ``abort``, which is the ordinary httpx shape, so
    this also pins that the abort call is optional rather than assumed.
    """

    # The cap is shrunk rather than fed 200 MiB of bytes; the public message
    # spells the real limit out as a literal, so it still reads "200 MiB".
    monkeypatch.setattr(upload_module, "_MAX_DRIVE_DOWNLOAD_BYTES", 8)
    _session, _bearer, pipeline = await _pipeline()
    response = _StubResponse(200, headers={"content-length": "99"}, chunks=(b"x" * 99,))
    stream = _StubStream(response)
    client = _StubDriveClient(stream=stream)

    with pytest.raises(ValidationError, match="99 bytes, over the 200 MiB download cap"):
        await pipeline._stream_drive_media(
            client,
            DRIVE_REF,
            "document.pdf",
            _credential(),
            tmp_path / "document.pdf",
            _deadline(),
        )

    # The point of the test: refused from the Content-Length alone. Without
    # this the assertions passed even when the body was fully drained first.
    assert response.reads == 0, "the body was read despite a header-only refusal"
    assert stream.exit_args is not None
    assert stream.exit_args[0] is ValidationError


@pytest.mark.asyncio
async def test_an_undeclared_body_that_outgrows_the_cap_is_cut_off_mid_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a declared length the running total is the only defence.

    The response supports ``abort``, so the connection must be torn down rather
    than drained.
    """

    # The cap is shrunk rather than fed 200 MiB of bytes; the public message
    # spells the real limit out as a literal, so it still reads "200 MiB".
    monkeypatch.setattr(upload_module, "_MAX_DRIVE_DOWNLOAD_BYTES", 8)
    _session, _bearer, pipeline = await _pipeline()
    response = _AbortableStubResponse(200, chunks=(b"x" * 5, b"y" * 5, b"z" * 5))
    stream = _StubStream(response)
    client = _StubDriveClient(stream=stream)
    destination = tmp_path / "document.pdf"

    with pytest.raises(ValidationError, match="exceeded the 200 MiB cap for 'document.pdf'"):
        await pipeline._stream_drive_media(
            client,
            DRIVE_REF,
            "document.pdf",
            _credential(),
            destination,
            _deadline(),
        )

    assert response.aborts == 1
    # The third chunk was never requested: the cap bites on the second.
    assert destination.read_bytes() == b"x" * 5
    assert pipeline._open_files == set()


@pytest.mark.asyncio
async def test_an_empty_drive_body_is_reported_instead_of_staged_as_a_source(
    tmp_path: Path,
) -> None:
    """A 200 with no bytes would otherwise become a silently empty source."""

    _session, _bearer, pipeline = await _pipeline()
    stream = _StubStream(_StubResponse(200, chunks=()))
    client = _StubDriveClient(stream=stream)

    with pytest.raises(ValidationError, match="Drive returned 0 bytes"):
        await pipeline._stream_drive_media(
            client,
            DRIVE_REF,
            "document.pdf",
            _credential(),
            tmp_path / "document.pdf",
            _deadline(),
        )

    assert stream.exits == 1


# --------------------------------------------------------------------------- #
# Bearer destination allowlist.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "match"),
    [
        pytest.param("https://notebooklm-pa.googleapis.com:port/x", "is invalid", id="bad-port"),
        pytest.param("http://notebooklm-pa.googleapis.com/x", "not allowlisted", id="plaintext"),
        pytest.param("https://evil.example.com/x", "not allowlisted", id="foreign-host"),
        pytest.param("https://notebooklm-pa.googleapis.com:8443/x", "not allowlisted", id="port"),
        pytest.param(
            "https://user:pw@notebooklm-pa.googleapis.com/x",
            "not allowlisted",
            id="userinfo",
        ),
    ],
)
async def test_a_bearer_is_never_minted_for_an_unapproved_destination(
    url: str,
    match: str,
) -> None:
    """The token must not be fetched at all -- refusing after minting is too late."""

    _session, bearer, pipeline = await _pipeline()

    with pytest.raises(RuntimeError, match=match):
        await pipeline._bearer_for(url, pipeline._active_epoch or 0, _deadline())

    assert bearer.calls == []


@pytest.mark.asyncio
async def test_a_bearer_provider_failure_survives_as_itself() -> None:
    """Nothing retired mid-fetch, so the provider's own diagnostic must reach the caller."""

    session, bearer, pipeline = await _pipeline()
    bearer.error = RuntimeError("no refresh token")

    with pytest.raises(RuntimeError, match="no refresh token") as excinfo:
        await pipeline._bearer_for(START_URL, session.epoch, _deadline())

    assert not isinstance(excinfo.value, _RetiredEpochError)


@pytest.mark.asyncio
async def test_a_close_during_the_token_fetch_outranks_the_provider_error() -> None:
    """A provider failure caused by teardown must be reported as a retired generation.

    Both are ``RuntimeError``; only the re-assert after the fetch tells the
    caller that retrying is pointless because the pipeline is gone.
    """

    session, bearer, pipeline = await _pipeline()

    async def _closes_then_fails(expected_epoch: int) -> BearerCredential:
        bearer.calls.append(expected_epoch)
        pipeline._closing = True
        raise RuntimeError("no refresh token")

    bearer.get = _closes_then_fails  # type: ignore[method-assign]

    with pytest.raises(_RetiredEpochError):
        await pipeline._bearer_for(START_URL, session.epoch, _deadline())

    assert bearer.calls == [session.epoch]


# --------------------------------------------------------------------------- #
# Secret-owning HTTP workers.
# --------------------------------------------------------------------------- #


class _StubUploadClient:
    def __init__(
        self,
        *,
        response: Any = None,
        error: BaseException | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.entered = 0
        self.exited = 0
        self.streamed: bytearray = bytearray()

    async def __aenter__(self) -> _StubUploadClient:
        self.entered += 1
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.exited += 1

    async def post(self, url: str, **kwargs: Any) -> Any:
        if self.error is not None:
            raise self.error
        return self.response

    async def put(self, url: str, *, headers: Any, content: Any, follow_redirects: bool) -> Any:
        if self.error is not None:
            raise self.error
        async for chunk in content:
            self.streamed.extend(chunk)
        return self.response


def _factory_for(client: Any) -> Any:
    def factory(**kwargs: Any) -> Any:
        return client

    return factory


@pytest.mark.asyncio
async def test_the_start_worker_reads_plain_headers_and_retires_a_rejected_bearer() -> None:
    """A 401 must invalidate the exact generation that was rejected.

    The stub also returns a plain ``dict`` of headers and omits ``aclose``,
    which is what a non-httpx transport looks like; neither may break the read.
    """

    response = _StubResponse(
        401,
        headers={"x-goog-upload-status": "final", "x-goog-upload-url": SESSION_URL},
    )
    client = _StubUploadClient(response=response)
    session, bearer, pipeline = await _pipeline(factory=_factory_for(client))

    outcome = await pipeline._start_worker(
        NOTEBOOK_ID, SOURCE_ID, "document.pdf", 10, "application/pdf", session.epoch, _deadline()
    )

    assert outcome == upload_module._HTTPOutcome(401, "final", SESSION_URL)
    assert bearer.invalidated == [1]
    assert client.exited == 1
    assert pipeline._transport_clients == set()


@pytest.mark.asyncio
async def test_a_response_close_failure_cannot_mask_the_start_outcome() -> None:
    """Cleanup is best-effort; the worker's verdict must still reach the caller."""

    response = _ClosableStubResponse(
        200,
        headers={"x-goog-upload-status": "active", "x-goog-upload-url": SESSION_URL},
        aclose_error=RuntimeError("aclose boom"),
    )
    client = _StubUploadClient(response=response)
    session, _bearer, pipeline = await _pipeline(factory=_factory_for(client))

    outcome = await pipeline._start_worker(
        NOTEBOOK_ID, SOURCE_ID, "document.pdf", 10, "application/pdf", session.epoch, _deadline()
    )

    assert outcome == upload_module._HTTPOutcome(200, "active", SESSION_URL)
    assert response.acloses == 1


@pytest.mark.asyncio
async def test_a_transport_error_becomes_an_opaque_start_failure() -> None:
    """The bearer-owning frames must never escape; only a kind marker does."""

    client = _StubUploadClient(error=httpx.ConnectError("dns went missing"))
    session, _bearer, pipeline = await _pipeline(factory=_factory_for(client))

    outcome = await pipeline._start_worker(
        NOTEBOOK_ID, SOURCE_ID, "document.pdf", 10, "application/pdf", session.epoch, _deadline()
    )

    assert outcome == upload_module._HTTPFailure("transport")


#: On Python 3.10/3.11 ``asyncio.wait_for`` wraps its awaitable in a Task, so a
#: ``BaseException`` raised inside it is re-raised into the EVENT LOOP by
#: ``Task.__step`` rather than reaching the awaiting frame — which tears down the
#: xdist worker instead of being caught by ``pytest.raises``. Python 3.12 removed
#: that wrapping. The production guard is version-independent; only this way of
#: observing it is not, so these two cases are pinned on 3.12+.
_NEEDS_UNWRAPPED_WAIT_FOR = pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="asyncio.wait_for wraps in a Task before 3.12, so a BaseException "
    "escapes to the event loop instead of the awaiting frame",
)


@_NEEDS_UNWRAPPED_WAIT_FOR
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interrupt",
    [KeyboardInterrupt, SystemExit],
    ids=["keyboard-interrupt", "system-exit"],
)
async def test_an_interpreter_interrupt_is_not_swallowed_as_a_start_failure(
    interrupt: type[BaseException],
) -> None:
    """``except BaseException -> _HTTPFailure`` must not eat a shutdown signal."""

    client = _StubUploadClient(error=interrupt())
    session, _bearer, pipeline = await _pipeline(factory=_factory_for(client))

    with pytest.raises(interrupt):
        await pipeline._start_worker(
            NOTEBOOK_ID,
            SOURCE_ID,
            "document.pdf",
            10,
            "application/pdf",
            session.epoch,
            _deadline(),
        )

    assert pipeline._transport_clients == set()


@pytest.mark.asyncio
async def test_a_retired_epoch_is_a_lifecycle_signal_not_a_start_failure() -> None:
    """Collapsing this into ``_HTTPFailure`` would retry against a dead client."""

    client = _StubUploadClient()
    session, bearer, pipeline = await _pipeline(factory=_factory_for(client))

    with pytest.raises(_RetiredEpochError):
        await pipeline._start_worker(
            NOTEBOOK_ID,
            SOURCE_ID,
            "document.pdf",
            10,
            "application/pdf",
            session.epoch + 1,
            _deadline(),
        )

    assert bearer.calls == []
    assert client.entered == 0


@pytest.mark.asyncio
async def test_a_retired_epoch_is_a_lifecycle_signal_not_a_finalize_failure() -> None:
    """The finalize leg carries the file handle, so it must fence identically."""

    client = _StubUploadClient()
    session, bearer, pipeline = await _pipeline(factory=_factory_for(client))

    with pytest.raises(_RetiredEpochError):
        await pipeline._finalize_worker(
            SESSION_URL,
            io.BytesIO(b"payload"),
            7,
            None,
            session.epoch + 1,
            _deadline(),
        )

    assert bearer.calls == []
    assert client.entered == 0


@pytest.mark.asyncio
async def test_a_response_close_failure_cannot_mask_the_finalize_outcome() -> None:
    """Same best-effort cleanup contract as the start leg."""

    response = _ClosableStubResponse(
        200, headers={"x-goog-upload-status": "final"}, aclose_error=RuntimeError("aclose boom")
    )
    client = _StubUploadClient(response=response)
    session, _bearer, pipeline = await _pipeline(factory=_factory_for(client))

    outcome = await pipeline._finalize_worker(
        SESSION_URL, io.BytesIO(b"payload"), 7, None, session.epoch, _deadline()
    )

    assert outcome == upload_module._HTTPOutcome(200, "final")
    assert response.acloses == 1
    assert bytes(client.streamed) == b"payload"


@pytest.mark.asyncio
async def test_a_finalize_response_without_aclose_is_left_alone() -> None:
    """Not every transport returns a closeable response object."""

    response = _StubResponse(200, headers={"x-goog-upload-status": "final"})
    client = _StubUploadClient(response=response)
    session, _bearer, pipeline = await _pipeline(factory=_factory_for(client))

    outcome = await pipeline._finalize_worker(
        SESSION_URL, io.BytesIO(b"payload"), 7, None, session.epoch, _deadline()
    )

    assert outcome == upload_module._HTTPOutcome(200, "final")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interrupt",
    [
        pytest.param(KeyboardInterrupt, id="keyboard-interrupt", marks=_NEEDS_UNWRAPPED_WAIT_FOR),
        pytest.param(SystemExit, id="system-exit", marks=_NEEDS_UNWRAPPED_WAIT_FOR),
        pytest.param(asyncio.CancelledError, id="cancellation"),
    ],
)
async def test_a_progress_callback_may_still_abort_the_whole_upload(
    interrupt: type[BaseException],
) -> None:
    """Ctrl-C or cancellation inside ``on_progress`` must not become a callback error.

    Ordinary callback exceptions are wrapped and re-raised to the caller; these
    three must keep unwinding untouched so shutdown is not deferred by a stream.
    """

    response = _StubResponse(200, headers={"x-goog-upload-status": "final"})
    client = _StubUploadClient(response=response)
    session, _bearer, pipeline = await _pipeline(factory=_factory_for(client))

    def on_progress(sent: int, total: int) -> None:
        raise interrupt()

    with pytest.raises(interrupt):
        await pipeline._finalize_worker(
            SESSION_URL, io.BytesIO(b"payload"), 7, on_progress, session.epoch, _deadline()
        )


# --------------------------------------------------------------------------- #
# Control plane.
# --------------------------------------------------------------------------- #


async def _never_registers(*args: Any) -> str:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")  # pragma: no cover


async def _registers(*args: Any) -> str:
    return SOURCE_ID


def _write_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "document.pdf"
    path.write_bytes(b"%PDF-1.7\n" + b"x" * 64)
    return path


@pytest.mark.asyncio
async def test_a_notebook_id_that_could_escape_the_upload_path_is_refused(
    tmp_path: Path,
) -> None:
    """The id is pasted straight into the Scotty URL path, so it must be inert."""

    _session, _bearer, pipeline = await _pipeline()

    with pytest.raises(ValidationError, match="Invalid notebook id for Android file upload"):
        await pipeline.upload_file(
            "../../other",
            _write_pdf(tmp_path),
            None,
            wait=False,
            wait_timeout=1.0,
            title=None,
            on_progress=None,
            register_tentative=_registers,
            wait_until_registered=cast(Any, None),
            wait_until_ready=cast(Any, None),
            rename_uploaded=cast(Any, None),
        )


@pytest.mark.asyncio
async def test_a_timeout_before_registration_reports_no_source_id(tmp_path: Path) -> None:
    """``SourceAddError.source_id`` must be absent, not ``None``.

    Callers use ``hasattr`` to decide whether an orphaned source needs cleanup;
    a placeholder attribute would send them chasing a source that never existed.
    """

    _session, _bearer, pipeline = await _pipeline(upload_timeout=0.05)

    with pytest.raises(SourceAddError) as excinfo:
        await pipeline.upload_file(
            NOTEBOOK_ID,
            _write_pdf(tmp_path),
            None,
            wait=False,
            wait_timeout=1.0,
            title=None,
            on_progress=None,
            register_tentative=_never_registers,
            wait_until_registered=cast(Any, None),
            wait_until_ready=cast(Any, None),
            rename_uploaded=cast(Any, None),
        )

    error = excinfo.value
    assert "failed during register: timed out" in str(error)
    assert error.stage == "register"
    assert not hasattr(error, "source_id")


@pytest.mark.asyncio
async def test_a_directory_is_rejected_before_it_is_opened(tmp_path: Path) -> None:
    """``exists()`` is not enough: a directory would blow up in the reader thread."""

    _session, _bearer, pipeline = await _pipeline()
    directory = tmp_path / "document.pdf"
    directory.mkdir()

    with pytest.raises(ValidationError, match="Not a regular file"):
        await pipeline._control_plane(
            NOTEBOOK_ID,
            directory,
            _deadline(),
            _UploadState(),
            None,
            "application/pdf",
            pipeline._active_epoch or 0,
            _registers,
        )


@pytest.mark.asyncio
async def test_a_failed_stat_closes_the_handle_and_releases_the_upload_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leaked descriptor or slot here would wedge every later upload."""

    _session, _bearer, pipeline = await _pipeline()
    path = _write_pdf(tmp_path)
    opened: list[Any] = []
    real_open = builtins.open

    def tracking_open(*args: Any, **kwargs: Any) -> Any:
        handle = real_open(*args, **kwargs)
        opened.append(handle)
        return handle

    class _OSProxy:
        def __getattr__(self, name: str) -> Any:
            return getattr(os, name)

        def fstat(self, fd: int) -> Any:
            raise OSError("fstat refused")

    monkeypatch.setattr(builtins, "open", tracking_open)
    monkeypatch.setattr(upload_module, "os", _OSProxy())

    with pytest.raises(OSError, match="fstat refused"):
        await pipeline._control_plane(
            NOTEBOOK_ID,
            path,
            _deadline(),
            _UploadState(),
            None,
            "application/pdf",
            pipeline._active_epoch or 0,
            _registers,
        )

    monkeypatch.undo()
    ours = [handle for handle in opened if getattr(handle, "name", None) == str(path)]
    assert ours and all(handle.closed for handle in ours)
    assert pipeline._open_files == set()
    assert not pipeline._upload_slot().locked()


@pytest.mark.asyncio
async def test_a_start_leg_transport_failure_is_reported_and_the_queue_wait_recorded(
    tmp_path: Path,
) -> None:
    """The queue-wait metric must be recorded even for an upload that then fails."""

    waits: list[float] = []
    client = _StubUploadClient(error=httpx.ConnectError("dns went missing"))
    _session, _bearer, pipeline = await _pipeline(
        factory=_factory_for(client),
        record_upload_queue_wait=waits.append,
    )
    state = _UploadState()

    with pytest.raises(SourceAddError) as excinfo:
        await pipeline._control_plane(
            NOTEBOOK_ID,
            _write_pdf(tmp_path),
            _deadline(),
            state,
            None,
            "application/pdf",
            pipeline._active_epoch or 0,
            _registers,
        )

    assert "failed during start: request failed" in str(excinfo.value)
    assert excinfo.value.source_id == SOURCE_ID
    assert state.stage == "start"
    assert len(waits) == 1
    # Elapsed queue wait, not an absolute clock reading — see the web twin in
    # tests/unit/test_source_upload_coverage.py. ``>= 0.0`` accepted both.
    assert 0.0 <= waits[0] < 1.0
    assert pipeline._open_files == set()
    assert not pipeline._upload_slot().locked()


@pytest.mark.asyncio
async def test_an_unusable_session_capability_fails_the_upload_at_the_start_stage(
    tmp_path: Path,
) -> None:
    """A rewritten ``X-Goog-Upload-URL`` must never be used to stream the file."""

    response = _StubResponse(
        200,
        headers={
            "x-goog-upload-status": "active",
            "x-goog-upload-url": "https://evil.example.com/upload",
        },
    )
    client = _StubUploadClient(response=response)
    _session, _bearer, pipeline = await _pipeline(factory=_factory_for(client))

    with pytest.raises(SourceAddError) as excinfo:
        await pipeline._control_plane(
            NOTEBOOK_ID,
            _write_pdf(tmp_path),
            _deadline(),
            _UploadState(),
            None,
            "application/pdf",
            pipeline._active_epoch or 0,
            _registers,
        )

    assert "failed during start: invalid session response" in str(excinfo.value)
    assert client.streamed == bytearray()


# --------------------------------------------------------------------------- #
# The aggregate deadline primitive.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_already_expired_budget_closes_the_work_it_refuses_to_start() -> None:
    """Refusing without closing the coroutine leaks a "never awaited" warning."""

    async def _work() -> str:
        raise AssertionError("must never run")  # pragma: no cover

    awaitable = _work()

    with pytest.raises(TimeoutError):
        await upload_module._bounded(awaitable, upload_module.RuntimeDeadline.start(0.0))

    assert inspect.getcoroutinestate(awaitable) == inspect.CORO_CLOSED


@pytest.mark.asyncio
async def test_an_already_expired_budget_refuses_an_awaitable_it_cannot_close() -> None:
    """A future is not closeable, so the guard must skip it rather than crash."""

    pending = asyncio.get_running_loop().create_future()

    with pytest.raises(TimeoutError):
        await upload_module._bounded(pending, upload_module.RuntimeDeadline.start(0.0))

    assert not pending.done()
    pending.cancel()
