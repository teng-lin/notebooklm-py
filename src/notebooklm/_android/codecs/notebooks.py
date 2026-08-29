"""Projection of Android notebook protobuf messages into public types."""

from __future__ import annotations

from datetime import timezone
from typing import Any

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message

from ...exceptions import DecodingError
from ...types import Notebook, SharePermission
from ..proto.google.internal.labs.tailwind.orchestration.v1 import b1_read_pb2

_PROJECT_ROLE_BY_NAME: dict[str, SharePermission] = {
    "PROJECT_ROLE_OWNER": SharePermission.OWNER,
    "PROJECT_ROLE_WRITER": SharePermission.EDITOR,
    "PROJECT_ROLE_READER": SharePermission.VIEWER,
}


def _enum_name(enum: Any, value: int) -> str | None:
    """Return an enum symbol without trusting backend-specific integer parity."""
    try:
        return str(enum.Name(value))
    except ValueError:
        return None


def decode_project(
    project: b1_read_pb2.Project,
    *,
    method_id: str,
) -> Notebook:
    """Decode one Android ``Project`` without inventing required identity."""
    if not project.id:
        raise DecodingError(
            "Android project response did not contain a notebook id",
            method_id=method_id,
        )

    created_at = None
    role = None
    if project.HasField("metadata"):
        if project.metadata.HasField("create_time"):
            created_at = project.metadata.create_time.ToDatetime(tzinfo=timezone.utc)
        role_name = _enum_name(b1_read_pb2.ProjectRole, project.metadata.user_role)
        role = _PROJECT_ROLE_BY_NAME.get(role_name or "")

    return Notebook(
        id=project.id,
        title=project.title,
        created_at=created_at,
        sources_count=len(project.sources),
        role=role,
        last_viewed_at=None,
        modified_at=None,
        emoji=project.emoji,
        # Project #10 remains an exact-package schema gap in B1. The flattened
        # recovered schema alone cannot admit it into the generated closure.
        premium_features=None,
        chat_sessions=[],
        chat_settings=None,
    )


def message_to_known_dict(message: Message) -> dict[str, Any]:
    """Return backend-shaped data for fields known to the generated descriptor."""
    return MessageToDict(message, preserving_proto_field_name=True)


__all__ = ["decode_project", "message_to_known_dict"]
