"""Lazy, supervised gRPC session for the private Android backend."""

from __future__ import annotations

import asyncio
import importlib
import math
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Generic, Protocol, TypeVar, cast

from .._deadline import RuntimeDeadline
from .._loop_affinity import assert_bound_loop
from .._runtime.call_supervisor import CallLease, CallSupervisor
from ..exceptions import MissingDependencyError
from .auth import BearerCredential, BearerProvider
from .errors import (
    GrpcStatus,
    grpc_status,
    raise_deadline_exceeded,
    raise_grpc_status,
    sanitize_escaping_exception,
)

ReqT = TypeVar("ReqT")
RespT = TypeVar("RespT")

ANDROID_GRPC_TARGET = "notebooklm-pa.googleapis.com:443"
_NOT_OPEN = "Client not initialized. Use 'async with' context."
_ANDROID_EXTRA = (
    "Android transport needs grpcio. Install: pip install 'notebooklm-py[android]'"
)


class _DefaultTelemetry(Enum):
    METHOD = auto()


_DEFAULT_TELEMETRY = _DefaultTelemetry.METHOD


class _DeadlineSignal(Exception):
    """Private, data-free signal for an exhausted aggregate deadline."""


@dataclass(frozen=True)
class _AttemptSuccess(Generic[RespT]):
    value: RespT


@dataclass(frozen=True)
class _AttemptFailure:
    status: GrpcStatus
    bearer_generation: int | None


def _default_grpc_loader() -> Any:
    try:
        return importlib.import_module("grpc")
    except ImportError:
        raise MissingDependencyError(_ANDROID_EXTRA) from None


async def _await_with_deadline(
    awaitable: Awaitable[RespT],
    deadline: RuntimeDeadline | None,
) -> RespT:
    if deadline is None:
        return await awaitable
    timed_out = False
    try:
        return await asyncio.wait_for(awaitable, timeout=deadline.remaining())
    except TimeoutError:
        timed_out = True
    if timed_out:  # pragma: no branch - documents the exception translation boundary
        raise _DeadlineSignal
    raise AssertionError("deadline wait exited without a result")  # pragma: no cover


class _Serializable(Protocol):
    def SerializeToString(self) -> bytes: ...


def _serialize_message(message: object) -> bytes:
    return cast(_Serializable, message).SerializeToString()


