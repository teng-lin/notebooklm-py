"""Offline B2 Android notebook mutation and guide contracts."""

from __future__ import annotations

from collections import Counter
from typing import Any, cast

import pytest
from google.protobuf.empty_pb2 import Empty

from notebooklm._android.notebooks import (
    COPY_PROJECT_METHOD,
    CREATE_PROJECT_METHOD,
    DELETE_PROJECTS_METHOD,
    GENERATE_NOTEBOOK_GUIDE_METHOD,
    LIST_RECENT_PROJECTS_METHOD,
    MUTATE_PROJECT_METHOD,
    AndroidNotebooksAPI,
)
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    read_pb2,
)
from notebooklm._android.proto.notebooklm.internal.android.wire.v1 import notebooks_pb2
from notebooklm._android.session import AndroidSession
from notebooklm._android.sources import AndroidSourcesAPI
from notebooklm._android.upload import AndroidUploadPipeline
from notebooklm.exceptions import (
    DecodingError,
    RPCError,
    ServerError,
    UnsupportedOperationError,
    ValidationError,
)
from notebooklm.types import SuggestedTopic


class SequenceTransport:
    """Record calls and return or raise method-specific queued outcomes."""

    def __init__(self, outcomes: dict[str, list[Any]] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        self.calls.append((method, request, kwargs))
        outcome = self.outcomes[method].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _project(project_id: str, title: str) -> read_pb2.Project:
    return read_pb2.Project(id=project_id, title=title)


def _api(transport: SequenceTransport) -> AndroidNotebooksAPI:
    session = cast(AndroidSession, transport)
    upload_pipeline = cast(AndroidUploadPipeline, object())
    return AndroidNotebooksAPI(session, AndroidSourcesAPI(session, upload_pipeline))


def _calls(transport: SequenceTransport, method: str) -> list[tuple[str, Any, dict[str, Any]]]:
    return [call for call in transport.calls if call[0] == method]


@pytest.mark.asyncio
async def test_create_keeps_base_baseline_then_single_send_workflow() -> None:
    transport = SequenceTransport(
        {
            LIST_RECENT_PROJECTS_METHOD: [
                read_pb2.ListRecentlyViewedProjectsResponse(
                    projects=[_project("old", "Existing")]
                )
            ],
            CREATE_PROJECT_METHOD: [_project("new", "Created")],
        }
    )

    created = await _api(transport).create("Created")

    assert created.id == "new"
    assert [method for method, _, _ in transport.calls] == [
        LIST_RECENT_PROJECTS_METHOD,
        CREATE_PROJECT_METHOD,
    ]
    _, request, kwargs = transport.calls[1]
    assert request == notebooks_pb2.WireCreateProjectRequest(name="Created")
    assert kwargs == {"replay_safe": False, "response_type": read_pb2.Project}


@pytest.mark.asyncio
async def test_create_transport_loss_uses_base_probe_without_replaying_send() -> None:
    created = _project("created-by-first-send", "Created")
    transport = SequenceTransport(
        {
            LIST_RECENT_PROJECTS_METHOD: [
                read_pb2.ListRecentlyViewedProjectsResponse(),
                read_pb2.ListRecentlyViewedProjectsResponse(projects=[created]),
            ],
            CREATE_PROJECT_METHOD: [
                ServerError("lost response", method_id=CREATE_PROJECT_METHOD, rpc_code=14)
            ],
        }
    )

    recovered = await _api(transport).create("Created")

    assert recovered.id == "created-by-first-send"
    assert Counter(method for method, _, _ in transport.calls) == {
        LIST_RECENT_PROJECTS_METHOD: 2,
        CREATE_PROJECT_METHOD: 1,
    }


@pytest.mark.asyncio
async def test_delete_sends_one_id_and_never_replays() -> None:
    transport = SequenceTransport({DELETE_PROJECTS_METHOD: [Empty()]})

    assert await _api(transport).delete("notebook-1") is None

    _, request, kwargs = transport.calls[0]
    assert request == notebooks_pb2.WireDeleteProjectsRequest(project_ids=["notebook-1"])
    assert kwargs == {"replay_safe": False, "response_type": Empty}


@pytest.mark.asyncio
async def test_delete_already_absent_is_idempotent_without_replay() -> None:
    transport = SequenceTransport(
        {
            DELETE_PROJECTS_METHOD: [
                RPCError("not found", method_id=DELETE_PROJECTS_METHOD, rpc_code=5)
            ]
        }
    )

    assert await _api(transport).delete("missing") is None
    assert len(transport.calls) == 1
    assert transport.calls[0][2]["replay_safe"] is False


@pytest.mark.asyncio
async def test_delete_non_not_found_failure_propagates_without_replay() -> None:
    failure = RPCError("denied", method_id=DELETE_PROJECTS_METHOD, rpc_code=7)
    transport = SequenceTransport({DELETE_PROJECTS_METHOD: [failure]})

    with pytest.raises(RPCError) as caught:
        await _api(transport).delete("notebook-1")

    assert caught.value is failure
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_title_only_update_decodes_bare_project_without_followup_read() -> None:
    transport = SequenceTransport({MUTATE_PROJECT_METHOD: [_project("notebook-1", "Renamed")]})

    updated = await _api(transport).update("notebook-1", title="Renamed")

    assert updated.title == "Renamed"
    assert len(transport.calls) == 1
    _, request, kwargs = transport.calls[0]
    assert request == notebooks_pb2.WireMutateProjectRequest(
        project_id="notebook-1",
        mutations=[
            notebooks_pb2.WireProjectMutation(
                change_property=notebooks_pb2.WireProjectChangeProperty(new_title="Renamed")
            )
        ],
    )
    assert kwargs == {"replay_safe": False, "response_type": read_pb2.Project}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"emoji": "📘"},
        {"title": "Renamed", "emoji": "📘"},
        {"title": "Renamed", "emoji": ""},
    ],
)
async def test_any_emoji_update_rejects_the_whole_call_before_io(
    kwargs: dict[str, str],
) -> None:
    transport = SequenceTransport()

    with pytest.raises(UnsupportedOperationError, match="emoji"):
        await _api(transport).update("notebook-1", **kwargs)

    assert transport.calls == []


