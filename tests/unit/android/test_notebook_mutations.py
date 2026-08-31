"""Offline notebook Android notebook mutation and guide contracts."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from google.protobuf.empty_pb2 import Empty
from tests._helpers.android_supervisor import SupervisedAndroidTransport

from notebooklm._android.notebooks import (
    COPY_PROJECT_METHOD,
    CREATE_PROJECT_METHOD,
    DELETE_PROJECTS_METHOD,
    GENERATE_NOTEBOOK_GUIDE_METHOD,
    GENERATE_PROMPT_SUGGESTIONS_METHOD,
    LIST_RECENT_PROJECTS_METHOD,
    MUTATE_PROJECT_METHOD,
    REMOVE_RECENTLY_VIEWED_PROJECT_METHOD,
    AndroidNotebooksAPI,
)
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    notebooks_pb2 as exact_notebooks_pb2,
)
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    read_pb2,
)
from notebooklm._android.proto.labs.language.tailwind.common.protos import common_pb2
from notebooklm._android.proto.notebooklm.internal.android.wire.v1 import notebooks_pb2
from notebooklm._android.session import AndroidSession
from notebooklm._notebooks import NotebooksAPI
from notebooklm.exceptions import (
    DecodingError,
    RPCError,
    ServerError,
    ValidationError,
)
from notebooklm.types import ChatSession, Notebook, PromptSuggestion, SuggestedTopic


class SequenceTransport:
    """Record calls and return or raise method-specific queued outcomes."""

    def __init__(self, outcomes: dict[str, list[Any]] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.scopes: list[str] = []

    @asynccontextmanager
    async def operation_scope(self, label: str, **kwargs: Any) -> AsyncIterator[_Lease]:
        assert not kwargs
        self.scopes.append(label)
        yield _Lease()

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        self.calls.append((method, request, kwargs))
        outcome = self.outcomes[method].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@dataclass(frozen=True)
class _Lease:
    epoch: int = 7


def _project(
    project_id: str,
    title: str,
    *,
    emoji: str = "",
    chat_session_id: str | None = None,
) -> read_pb2.Project:
    return read_pb2.Project(
        id=project_id,
        title=title,
        emoji=emoji,
        chat_sessions=(
            [common_pb2.ChatSession(chat_session_id=chat_session_id)]
            if chat_session_id is not None
            else []
        ),
    )


def _api(transport: SequenceTransport) -> AndroidNotebooksAPI:
    session = cast(AndroidSession, transport)

    class EmptySources:
        async def list(self, _notebook_id: str) -> list[Any]:
            return []

    return AndroidNotebooksAPI(session, EmptySources())


def _calls(transport: SequenceTransport, method: str) -> list[tuple[str, Any, dict[str, Any]]]:
    return [call for call in transport.calls if call[0] == method]


@pytest.mark.asyncio
async def test_create_keeps_base_baseline_then_single_send_workflow() -> None:
    transport = SequenceTransport(
        {
            LIST_RECENT_PROJECTS_METHOD: [
                read_pb2.ListRecentlyViewedProjectsResponse(projects=[_project("old", "Existing")])
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
    assert request == exact_notebooks_pb2.CreateProjectRequest(name="Created")
    assert kwargs == {
        "replay_safe": False,
        "response_type": read_pb2.Project,
        "expected_epoch": 7,
    }


@pytest.mark.asyncio
async def test_create_projects_and_volunteers_exact_chat_session_once() -> None:
    transport = SequenceTransport(
        {
            LIST_RECENT_PROJECTS_METHOD: [read_pb2.ListRecentlyViewedProjectsResponse()],
            CREATE_PROJECT_METHOD: [_project("new", "Created", chat_session_id="conversation-1")],
        }
    )
    api = _api(transport)

    created = await api.create("Created")

    assert [session.id for session in created.chat_sessions] == ["conversation-1"]
    assert api._take_created_chat_session_id(created.id) == "conversation-1"
    assert api._take_created_chat_session_id(created.id) is None


@pytest.mark.asyncio
async def test_create_malformed_success_is_unconfirmed_and_never_replayed() -> None:
    transport = SequenceTransport(
        {
            LIST_RECENT_PROJECTS_METHOD: [read_pb2.ListRecentlyViewedProjectsResponse()],
            CREATE_PROJECT_METHOD: [_project("", "Created")],
        }
    )

    with pytest.raises(DecodingError) as raised:
        await _api(transport).create("Created")

    assert getattr(raised.value, "unconfirmed", False) is True
    assert [method for method, _request, _kwargs in transport.calls] == [
        LIST_RECENT_PROJECTS_METHOD,
        CREATE_PROJECT_METHOD,
    ]


def test_created_chat_session_hints_are_bounded_and_refresh_recency() -> None:
    api = _api(SequenceTransport())
    for index in range(260):
        api._remember_created_chat_session(
            Notebook(
                id=f"notebook-{index}",
                title="scratch",
                chat_sessions=[ChatSession(id=f"session-{index}")],
            )
        )

    assert len(api._created_chat_session_ids) == 256
    assert api._take_created_chat_session_id("notebook-0") is None
    assert api._take_created_chat_session_id("notebook-259") == "session-259"


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
async def test_create_workflow_finishes_during_graceful_drain_in_one_epoch() -> None:
    transport = SupervisedAndroidTransport()
    create_started = asyncio.Event()
    create_release = asyncio.Event()
    transport.handlers[LIST_RECENT_PROJECTS_METHOD] = read_pb2.ListRecentlyViewedProjectsResponse()

    async def _create(_request: Any, _kwargs: dict[str, Any]) -> Any:
        create_started.set()
        await create_release.wait()
        return _project("new", "Created")

    transport.handlers[CREATE_PROJECT_METHOD] = _create
    task = asyncio.create_task(_api(cast(Any, transport)).create("Created"))
    await create_started.wait()

    await transport.supervisor.stop_accepting(1)
    create_release.set()

    assert (await task).id == "new"
    assert [kwargs["expected_epoch"] for _method, _request, kwargs in transport.calls] == [1, 1]
    await transport.supervisor.wait_for_idle(1, 0.1)


@pytest.mark.asyncio
async def test_create_probe_cannot_cross_forced_close_and_reopen() -> None:
    transport = SupervisedAndroidTransport()
    create_started = asyncio.Event()
    create_release = asyncio.Event()
    transport.handlers[LIST_RECENT_PROJECTS_METHOD] = read_pb2.ListRecentlyViewedProjectsResponse()

    async def _lost_create(_request: Any, _kwargs: dict[str, Any]) -> Any:
        create_started.set()
        await create_release.wait()
        return ServerError("lost", method_id=CREATE_PROJECT_METHOD, rpc_code=14)

    transport.handlers[CREATE_PROJECT_METHOD] = _lost_create
    task = asyncio.create_task(_api(cast(Any, transport)).create("Created"))
    await create_started.wait()

    old_generation = await transport.force_close_and_reopen()
    create_release.set()

    with pytest.raises(RPCError) as raised:
        await task
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert "retired resource generation" in str(raised.value.__cause__)
    assert [method for method, _request, _kwargs in transport.calls] == [
        LIST_RECENT_PROJECTS_METHOD,
        CREATE_PROJECT_METHOD,
    ]
    assert old_generation.in_flight == 0
    assert transport.supervisor._current is not None
    assert transport.supervisor._current.epoch == 2


@pytest.mark.asyncio
async def test_delete_sends_one_id_and_never_replays() -> None:
    transport = SequenceTransport({DELETE_PROJECTS_METHOD: [Empty()]})

    assert await _api(transport).delete("notebook-1") is None

    _, request, kwargs = transport.calls[0]
    assert request == exact_notebooks_pb2.DeleteProjectsRequest(project_ids=["notebook-1"])
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
async def test_inherited_rename_delegates_to_android_title_update() -> None:
    transport = SequenceTransport(
        {MUTATE_PROJECT_METHOD: [_project("notebook-1", "Renamed via base")]}
    )
    api = _api(transport)

    renamed = await api.rename("notebook-1", "Renamed via base")

    assert AndroidNotebooksAPI.rename is NotebooksAPI.rename
    assert renamed.title == "Renamed via base"
    assert len(transport.calls) == 1
    _, request, kwargs = transport.calls[0]
    assert request == notebooks_pb2.WireMutateProjectRequest(
        project_id="notebook-1",
        mutations=[
            notebooks_pb2.WireProjectMutation(
                change_property=notebooks_pb2.WireProjectChangeProperty(
                    new_title="Renamed via base"
                )
            )
        ],
    )
    assert kwargs == {"replay_safe": False, "response_type": read_pb2.Project}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "response", "expected_change"),
    [
        (
            {"emoji": "📘"},
            _project("notebook-1", "Existing", emoji="📘"),
            notebooks_pb2.WireProjectChangeProperty(new_emoji="📘"),
        ),
        (
            {"title": "Renamed", "emoji": "📘"},
            _project("notebook-1", "Renamed", emoji="📘"),
            notebooks_pb2.WireProjectChangeProperty(new_title="Renamed", new_emoji="📘"),
        ),
        (
            {"title": "Renamed", "emoji": ""},
            _project("notebook-1", "Renamed"),
            notebooks_pb2.WireProjectChangeProperty(new_title="Renamed", new_emoji=""),
        ),
    ],
)
async def test_emoji_update_uses_live_verified_optional_tag_three(
    kwargs: dict[str, str],
    response: read_pb2.Project,
    expected_change: notebooks_pb2.WireProjectChangeProperty,
) -> None:
    transport = SequenceTransport({MUTATE_PROJECT_METHOD: [response]})

    updated = await _api(transport).update("notebook-1", **kwargs)

    assert (updated.title, updated.emoji) == (response.title, response.emoji)
    assert len(transport.calls) == 1
    _, request, call_kwargs = transport.calls[0]
    assert request == notebooks_pb2.WireMutateProjectRequest(
        project_id="notebook-1",
        mutations=[notebooks_pb2.WireProjectMutation(change_property=expected_change)],
    )
    assert request.mutations[0].change_property.HasField("new_emoji")
    assert call_kwargs == {"replay_safe": False, "response_type": read_pb2.Project}


@pytest.mark.asyncio
async def test_inherited_set_emoji_delegates_and_explicit_empty_value_stays_on_wire() -> None:
    transport = SequenceTransport({MUTATE_PROJECT_METHOD: [_project("notebook-1", "Existing")]})
    api = _api(transport)

    updated = await api.set_emoji("notebook-1", "")

    assert AndroidNotebooksAPI.set_emoji is NotebooksAPI.set_emoji
    assert updated.emoji == ""
    request = transport.calls[0][1]
    assert request.mutations[0].change_property.HasField("new_emoji")
    assert (
        request.SerializeToString(deterministic=True).hex()
        == "0a0a6e6f7465626f6f6b2d31120422021a00"
    )


@pytest.mark.asyncio
async def test_empty_update_rejects_before_io() -> None:
    transport = SequenceTransport()

    with pytest.raises(ValidationError, match="At least one"):
        await _api(transport).update("notebook-1")

    assert transport.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        _project("other-notebook", "Renamed"),
        _project("notebook-1", "Stale"),
        _project("notebook-1", "Renamed", emoji="stale"),
    ],
)
async def test_update_rejects_wrong_identity_or_stale_requested_properties(
    response: read_pb2.Project,
) -> None:
    transport = SequenceTransport({MUTATE_PROJECT_METHOD: [response]})

    with pytest.raises(DecodingError, match="unexpected notebook"):
        await _api(transport).update("notebook-1", title="Renamed", emoji="📘")


@pytest.mark.asyncio
async def test_copy_validates_then_decodes_one_bare_project_response() -> None:
    transport = SequenceTransport({COPY_PROJECT_METHOD: [_project("copy-1", "Copy")]})
    api = _api(transport)

    copied = await api.copy("source-1", "Copy")

    assert copied.id == "copy-1"
    _, request, kwargs = transport.calls[0]
    assert isinstance(request, exact_notebooks_pb2.CopyProjectRequest)
    assert request.source_project_id == "source-1"
    assert request.title == "Copy"
    assert request.HasField("request_context")
    assert request.request_context.client_type != 0
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

    with pytest.raises(DecodingError, match="did not contain") as missing:
        await api.copy("source-1", "Copy")
    with pytest.raises(DecodingError, match="reused") as reused:
        await api.copy("source-1", "Copy")
    assert getattr(missing.value, "unconfirmed", False) is True
    assert getattr(reused.value, "unconfirmed", False) is True


@pytest.mark.asyncio
async def test_copy_rejects_a_new_project_with_the_wrong_title() -> None:
    transport = SequenceTransport({COPY_PROJECT_METHOD: [_project("copy-1", "Unrelated")]})

    with pytest.raises(DecodingError, match="unexpected notebook title") as raised:
        await _api(transport).copy("source-1", "Copy")
    assert getattr(raised.value, "unconfirmed", False) is True


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
            summary=exact_notebooks_pb2.NotebookSummary(text_summary="A summary"),
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
        assert request == exact_notebooks_pb2.GenerateNotebookGuideRequest(project_id="notebook-1")
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
async def test_suggest_prompts_uses_all_sources_query_field_six_and_maps_rows() -> None:
    transport = SequenceTransport(
        {
            GENERATE_PROMPT_SUGGESTIONS_METHOD: [
                exact_notebooks_pb2.GeneratePromptSuggestionsResponse(
                    suggestions=[
                        exact_notebooks_pb2.PromptSuggestion(
                            title="\n- First topic ",
                            prompt=" * Ask the first question ",
                        ),
                        exact_notebooks_pb2.PromptSuggestion(
                            title="2026. Outlook",
                            prompt="Compare the evidence",
                        ),
                    ]
                )
            ]
        }
    )
    api = _api(transport)
    source_lister = AsyncMock(return_value=["source-1", "source-2"])
    api.get_source_ids = cast(Any, source_lister)

    suggestions = await api.suggest_prompts("notebook-1", mode=4, query="focus")

    assert suggestions == [
        PromptSuggestion(title="First topic", prompt="Ask the first question"),
        PromptSuggestion(title="2026. Outlook", prompt="Compare the evidence"),
    ]
    source_lister.assert_awaited_once_with("notebook-1")
    _, request, kwargs = transport.calls[0]
    assert isinstance(request, exact_notebooks_pb2.GeneratePromptSuggestionsRequest)
    assert request.project_id == "notebook-1"
    assert [source.id for source in request.source_ids] == ["source-1", "source-2"]
    assert request.config_id == 4
    assert request.query == "focus"
    assert request.HasField("request_context")
    assert kwargs == {
        "replay_safe": True,
        "response_type": exact_notebooks_pb2.GeneratePromptSuggestionsResponse,
    }


@pytest.mark.asyncio
async def test_suggest_prompts_validates_before_io_and_normalizes_blank_query() -> None:
    transport = SequenceTransport(
        {
            GENERATE_PROMPT_SUGGESTIONS_METHOD: [
                exact_notebooks_pb2.GeneratePromptSuggestionsResponse()
            ]
        }
    )
    api = _api(transport)

    for mode in (0, 11):
        with pytest.raises(ValidationError, match="inclusive range 1..10"):
            await api.suggest_prompts("notebook-1", source_ids=[], mode=mode)
    assert transport.calls == []

    assert (
        await api.suggest_prompts(
            "notebook-1",
            source_ids=["source-1"],
            query=" \t ",
        )
        == []
    )
    assert transport.calls[0][1].query == ""


@pytest.mark.asyncio
async def test_remove_from_recent_uses_exact_apk_signature_and_android_context() -> None:
    transport = SequenceTransport({REMOVE_RECENTLY_VIEWED_PROJECT_METHOD: [Empty()]})

    assert await _api(transport).remove_from_recent("notebook-1") is None

    assert len(transport.calls) == 1
    method, request, kwargs = transport.calls[0]
    assert method == REMOVE_RECENTLY_VIEWED_PROJECT_METHOD
    assert request == exact_notebooks_pb2.RemoveRecentlyViewedProjectRequest(
        project_id="notebook-1",
        request_context=request.request_context,
    )
    assert request.request_context.client_type != 0
    assert request.request_context.client_metadata.client_version
    assert kwargs == {"replay_safe": False, "response_type": Empty}


@pytest.mark.asyncio
async def test_remove_from_recent_calls_the_native_route() -> None:
    transport = SequenceTransport({REMOVE_RECENTLY_VIEWED_PROJECT_METHOD: [Empty()]})
    session = cast(AndroidSession, transport)

    class EmptySources:
        async def list(self, _notebook_id: str) -> list[Any]:
            return []

    api = AndroidNotebooksAPI(session, EmptySources())

    assert await api.remove_from_recent("notebook-1") is None

    (method, request, kwargs) = transport.calls[0]
    assert method == REMOVE_RECENTLY_VIEWED_PROJECT_METHOD
    assert request.project_id == "notebook-1"
    assert kwargs["replay_safe"] is False


@pytest.mark.asyncio
async def test_remove_from_recent_treats_internal_as_the_web_no_op() -> None:
    """INTERNAL means the project is owned, i.e. never in the shared-recents list.

    Web returns success and leaves such a project in place, so the postcondition
    already holds; raising here would be a backend-visible parity break.
    """
    error = ServerError(
        "server rejected remove-recent",
        method_id=REMOVE_RECENTLY_VIEWED_PROJECT_METHOD,
        rpc_code=13,
    )
    transport = SequenceTransport({REMOVE_RECENTLY_VIEWED_PROJECT_METHOD: [error]})

    assert await _api(transport).remove_from_recent("notebook-1") is None

    assert len(transport.calls) == 1
    assert transport.calls[0][2]["replay_safe"] is False


@pytest.mark.asyncio
async def test_remove_from_recent_propagates_every_other_status() -> None:
    error = ServerError(
        "backend unavailable",
        method_id=REMOVE_RECENTLY_VIEWED_PROJECT_METHOD,
        rpc_code=14,  # UNAVAILABLE
    )
    transport = SequenceTransport({REMOVE_RECENTLY_VIEWED_PROJECT_METHOD: [error]})

    with pytest.raises(ServerError) as raised:
        await _api(transport).remove_from_recent("notebook-1")

    assert raised.value is error
