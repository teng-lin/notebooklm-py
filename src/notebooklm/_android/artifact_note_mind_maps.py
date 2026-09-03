"""Native note-backed mind-map generation for the Android artifact adapter."""

from __future__ import annotations

import json
from typing import Any, cast

from .._idempotency import call_unconfirmed_on_transport_loss
from .._types.research import MindMapResult
from .artifact_proto import READ_PROTO as _READ_PROTO
from .session import AndroidSession

_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
ACT_ON_SOURCES_METHOD = f"/{_SERVICE}/ActOnSources"


def _chat_proto() -> Any:
    from .proto.google.internal.labs.tailwind.orchestration.v1 import chat_pb2

    return cast(Any, chat_pb2)


def _sources_proto() -> Any:
    from .proto.google.internal.labs.tailwind.orchestration.v1 import sources_pb2

    return cast(Any, sources_pb2)


async def generate_note_backed_mind_map(
    session: AndroidSession,
    notebook_id: str,
    source_ids: list[str],
    *,
    language: str,
    instructions: str | None,
    expected_epoch: int,
) -> MindMapResult:
    """Generate and persist one note-backed map within the caller's transport epoch."""

    from .notes import create_note
    from .upload import android_request_context

    chat_proto = _chat_proto()
    request = chat_proto.ActOnSourcesRequest(
        sources=[
            _sources_proto().InputSource(source_id=_READ_PROTO.SourceId(id=source_id))
            for source_id in source_ids
        ],
        mind_map_action=chat_proto.ActOnSourcesMindMapAction(
            action="interactive_mindmap",
            context=[
                chat_proto.ActOnSourcesMindMapContext(
                    key="[CONTEXT]",
                    value=instructions or "",
                )
            ],
            language=language,
        ),
        request_context=android_request_context(),
    )
    response = await call_unconfirmed_on_transport_loss(
        lambda: session.unary(
            ACT_ON_SOURCES_METHOD,
            request,
            replay_safe=False,
            response_type=chat_proto.ActOnSourcesResponse,
            expected_epoch=expected_epoch,
        ),
        method=ACT_ON_SOURCES_METHOD,
        what="ActOnSources mind-map generation",
        chain=None,
    )
    raw_tree = response.response.response if response.HasField("response") else ""
    if not raw_tree:
        return MindMapResult()
    try:
        mind_map: Any = json.loads(raw_tree)
    except json.JSONDecodeError:
        mind_map = raw_tree

    title = "Mind Map"
    if isinstance(mind_map, dict):
        candidate = mind_map.get("name")
        if isinstance(candidate, str) and candidate:
            title = candidate
    note = await create_note(
        session,
        notebook_id,
        title=title,
        content=raw_tree,
        expected_epoch=expected_epoch,
    )
    return MindMapResult(
        mind_map=mind_map,
        note_id=note.id or None,
        created_at=note.created_at,
    )


__all__ = ["ACT_ON_SOURCES_METHOD", "generate_note_backed_mind_map"]
