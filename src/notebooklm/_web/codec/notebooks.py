"""Web notebook response codecs returning transport-neutral records."""

from __future__ import annotations

import logging
import reprlib
import types
from datetime import datetime, timezone
from typing import Any, cast

from ..._backend import BackendError, BackendErrorReason
from ..._binding import CodecPayload
from ..._operations import Operation
from ..._records import (
    NotebookAllocateInput,
    NotebookAllocateResult,
    NotebookChatSessionRecord,
    NotebookChatSettingsRecord,
    NotebookDeleteInput,
    NotebookDeleteResult,
    NotebookDescriptionRecord,
    NotebookGetInput,
    NotebookGetResult,
    NotebookGuideInput,
    NotebookGuideResult,
    NotebookListInput,
    NotebookListResult,
    NotebookPatchInput,
    NotebookPatchResult,
    NotebookPremiumFeaturesRecord,
    NotebookRecord,
    NotebookRemoveRecentInput,
    NotebookRemoveRecentResult,
    SuggestedTopicRecord,
)
from ..._row_adapters.chat import unwrap_chat_settings
from ..._row_adapters.notebooks import ProjectRow
from ...exceptions import DecodingError, UnknownRPCMethodError
from ...rpc import RPCMethod, safe_index
from ...rpc.types import ChatGoal, ChatResponseLength
from .source_ids import decode_notebook_source_ids

logger = logging.getLogger("notebooklm._types.notebooks")

_METHOD_ID = RPCMethod.LIST_NOTEBOOKS.value
_ROLE_LABELS = {1: "owner", 2: "editor", 3: "viewer"}


def encode_notebook_guide(notebook_id: str) -> list[Any]:
    """Encode the live ``GenerateNotebookGuide`` request."""
    return [notebook_id, [2]]


def encode_remove_from_recent(notebook_id: str) -> list[Any]:
    """Encode one idempotent recent-list removal."""
    return [notebook_id]


def encode_list_notebooks() -> list[Any]:
    """Encode the account-scoped ``LIST_NOTEBOOKS`` request."""
    return [None, 1, None, [2]]


def encode_delete_notebook(notebook_id: str) -> list[Any]:
    """Encode one idempotent notebook deletion."""
    return [[notebook_id], [2]]


# Row-facing encoders (P9.3). Each returns the full request payload one codec
# row dispatches — params plus the route and typed options exactly as the P2
# handlers passed them — and never names a method: the row's ``NativeCallSpec``
# is the sole method authority.
def encode_notebook_list(value: NotebookListInput) -> CodecPayload:
    """Payload for the ``notebook.list`` codec row."""
    del value
    return CodecPayload(params=encode_list_notebooks(), source_path="/")


def encode_notebook_get(value: NotebookGetInput) -> CodecPayload:
    """Payload for the ``notebook.get`` codec row."""
    # Local import: ``_notebook_payloads`` reaches ``_source`` and would close an
    # import cycle through ``_web.codec`` at module load.
    from ..._notebook_payloads import build_get_notebook_params

    return CodecPayload(
        params=build_get_notebook_params(value.notebook_id),
        source_path=f"/notebook/{value.notebook_id}",
    )


def encode_notebook_patch(value: NotebookPatchInput) -> CodecPayload:
    """Payload for one title/emoji property-mask mutation."""
    from ..._notebook_payloads import build_update_notebook_params

    return CodecPayload(
        params=build_update_notebook_params(
            value.notebook_id,
            title=value.title,
            emoji=value.emoji,
        ),
        source_path="/",
        allow_null=True,
    )


def encode_notebook_allocate(value: NotebookAllocateInput) -> CodecPayload:
    """Payload for one guarded notebook allocation attempt."""
    from ..._notebook_payloads import build_create_notebook_params

    return CodecPayload(params=build_create_notebook_params(value.title), source_path="/")


def encode_notebook_delete(value: NotebookDeleteInput) -> CodecPayload:
    """Payload for the ``notebook.delete`` codec row."""
    return CodecPayload(params=encode_delete_notebook(value.notebook_id), source_path="/")


