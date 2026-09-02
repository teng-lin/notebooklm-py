from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from types import SimpleNamespace

import pytest

from notebooklm._android.auth import BearerCredential
from notebooklm._android.session import (
    ANDROID_GRPC_MAX_RECEIVE_MESSAGE_BYTES,
    ANDROID_GRPC_TARGET,
    AndroidSession,
    _await_with_deadline,
    _default_grpc_loader,
    _default_protobuf_loader,
)
from notebooklm._client_metrics import ClientMetrics
from notebooklm._deadline import RuntimeDeadline
from notebooklm._runtime.call_supervisor import CallSupervisor
from notebooklm._transport_drain import TransportDrainTracker
from notebooklm.exceptions import (
    AuthError,
    ClientError,
    MissingDependencyError,
    RateLimitError,
    RPCError,
    RPCResponseTooLargeError,
    RPCTimeoutError,
    ServerError,
)

METHOD = "/package.Service/Method"
BEARER = "ya29.session-secret"


class _Status(Enum):
    CANCELLED = (1, "cancelled")
    UNKNOWN = (2, "unknown")
    INVALID_ARGUMENT = (3, "invalid argument")
    DEADLINE_EXCEEDED = (4, "deadline exceeded")
    NOT_FOUND = (5, "not found")
    ALREADY_EXISTS = (6, "already exists")
    PERMISSION_DENIED = (7, "permission denied")
    RESOURCE_EXHAUSTED = (8, "resource exhausted")
    FAILED_PRECONDITION = (9, "failed precondition")
    ABORTED = (10, "aborted")
    OUT_OF_RANGE = (11, "out of range")
    UNIMPLEMENTED = (12, "unimplemented")
    INTERNAL = (13, "internal")
    UNAVAILABLE = (14, "unavailable")
    DATA_LOSS = (15, "data loss")
    UNAUTHENTICATED = (16, "unauthenticated")


class _RawRpcError(Exception):
    def __init__(self, status: _Status) -> None:
        super().__init__(f"raw-{status.name} {BEARER}")
        self._status = status

    def code(self):
        return self._status

    def details(self):
        return f"raw details {BEARER}"


@dataclass(frozen=True)
class _Message:
    payload: bytes

    def SerializeToString(self) -> bytes:
        return self.payload

    @classmethod
    def FromString(cls, payload: bytes):
        return cls(payload)


class _Bearer:
    def __init__(self, tokens: list[str] | None = None) -> None:
        self.tokens = tokens or [BEARER]
        self.gets = 0
        self.invalidated: list[int] = []
        self.activations: list[int] = []
        self.closed = 0
        self.wait: asyncio.Event | None = None

    async def activate(self, epoch: int) -> None:
        self.activations.append(epoch)

    async def get(self, expected_epoch: int) -> BearerCredential:
        if self.wait is not None:
            await self.wait.wait()
        index = min(self.gets, len(self.tokens) - 1)
        self.gets += 1
        return BearerCredential(self.tokens[index], self.gets)

    def invalidate(self, generation: int) -> None:
        self.invalidated.append(generation)

    async def prepare_close(self) -> None:
        self.closed += 1


class _StreamCall:
    def __init__(self, outcomes) -> None:
        self._outcomes = iter(outcomes)
        self.cancelled = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            outcome = next(self._outcomes)
        except StopIteration:
            raise StopAsyncIteration from None
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            outcome = outcome()
        await asyncio.sleep(0)
        return outcome

    def cancel(self) -> None:
        self.cancelled = True


class _Channel:
    def __init__(self) -> None:
        self.unary_outcomes: list[bytes | BaseException | asyncio.Future[bytes]] = [b"response"]
        self.stream_outcomes = [[_Message(b"one"), _Message(b"two")]]
        self.invocations: list[
            tuple[str, bytes, tuple[tuple[str, str | bytes], ...], float | None]
        ] = []
        self.created_callables: list[tuple[str, str]] = []
        self.stream_calls: list[_StreamCall] = []
        self.closed = 0

    def unary_unary(self, method, *, request_serializer, response_deserializer):
        self.created_callables.append(("unary", method))

        async def invoke(request, *, metadata, timeout):
            payload = request_serializer(request)
            self.invocations.append((method, payload, metadata, timeout))
            outcome = self.unary_outcomes.pop(0)
            if isinstance(outcome, asyncio.Future):
                outcome = await outcome
            if isinstance(outcome, BaseException):
                raise outcome
            return response_deserializer(outcome)

        return invoke

    def unary_stream(self, method, *, request_serializer, response_deserializer):
        self.created_callables.append(("stream", method))

        def invoke(request, *, metadata, timeout):
            payload = request_serializer(request)
            self.invocations.append((method, payload, metadata, timeout))
            outcomes = self.stream_outcomes.pop(0)
            call = _StreamCall(outcomes)
            self.stream_calls.append(call)
            return call

        return invoke

    async def close(self) -> None:
        self.closed += 1


