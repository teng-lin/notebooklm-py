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


class _LazyArtifactWireProto:
    def __getattr__(self, name: str) -> Any:
        from .proto.notebooklm.internal.android.wire.v1 import artifacts_pb2

        return getattr(artifacts_pb2, name)


def empty_response_type() -> Any:
    from google.protobuf import empty_pb2

    return empty_pb2.Empty


def table_artifact_projection(message: Any) -> Any | None:
    """Decode the live table branch from an exact Artifact's unknown-field set."""

    projection = ARTIFACT_WIRE_PROTO.WireArtifactTableProjection()
    projection.ParseFromString(message.SerializeToString())
    return projection.table if projection.HasField("table") else None


ARTIFACTS_PROTO = cast(Any, _LazyArtifactsProto())
READ_PROTO = cast(Any, _LazyReadProto())
ARTIFACT_WIRE_PROTO = cast(Any, _LazyArtifactWireProto())

__all__ = [
    "ARTIFACTS_PROTO",
    "ARTIFACT_WIRE_PROTO",
    "READ_PROTO",
    "empty_response_type",
    "table_artifact_projection",
]
