"""Lazy, supervised gRPC session for the private Android backend."""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar, cast

from .._backoff import (
    RETRY_BACKOFF_BASE_SECONDS,
    RETRY_BACKOFF_CAP_SECONDS,
    RETRY_BACKOFF_JITTER_RATIO,
    RETRY_BACKOFF_MIN_SECONDS,
    compute_backoff_delay,
)
from .._deadline import RuntimeDeadline, await_with_deadline
from .._loop_affinity import assert_bound_loop
from .._loop_bound import EpochFenced
from .._runtime.auth_refresh_retry import RefreshBudget, refresh_and_count
from .._runtime.call_supervisor import CallLease, CallSupervisor, OperationLease
from .._runtime.config import CORE_LOGGER_NAME, DEFAULT_CHAT_RESPONSE_MAX_BYTES
from .._runtime.helpers import is_auth_error, resolve_sleep
from ..exceptions import MissingDependencyError, RPCResponseTooLargeError
from .auth import BearerCredential, BearerProvider
from .epoch import workflow_epoch_for
from .errors import (
    GrpcStatus,
    grpc_status,
    raise_deadline_exceeded,
    raise_grpc_status,
    sanitize_escaping_exception,
)
from .retry_policy import replay_safe_for

ReqT = TypeVar("ReqT")
RespT = TypeVar("RespT")

RequestSerializer = Callable[[ReqT], bytes]
ResponseDeserializer = Callable[[bytes], RespT]
ResponseSizer = Callable[[RespT], int]

ANDROID_GRPC_TARGET = "notebooklm-pa.googleapis.com:443"
# grpcio otherwise enforces a 4 MiB receive ceiling before this library can
# apply its cumulative streamed-response guard.  Use the documented 256 MiB
# Android chat ceiling as the shared transport bound: large project/artifact
# unary responses and valid chat frames can cross 4 MiB, while callers that
# disable the per-chat aggregate guard still inherit a finite RPC limit.
ANDROID_GRPC_MAX_RECEIVE_MESSAGE_BYTES = DEFAULT_CHAT_RESPONSE_MAX_BYTES
_ANDROID_GRPC_CHANNEL_OPTIONS = (
    ("grpc.max_receive_message_length", ANDROID_GRPC_MAX_RECEIVE_MESSAGE_BYTES),
)
_NOT_OPEN = "Client not initialized. Use 'async with' context."
_ANDROID_GRPC_EXTRA = (
    "Android transport needs grpcio. Install: pip install 'notebooklm-py[android]'"
)
_ANDROID_PROTOBUF_EXTRA = (
    "Android transport needs a protobuf runtime compatible with its generated protocol. "
    "Install: pip install 'notebooklm-py[android]'"
)
_ANDROID_NOTES_PROTO = (
    "notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1.notes_pb2"
)
logger = logging.getLogger(__name__)
retry_logger = logging.getLogger(CORE_LOGGER_NAME)

if TYPE_CHECKING:
    from .._client_metrics import ClientMetrics


class _DefaultTelemetry(Enum):
    METHOD = auto()


_DEFAULT_TELEMETRY = _DefaultTelemetry.METHOD


class _RetryClass(Enum):
    AUTH = auto()
    RATE_LIMIT = auto()
    SERVER = auto()


_RAW_REPLAY_CAPABILITY = object()


@dataclass(frozen=True)
class _RawReplayClassification:
    """Explicit replay decision carried only by the public raw adapter.

    Typed adapters never construct this capability and therefore remain bound
    to the total Android method manifest below.  Raw method paths are unknown
    by definition, so their descriptor supplies the separate, fail-closed
    classification required by the public escape hatch.
    """

    replay_safe: bool
    capability: object


def classify_raw_replay(replay_safe: bool) -> _RawReplayClassification:
    """Create the private capability used for an explicitly classified raw call."""

    if type(replay_safe) is not bool:
        raise TypeError("raw replay classification must be bool")
    return _RawReplayClassification(replay_safe, _RAW_REPLAY_CAPABILITY)