class _Grpc:
    def __init__(self, channel: _Channel) -> None:
        self.channel = channel
        self.loads = 0
        self.targets: list[tuple[str, object, tuple[tuple[str, int], ...]]] = []
        self.aio = SimpleNamespace(secure_channel=self.secure_channel)

    def ssl_channel_credentials(self):
        return object()

    def secure_channel(self, target, credentials, *, options):
        self.loads += 1
        self.targets.append((target, credentials, options))
        return self.channel


def _supervisor(*, limit: int | None = 2, events=None) -> CallSupervisor:
    metrics = ClientMetrics(on_rpc_event=None if events is None else events.append)
    return CallSupervisor(
        metrics=metrics,
        drain_tracker=TransportDrainTracker(),
        max_concurrent_rpcs=limit,
    )


async def _open(
    *,
    channel: _Channel | None = None,
    bearer: _Bearer | None = None,
    supervisor: CallSupervisor | None = None,
    timeout: float | None = 1.0,
    monotonic: Callable[[], float] = time.monotonic,
):
    channel = channel or _Channel()
    bearer = bearer or _Bearer()
    grpc = _Grpc(channel)
    supervisor = supervisor or _supervisor()
    loop = asyncio.get_running_loop()
    supervisor.set_bound_loop(loop)
    supervisor.reset_after_open()
    supervisor.prepare_generation(1)
    supervisor.start_accepting(1)
    session = AndroidSession(
        bearer,  # type: ignore[arg-type]
        supervisor,
        timeout=timeout,
        grpc_loader=lambda: grpc,
        monotonic=monotonic,
    )
    session.set_bound_loop(loop)
    session.reset_after_open()
    await session.open(loop, 1)
    return session, bearer, channel, grpc, supervisor


@pytest.mark.asyncio
async def test_open_is_lazy_and_unary_uses_fixed_tls_channel_and_metadata() -> None:
    events = []
    session, bearer, channel, grpc, supervisor = await _open(supervisor=_supervisor(events=events))

    assert grpc.loads == 0
    result = await session.unary(
        METHOD,
        _Message(b"request"),
        replay_safe=True,
        response_type=_Message,
    )

    assert result == _Message(b"response")
    assert grpc.loads == 1
    assert grpc.targets[0][0] == ANDROID_GRPC_TARGET
    assert grpc.targets[0][2] == (
        ("grpc.max_receive_message_length", ANDROID_GRPC_MAX_RECEIVE_MESSAGE_BYTES),
    )
    assert channel.invocations[0][1:3] == (
        b"request",
        (("authorization", f"Bearer {BEARER}"),),
    )
    snapshot = supervisor._metrics.snapshot()
    assert snapshot.rpc_calls_started == 1
    assert snapshot.rpc_calls_succeeded == 1
    assert [event.method for event in events] == [METHOD]


@pytest.mark.asyncio
async def test_unary_appends_augmented_metadata() -> None:
    session, _, channel, _, _ = await _open()

    async def augment(bearer: str) -> tuple[tuple[str, bytes], ...]:
        assert bearer == BEARER
        return (("x-extra-bin", b"extra"),)

    assert await session.unary(
        METHOD,
        _Message(b"request"),
        replay_safe=False,
        response_type=_Message,
        metadata_augmentor=augment,
    ) == _Message(b"response")
    assert channel.invocations[0][2] == (
        ("authorization", f"Bearer {BEARER}"),
        ("x-extra-bin", b"extra"),
    )


@pytest.mark.asyncio
async def test_metadata_augmentor_rechecks_epoch_before_wire_dispatch() -> None:
    session, _, channel, _, _ = await _open()
    started = asyncio.Event()
    release = asyncio.Event()

    async def augment(bearer: str) -> tuple[tuple[str, bytes], ...]:
        assert bearer == BEARER
        started.set()
        await release.wait()
        return (("x-extra-bin", b"extra"),)

    call = asyncio.create_task(
        session.unary(
            METHOD,
            _Message(b"request"),
            replay_safe=False,
            response_type=_Message,
            metadata_augmentor=augment,
        )
    )
    await started.wait()
    await session.prepare_close()
    release.set()

    with pytest.raises(RuntimeError, match="retired resource generation"):
        await call
    assert channel.invocations == []


@pytest.mark.asyncio
async def test_explicit_none_telemetry_still_runs_without_rpc_counters() -> None:
    session, _, _, _, supervisor = await _open()
    assert await session.unary(
        METHOD,
        _Message(b"request"),
        replay_safe=True,
        response_type=_Message,
        telemetry_method=None,
    ) == _Message(b"response")
    snapshot = supervisor._metrics.snapshot()
    assert snapshot.rpc_calls_started == 0
    assert snapshot.rpc_calls_succeeded == 0


