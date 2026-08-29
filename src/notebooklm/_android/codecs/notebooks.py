"""Projection of Android notebook protobuf messages into public types."""

from __future__ import annotations

from datetime import timezone
from typing import Any, cast

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message

from ...exceptions import DecodingError, NotebookNotFoundError, RPCError
from ...types import Notebook, NotebookDescription, SharePermission, SuggestedTopic
from ..proto.google.internal.labs.tailwind.orchestration.v1 import read_pb2

_PROTO = cast(Any, read_pb2)

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


def map_get_project_error(
    notebook_id: str,
    error: RPCError,
    *,
    method_id: str,
) -> RPCError:
    """Map only gRPC NOT_FOUND to the public notebook miss exception."""
    if error.rpc_code != 5:
        return error
    return NotebookNotFoundError(
        notebook_id,
        method_id=method_id,
        raw_response=error.raw_response,
        rpc_code=error.rpc_code,
        found_ids=error.found_ids,
        detail=str(error),
    )


def _decode_project(
    project: Any,
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
        role_name = _enum_name(_PROTO.ProjectRole, project.metadata.user_role)
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


def decode_project(
    project: Any,
    *,
    method_id: str,
) -> Notebook:
    """Decode one project and normalize projection failures to bounded drift."""
    try:
        return _decode_project(project, method_id=method_id)
    except DecodingError:
        raise
    except Exception:
        raise DecodingError(
            "Could not decode Android project response",
            method_id=method_id,
        ) from None


def message_to_known_dict(message: Message, *, method_id: str) -> dict[str, Any]:
    """Return backend-shaped data for fields known to the generated descriptor."""
    try:
        return MessageToDict(message, preserving_proto_field_name=True)
    except Exception:
        raise DecodingError(
            "Could not render Android protobuf response",
            method_id=method_id,
        ) from None


def decode_notebook_guide(response: Any, *, method_id: str) -> NotebookDescription:
    """Project a captured Android guide response into the public description."""

    try:
        if not response.HasField("notebook_guide"):
            return NotebookDescription(summary="", suggested_topics=[])
        guide = response.notebook_guide
        summary = guide.summary.text_summary if guide.HasField("summary") else ""
        topics = []
        if guide.HasField("suggested_topics"):
            topics = [
                SuggestedTopic(question=topic.question, prompt=topic.prompt)
                for topic in guide.suggested_topics.topics
            ]
        return NotebookDescription(summary=summary, suggested_topics=topics)
    except Exception:
        raise DecodingError(
            "Could not decode Android notebook guide response",
            method_id=method_id,
        ) from None


__all__ = [
    "decode_notebook_guide",
    "decode_project",
    "map_get_project_error",
    "message_to_known_dict",
]
