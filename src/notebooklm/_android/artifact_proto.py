"""Lazy protobuf module handles shared by Android artifact adapters."""

from __future__ import annotations

from typing import Any, cast


class _LazyArtifactsProto:
    def __getattr__(self, name: str) -> Any:
        from .proto.google.internal.labs.tailwind.orchestration.v1 import artifacts_pb2

        return getattr(artifacts_pb2, name)


class _LazyReadProto:
    def __getattr__(self, name: str) -> Any:
        from .proto.google.internal.labs.tailwind.orchestration.v1 import read_pb2

        return getattr(read_pb2, name)


def empty_response_type() -> Any:
    from google.protobuf import empty_pb2

    return empty_pb2.Empty


ARTIFACTS_PROTO = cast(Any, _LazyArtifactsProto())
READ_PROTO = cast(Any, _LazyReadProto())

__all__ = ["ARTIFACTS_PROTO", "READ_PROTO", "empty_response_type"]