@pytest.mark.asyncio
async def test_empty_update_rejects_before_io() -> None:
    transport = SequenceTransport()

    with pytest.raises(ValidationError, match="At least one"):
        await _api(transport).update("notebook-1")

    assert transport.calls == []


@pytest.mark.asyncio
async def test_copy_validates_then_decodes_one_bare_project_response() -> None:
    transport = SequenceTransport({COPY_PROJECT_METHOD: [_project("copy-1", "Copy")]})
    api = _api(transport)

    copied = await api.copy("source-1", "Copy")

    assert copied.id == "copy-1"
    _, request, kwargs = transport.calls[0]
    assert request == notebooks_pb2.WireCopyProjectRequest(
        source_project_id="source-1",
        title="Copy",
    )
    assert kwargs == {"replay_safe": False, "response_type": read_pb2.Project}

    for notebook_id, title in [("", "Copy"), ("source-1", ""), ("source-1", "  ")]:
        with pytest.raises(ValidationError):
            await api.copy(notebook_id, title)
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_copy_rejects_missing_or_reused_response_identity() -> None:
    transport = SequenceTransport(
        {
            COPY_PROJECT_METHOD: [
                _project("", "Copy"),
                _project("source-1", "Copy"),
            ]
        }
    )
    api = _api(transport)

    with pytest.raises(DecodingError, match="did not contain"):
        await api.copy("source-1", "Copy")
    with pytest.raises(DecodingError, match="reused"):
        await api.copy("source-1", "Copy")


@pytest.mark.asyncio
async def test_copy_lost_response_is_ambiguous_and_never_replayed() -> None:
    transport = SequenceTransport(
        {
            COPY_PROJECT_METHOD: [
                ServerError("lost response", method_id=COPY_PROJECT_METHOD, rpc_code=14)
            ]
        }
    )

    with pytest.raises(RPCError, match="list notebooks.*manually") as caught:
        await _api(transport).copy("source-1", "Copy")

    assert getattr(caught.value, "unconfirmed", False) is True
    assert caught.value.method_id == COPY_PROJECT_METHOD
    assert caught.value.rpc_code == 14
    assert isinstance(caught.value.__cause__, ServerError)
    assert len(transport.calls) == 1
    assert transport.calls[0][2]["replay_safe"] is False


def _guide_response() -> notebooks_pb2.WireGenerateNotebookGuideResponse:
    return notebooks_pb2.WireGenerateNotebookGuideResponse(
        notebook_guide=notebooks_pb2.WireNotebookGuide(
            summary=notebooks_pb2.WireNotebookSummary(text_summary="A summary"),
            suggested_topics=notebooks_pb2.WireSuggestedTopics(
                topics=[
                    notebooks_pb2.WireSuggestedTopic(
                        question="What happened?",
                        prompt="Explain what happened",
                    )
                ]
            ),
        )
    )


@pytest.mark.asyncio
async def test_summary_and_description_each_make_one_nonreplayed_stateful_call() -> None:
    transport = SequenceTransport(
        {GENERATE_NOTEBOOK_GUIDE_METHOD: [_guide_response(), _guide_response()]}
    )
    api = _api(transport)

    assert await api.get_summary("notebook-1") == "A summary"
    description = await api.get_description("notebook-1")

    assert description.summary == "A summary"
    assert description.suggested_topics == [
        SuggestedTopic(question="What happened?", prompt="Explain what happened")
    ]
    for _, request, kwargs in transport.calls:
        assert request == notebooks_pb2.WireGenerateNotebookGuideRequest(project_id="notebook-1")
        assert kwargs == {
            "replay_safe": False,
            "response_type": notebooks_pb2.WireGenerateNotebookGuideResponse,
        }


def test_guide_decode_failure_is_bounded_and_suppresses_raw_cause() -> None:
    from notebooklm._android.codecs.notebooks import decode_notebook_guide

    class BrokenResponse:
        def HasField(self, field: str) -> bool:
            raise ValueError(f"raw guide diagnostic for {field}")

    with pytest.raises(DecodingError, match="Could not decode") as caught:
        decode_notebook_guide(BrokenResponse(), method_id=GENERATE_NOTEBOOK_GUIDE_METHOD)

    assert caught.value.method_id == GENERATE_NOTEBOOK_GUIDE_METHOD
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert "raw guide diagnostic" not in str(caught.value)


@pytest.mark.asyncio
async def test_remaining_notebook_operations_still_reject_before_io() -> None:
    transport = SequenceTransport()
    api = _api(transport)

    for invoke in (
        lambda: api.suggest_prompts("notebook-1"),
        lambda: api.set_emoji("notebook-1", "📘"),
        lambda: api.remove_from_recent("notebook-1"),
    ):
        with pytest.raises(UnsupportedOperationError, match="web backend"):
            await invoke()
    assert transport.calls == []
