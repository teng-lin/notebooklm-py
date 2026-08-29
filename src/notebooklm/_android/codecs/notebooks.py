"""Projection of Android notebook protobuf messages into public types."""

from __future__ import annotations

from datetime import timezone
from typing import Any, cast

from ...exceptions import DecodingError, NotebookNotFoundError, RPCError
from ...types import (
    ChatGoal,
    ChatResponseLength,
    ChatSession,
    ChatSettings,
    Notebook,
    NotebookDescription,
    PremiumFeatureInfo,
    SharePermission,
    SuggestedTopic,
)


def _read_proto() -> Any:
    from ..proto.google.internal.labs.tailwind.orchestration.v1 import read_pb2

    return cast(Any, read_pb2)


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


def validate_project_identity(
    project: Any,
    expected_id: str,
    *,
    method_id: str,
) -> None:
    """Require an exact project identity on a project-bearing response."""
    echoed_id = str(getattr(project, "id", ""))
    if not echoed_id:
        raise DecodingError(
            "Android project response did not contain a notebook id",
            method_id=method_id,
        )
    if echoed_id != expected_id:
        raise DecodingError(
            "Android project response returned an unexpected notebook id",
            method_id=method_id,
        )


def _decode_project(
    project: Any,
    *,
    method_id: str,
    include_chat_settings: bool,
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
        role_name = _enum_name(_read_proto().ProjectRole, project.metadata.user_role)
        role = _PROJECT_ROLE_BY_NAME.get(role_name or "")

    chat_settings = None
    if include_chat_settings:
        fields = project.DESCRIPTOR.fields_by_name
        if "advanced_settings" not in fields or not project.HasField("advanced_settings"):
            chat_settings = ChatSettings(
                goal=ChatGoal.DEFAULT,
                response_length=ChatResponseLength.DEFAULT,
            )
        else:
            settings = project.advanced_settings
            if settings.HasField("goal_settings") and settings.HasField("response_style_settings"):
                try:
                    goal = ChatGoal(int(settings.goal_settings.goal))
                    response_length = ChatResponseLength(
                        int(settings.response_style_settings.response_length)
                    )
                except ValueError:
                    pass
                else:
                    custom_prompt = settings.goal_settings.custom_prompt or None
                    if goal is not ChatGoal.CUSTOM:
                        custom_prompt = None
                    if goal is not ChatGoal.CUSTOM or custom_prompt is not None:
                        chat_settings = ChatSettings(
                            goal=goal,
                            response_length=response_length,
                            custom_prompt=custom_prompt,
                        )

    return Notebook(
        id=project.id,
        title=project.title,
        created_at=created_at,
        sources_count=len(project.sources),
        role=role,
        last_viewed_at=None,
        modified_at=None,
        emoji=project.emoji,
        premium_features=(
            PremiumFeatureInfo(
                can_edit_advanced_settings=(
                    project.premium_feature_info.can_edit_advanced_settings
                    if project.premium_feature_info.HasField("can_edit_advanced_settings")
                    else None
                ),
                can_edit_guidebook_config=(
                    project.premium_feature_info.can_edit_guidebook_config
                    if project.premium_feature_info.HasField("can_edit_guidebook_config")
                    else None
                ),
                can_view_analytics=(
                    project.premium_feature_info.can_view_analytics
                    if project.premium_feature_info.HasField("can_view_analytics")
                    else None
                ),
            )
            if project.HasField("premium_feature_info")
            else None
        ),
        chat_sessions=[
            ChatSession(id=session.chat_session_id)
            for session in project.chat_sessions
            if session.chat_session_id
        ],
        chat_settings=chat_settings,
    )


def decode_project(
    project: Any,
    *,
    method_id: str,
    include_chat_settings: bool = False,
) -> Notebook:
    """Decode one project and normalize projection failures to bounded drift."""
    try:
        return _decode_project(
            project,
            method_id=method_id,
            include_chat_settings=include_chat_settings,
        )
    except DecodingError:
        raise
    except Exception:
        raise DecodingError(
            "Could not decode Android project response",
            method_id=method_id,
        ) from None


def message_to_known_dict(message: Any, *, method_id: str) -> dict[str, Any]:
    """Return backend-shaped data for fields known to the generated descriptor."""
    try:
        from google.protobuf.json_format import MessageToDict

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
    "validate_project_identity",
]