@pytest.mark.asyncio
async def test_close_reopen_uses_fresh_epoch_channel_and_callable_cache() -> None:
    session, bearer, first_channel, grpc, supervisor = await _open()
    await session.unary(
        METHOD,
        _Message(b"first"),
        replay_safe=True,
        response_type=_Message,
    )
    await supervisor.begin_closing(1)
    await session.prepare_close()
    await session.close_resources()
    supervisor.mark_closed(1)

    second_channel = _Channel()
    grpc.channel = second_channel
    loop = asyncio.get_running_loop()
    supervisor.set_bound_loop(loop)
    supervisor.reset_after_open()
    supervisor.prepare_generation(2)
    supervisor.start_accepting(2)
    session.set_bound_loop(loop)
    session.reset_after_open()
    await session.open(loop, 2)
    result = await session.unary(
        METHOD,
        _Message(b"second"),
        replay_safe=True,
        response_type=_Message,
    )

    assert result == _Message(b"response")
    assert first_channel.closed == 1
    assert first_channel.invocations[0][1] == b"first"
    assert second_channel.invocations[0][1] == b"second"
    assert bearer.activations == [1, 2]
    assert grpc.loads == 2


@pytest.mark.asyncio
async def test_concurrent_first_calls_publish_one_channel_and_callable() -> None:
    session, _, channel, grpc, _ = await _open()
    channel.unary_outcomes = [b"response", b"response"]
    results = await asyncio.gather(
        *(
            session.unary(
                METHOD,
                _Message(str(index).encode()),
                replay_safe=True,
                response_type=_Message,
            )
            for index in range(2)
        )
    )
    assert results == [_Message(b"response"), _Message(b"response")]
    assert grpc.loads == 1
    assert channel.created_callables == [("unary", METHOD)]


@pytest.mark.asyncio
async def test_safe_read_replays_unauthenticated_once_and_mutation_never_replays() -> None:
    channel = _Channel()
    channel.unary_outcomes = [_RawRpcError(_Status.UNAUTHENTICATED), b"ok"]
    session, bearer, _, _, _ = await _open(
        channel=channel,
        bearer=_Bearer(["ya29.first", "ya29.second"]),
    )
    assert await session.unary(
        METHOD,
        _Message(b"request"),
        replay_safe=True,
        response_type=_Message,
    ) == _Message(b"ok")
    assert len(channel.invocations) == 2
    assert bearer.invalidated == [1]

    mutation_channel = _Channel()
    mutation_channel.unary_outcomes = [_RawRpcError(_Status.UNAUTHENTICATED), b"must-not-run"]
    mutation, mutation_bearer, _, _, _ = await _open(channel=mutation_channel)
    with pytest.raises(AuthError) as captured:
        await mutation.unary(
            METHOD,
            _Message(b"mutation"),
            replay_safe=False,
            response_type=_Message,
        )
    assert len(mutation_channel.invocations) == 1
    assert mutation_bearer.invalidated == [1]
    assert captured.value.rpc_code == 16


@pytest.mark.asyncio
async def test_safe_read_replays_unavailable_once_and_mutation_never_replays() -> None:
    channel = _Channel()
    channel.unary_outcomes = [_RawRpcError(_Status.UNAVAILABLE), b"ok"]
    session, _, _, _, _ = await _open(channel=channel)

    assert await session.unary(
        METHOD,
        _Message(b"request"),
        replay_safe=True,
        response_type=_Message,
    ) == _Message(b"ok")
    assert len(channel.invocations) == 2

    mutation_channel = _Channel()
    mutation_channel.unary_outcomes = [_RawRpcError(_Status.UNAVAILABLE), b"must-not-run"]
    mutation, _, _, _, _ = await _open(channel=mutation_channel)
    with pytest.raises(ServerError) as captured:
        await mutation.unary(
            METHOD,
            _Message(b"mutation"),
            replay_safe=False,
            response_type=_Message,
        )
    assert len(mutation_channel.invocations) == 1
    assert captured.value.rpc_code == 14


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (_Status.NOT_FOUND, RPCError),
        (_Status.UNAUTHENTICATED, AuthError),
        (_Status.PERMISSION_DENIED, ClientError),
        (_Status.RESOURCE_EXHAUSTED, RateLimitError),
        (_Status.DEADLINE_EXCEEDED, RPCTimeoutError),
        (_Status.UNAVAILABLE, ServerError),
        (_Status.INTERNAL, ServerError),
        (_Status.INVALID_ARGUMENT, ClientError),
        (_Status.FAILED_PRECONDITION, ClientError),
        (_Status.CANCELLED, RPCError),
        (_Status.ABORTED, RPCError),
        (_Status.ALREADY_EXISTS, RPCError),
        (_Status.OUT_OF_RANGE, RPCError),
        (_Status.UNIMPLEMENTED, RPCError),
        (_Status.UNKNOWN, RPCError),
        (_Status.DATA_LOSS, RPCError),
    ],
)
@pytest.mark.asyncio
async def test_unary_status_mapping_is_sanitized(status, error_type) -> None:
    channel = _Channel()
    channel.unary_outcomes = [_RawRpcError(status)]
    session, _, _, _, _ = await _open(channel=channel)

    with pytest.raises(error_type) as captured:
        await session.unary(
            METHOD,
            _Message(b"request"),
            replay_safe=False,
            response_type=_Message,
        )

    error = captured.value
    assert BEARER not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.method_id == METHOD
    traceback = error.__traceback__
    while traceback is not None:
        local_values = traceback.tb_frame.f_locals
        if traceback.tb_frame.f_globals.get("__name__", "").startswith("notebooklm"):
            assert "self" not in local_values
            assert "session" not in local_values
            assert not any(isinstance(value, _RawRpcError) for value in local_values.values())
            assert BEARER not in repr(local_values)
        traceback = traceback.tb_next
    if isinstance(error, RPCError):
        assert error.rpc_code == status.value[0]
    else:
        assert error.original_error is None