def encode_notebook_remove_recent(value: NotebookRemoveRecentInput) -> CodecPayload:
    """Payload for the ``notebook.remove_recent`` codec row (null success accepted)."""
    return CodecPayload(
        params=encode_remove_from_recent(value.notebook_id),
        source_path="/",
        allow_null=True,
    )


def encode_notebook_guide_request(value: NotebookGuideInput) -> CodecPayload:
    """Payload shared by the ``notebook.summarize``/``notebook.describe`` codec rows."""
    return CodecPayload(
        params=encode_notebook_guide(value.notebook_id),
        source_path=f"/notebook/{value.notebook_id}",
    )


def _datetime_from_timestamp(value: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(cast(float, value), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _timestamp(meta: list[Any] | None, position: int, source: str) -> datetime | None:
    if meta is None or len(meta) <= position:
        return None
    block = safe_index(meta, position, method_id=_METHOD_ID, source=source)
    if not isinstance(block, list) or not block:
        return None
    return _datetime_from_timestamp(safe_index(block, 0, method_id=_METHOD_ID, source=source))


def decode_notebook(data: list[Any], *, include_chat_settings: bool = False) -> NotebookRecord:
    """Decode one ``Project`` row without constructing an exported model."""

    project = ProjectRow(data)
    title_slot = (
        safe_index(data, 0, method_id=_METHOD_ID, source="Notebook.title") if data else None
    )
    title = (title_slot if isinstance(title_slot, str) else "").replace("thought\n", "").strip()
    sources = (
        safe_index(data, 1, method_id=_METHOD_ID, source="Notebook.sources_count")
        if len(data) > 1
        else None
    )
    notebook_id = ""
    if len(data) > 2:
        raw_id = safe_index(data, 2, method_id=_METHOD_ID, source="Notebook.id")
        if isinstance(raw_id, str):
            notebook_id = raw_id
        elif raw_id is not None:
            logger.warning(
                "Notebook row id slot malformed — fabricating empty id "
                "(expected str at data[2], got %s; row=%s)",
                type(raw_id).__name__,
                reprlib.repr(data),
            )

    meta_slot = (
        safe_index(data, 5, method_id=_METHOD_ID, source="Notebook.metadata")
        if len(data) > 5
        else None
    )
    meta = meta_slot if isinstance(meta_slot, list) else None
    role = None
    if meta:
        raw_role = safe_index(meta, 0, method_id=_METHOD_ID, source="Notebook.role")
        if isinstance(raw_role, int) and not isinstance(raw_role, bool):
            role = _ROLE_LABELS.get(raw_role)
        if raw_role is not None and role is None:
            logger.warning(
                "Notebook row userRole slot unmapped — reporting unknown role "
                "(expected 1/2/3 at data[5][0], got %r; row=%s)",
                raw_role,
                reprlib.repr(data),
            )

    premium_flags = project.premium_feature_flags
    premium = NotebookPremiumFeaturesRecord(*premium_flags) if premium_flags is not None else None
    chat_settings = None
    if include_chat_settings:
        try:
            settings = unwrap_chat_settings(data, source="Notebook.chat_settings")
            chat_settings = NotebookChatSettingsRecord(
                goal=ChatGoal(settings.goal_code).name.lower(),
                response_length=ChatResponseLength(settings.response_length_code).name.lower(),
                custom_prompt=settings.custom_prompt,
            )
        except (UnknownRPCMethodError, ValueError):
            logger.warning(
                "Notebook row chat-settings slot could not be decoded — reporting unknown "
                "settings (row=%s)",
                reprlib.repr(data),
            )

    return NotebookRecord(
        id=notebook_id,
        title=title,
        created_at=_timestamp(meta, 8, "Notebook.created_at"),
        sources_count=len(sources) if isinstance(sources, list) else 0,
        is_owner=role in (None, "owner"),
        role=role,
        last_viewed_at=_timestamp(meta, 5, "Notebook.last_viewed_at"),
        emoji=project.emoji,
        premium_features=premium,
        chat_sessions=tuple(
            NotebookChatSessionRecord(chat_session_id)
            for chat_session_id in project.chat_session_ids
        ),
        chat_settings=chat_settings,
    )


def _decode_summary(outer: Any) -> str:
    if outer is None:
        return ""
    if isinstance(outer, list) and (
        not outer
        or safe_index(
            outer, 0, method_id=RPCMethod.SUMMARIZE.value, source="_notebooks._extract_summary"
        )
        is None
    ):
        return ""
    value = safe_index(
        outer,
        0,
        0,
        method_id=RPCMethod.SUMMARIZE.value,
        source="_notebooks._extract_summary",
    )
    return "" if value is None else str(value)


def _decode_topics(outer: Any) -> tuple[SuggestedTopicRecord, ...]:
    if not isinstance(outer, list) or len(outer) < 2:
        logger.debug("_extract_suggested_topics: Partial description — no outer[1] slot")
        return ()
    container = safe_index(
        outer,
        1,
        method_id=RPCMethod.SUMMARIZE.value,
        source="_notebooks._extract_suggested_topics",
    )
    if not isinstance(container, list) or not container:
        logger.debug(
            "_extract_suggested_topics: Partial description — outer[1] is empty or non-list"
        )
        return ()
    topics = safe_index(
        container,
        0,
        method_id=RPCMethod.SUMMARIZE.value,
        source="_notebooks._extract_suggested_topics",
    )
    if not isinstance(topics, list):
        if topics is not None:
            logger.debug(
                "_extract_suggested_topics: expected list at outer[1][0], got %s",
                type(topics).__name__,
            )
        return ()
    decoded: list[SuggestedTopicRecord] = []
    for index, topic in enumerate(topics):
        if not isinstance(topic, list) or len(topic) < 2:
            logger.debug(
                "_extract_suggested_topics: skipping malformed topic at index %d (type=%s)",
                index,
                type(topic).__name__,
            )
            continue
        question = safe_index(
            topic,
            0,
            method_id=RPCMethod.SUMMARIZE.value,
            source="_notebooks._extract_suggested_topics",
        )
        prompt = safe_index(
            topic,
            1,
            method_id=RPCMethod.SUMMARIZE.value,
            source="_notebooks._extract_suggested_topics",
        )
        decoded.append(
            SuggestedTopicRecord(
                question=str(question) if question else "",
                prompt=str(prompt) if prompt else "",
            )
        )
    return tuple(decoded)


def decode_notebook_description(result: Any) -> NotebookDescriptionRecord:
    """Decode a ``SUMMARIZE`` guide response into a neutral record."""

    outer = (
        safe_index(
            result,
            0,
            method_id=RPCMethod.SUMMARIZE.value,
            source="NotebooksAPI.get_description",
        )
        if isinstance(result, list) and result
        else None
    )
    return NotebookDescriptionRecord(
        summary=_decode_summary(outer),
        suggested_topics=_decode_topics(outer),
    )


def decode_notebook_list_result(result: Any) -> NotebookListResult:
    """Decode the three ``LIST_NOTEBOOKS`` payload shapes: empty, ``[None]``, ``[[rows]]``."""
    if not result:
        return NotebookListResult(notebooks=())
    if isinstance(result, list):
        raw_notebooks = safe_index(
            result,
            0,
            method_id=RPCMethod.LIST_NOTEBOOKS.value,
            source="codec.notebooks.decode_notebook_list_result",
        )
        if isinstance(raw_notebooks, list):
            return NotebookListResult(
                notebooks=tuple(decode_notebook(row) for row in raw_notebooks)
            )
        if raw_notebooks is None:
            return NotebookListResult(notebooks=())
    raise DecodingError(
        "Unrecognized LIST_NOTEBOOKS payload shape",
        raw_response=reprlib.repr(result),
        method_id=RPCMethod.LIST_NOTEBOOKS.value,
    )


def decode_notebook_list(value: NotebookListInput, result: Any) -> NotebookListResult:
    """Row decoder for ``notebook.list``; the input carries nothing the decode needs."""
    del value
    return decode_notebook_list_result(result)


def _source_ids(value: NotebookGetInput, result: Any) -> tuple[str, ...]:
    """Decode the embedded ids with the diagnostics the caller asked for."""
    return decode_notebook_source_ids(
        result, notebook_id=value.notebook_id, diagnostics=value.source_diagnostics
    )


def decode_notebook_get(value: NotebookGetInput, result: Any) -> NotebookGetResult:
    """Row decoder for ``notebook.get``: the input selects the source-id-only branch."""
    if value.include_raw:
        # Undecoded compatibility branch. ``NotebooksAPI.get_raw`` publishes the
        # payload the transport produced, so nothing positional may run here —
        # any decode would turn a shape this row tolerates into a failure the
        # raw helper never raised.
        return NotebookGetResult(notebook=None, source_ids=(), raw=result)
    if not value.include_notebook:
        return NotebookGetResult(notebook=None, source_ids=_source_ids(value, result))
    source_ids: tuple[str, ...] = ()
    notebook_row = (
        safe_index(
            result,
            0,
            method_id=RPCMethod.GET_NOTEBOOK.value,
            source="codec.notebooks.decode_notebook_get",
        )
        if result and isinstance(result, list)
        else None
    )
    if not notebook_row:
        if value.require_notebook:
            raise BackendError(
                message=f"Notebook not found: {value.notebook_id}",
                operation=Operation.NOTEBOOK_GET,
                diagnostics=types.MappingProxyType(
                    {
                        "notebook_id": value.notebook_id,
                        "method_id": RPCMethod.GET_NOTEBOOK.value,
                    }
                ),
                reason=BackendErrorReason.NOT_FOUND,
            )
        return NotebookGetResult(notebook=None, source_ids=source_ids)
    notebook = decode_notebook(notebook_row, include_chat_settings=True)
    if not notebook.id and not notebook.title:
        if value.require_notebook:
            raise BackendError(
                message=f"Notebook not found: {value.notebook_id}",
                operation=Operation.NOTEBOOK_GET,
                diagnostics=types.MappingProxyType(
                    {
                        "notebook_id": value.notebook_id,
                        "method_id": RPCMethod.GET_NOTEBOOK.value,
                    }
                ),
                reason=BackendErrorReason.NOT_FOUND,
            )
        return NotebookGetResult(notebook=None, source_ids=source_ids)
    return NotebookGetResult(notebook=notebook, source_ids=_source_ids(value, result))


def decode_notebook_patch(value: NotebookPatchInput, result: Any) -> NotebookPatchResult:
    """The mutate acknowledgement carries no semantic value."""
    del value, result
    return NotebookPatchResult()


def decode_notebook_allocate(
    value: NotebookAllocateInput,
    result: Any,
) -> NotebookAllocateResult:
    """Decode the notebook returned by one successful allocation attempt."""
    del value
    return NotebookAllocateResult(decode_notebook(result))


def decode_notebook_delete(value: NotebookDeleteInput, result: Any) -> NotebookDeleteResult:
    """Row decoder for ``notebook.delete``: the acknowledgement carries no signal."""
    del value, result
    return NotebookDeleteResult()


def decode_notebook_remove_recent(
    value: NotebookRemoveRecentInput, result: Any
) -> NotebookRemoveRecentResult:
    """Row decoder for ``notebook.remove_recent``: null or empty success."""
    del value, result
    return NotebookRemoveRecentResult()


def decode_notebook_guide(value: NotebookGuideInput, result: Any) -> NotebookGuideResult:
    """Row decoder shared by ``notebook.summarize`` and ``notebook.describe``."""
    del value
    return NotebookGuideResult(decode_notebook_description(result))


__all__ = [
    "decode_notebook_allocate",
    "decode_notebook",
    "decode_notebook_delete",
    "decode_notebook_description",
    "decode_notebook_get",
    "decode_notebook_guide",
    "decode_notebook_list",
    "decode_notebook_list_result",
    "decode_notebook_patch",
    "decode_notebook_remove_recent",
    "encode_delete_notebook",
    "encode_list_notebooks",
    "encode_notebook_allocate",
    "encode_notebook_delete",
    "encode_notebook_get",
    "encode_notebook_guide",
    "encode_notebook_guide_request",
    "encode_notebook_list",
    "encode_notebook_patch",
    "encode_notebook_remove_recent",
    "encode_remove_from_recent",
]