def _resolve_replay_safe(
    method: str,
    replay_safe: bool,
    operation_variant: str | None,
    raw_replay: _RawReplayClassification | None,
) -> bool:
    if raw_replay is not None:
        if raw_replay.capability is not _RAW_REPLAY_CAPABILITY:
            raise ValueError("invalid raw replay classification")
        policy_replay_safe = raw_replay.replay_safe
        if replay_safe is not policy_replay_safe:
            raise ValueError(
                f"Android RPC replay_safe={replay_safe} disagrees with raw classification "
                f"{policy_replay_safe} for {method}"
            )
        return policy_replay_safe

    del operation_variant
    return replay_safe_for(method, replay_safe)


_GRPC_RETRY_CLASS = {
    8: _RetryClass.RATE_LIMIT,
    13: _RetryClass.SERVER,
    14: _RetryClass.SERVER,
    16: _RetryClass.AUTH,
}


class _DeadlineSignal(Exception):
    """Private, data-free signal for an exhausted aggregate deadline."""


def _log_unknown_wire_error(error: Exception) -> None:
    """Record only the exception type when a wire failure has no safe status."""

    logger.debug("Android gRPC wire error type: %s", type(error).__name__)


@dataclass(frozen=True)
class _AttemptSuccess(Generic[RespT]):
    value: RespT


@dataclass(frozen=True)
class _AttemptFailure:
    status: GrpcStatus
    bearer_generation: int | None


def _grpc_status_error(
    status: GrpcStatus,
    *,
    method: str,
    timeout_seconds: float | None,
) -> Exception:
    """Build the public sanitized status error without retaining a wire cause."""

    try:
        raise_grpc_status(status, method=method, timeout_seconds=timeout_seconds)
    except Exception as error:
        return error


def _default_grpc_loader(
    import_module: Callable[[str], Any] = importlib.import_module,
) -> Any:
    try:
        return import_module("grpc")
    except ImportError:
        raise MissingDependencyError(_ANDROID_GRPC_EXTRA) from None


def _default_protobuf_loader(
    import_module: Callable[[str], Any] = importlib.import_module,
) -> Any:
    try:
        runtime_version = import_module("google.protobuf.runtime_version")
    except ImportError:
        raise MissingDependencyError(_ANDROID_PROTOBUF_EXTRA) from None

    version_error = getattr(runtime_version, "VersionError", ())
    try:
        # Import the exact generated closure used by Notes, so protobuf's
        # generated/runtime compatibility check runs during client open rather
        # than surprising the first Notes call.
        return import_module(_ANDROID_NOTES_PROTO)
    except ImportError:
        raise MissingDependencyError(_ANDROID_PROTOBUF_EXTRA) from None
    except version_error:
        raise MissingDependencyError(_ANDROID_PROTOBUF_EXTRA) from None


class _Serializable(Protocol):
    def SerializeToString(self) -> bytes: ...


def _serialize_message(message: object) -> bytes:
    return cast(_Serializable, message).SerializeToString()


# A callback that, given the current bearer token, returns extra gRPC metadata to
# append for one call (e.g. the Play Books Phenotype experiment header). Kept as a
# generic seam so the transport stays unaware of the Phenotype concern.
MetadataAugmentor = Callable[[str], Awaitable[Sequence[tuple[str, str | bytes]]]]