@pytest.mark.asyncio
async def test_unknown_wire_error_debug_log_contains_type_not_sensitive_message(caplog) -> None:
    sensitive = "wire-detail bearer-secret-must-not-be-logged"
    channel = _Channel()
    channel.unary_outcomes = [RuntimeError(sensitive)]
    session, _, _, _, _ = await _open(channel=channel)

    with (
        caplog.at_level(logging.DEBUG, logger="notebooklm._android.session"),
        pytest.raises(RPCError) as captured,
    ):
        await session.unary(
            METHOD,
            _Message(b"request"),
            replay_safe=False,
            response_type=_Message,
        )

    assert captured.value.rpc_code == 2
    assert "RuntimeError" in caplog.text
    assert sensitive not in caplog.text
    assert sensitive not in str(captured.value)


@pytest.mark.asyncio
async def test_stream_is_lazy_holds_scope_and_never_replays_auth_failure() -> None:
    channel = _Channel()
    channel.stream_outcomes = [[_Message(b"one"), _RawRpcError(_Status.UNAUTHENTICATED)]]
    session, bearer, _, _, supervisor = await _open(channel=channel)
    stream = session.stream(METHOD, _Message(b"request"), response_type=_Message)

    assert supervisor._metrics.snapshot().rpc_calls_started == 0
    assert channel.invocations == []
    assert await anext(stream) == _Message(b"one")
    assert supervisor._current is not None and supervisor._current.in_flight == 1
    with pytest.raises(AuthError) as captured:
        await anext(stream)

    assert captured.value.rpc_code == 16
    assert bearer.invalidated == [1]
    assert len(channel.invocations) == 1
    wire_timeout = channel.invocations[0][3]
    assert wire_timeout is not None and 0.0 < wire_timeout <= 1.0
    assert supervisor._current is not None and supervisor._current.in_flight == 0


@pytest.mark.asyncio
async def test_stream_uses_one_aggregate_deadline_across_frames() -> None:
    now = 0.0

    def monotonic() -> float:
        return now

    def first_frame() -> _Message:
        nonlocal now
        now = 1.0
        return _Message(b"one")

    channel = _Channel()
    channel.stream_outcomes = [[first_frame, _Message(b"must-not-arrive")]]
    session, _, _, _, _ = await _open(
        channel=channel,
        timeout=1.0,
        monotonic=monotonic,
    )
    stream = session.stream(METHOD, _Message(b"request"), response_type=_Message)

    assert await anext(stream) == _Message(b"one")
    with pytest.raises(RPCTimeoutError):
        await anext(stream)

    assert len(channel.invocations) == 1
    assert channel.stream_calls[0].cancelled is True


@pytest.mark.asyncio
async def test_stream_aclose_cancels_wire_call_and_releases_scope() -> None:
    channel = _Channel()
    channel.stream_outcomes = [[_Message(b"one"), _Message(b"two")]]
    session, _, _, _, supervisor = await _open(channel=channel)
    stream = session.stream(METHOD, _Message(b"request"), response_type=_Message)
    assert await anext(stream) == _Message(b"one")

    await stream.aclose()

    assert channel.stream_calls[0].cancelled
    assert supervisor._current is not None and supervisor._current.in_flight == 0


@pytest.mark.asyncio
async def test_stream_enforces_cumulative_response_byte_cap_and_cancels_wire() -> None:
    channel = _Channel()
    channel.stream_outcomes = [[_Message(b"one"), _Message(b"two")]]
    session, _, _, _, supervisor = await _open(channel=channel)
    stream = session.stream(
        METHOD,
        _Message(b"request"),
        response_type=_Message,
        max_response_bytes=3,
    )

    assert await anext(stream) == _Message(b"one")
    with pytest.raises(RPCResponseTooLargeError) as captured:
        await anext(stream)

    assert captured.value.limit_bytes == 3
    assert captured.value.bytes_read == 6
    assert captured.value.method_id == METHOD
    assert channel.stream_calls[0].cancelled
    assert supervisor._current is not None and supervisor._current.in_flight == 0


