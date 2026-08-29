"""Strict projections for the Android organization wire overlays."""

from __future__ import annotations

import builtins
import uuid
from typing import Any, cast

from ...exceptions import DecodingError
from ...types import Collection, Label


def _canonical_uuid(value: str, *, field: str, method_id: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError):
        parsed = None
    canonical = None if parsed is None else str(parsed)
    if canonical is None or value.lower() != canonical:
        raise DecodingError(
            f"Android organization response contained a malformed {field}",
            method_id=method_id,
        )
    return canonical


def _decode_source_member(payload: bytes, *, method_id: str) -> str:
    # Import the generated exact type only while decoding a selected Android
    # operation. Importing this codec and constructing adapters stays usable
    # without the optional protobuf runtime.
    try:
        from ..proto.google.internal.labs.tailwind.orchestration.v1 import read_pb2

        value = cast(Any, read_pb2).SourceId.FromString(payload).id
    except Exception:
        raise DecodingError(
            "Android GetLabels returned a malformed source member",
            method_id=method_id,
        ) from None
    return _canonical_uuid(value, field="source member ID", method_id=method_id)


def _decode_notebook_member(payload: bytes, *, method_id: str) -> str:
    try:
        value = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise DecodingError(
            "Android GetLabels returned a malformed notebook member",
            method_id=method_id,
        ) from None
    return _canonical_uuid(value, field="notebook member ID", method_id=method_id)


def decode_labels(
    response: Any,
    notebook_id: str,
    *,
    method_id: str,
) -> builtins.list[Label]:
    """Decode source-label rows, including wrapped exact ``SourceId`` members."""

    result: builtins.list[Label] = []
    for row in response.labels:
        label_id = _canonical_uuid(row.id, field="label ID", method_id=method_id)
        result.append(
            Label(
                id=label_id,
                name=row.name,
                notebook_id=notebook_id,
                emoji=row.emoji or None,
                source_ids=[
                    _decode_source_member(payload, method_id=method_id)
                    for payload in row.member_ids
                ],
            )
        )
    return result


def decode_created_labels(
    response: Any,
    notebook_id: str,
    *,
    method_id: str,
) -> builtins.list[Label]:
    """Decode the exact source-label rows returned by ``CreateLabel``."""

    result: builtins.list[Label] = []
    for row in response.label_and_sources:
        label_id = _canonical_uuid(row.label_id, field="label ID", method_id=method_id)
        result.append(
            Label(
                id=label_id,
                name=row.label,
                notebook_id=notebook_id,
                emoji=row.emoji or None,
                source_ids=[
                    _canonical_uuid(source.id, field="source member ID", method_id=method_id)
                    for source in row.source_ids
                ],
            )
        )
    return result


def decode_created_collections(response: Any, *, method_id: str) -> builtins.list[Collection]:
    """Decode the exact notebook-collection rows returned by ``CreateLabel``."""

    result: builtins.list[Collection] = []
    for row in response.notebook_collections:
        collection_id = _canonical_uuid(row.id, field="collection ID", method_id=method_id)
        result.append(
            Collection(
                id=collection_id,
                name=row.name,
                emoji=row.emoji or None,
                notebook_ids=[
                    _canonical_uuid(
                        notebook_id,
                        field="notebook member ID",
                        method_id=method_id,
                    )
                    for notebook_id in row.notebook_ids
                ],
            )
        )
    return result


def decode_collections(response: Any, *, method_id: str) -> builtins.list[Collection]:
    """Decode collection rows whose member slot contains bare UUID strings."""

    result: builtins.list[Collection] = []
    for row in response.collections:
        collection_id = _canonical_uuid(row.id, field="collection ID", method_id=method_id)
        result.append(
            Collection(
                id=collection_id,
                name=row.name,
                emoji=row.emoji or None,
                notebook_ids=[
                    _decode_notebook_member(payload, method_id=method_id)
                    for payload in row.member_ids
                ],
            )
        )
    return result


__all__ = [
    "decode_collections",
    "decode_created_collections",
    "decode_created_labels",
    "decode_labels",
]