class AndroidSession(EpochFenced):
    """One lazy gRPC channel plus protocol-neutral call supervision."""

    name = "android"

    def __init__(
        self,
        bearer_provider: BearerProvider,
        call_supervisor: CallSupervisor,
        *,
        timeout: float | None = 30.0,
        rate_limit_max_retries: int = 3,
        server_error_max_retries: int = 3,
        refresh_retry_delay: float = 0.2,
        metrics: ClientMetrics | None = None,
        sleep: Callable[[float], Awaitable[object]] | None = None,
        grpc_loader: Callable[[], Any] = _default_grpc_loader,
        protobuf_loader: Callable[[], Any] = _default_protobuf_loader,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(
            "Android transport call belongs to a retired resource generation",
            assert_loop=True,
        )
        self._bearer_provider = bearer_provider
        self._call_supervisor = call_supervisor
        self._timeout = timeout
        self._rate_limit_max_retries = rate_limit_max_retries
        self._server_error_max_retries = server_error_max_retries
        self._refresh_retry_delay = refresh_retry_delay
        self._metrics = metrics
        self._sleep = sleep
        self._grpc_loader = grpc_loader
        self._protobuf_loader = protobuf_loader
        self._monotonic = monotonic
        self._workflow_session_id = object()
        self._connection_lock: asyncio.Lock | None = None
        self._channel: Any | None = None
        self._callables: dict[tuple[str, str, int, int], Any] = {}

    @property
    def active_epoch(self) -> int | None:
        """Expose the active resource epoch for private assembly tests."""

        return self._active_epoch

    def set_bound_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """Receive the root lifecycle's loop binding before transport open."""

        super().set_bound_loop(loop)

    def reset_after_open(self) -> None:
        """Discard transport-owned lazy state for the next resource generation."""

        self._connection_lock = None
        self._channel = None
        self._callables.clear()

    async def open(self, loop: asyncio.AbstractEventLoop, epoch: int) -> None:
        """Validate dependencies and credentials without opening a channel."""

        if self._bound_loop is not loop:
            raise RuntimeError("Android transport was not bound by the client lifecycle.")
        assert_bound_loop(self._bound_loop)
        # Import validation belongs to asynchronous open for a selected
        # namespace. Channel construction remains lazy until the first call.
        self._grpc_loader()
        self._protobuf_loader()
        self.activate(epoch)
        await self._bearer_provider.activate_for_epoch(epoch)

    async def prepare_close(self) -> None:
        """Fence new channel/token publication before the first await."""

        if self._bound_loop is not None:
            assert_bound_loop(self._bound_loop)
        self.fence()
        await self._bearer_provider.prepare_close()

    async def close_resources(self) -> None:
        """Close the old channel and detach every channel-owned callable."""

        if self._bound_loop is not None:
            assert_bound_loop(self._bound_loop)
        channel = self._channel
        try:
            if channel is not None:
                await channel.close()
        finally:
            if self._channel is channel:
                self._channel = None
            self._callables.clear()
            self._connection_lock = None

    def _require_active(self) -> int:
        epoch = self._active_epoch
        if epoch is None:
            raise RuntimeError(_NOT_OPEN)
        assert_bound_loop(self._bound_loop)
        return epoch

    def _resolve_expected_epoch(self, expected_epoch: int | None) -> int:
        """Resolve explicit or task-local workflow fencing for one call."""

        active_epoch = self._require_active()
        if expected_epoch is None:
            expected_epoch = workflow_epoch_for(self)
        if expected_epoch is None:
            return active_epoch
        if expected_epoch != active_epoch:
            self.assert_epoch(expected_epoch)
        return expected_epoch

    def _deadline(self, timeout: float | None) -> RuntimeDeadline | None:
        resolved = self._timeout if timeout is None else timeout
        return RuntimeDeadline.from_timeout(resolved, monotonic=self._monotonic)

    @staticmethod
    def _telemetry_method(
        method: str,
        telemetry_method: str | None | _DefaultTelemetry,
    ) -> str | None:
        return (
            method if telemetry_method is _DEFAULT_TELEMETRY else cast(str | None, telemetry_method)
        )

    async def _ensure_channel(
        self,
        *,
        expected_epoch: int,
        deadline: RuntimeDeadline | None,
    ) -> Any:
        self.assert_epoch(expected_epoch)
        channel = self._channel
        if channel is not None:
            return channel

        lock = self._connection_lock
        if lock is None:
            lock = asyncio.Lock()
            self._connection_lock = lock
        await await_with_deadline(lock.acquire(), deadline, on_timeout=_DeadlineSignal)
        try:
            self.assert_epoch(expected_epoch)
            channel = self._channel
            if channel is None:
                grpc = self._grpc_loader()
                credentials = grpc.ssl_channel_credentials()
                channel = grpc.aio.secure_channel(
                    ANDROID_GRPC_TARGET,
                    credentials,
                    options=_ANDROID_GRPC_CHANNEL_OPTIONS,
                )
                self.assert_epoch(expected_epoch)
                self._channel = channel
            return channel
        finally:
            lock.release()

    def operation_scope(
        self,
        label: str,
        *,
        expected_epoch: int | None = None,
    ) -> AbstractAsyncContextManager[OperationLease]:
        """Expose the one supervisor-owned workflow admission seam."""

        return self._call_supervisor.operation_scope(
            label,
            expected_epoch=self._resolve_expected_epoch(expected_epoch),
        )

    async def prepare_metadata(
        self,
        metadata_augmentor: MetadataAugmentor,
        *,
        expected_epoch: int,
    ) -> tuple[tuple[str, str | bytes], ...]:
        """Resolve credential-derived metadata before a multi-call write begins."""

        session = self
        failure: BaseException | None = None
        result: tuple[tuple[str, str | bytes], ...] | None = None
        try:
            result = await session._prepare_metadata_impl(
                metadata_augmentor,
                expected_epoch=expected_epoch,
            )
        except BaseException as error:
            failure = sanitize_escaping_exception(error)
        finally:
            del self, session
        if failure is not None:
            raise failure
        assert result is not None
        return result

    async def _prepare_metadata_impl(
        self,
        metadata_augmentor: MetadataAugmentor,
        *,
        expected_epoch: int,
    ) -> tuple[tuple[str, str | bytes], ...]:
        credential: BearerCredential | None = None
        try:
            self.assert_epoch(expected_epoch)
            credential = await self._bearer_provider.get(expected_epoch=expected_epoch)
            self.assert_epoch(expected_epoch)
            metadata = tuple(await metadata_augmentor(credential.token))
            self.assert_epoch(expected_epoch)
            return metadata
        finally:
            del credential

    async def _unary_callable(
        self,
        method: str,
        response_type: type[RespT] | None,
        *,
        expected_epoch: int,
        deadline: RuntimeDeadline | None,
        request_serializer: RequestSerializer[ReqT] | None,
        response_deserializer: ResponseDeserializer[RespT] | None,
    ) -> Any:
        channel = await self._ensure_channel(
            expected_epoch=expected_epoch,
            deadline=deadline,
        )
        serializer = request_serializer or _serialize_message
        if response_deserializer is None:
            if response_type is None:
                raise TypeError("response_type or response_deserializer is required")
            deserializer = cast(
                ResponseDeserializer[RespT],
                cast(Any, response_type).FromString,
            )
            deserializer_key: object = response_type
        else:
            deserializer = response_deserializer
            deserializer_key = response_deserializer
        key = ("unary", method, id(serializer), id(deserializer_key))
        callable_ = self._callables.get(key)
        if callable_ is None:
            callable_ = channel.unary_unary(
                method,
                request_serializer=serializer,
                response_deserializer=deserializer,
            )
            self.assert_epoch(expected_epoch)
            self._callables[key] = callable_
        return callable_

    async def _stream_callable(
        self,
        method: str,
        response_type: type[RespT] | None,
        *,
        expected_epoch: int,
        deadline: RuntimeDeadline | None,
        request_serializer: RequestSerializer[ReqT] | None,
        response_deserializer: ResponseDeserializer[RespT] | None,
    ) -> Any:
        channel = await self._ensure_channel(
            expected_epoch=expected_epoch,
            deadline=deadline,
        )
        serializer = request_serializer or _serialize_message
        if response_deserializer is None:
            if response_type is None:
                raise TypeError("response_type or response_deserializer is required")
            deserializer = cast(
                ResponseDeserializer[RespT],
                cast(Any, response_type).FromString,
            )
            deserializer_key: object = response_type
        else:
            deserializer = response_deserializer
            deserializer_key = response_deserializer
        key = ("stream", method, id(serializer), id(deserializer_key))
        callable_ = self._callables.get(key)
        if callable_ is None:
            callable_ = channel.unary_stream(
                method,
                request_serializer=serializer,
                response_deserializer=deserializer,
            )
            self.assert_epoch(expected_epoch)
            self._callables[key] = callable_
        return callable_

    async def _unary_attempt(
        self,
        lease: CallLease,
        method: str,
        request: ReqT,
        response_type: type[RespT] | None,
        metadata_augmentor: MetadataAugmentor | None = None,
        caller_metadata: Sequence[tuple[str, str | bytes]] = (),
        request_serializer: RequestSerializer[ReqT] | None = None,
        response_deserializer: ResponseDeserializer[RespT] | None = None,
    ) -> _AttemptSuccess[RespT] | _AttemptFailure:
        credential: BearerCredential | None = None
        wire_metadata: tuple[tuple[str, str | bytes], ...] | None = None
        wire_call: Awaitable[RespT] | None = None
        try:
            try:
                credential = await await_with_deadline(
                    self._bearer_provider.get(expected_epoch=lease.epoch),
                    lease.deadline,
                    on_timeout=_DeadlineSignal,
                )
                callable_ = await self._unary_callable(
                    method,
                    response_type,
                    expected_epoch=lease.epoch,
                    deadline=lease.deadline,
                    request_serializer=request_serializer,
                    response_deserializer=response_deserializer,
                )
            except _DeadlineSignal:
                return _AttemptFailure(
                    GrpcStatus("DEADLINE_EXCEEDED", 4),
                    None if credential is None else credential.generation,
                )

            self.assert_epoch(lease.epoch)
            wire_metadata = (("authorization", f"Bearer {credential.token}"),) + tuple(
                caller_metadata
            )
            if metadata_augmentor is not None:
                try:
                    extra = await await_with_deadline(
                        metadata_augmentor(credential.token),
                        lease.deadline,
                        on_timeout=_DeadlineSignal,
                    )
                except _DeadlineSignal:
                    return _AttemptFailure(
                        GrpcStatus("DEADLINE_EXCEEDED", 4),
                        credential.generation,
                    )
                self.assert_epoch(lease.epoch)
                wire_metadata = wire_metadata + tuple(extra)
            try:
                wire_call = callable_(
                    request,
                    metadata=wire_metadata,
                    timeout=None if lease.deadline is None else lease.deadline.remaining(),
                )
                value = await await_with_deadline(
                    wire_call,
                    lease.deadline,
                    on_timeout=_DeadlineSignal,
                )
            except _DeadlineSignal:
                return _AttemptFailure(
                    GrpcStatus("DEADLINE_EXCEEDED", 4),
                    credential.generation,
                )
            except asyncio.CancelledError:
                raise
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as error:
                status = grpc_status(error)
                if status.name == "UNKNOWN":
                    _log_unknown_wire_error(error)
                return _AttemptFailure(
                    status,
                    None if credential is None else credential.generation,
                )
            return _AttemptSuccess(value)
        finally:
            del credential, caller_metadata, wire_metadata, wire_call

    async def unary(
        self,
        method: str,
        request: ReqT,
        *,
        replay_safe: bool,
        operation_variant: str | None = None,
        timeout: float | None = None,
        response_type: type[RespT] | None,
        telemetry_method: str | None | _DefaultTelemetry = _DEFAULT_TELEMETRY,
        expected_epoch: int | None = None,
        metadata_augmentor: MetadataAugmentor | None = None,
        metadata: Sequence[tuple[str, str | bytes]] = (),
        request_serializer: RequestSerializer[ReqT] | None = None,
        response_deserializer: ResponseDeserializer[RespT] | None = None,
        raw_replay: _RawReplayClassification | None = None,
    ) -> RespT:
        """Invoke a unary RPC without retaining this secret owner in failures."""

        policy_replay_safe = _resolve_replay_safe(
            method,
            replay_safe,
            operation_variant,
            raw_replay,
        )
        session = self
        failure: BaseException | None = None
        result: RespT | None = None
        try:
            result = await session._unary_impl(
                method,
                request,
                metadata_augmentor=metadata_augmentor,
                replay_safe=policy_replay_safe,
                timeout=timeout,
                response_type=response_type,
                telemetry_method=telemetry_method,
                expected_epoch=expected_epoch,
                caller_metadata=metadata,
                request_serializer=request_serializer,
                response_deserializer=response_deserializer,
            )
        except BaseException as error:
            failure = sanitize_escaping_exception(error)
        finally:
            del self, session
        if failure is not None:
            raise failure
        return cast(RespT, result)

    async def _unary_impl(
        self,
        method: str,
        request: ReqT,
        *,
        replay_safe: bool,
        timeout: float | None,
        response_type: type[RespT] | None,
        telemetry_method: str | None | _DefaultTelemetry,
        expected_epoch: int | None,
        metadata_augmentor: MetadataAugmentor | None = None,
        caller_metadata: Sequence[tuple[str, str | bytes]] = (),
        request_serializer: RequestSerializer[ReqT] | None = None,
        response_deserializer: ResponseDeserializer[RespT] | None = None,
    ) -> RespT:
        """Invoke one typed unary RPC with bounded replay of safe reads."""

        expected_epoch = self._resolve_expected_epoch(expected_epoch)
        telemetry = self._telemetry_method(method, telemetry_method)
        self._call_supervisor.record_started(telemetry)
        deadline = self._deadline(timeout)
        queue_timed_out = False
        try:
            async with self._call_supervisor.call_scope(
                method,
                telemetry,
                deadline,
                expected_epoch=expected_epoch,
            ) as lease:
                refresh_budget = RefreshBudget()
                rate_limit_retries = 0
                server_error_retries = 0
                while True:
                    outcome = await self._unary_attempt(
                        lease,
                        method,
                        request,
                        response_type,
                        metadata_augmentor,
                        caller_metadata,
                        request_serializer,
                        response_deserializer,
                    )
                    if isinstance(outcome, _AttemptSuccess):
                        return outcome.value
                    error = _grpc_status_error(
                        outcome.status,
                        method=method,
                        timeout_seconds=None if deadline is None else deadline.timeout,
                    )
                    retry_class = _GRPC_RETRY_CLASS.get(outcome.status.code)
                    if is_auth_error(error):
                        retry_class = _RetryClass.AUTH

                    if retry_class is _RetryClass.AUTH:
                        if outcome.bearer_generation is not None:
                            self._bearer_provider.invalidate(outcome.bearer_generation)
                        if replay_safe and refresh_budget.consume():

                            def preserve_terminal_error(
                                _refresh_error: Exception,
                                *,
                                terminal: Exception = error,
                            ) -> BaseException:
                                return terminal

                            await refresh_and_count(
                                refresh=lambda: self._bearer_provider.get(
                                    expected_epoch=lease.epoch
                                ),
                                on_refresh_failure=preserve_terminal_error,
                                sleep=resolve_sleep(self._sleep),
                                refresh_retry_delay=self._refresh_retry_delay,
                                log_label=telemetry or method,
                                logger=retry_logger,
                                metrics=self._metrics,
                                retry_deadline=deadline,
                            )
                            continue
                    elif (
                        retry_class is _RetryClass.RATE_LIMIT
                        and replay_safe
                        and rate_limit_retries < self._rate_limit_max_retries
                    ):
                        await self._wait_before_retry(
                            attempt=rate_limit_retries,
                            deadline=deadline,
                            label=telemetry or method,
                            retry_class=retry_class,
                            terminal_error=error,
                        )
                        rate_limit_retries += 1
                        if self._metrics is not None:
                            self._metrics.increment(rpc_rate_limit_retries=1)
                        continue
                    elif (
                        retry_class is _RetryClass.SERVER
                        and replay_safe
                        and server_error_retries < self._server_error_max_retries
                    ):
                        await self._wait_before_retry(
                            attempt=server_error_retries,
                            deadline=deadline,
                            label=telemetry or method,
                            retry_class=retry_class,
                            terminal_error=error,
                        )
                        server_error_retries += 1
                        if self._metrics is not None:
                            self._metrics.increment(rpc_server_error_retries=1)
                        continue
                    raise error
        except TimeoutError:
            queue_timed_out = True
        if queue_timed_out:
            raise_deadline_exceeded(
                method,
                None if deadline is None else deadline.timeout,
            )
        raise AssertionError("unary call exited without a result")  # pragma: no cover

    async def _wait_before_retry(
        self,
        *,
        attempt: int,
        deadline: RuntimeDeadline | None,
        label: str,
        retry_class: _RetryClass,
        terminal_error: Exception,
    ) -> None:
        delay = max(
            RETRY_BACKOFF_MIN_SECONDS,
            compute_backoff_delay(
                attempt,
                base=RETRY_BACKOFF_BASE_SECONDS,
                cap=RETRY_BACKOFF_CAP_SECONDS,
                jitter_ratio=RETRY_BACKOFF_JITTER_RATIO,
            ),
        )
        if deadline is not None:
            remaining = deadline.remaining()
            if remaining <= 0.0 or delay >= remaining:
                raise terminal_error
        actual_delay = delay if deadline is None else deadline.clamp_sleep(delay)
        retry_logger.warning(
            "%s Android %s error; backing off %.1fs before retry %d",
            label,
            "rate-limit" if retry_class is _RetryClass.RATE_LIMIT else "server",
            actual_delay,
            attempt + 1,
        )
        if actual_delay > 0:
            await resolve_sleep(self._sleep)(actual_delay)

    async def stream(
        self,
        method: str,
        request: ReqT,
        *,
        replay_safe: bool = False,
        operation_variant: str | None = None,
        timeout: float | None = None,
        response_type: type[RespT] | None,
        telemetry_method: str | None | _DefaultTelemetry = _DEFAULT_TELEMETRY,
        max_response_bytes: int | None = None,
        metadata: Sequence[tuple[str, str | bytes]] = (),
        request_serializer: RequestSerializer[ReqT] | None = None,
        response_deserializer: ResponseDeserializer[RespT] | None = None,
        response_sizer: ResponseSizer[RespT] | None = None,
        raw_replay: _RawReplayClassification | None = None,
    ) -> AsyncIterator[RespT]:
        """Yield a stream without retaining this secret owner in failures."""

        # Resolve the method through the same total policy table as unary calls
        # so a new stream cannot bypass classification. Streams remain
        # single-attempt even if a future read-only stream is added: the only
        # currently admitted stream creates a chat turn, and web has no stream
        # auth replay to mirror.
        _resolve_replay_safe(
            method,
            replay_safe,
            operation_variant,
            raw_replay,
        )
        session = self
        iterator = cast(
            AsyncGenerator[RespT, None],
            session._stream_impl(
                method,
                request,
                timeout=timeout,
                response_type=response_type,
                telemetry_method=telemetry_method,
                max_response_bytes=max_response_bytes,
                caller_metadata=metadata,
                request_serializer=request_serializer,
                response_deserializer=response_deserializer,
                response_sizer=response_sizer,
            ),
        )
        failure: BaseException | None = None
        try:
            async for item in iterator:
                yield item
        except BaseException as error:
            failure = sanitize_escaping_exception(error)
        finally:
            try:
                await iterator.aclose()
            except BaseException as error:
                if failure is None:
                    failure = sanitize_escaping_exception(error)
            del self, session, iterator
        if failure is not None:
            raise failure

    async def _stream_impl(
        self,
        method: str,
        request: ReqT,
        *,
        timeout: float | None,
        response_type: type[RespT] | None,
        telemetry_method: str | None | _DefaultTelemetry,
        max_response_bytes: int | None,
        caller_metadata: Sequence[tuple[str, str | bytes]],
        request_serializer: RequestSerializer[ReqT] | None,
        response_deserializer: ResponseDeserializer[RespT] | None,
        response_sizer: ResponseSizer[RespT] | None,
    ) -> AsyncIterator[RespT]:
        """Yield a typed server stream while retaining one supervisor lease."""

        expected_epoch = self._resolve_expected_epoch(None)
        telemetry = self._telemetry_method(method, telemetry_method)
        self._call_supervisor.record_started(telemetry)
        deadline = self._deadline(timeout)
        queue_timed_out = False
        try:
            async with self._call_supervisor.call_scope(
                method,
                telemetry,
                deadline,
                expected_epoch=expected_epoch,
            ) as lease:
                failure: _AttemptFailure | None = None
                credential: BearerCredential | None = None
                wire_metadata: tuple[tuple[str, str | bytes], ...] | None = None
                call: Any | None = None
                iterator: Any | None = None
                exhausted = False
                response_bytes = 0
                try:
                    try:
                        credential = await await_with_deadline(
                            self._bearer_provider.get(expected_epoch=lease.epoch),
                            lease.deadline,
                            on_timeout=_DeadlineSignal,
                        )
                        callable_ = await self._stream_callable(
                            method,
                            response_type,
                            expected_epoch=lease.epoch,
                            deadline=lease.deadline,
                            request_serializer=request_serializer,
                            response_deserializer=response_deserializer,
                        )
                    except _DeadlineSignal:
                        failure = _AttemptFailure(
                            GrpcStatus("DEADLINE_EXCEEDED", 4),
                            None if credential is None else credential.generation,
                        )

                    if failure is None:
                        self.assert_epoch(lease.epoch)
                        assert credential is not None
                        wire_metadata = (("authorization", f"Bearer {credential.token}"),) + tuple(
                            caller_metadata
                        )
                        try:
                            call = callable_(
                                request,
                                metadata=wire_metadata,
                                timeout=(
                                    None if lease.deadline is None else lease.deadline.remaining()
                                ),
                            )
                            iterator = call.__aiter__()
                            while True:
                                try:
                                    item = await await_with_deadline(
                                        iterator.__anext__(),
                                        lease.deadline,
                                        on_timeout=_DeadlineSignal,
                                    )
                                except StopAsyncIteration:
                                    exhausted = True
                                    break
                                if max_response_bytes is not None:
                                    response_bytes += (
                                        response_sizer(item)
                                        if response_sizer is not None
                                        else len(_serialize_message(item))
                                    )
                                    if response_bytes > max_response_bytes:
                                        raise RPCResponseTooLargeError(
                                            f"RPC response exceeded {max_response_bytes} bytes "
                                            f"(read {response_bytes} bytes before aborting)",
                                            limit_bytes=max_response_bytes,
                                            bytes_read=response_bytes,
                                            method_id=method,
                                        )
                                yield item
                        except _DeadlineSignal:
                            failure = _AttemptFailure(
                                GrpcStatus("DEADLINE_EXCEEDED", 4),
                                credential.generation,
                            )
                        except asyncio.CancelledError:
                            raise
                        except (KeyboardInterrupt, SystemExit, GeneratorExit):
                            raise
                        except RPCResponseTooLargeError:
                            raise
                        except Exception as error:
                            status = grpc_status(error)
                            if status.name == "UNKNOWN":
                                _log_unknown_wire_error(error)
                            failure = _AttemptFailure(
                                status,
                                credential.generation,
                            )
                finally:
                    if call is not None and not exhausted:
                        cancel = getattr(call, "cancel", None)
                        if callable(cancel):
                            try:
                                cancel()
                            except Exception:
                                pass
                    del credential, caller_metadata, wire_metadata, call, iterator

                if failure is not None:
                    if (
                        failure.status.name == "UNAUTHENTICATED"
                        and failure.bearer_generation is not None
                    ):
                        self._bearer_provider.invalidate(failure.bearer_generation)
                    raise_grpc_status(
                        failure.status,
                        method=method,
                        timeout_seconds=None if deadline is None else deadline.timeout,
                    )
        except TimeoutError:
            queue_timed_out = True
        if queue_timed_out:
            raise_deadline_exceeded(
                method,
                None if deadline is None else deadline.timeout,
            )


__all__ = ["ANDROID_GRPC_TARGET", "AndroidSession"]