@pytest.mark.asyncio
async def test_local_cancellation_is_unchanged_and_not_counted_failed() -> None:
    channel = _Channel()
    pending: asyncio.Future[bytes] = asyncio.Future()
    channel.unary_outcomes = [pending]
    session, _, _, _, supervisor = await _open(channel=channel)
    call = asyncio.create_task(
        session.unary(
            METHOD,
            _Message(b"request"),
            replay_safe=True,
            response_type=_Message,
        )
    )
    while not channel.invocations:
        await asyncio.sleep(0)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    snapshot = supervisor._metrics.snapshot()
    assert snapshot.rpc_calls_started == 1
    assert snapshot.rpc_calls_failed == 0


@pytest.mark.asyncio
async def test_bearer_wait_consumes_aggregate_deadline_before_wire() -> None:
    bearer = _Bearer()
    bearer.wait = asyncio.Event()
    session, _, channel, _, _ = await _open(bearer=bearer, timeout=0.01)

    with pytest.raises(RPCTimeoutError) as captured:
        await session.unary(
            METHOD,
            _Message(b"request"),
            replay_safe=True,
            response_type=_Message,
        )
    assert captured.value.timeout_seconds == 0.01
    assert channel.invocations == []


@pytest.mark.asyncio
async def test_preopen_error_stream_laziness_missing_extra_and_phased_close() -> None:
    supervisor = _supervisor()
    bearer = _Bearer()
    session = AndroidSession(bearer, supervisor)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="Client not initialized"):
        await session.unary(
            METHOD,
            _Message(b"request"),
            replay_safe=True,
            response_type=_Message,
        )
    stream = session.stream(METHOD, _Message(b"request"), response_type=_Message)
    with pytest.raises(RuntimeError, match="Client not initialized"):
        await anext(stream)

    loop = asyncio.get_running_loop()
    supervisor.set_bound_loop(loop)
    supervisor.reset_after_open()
    supervisor.prepare_generation(1)
    supervisor.start_accepting(1)

    def missing_import(name: str) -> object:
        raise ImportError(name)

    missing = AndroidSession(  # type: ignore[arg-type]
        bearer,
        supervisor,
        grpc_loader=lambda: _default_grpc_loader(missing_import),
    )
    missing.set_bound_loop(loop)
    missing.reset_after_open()
    with pytest.raises(MissingDependencyError, match=r"notebooklm-py\[android\]"):
        await missing.open(loop, 1)

    opened, _, channel, _, _ = await _open()
    await opened.unary(
        METHOD,
        _Message(b"request"),
        replay_safe=True,
        response_type=_Message,
    )
    await opened.prepare_close()
    assert channel.closed == 0
    await opened.close_resources()
    assert channel.closed == 1


def test_private_android_package_import_does_not_load_grpc() -> None:
    import sys

    sys.modules.pop("grpc", None)
    import notebooklm._android as android

    assert android.__all__ == []
    assert "grpc" not in sys.modules


@pytest.mark.parametrize(
    "missing_module",
    [
        "google.protobuf.runtime_version",
        "notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1.notes_pb2",
    ],
)
def test_default_protobuf_loader_maps_missing_runtime_or_generated_closure(
    missing_module: str,
) -> None:
    calls: list[str] = []

    class FakeVersionError(Exception):
        pass

    def import_module(name: str) -> object:
        calls.append(name)
        if name == missing_module:
            raise ImportError(f"raw import detail for {name}")
        if name == "google.protobuf.runtime_version":
            return SimpleNamespace(VersionError=FakeVersionError)
        return object()

    with pytest.raises(MissingDependencyError, match="protobuf runtime") as captured:
        _default_protobuf_loader(import_module)

    assert "raw import detail" not in str(captured.value)
    assert calls == [
        "google.protobuf.runtime_version",
        *(
            []
            if missing_module == "google.protobuf.runtime_version"
            else [
                "notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1.notes_pb2"
            ]
        ),
    ]


def test_default_protobuf_loader_maps_generated_runtime_version_mismatch() -> None:
    calls: list[str] = []

    class FakeVersionError(Exception):
        pass

    def import_module(name: str) -> object:
        calls.append(name)
        if name == "google.protobuf.runtime_version":
            return SimpleNamespace(VersionError=FakeVersionError)
        raise FakeVersionError("raw incompatible-version detail")

    with pytest.raises(MissingDependencyError, match="protobuf runtime") as captured:
        _default_protobuf_loader(import_module)

    assert "raw incompatible-version detail" not in str(captured.value)
    assert calls == [
        "google.protobuf.runtime_version",
        "notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1.notes_pb2",
    ]


# ===========================================================================
# Lifecycle, deadline, and epoch-fencing branches
#
# The cases above cover the wire path and its status mapping. These target the
# surrounding transport contract: the unbounded-timeout path, the loop/epoch
# assertions that fence a retired resource generation, ``prepare_metadata``'s
# sanitization, and the queue-admission timeout that must surface as
# DEADLINE_EXCEEDED rather than a bare ``TimeoutError``.
# ===========================================================================