class AndroidSession:
    """One lazy gRPC channel plus protocol-neutral call supervision."""

    name = "android"

    def __init__(
        self,
        bearer_provider: BearerProvider,
        call_supervisor: CallSupervisor,
        *,
        timeout: float | None = 30.0,
        grpc_loader: Callable[[], Any] = _default_grpc_loader,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bearer_provider = bearer_provider
        self._call_supervisor = call_supervisor
        self._timeout = timeout
        self._grpc_loader = grpc_loader
        self._monotonic = monotonic
        self._bound_loop: asyncio.AbstractEventLoop | None = None
        self._active_epoch: int | None = None
        self._closing = False
        self._connection_lock: asyncio.Lock | None = None
        self._channel: Any | None = None
        self._callables: dict[tuple[str, str, type[Any]], Any] = {}

    @property
    def active_epoch(self) -> int | None:
        """Expose the active resource epoch for private assembly tests."""

        return self._active_epoch

    async def open(self, loop: asyncio.AbstractEventLoop, epoch: int) -> None:
        """Activate credentials and reset lazy transport state without connecting."""

        assert_bound_loop(loop)
        self._bound_loop = loop
        self._active_epoch = epoch
        self._closing = False
        self._connection_lock = None
        self._channel = None
        self._callables.clear()
        await self._bearer_provider.activate(epoch)

    async def prepare_close(self) -> None:
        """Fence new channel/token publication before the first await."""

        if self._bound_loop is not None:
            assert_bound_loop(self._bound_loop)
        self._closing = True
        self._active_epoch = None
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

    def _deadline(self, timeout: float | None) -> RuntimeDeadline | None:
        resolved = self._timeout if timeout is None else timeout
        if resolved is None or not math.isfinite(float(resolved)):
            return None
        return RuntimeDeadline.start(float(resolved), monotonic=self._monotonic)

    @staticmethod
    def _telemetry_method(
        method: str,
        telemetry_method: str | None | _DefaultTelemetry,
    ) -> str | None:
        return method if telemetry_method is _DEFAULT_TELEMETRY else cast(str | None, telemetry_method)

    async def _ensure_channel(
        self,
        *,
        expected_epoch: int,
        deadline: RuntimeDeadline | None,
    ) -> Any:
        self._assert_epoch(expected_epoch)
        channel = self._channel
        if channel is not None:
            return channel

        lock = self._connection_lock
        if lock is None:
            lock = asyncio.Lock()
            self._connection_lock = lock
        await _await_with_deadline(lock.acquire(), deadline)
        try:
            self._assert_epoch(expected_epoch)
            channel = self._channel
            if channel is None:
                grpc = self._grpc_loader()
                credentials = grpc.ssl_channel_credentials()
                channel = grpc.aio.secure_channel(ANDROID_GRPC_TARGET, credentials)
                self._assert_epoch(expected_epoch)
                self._channel = channel
            return channel
        finally:
            lock.release()

    def _assert_epoch(self, expected_epoch: int) -> None:
        assert_bound_loop(self._bound_loop)
        if self._closing or self._active_epoch != expected_epoch:
            raise RuntimeError(
                "Android transport call belongs to a retired resource generation "
                f"(expected={expected_epoch}, active={self._active_epoch})."
            )

    async def _unary_callable(
        self,
        method: str,
        response_type: type[RespT],
        *,
        expected_epoch: int,
        deadline: RuntimeDeadline | None,
    ) -> Any:
        channel = await self._ensure_channel(
            expected_epoch=expected_epoch,
            deadline=deadline,
        )
        key = ("unary", method, response_type)
        callable_ = self._callables.get(key)
        if callable_ is None:
            callable_ = channel.unary_unary(
                method,
                request_serializer=_serialize_message,
                response_deserializer=cast(Any, response_type).FromString,
            )
            self._assert_epoch(expected_epoch)
            self._callables[key] = callable_
        return callable_

    async def _stream_callable(
        self,
        method: str,
        response_type: type[RespT],
        *,
        expected_epoch: int,
        deadline: RuntimeDeadline | None,
    ) -> Any:
        channel = await self._ensure_channel(
            expected_epoch=expected_epoch,
            deadline=deadline,
        )
        key = ("stream", method, response_type)
        callable_ = self._callables.get(key)
        if callable_ is None:
            callable_ = channel.unary_stream(
                method,
                request_serializer=_serialize_message,
                response_deserializer=cast(Any, response_type).FromString,
            )
            self._assert_epoch(expected_epoch)
            self._callables[key] = callable_
        return callable_

    async def _unary_attempt(
        self,
        lease: CallLease,
        method: str,
        request: ReqT,
        response_type: type[RespT],
    ) -> _AttemptSuccess[RespT] | _AttemptFailure:
        credential: BearerCredential | None = None
        metadata: tuple[tuple[str, str], ...] | None = None
        wire_call: Awaitable[RespT] | None = None
        try:
            try:
                credential = await _await_with_deadline(
                    self._bearer_provider.get(expected_epoch=lease.epoch),
                    lease.deadline,
                )
                callable_ = await self._unary_callable(
                    method,
                    response_type,
                    expected_epoch=lease.epoch,
                    deadline=lease.deadline,
                )
            except _DeadlineSignal:
                return _AttemptFailure(
                    GrpcStatus("DEADLINE_EXCEEDED", 4),
                    None if credential is None else credential.generation,
                )

            self._assert_epoch(lease.epoch)
            metadata = (("authorization", f"Bearer {credential.token}"),)
            try:
                wire_call = callable_(
                    request,
                    metadata=metadata,
                    timeout=None if lease.deadline is None else lease.deadline.remaining(),
                )
                value = await _await_with_deadline(wire_call, lease.deadline)
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
                return _AttemptFailure(
                    status,
                    None if credential is None else credential.generation,
                )
            return _AttemptSuccess(value)
        finally:
            del credential, metadata, wire_call

    async def unary(
        self,
        method: str,
        request: ReqT,
        *,
        replay_safe: bool,
        timeout: float | None = None,
        response_type: type[RespT],
        telemetry_method: str | None | _DefaultTelemetry = _DEFAULT_TELEMETRY,
    ) -> RespT:
        """Invoke a unary RPC without retaining this secret owner in failures."""

        session = self
        failure: BaseException | None = None
        result: RespT | None = None
        try:
            result = await session._unary_impl(
                method,
                request,
                replay_safe=replay_safe,
                timeout=timeout,
                response_type=response_type,
                telemetry_method=telemetry_method,
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
        response_type: type[RespT],
        telemetry_method: str | None | _DefaultTelemetry,
    ) -> RespT:
        """Invoke one typed unary RPC, optionally replaying one safe read."""

        expected_epoch = self._require_active()
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
                for attempt in range(2):
                    outcome = await self._unary_attempt(lease, method, request, response_type)
                    if isinstance(outcome, _AttemptSuccess):
                        return outcome.value
                    if outcome.status.name == "UNAUTHENTICATED":
                        if outcome.bearer_generation is not None:
                            self._bearer_provider.invalidate(outcome.bearer_generation)
                        if replay_safe and attempt == 0:
                            continue
                    raise_grpc_status(
                        outcome.status,
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
        raise AssertionError("unary call exited without a result")  # pragma: no cover

    async def stream(
        self,
        method: str,
        request: ReqT,
        *,
        timeout: float | None = None,
        response_type: type[RespT],
        telemetry_method: str | None | _DefaultTelemetry = _DEFAULT_TELEMETRY,
    ) -> AsyncIterator[RespT]:
        """Yield a stream without retaining this secret owner in failures."""

        session = self
        iterator = cast(
            AsyncGenerator[RespT, None],
            session._stream_impl(
                method,
                request,
                timeout=timeout,
                response_type=response_type,
                telemetry_method=telemetry_method,
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
        response_type: type[RespT],
        telemetry_method: str | None | _DefaultTelemetry,
    ) -> AsyncIterator[RespT]:
        """Yield a typed server stream while retaining one supervisor lease."""

        expected_epoch = self._require_active()
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
                metadata: tuple[tuple[str, str], ...] | None = None
                call: Any | None = None
                iterator: Any | None = None
                exhausted = False
                try:
                    try:
                        credential = await _await_with_deadline(
                            self._bearer_provider.get(expected_epoch=lease.epoch),
                            lease.deadline,
                        )
                        callable_ = await self._stream_callable(
                            method,
                            response_type,
                            expected_epoch=lease.epoch,
                            deadline=lease.deadline,
                        )
                    except _DeadlineSignal:
                        failure = _AttemptFailure(
                            GrpcStatus("DEADLINE_EXCEEDED", 4),
                            None if credential is None else credential.generation,
                        )

                    if failure is None:
                        self._assert_epoch(lease.epoch)
                        assert credential is not None
                        metadata = (("authorization", f"Bearer {credential.token}"),)
                        try:
                            call = callable_(
                                request,
                                metadata=metadata,
                                timeout=(
                                    None
                                    if lease.deadline is None
                                    else lease.deadline.remaining()
                                ),
                            )
                            iterator = call.__aiter__()
                            while True:
                                try:
                                    item = await _await_with_deadline(
                                        iterator.__anext__(),
                                        lease.deadline,
                                    )
                                except StopAsyncIteration:
                                    exhausted = True
                                    break
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
                        except Exception as error:
                            failure = _AttemptFailure(
                                grpc_status(error),
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
                    del credential, metadata, call, iterator

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
