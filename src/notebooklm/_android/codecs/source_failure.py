"""Experimental failure diagnostic, without inventing a named protobuf field."""

from __future__ import annotations

from typing import Any


def _field(message: Any, number: int, wire_type: int) -> Any:
    from google.protobuf.unknown_fields import UnknownFieldSet

    fields = [field for field in UnknownFieldSet(message) if field.field_number == number]
    if len(fields) != 1:
        return None
    (diagnostic,) = fields
    return diagnostic.data if diagnostic.wire_type == wire_type else None


def experimental_failure_code(settings: Any) -> int | None:
    """Decode unknown tags 3 → 7 → 1 on ERROR; never guess a schema name.

    The nested shape is corroborated by the web settings capture for #2110.
    Empty preserves unknown protobuf fields without declaring invented fields.
    Wrong wire types, duplicates and malformed nested messages fail closed.
    """
    from google.protobuf.empty_pb2 import Empty
    from google.protobuf.message import DecodeError

    if settings.status != 3:
        return None
    try:
        message = settings
        for number in (3, 7):
            data = _field(message, number, 2)
            if not isinstance(data, bytes):
                return None
            message = Empty()
            message.ParseFromString(data)
        value = _field(message, 1, 0)
    except DecodeError:
        return None
    return value if type(value) is int and value > 0 else None