# ---------------------------------------------------------------------------
# _await_with_deadline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_absent_deadline_awaits_without_a_timeout_wrapper() -> None:
    """No deadline means no ``wait_for`` — the awaitable is returned directly."""

    async def _work() -> str:
        return "done"

    assert await _await_with_deadline(_work(), None) == "done"


# ---------------------------------------------------------------------------
# Timeout resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "timeout",
    [
        pytest.param(None, id="unset"),
        pytest.param(float("inf"), id="infinite"),
        pytest.param(float("nan"), id="nan"),
    ],
)
async def test_a_non_finite_timeout_dispatches_without_a_deadline(timeout: float | None) -> None:
    session, _bearer, channel, _grpc, _sup = await _open(timeout=timeout)

    await session.unary(METHOD, _Message(b"request"), replay_safe=True, response_type=_Message)

    # The wire call carries no timeout rather than a synthesized one.
    assert channel.invocations[0][3] is None


@pytest.mark.asyncio
async def test_a_per_call_timeout_overrides_the_session_default() -> None:
    session, _bearer, channel, _grpc, _sup = await _open(timeout=None)

    await session.unary(
        METHOD, _Message(b"request"), replay_safe=True, response_type=_Message, timeout=30.0
    )

    assert channel.invocations[0][3] is not None


# ---------------------------------------------------------------------------
# Loop and epoch fencing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_refuses_a_loop_the_lifecycle_did_not_bind() -> None:
    session = AndroidSession(
        _Bearer(),  # type: ignore[arg-type]
        _supervisor(),
        timeout=1.0,
        grpc_loader=lambda: _Grpc(_Channel()),
    )
    session.set_bound_loop(asyncio.get_running_loop())

    other_loop = asyncio.new_event_loop()
    try:
        with pytest.raises(RuntimeError, match="not bound by the client lifecycle"):
            await session.open(other_loop, 1)
    finally:
        other_loop.close()


@pytest.mark.asyncio
async def test_assert_epoch_accepts_the_active_generation() -> None:
    session, _bearer, _channel, _grpc, _sup = await _open()

    session.assert_epoch(1)


@pytest.mark.asyncio
async def test_assert_epoch_rejects_a_retired_generation() -> None:
    session, _bearer, _channel, _grpc, _sup = await _open()

    with pytest.raises(RuntimeError, match="retired resource generation"):
        session.assert_epoch(2)


@pytest.mark.asyncio
async def test_assert_epoch_rejects_every_generation_once_closing() -> None:
    session, _bearer, _channel, _grpc, _sup = await _open()
    await session.prepare_close()

    with pytest.raises(RuntimeError, match="retired resource generation"):
        session.assert_epoch(1)


@pytest.mark.asyncio
async def test_a_unary_call_naming_a_retired_generation_is_refused() -> None:
    session, _bearer, channel, _grpc, _sup = await _open()

    with pytest.raises(RuntimeError, match="retired resource generation"):
        await session.unary(
            METHOD,
            _Message(b"request"),
            replay_safe=True,
            response_type=_Message,
            expected_epoch=99,
        )

    assert channel.invocations == []


@pytest.mark.asyncio
async def test_active_epoch_tracks_open_and_close() -> None:
    session, _bearer, _channel, _grpc, _sup = await _open()

    assert session.active_epoch == 1
    await session.prepare_close()
    assert session.active_epoch is None


# ---------------------------------------------------------------------------
# prepare_metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_metadata_resolves_credential_derived_headers() -> None:
    session, _bearer, _channel, _grpc, _sup = await _open()

    async def augment(bearer: str) -> tuple[tuple[str, bytes], ...]:
        assert bearer == BEARER
        return (("x-extra-bin", b"extra"),)

    metadata = await session.prepare_metadata(augment, expected_epoch=1)

    assert metadata == (("x-extra-bin", b"extra"),)


@pytest.mark.asyncio
async def test_prepare_metadata_refuses_a_retired_generation() -> None:
    session, _bearer, _channel, _grpc, _sup = await _open()

    async def augment(_bearer: str) -> tuple[tuple[str, bytes], ...]:
        raise AssertionError("augmentor must not run for a retired generation")

    with pytest.raises(RuntimeError, match="retired resource generation"):
        await session.prepare_metadata(augment, expected_epoch=99)


def _traceback_function_names(error: BaseException) -> list[str]:
    names = []
    tb = error.__traceback__
    while tb is not None:
        names.append(tb.tb_frame.f_code.co_name)
        tb = tb.tb_next
    return names


