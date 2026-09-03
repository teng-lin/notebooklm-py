"""Advanced, backend-selected raw NotebookLM wire access.

The stable, typed namespaces on :class:`notebooklm.NotebookLMClient` should be
preferred whenever they cover an operation.  This module is the deliberately
lower-level escape hatch for callers investigating a newly recovered method:

* Web clients install :class:`WebRawAPI`, which accepts Web ``RPCMethod`` IDs.
* Android clients install :class:`AndroidRawAPI`, which accepts explicit gRPC
  descriptors and caller-owned protobuf-compatible messages or codecs.

Raw method paths and wire schemas are controlled by Google and are not a
compatibility surface of this package.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, Protocol, TypeVar, cast

from ._android.errors import sanitize_async_boundary, sanitize_escaping_exception
from ._web.raw import WebRawAPI
from .exceptions import DecodingError

__all__ = [
    "AndroidRawAPI",
    "GrpcUnaryMethod",
    "GrpcUnaryStreamMethod",
    "ReplayPolicy",
    "WebRawAPI",
]


RequestT = TypeVar("RequestT")
ResponseT = TypeVar("ResponseT")

RequestSerializer = Callable[[RequestT], bytes]
ResponseDeserializer = Callable[[bytes], ResponseT]
GrpcMetadata = Sequence[tuple[str, str | bytes]]

_METHOD_PATH_RE = re.compile(r"^/[A-Za-z0-9_.]+/[A-Za-z0-9_]+$")
_METADATA_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_LIBRARY_OWNED_METADATA = frozenset(
    {
        "authorization",
        "x-goog-ext-174067345-bin",
        "x-goog-ext-202964622-bin",
    }
)

# Match the ordinary Web RPC response cap.  Callers may select a different
# finite ceiling per call, but an omitted argument can never create an
# unbounded server stream.
_DEFAULT_RAW_STREAM_MAX_RESPONSE_BYTES = 50 * 1024 * 1024


class ReplayPolicy(str, Enum):
    """Whether a raw Android call may be replayed after a transient failure.

    ``NEVER`` is the safe default for an unknown method.  ``SAFE_READ`` must be
    chosen explicitly and should only be used when replaying the method cannot
    create, mutate, or charge for server-side work.
    """

    NEVER = "never"
    SAFE_READ = "safe_read"


def _validate_descriptor(
    *,
    path: str,
    response_type: type[Any] | None,
    request_serializer: Callable[[Any], bytes] | None,
    response_deserializer: Callable[[bytes], Any] | None,
    replay_policy: ReplayPolicy,
) -> None:
    if not isinstance(path, str) or _METHOD_PATH_RE.fullmatch(path) is None:
        raise ValueError(
            "gRPC method path must have the form '/package.Service/Method'; "
            "targets and URLs are not accepted"
        )
    if not isinstance(replay_policy, ReplayPolicy):
        raise TypeError("replay_policy must be a ReplayPolicy")
    if response_type is None and response_deserializer is None:
        raise ValueError("response_type or response_deserializer is required")
    if response_type is not None and response_deserializer is not None:
        raise ValueError("provide response_type or response_deserializer, not both")
    if request_serializer is not None and not callable(request_serializer):
        raise TypeError("request_serializer must be callable")
    if response_deserializer is not None and not callable(response_deserializer):
        raise TypeError("response_deserializer must be callable")
    if response_type is not None and not callable(getattr(response_type, "FromString", None)):
        raise TypeError("response_type must provide a callable FromString(bytes) constructor")


@dataclass(frozen=True)
class GrpcUnaryMethod(Generic[RequestT, ResponseT]):
    """Description of one unary Android gRPC method.

    Request values default to protobuf's ``SerializeToString`` convention.
    Pass ``request_serializer`` for another wire representation.  Responses
    similarly use ``response_type.FromString`` unless an explicit
    ``response_deserializer`` is supplied.
    """

    path: str
    response_type: type[ResponseT] | None = None
    replay_policy: ReplayPolicy = ReplayPolicy.NEVER
    request_serializer: RequestSerializer[RequestT] | None = None
    response_deserializer: ResponseDeserializer[ResponseT] | None = None

    def __post_init__(self) -> None:
        _validate_descriptor(
            path=self.path,
            response_type=self.response_type,
            request_serializer=self.request_serializer,
            response_deserializer=self.response_deserializer,
            replay_policy=self.replay_policy,
        )


@dataclass(frozen=True)
class GrpcUnaryStreamMethod(Generic[RequestT, ResponseT]):
    """Description of one unary-request, server-streaming Android method."""

    path: str
    response_type: type[ResponseT] | None = None
    replay_policy: ReplayPolicy = ReplayPolicy.NEVER
    request_serializer: RequestSerializer[RequestT] | None = None
    response_deserializer: ResponseDeserializer[ResponseT] | None = None

    def __post_init__(self) -> None:
        _validate_descriptor(
            path=self.path,
            response_type=self.response_type,
            request_serializer=self.request_serializer,
            response_deserializer=self.response_deserializer,
            replay_policy=self.replay_policy,
        )


class _ProtobufRequest(Protocol):
    def SerializeToString(self) -> bytes: ...


def _serialize_protobuf(request: object) -> bytes:
    return cast(_ProtobufRequest, request).SerializeToString()


def _identity_bytes(payload: bytes) -> bytes:
    return payload


def _bytes_size(payload: bytes) -> int:
    return len(payload)


def _codec_failure(*, path: str, stage: str, error: BaseException) -> DecodingError:
    sanitize_escaping_exception(error)
    return DecodingError(
        f"Raw Android gRPC {stage} failed.",
        method_id=path,
    )


def _serialize_request(
    method: GrpcUnaryMethod[RequestT, Any] | GrpcUnaryStreamMethod[RequestT, Any],
    request: RequestT,
) -> bytes:
    serializer = method.request_serializer or _serialize_protobuf
    try:
        payload = serializer(request)
        if type(payload) is not bytes:
            raise TypeError("request serializer must return bytes")
        return payload
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except BaseException as error:
        raise _codec_failure(path=method.path, stage="request serialization", error=error) from None


def _deserialize_response(
    method: GrpcUnaryMethod[Any, ResponseT] | GrpcUnaryStreamMethod[Any, ResponseT],
    payload: bytes,
) -> ResponseT:
    deserializer = method.response_deserializer
    if deserializer is None:
        response_type = method.response_type
        assert response_type is not None  # descriptor invariant
        deserializer = cast(ResponseDeserializer[ResponseT], cast(Any, response_type).FromString)
    try:
        return deserializer(payload)
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except BaseException as error:
        raise _codec_failure(
            path=method.path,
            stage="response deserialization",
            error=error,
        ) from None


def _validated_metadata(metadata: GrpcMetadata | None) -> tuple[tuple[str, str | bytes], ...]:
    if metadata is None:
        return ()
    validated: list[tuple[str, str | bytes]] = []
    for item in metadata:
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError("gRPC metadata entries must be (key, value) tuples")
        key, value = item
        if not isinstance(key, str) or _METADATA_KEY_RE.fullmatch(key.lower()) is None:
            raise ValueError("gRPC metadata keys must use valid ASCII metadata syntax")
        normalized = key.lower()
        if normalized in _LIBRARY_OWNED_METADATA:
            raise ValueError(f"caller metadata cannot set library-owned header {normalized!r}")
        if not isinstance(value, (str, bytes)):
            raise TypeError("gRPC metadata values must be str or bytes")
        if normalized.endswith("-bin") and not isinstance(value, bytes):
            raise TypeError("binary gRPC metadata values must be bytes")
        if not normalized.endswith("-bin") and not isinstance(value, str):
            raise TypeError("non-binary gRPC metadata values must be str")
        validated.append((normalized, value))
    return tuple(validated)


class AndroidRawAPI:
    """Raw unary and unary-stream Android calls over one ``AndroidSession``."""

    def __init__(self, session: Any) -> None:
        self._transport = session

    @sanitize_async_boundary
    async def unary(
        self,
        method: GrpcUnaryMethod[RequestT, ResponseT],
        request: RequestT,
        *,
        timeout: float | None = None,
        metadata: GrpcMetadata | None = None,
    ) -> ResponseT:
        """Invoke one raw unary method with an explicit replay classification."""

        # Keep caller-controlled codecs outside grpcio.  grpcio may otherwise
        # collapse a serializer/deserializer exception into an opaque UNKNOWN
        # status before the library can sanitize it.
        safe_metadata = _validated_metadata(metadata)
        wire_request = _serialize_request(method, request)
        replay_safe = method.replay_policy is ReplayPolicy.SAFE_READ

        from ._android.session import classify_raw_replay

        wire_response = await self._transport.unary(
            method.path,
            wire_request,
            replay_safe=replay_safe,
            timeout=timeout,
            response_type=None,
            telemetry_method="raw.unary",
            metadata=safe_metadata,
            request_serializer=_identity_bytes,
            response_deserializer=_identity_bytes,
            raw_replay=classify_raw_replay(replay_safe),
        )
        return _deserialize_response(method, wire_response)

    def unary_stream(
        self,
        method: GrpcUnaryStreamMethod[RequestT, ResponseT],
        request: RequestT,
        *,
        timeout: float | None = None,
        max_response_bytes: int | None = _DEFAULT_RAW_STREAM_MAX_RESPONSE_BYTES,
        metadata: GrpcMetadata | None = None,
    ) -> AsyncIterator[ResponseT]:
        """Yield one bounded raw server stream; streams are never replayed."""

        return self._unary_stream_impl(
            method,
            request,
            timeout=timeout,
            max_response_bytes=max_response_bytes,
            metadata=metadata,
        )

    async def _unary_stream_impl(
        self,
        method: GrpcUnaryStreamMethod[RequestT, ResponseT],
        request: RequestT,
        *,
        timeout: float | None,
        max_response_bytes: int | None,
        metadata: GrpcMetadata | None,
    ) -> AsyncIterator[ResponseT]:
        session = self._transport
        iterator: AsyncIterator[bytes] | None = None
        failure: BaseException | None = None
        safe_metadata: tuple[tuple[str, str | bytes], ...] | None = None
        wire_request: bytes | None = None
        wire_response: bytes | None = None
        try:
            if max_response_bytes is not None and max_response_bytes < 1:
                raise ValueError("max_response_bytes must be >= 1 when supplied")
            safe_metadata = _validated_metadata(metadata)
            wire_request = _serialize_request(method, request)
            replay_safe = method.replay_policy is ReplayPolicy.SAFE_READ

            from ._android.session import classify_raw_replay

            iterator = session.stream(
                method.path,
                wire_request,
                replay_safe=replay_safe,
                timeout=timeout,
                response_type=None,
                telemetry_method="raw.unary_stream",
                max_response_bytes=max_response_bytes,
                metadata=safe_metadata,
                request_serializer=_identity_bytes,
                response_deserializer=_identity_bytes,
                response_sizer=_bytes_size,
                raw_replay=classify_raw_replay(replay_safe),
            )
            async for wire_response in iterator:
                yield _deserialize_response(method, wire_response)
        except BaseException as error:
            failure = sanitize_escaping_exception(error)
        finally:
            if iterator is not None:
                try:
                    close = getattr(iterator, "aclose", None)
                    if close is not None:
                        await close()
                except BaseException as error:
                    if failure is None:
                        failure = sanitize_escaping_exception(error)
            del (
                self,
                session,
                iterator,
                request,
                method,
                metadata,
                safe_metadata,
                wire_request,
                wire_response,
            )
        if failure is not None:
            raise failure from None
