"""Android implementation of the public raw gRPC adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from .._idempotency import ReplayGrant, replay_allowed
from ..raw import (
    GrpcMetadata,
    GrpcUnaryMethod,
    GrpcUnaryStreamMethod,
    ReplayPolicy,
    RequestT,
    ResponseT,
    _bytes_size,
    _deserialize_response,
    _identity_bytes,
    _serialize_request,
    _validated_metadata,
)
from .errors import sanitize_async_boundary, sanitize_escaping_exception

_DEFAULT_RAW_STREAM_MAX_RESPONSE_BYTES = 50 * 1024 * 1024


def _raw_replay_grant(policy: ReplayPolicy) -> ReplayGrant:
    return ReplayGrant.REPLAY_SAFE if policy is ReplayPolicy.SAFE_READ else ReplayGrant.NO_REPLAY


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

        safe_metadata = _validated_metadata(metadata)
        wire_request = _serialize_request(method, request)
        replay_safe = replay_allowed(
            None,
            grant=_raw_replay_grant(method.replay_policy),
            disabled=False,
            remaining=None,
        )

        from .session import classify_raw_replay

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
            replay_safe = replay_allowed(
                None,
                grant=_raw_replay_grant(method.replay_policy),
                disabled=False,
                remaining=None,
            )

            from .session import classify_raw_replay

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


# Preserve the 0.x public class identity metadata and pickle path even though
# the implementation now loads only with the Android branch.
AndroidRawAPI.__module__ = "notebooklm.raw"

__all__ = ["AndroidRawAPI"]