@pytest.mark.asyncio
async def test_prepare_metadata_detaches_the_frames_of_an_escaping_failure() -> None:
    """The credential lives in the frames this path unwinds through.

    ``sanitize_escaping_exception`` does not rewrite the message — it drops the
    traceback and chain so those frames (and the bearer in their locals) are
    not reachable from the exception a caller catches.
    """
    session, _bearer, _channel, _grpc, _sup = await _open()
    inner = RuntimeError("upstream")

    async def augment(_bearer: str) -> tuple[tuple[str, bytes], ...]:
        try:
            raise inner
        except RuntimeError as error:
            raise ValueError("augmentor blew up") from error

    with pytest.raises(ValueError) as caught:
        await session.prepare_metadata(augment, expected_epoch=1)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    # The re-raise in ``prepare_metadata`` attaches its own frame, but the
    # credential-bearing frames it unwound through are gone.
    frames = _traceback_function_names(caught.value)
    assert "augment" not in frames
    assert "_prepare_metadata_impl" not in frames


# ---------------------------------------------------------------------------
# Deadlines during an attempt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_deadline_reached_while_minting_reports_deadline_exceeded() -> None:
    """The bearer wait is inside the deadline, so it maps to the gRPC status."""
    bearer = _Bearer()
    bearer.wait = asyncio.Event()
    session, _bearer, _channel, _grpc, _sup = await _open(bearer=bearer, timeout=0.01)

    with pytest.raises(RPCTimeoutError):
        await session.unary(METHOD, _Message(b"request"), replay_safe=False, response_type=_Message)


@pytest.mark.asyncio
async def test_a_deadline_reached_inside_the_augmentor_reports_deadline_exceeded() -> None:
    session, _bearer, _channel, _grpc, _sup = await _open(timeout=0.01)

    async def augment(_bearer: str) -> tuple[tuple[str, bytes], ...]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    with pytest.raises(RPCTimeoutError):
        await session.unary(
            METHOD,
            _Message(b"request"),
            replay_safe=False,
            response_type=_Message,
            metadata_augmentor=augment,
        )


# ---------------------------------------------------------------------------
# Queue admission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_waiting_for_a_call_slot_past_the_deadline_is_a_timeout_not_an_error() -> None:
    """A caller that never gets admitted must see DEADLINE_EXCEEDED."""
    supervisor = _supervisor(limit=1)
    blocker = asyncio.Event()
    channel = _Channel()

    async def _blocked() -> bytes:
        await blocker.wait()
        return b"response"

    channel.unary_outcomes = [asyncio.ensure_future(_blocked()), b"response"]
    session, _bearer, _channel, _grpc, _sup = await _open(
        channel=channel, supervisor=supervisor, timeout=None
    )

    holder = asyncio.create_task(
        session.unary(METHOD, _Message(b"a"), replay_safe=False, response_type=_Message)
    )
    await asyncio.sleep(0)

    with pytest.raises(RPCTimeoutError):
        await session.unary(
            METHOD, _Message(b"b"), replay_safe=False, response_type=_Message, timeout=0.01
        )

    blocker.set()
    await asyncio.gather(holder, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_deadline_is_driven_by_the_injected_clock() -> None:
    """``RuntimeDeadline`` must read the session's monotonic, not the global one.

    Asserting ``deadline.timeout`` alone would pass either way; this drives the
    injected clock forward and checks the budget it reports actually moves.
    """
    clock = {"now": 100.0}
    session, _bearer, _channel, _grpc, _sup = await _open(
        timeout=5.0, monotonic=lambda: clock["now"]
    )

    deadline = session._deadline(None)

    assert isinstance(deadline, RuntimeDeadline)
    assert deadline.monotonic is not time.monotonic
    assert deadline.started_at == 100.0
    assert deadline.remaining() == 5.0

    clock["now"] = 102.0
    assert deadline.elapsed() == 2.0
    assert deadline.remaining() == 3.0

    clock["now"] = 200.0
    assert deadline.remaining() == 0.0
    assert deadline.expired() is True


@pytest.mark.asyncio
async def test_a_stream_deadline_reached_while_minting_reports_deadline_exceeded() -> None:
    bearer = _Bearer()
    bearer.wait = asyncio.Event()
    session, _bearer, _channel, _grpc, _sup = await _open(bearer=bearer, timeout=0.01)

    stream = session.stream(METHOD, _Message(b"request"), response_type=_Message)
    with pytest.raises(RPCTimeoutError):
        async for _item in stream:
            pass


@pytest.mark.asyncio
async def test_a_stream_deadline_reached_mid_iteration_reports_deadline_exceeded() -> None:
    """The per-frame await is inside the deadline, so a late frame times out.

    The clock is advanced from the consumer between frames rather than by
    sleeping, so the deadline expires deterministically.
    """
    channel = _Channel()
    channel.stream_outcomes = [[_Message(b"one"), _Message(b"two")]]
    clock = {"now": 0.0}
    session, _bearer, _channel, _grpc, _sup = await _open(
        channel=channel, timeout=1.0, monotonic=lambda: clock["now"]
    )

    received = []
    stream = session.stream(METHOD, _Message(b"request"), response_type=_Message)
    with pytest.raises(RPCTimeoutError):
        async for item in stream:
            received.append(item)
            clock["now"] = 1_000.0

    assert received == [_Message(b"one")]
    # The abandoned call is cancelled rather than left running.
    assert channel.stream_calls[0].cancelled is True


@pytest.mark.asyncio
async def test_an_unmapped_stream_wire_error_logs_only_its_type(caplog) -> None:
    """The wire error may embed the bearer; only the class name is recorded."""

    class _Opaque(Exception):
        def __str__(self) -> str:  # pragma: no cover - must never be logged
            return f"opaque failure {BEARER}"

    channel = _Channel()
    channel.stream_outcomes = [[_Message(b"one"), _Opaque()]]
    session, _bearer, _channel, _grpc, _sup = await _open(channel=channel)

    with caplog.at_level(logging.DEBUG, logger="notebooklm._android.session"):
        stream = session.stream(METHOD, _Message(b"request"), response_type=_Message)
        with pytest.raises(RPCError):
            async for _item in stream:
                pass

    assert "_Opaque" in caplog.text
    assert BEARER not in caplog.text


@pytest.mark.asyncio
async def test_a_stream_cancel_that_itself_fails_is_swallowed() -> None:
    """Teardown must not replace the real failure with the cancel's error."""

    class _UncancellableCall(_StreamCall):
        def cancel(self) -> None:
            raise RuntimeError("cancel is not supported by this transport")

    channel = _Channel()

    def unary_stream(method, *, request_serializer, response_deserializer):
        def invoke(request, *, metadata, timeout):
            return _UncancellableCall([_Message(b"one"), _RawRpcError(_Status.INTERNAL)])

        return invoke

    channel.unary_stream = unary_stream  # type: ignore[method-assign]
    session, _bearer, _channel, _grpc, _sup = await _open(channel=channel)

    stream = session.stream(METHOD, _Message(b"request"), response_type=_Message)
    with pytest.raises(ServerError):
        async for _item in stream:
            pass


@pytest.mark.asyncio
async def test_a_stream_waiting_for_a_call_slot_past_its_deadline_times_out() -> None:
    supervisor = _supervisor(limit=1)
    blocker = asyncio.Event()
    channel = _Channel()

    async def _blocked() -> bytes:
        await blocker.wait()
        return b"response"

    channel.unary_outcomes = [asyncio.ensure_future(_blocked())]
    channel.stream_outcomes = [[_Message(b"one")]]
    session, _bearer, _channel, _grpc, _sup = await _open(
        channel=channel, supervisor=supervisor, timeout=None
    )

    holder = asyncio.create_task(
        session.unary(METHOD, _Message(b"a"), replay_safe=False, response_type=_Message)
    )
    await asyncio.sleep(0)

    stream = session.stream(METHOD, _Message(b"b"), response_type=_Message, timeout=0.01)
    with pytest.raises(RPCTimeoutError):
        async for _item in stream:
            pass

    blocker.set()
    await asyncio.gather(holder, return_exceptions=True)


@pytest.mark.asyncio
async def test_closing_a_session_that_never_opened_a_channel_is_a_no_op() -> None:
    """``close_resources`` runs on the teardown path even for an unused session."""
    session, _bearer, channel, _grpc, _sup = await _open()

    await session.close_resources()

    assert channel.closed == 0
    assert session._channel is None


@pytest.mark.asyncio
async def test_closing_is_idempotent_after_the_channel_is_released() -> None:
    session, _bearer, channel, _grpc, _sup = await _open()
    await session.unary(METHOD, _Message(b"request"), replay_safe=True, response_type=_Message)

    await session.close_resources()
    await session.close_resources()

    assert channel.closed == 1
    assert session._channel is None
    assert session._callables == {}


@pytest.mark.asyncio
async def test_lifecycle_teardown_tolerates_a_session_never_bound_to_a_loop() -> None:
    """Both teardown hooks skip their loop assertion when nothing was bound."""
    session = AndroidSession(
        _Bearer(),  # type: ignore[arg-type]
        _supervisor(),
        timeout=1.0,
        grpc_loader=lambda: _Grpc(_Channel()),
    )

    await session.prepare_close()
    await session.close_resources()

    assert session.active_epoch is None


@pytest.mark.asyncio
async def test_a_stream_call_without_a_cancel_hook_is_abandoned_cleanly() -> None:
    """Not every transport exposes ``cancel`` — teardown must not require it."""

    class _NoCancelCall(_StreamCall):
        cancel = None  # type: ignore[assignment]

    channel = _Channel()

    def unary_stream(method, *, request_serializer, response_deserializer):
        def invoke(request, *, metadata, timeout):
            return _NoCancelCall([_Message(b"one"), _RawRpcError(_Status.INTERNAL)])

        return invoke

    channel.unary_stream = unary_stream  # type: ignore[method-assign]
    session, _bearer, _channel, _grpc, _sup = await _open(channel=channel)

    stream = session.stream(METHOD, _Message(b"request"), response_type=_Message)
    with pytest.raises(ServerError):
        async for _item in stream:
            pass
