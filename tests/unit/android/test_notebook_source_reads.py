"""Offline contract tests for the Android notebook/source read graph."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Collection, Iterator
from pathlib import Path
from typing import Any, cast, get_type_hints

import pytest
from google.protobuf import text_format
from google.protobuf.timestamp_pb2 import Timestamp

from notebooklm._android.codecs.sources import decode_source
from notebooklm._android.notebooks import (
    GET_PROJECT_METHOD,
    LIST_RECENT_PROJECTS_METHOD,
    AndroidNotebooksAPI,
)
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    read_pb2,
)
from notebooklm._android.proto.google.internal.labs.tailwind.v1 import source_settings_pb2
from notebooklm._android.proto.notebooklm.internal.android.wire.v1 import (
    notebooks_pb2 as wire_notebooks_pb2,
)
from notebooklm._android.session import AndroidSession
from notebooklm._android.sources import AndroidSourcesAPI
from notebooklm._android.upload import AndroidUploadPipeline
from notebooklm._notebook_metadata import NotebookSourceLister
from notebooklm._notebooks import NotebooksAPI
from notebooklm._sources import SourcesAPI
from notebooklm.exceptions import (
    DecodingError,
    NotebookNotFoundError,
    RPCError,
    SourceNotFoundError,
)
from notebooklm.types import (
    ChatGoal,
    ChatResponseLength,
    ChatSettings,
    DriveSourceStatus,
    SharePermission,
    SourceStatus,
    SourceType,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "android"

EXPECTED_NOTEBOOK_PUBLIC_CALLABLES = {
    "copy",
    "create",
    "delete",
    "get",
    "get_description",
    "get_metadata",
    "get_or_none",
    "get_raw",
    "get_share_url",
    "get_source_ids",
    "get_summary",
    "list",
    "remove_from_recent",
    "rename",
    "set_emoji",
    "suggest_next_steps",
    "suggest_prompts",
    "update",
}


class FakeSession:
    """Narrow recording fake for AndroidSession.unary()."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.error: Exception | None = None
        self.on_call: Callable[[], None] | None = None

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        self.calls.append((method, request, kwargs))
        if self.on_call is not None:
            self.on_call()
        if self.error is not None:
            raise self.error
        return self.responses[method]


def _android_session(fake: FakeSession) -> AndroidSession:
    return cast(AndroidSession, fake)


def _timestamp(seconds: int, nanos: int = 0) -> Timestamp:
    return Timestamp(seconds=seconds, nanos=nanos)


def _source(
    source_id: str,
    *,
    title: str,
    content_type: int,
    status: int,
    url: str | None = None,
    google_docs_document_id: str | None = None,
    drive_document_id: str | None = None,
    drive_status: int = 0,
    content_mime: str | None = None,
) -> read_pb2.Source:
    metadata = read_pb2.SourceMetadata(original_source_content_type=content_type)
    if url is not None:
        metadata.webpage_metadata.url = url
    if google_docs_document_id is not None:
        metadata.google_docs_metadata.document_id = google_docs_document_id
    if drive_document_id is not None or content_mime is not None:
        metadata.google_drive_source_metadata.document_id = drive_document_id or ""
        metadata.google_drive_source_metadata.mime_type = content_mime or ""
    return read_pb2.Source(
        source_id=read_pb2.SourceId(id=source_id),
        title=title,
        metadata=metadata,
        settings=source_settings_pb2.SourceSettings(
            status=status,
            user_drive_source_status=drive_status,
        ),
    )


def _project(
    project_id: str = "notebook-1",
    *,
    title: str = "Android notebook",
    sources: list[read_pb2.Source] | None = None,
) -> read_pb2.Project:
    return read_pb2.Project(
        id=project_id,
        title=title,
        emoji="",
        metadata=read_pb2.ProjectMetadata(
            user_role=read_pb2.PROJECT_ROLE_OWNER,
            create_time=_timestamp(1_700_000_000, 123_000_000),
        ),
        sources=sources or [],
    )


def _source_fixture() -> list[read_pb2.Source]:
    return [
        _source(
            "url-1",
            title="Website",
            content_type=read_pb2.SOURCE_CONTENT_TYPE_URL,
            status=source_settings_pb2.SOURCE_STATUS_COMPLETE,
            url="https://example.test/article",
        ),
        _source(
            "pdf-1",
            title="Paper",
            content_type=read_pb2.SOURCE_CONTENT_TYPE_PDF,
            status=source_settings_pb2.SOURCE_STATUS_PENDING,
        ),
        _source(
            "tentative-1",
            title="Draft",
            content_type=read_pb2.SOURCE_CONTENT_TYPE_MARKDOWN,
            status=source_settings_pb2.SOURCE_STATUS_TENTATIVE,
        ),
        _source(
            "drive-1",
            title="Drive file",
            content_type=read_pb2.SOURCE_CONTENT_TYPE_DRIVE,
            status=source_settings_pb2.SOURCE_STATUS_COMPLETE,
            drive_document_id="drive-document-1",
            drive_status=source_settings_pb2.DRIVE_SOURCE_STATUS_ACTIVE,
            content_mime="application/vnd.google-apps.document",
        ),
    ]


def _graph(
    project: read_pb2.Project | None = None,
) -> tuple[FakeSession, AndroidSourcesAPI, AndroidNotebooksAPI]:
    selected = project or _project(sources=_source_fixture())
    wire_project = wire_notebooks_pb2.WireProjectWithAdvancedSettings()
    wire_project.ParseFromString(selected.SerializeToString())
    fake = FakeSession(
        {
            GET_PROJECT_METHOD: wire_notebooks_pb2.WireGetProjectResponse(project=wire_project),
            LIST_RECENT_PROJECTS_METHOD: read_pb2.ListRecentlyViewedProjectsResponse(
                projects=[selected]
            ),
        }
    )
    sources = AndroidSourcesAPI(
        _android_session(fake),
        cast(AndroidUploadPipeline, object()),
    )
    notebooks = AndroidNotebooksAPI(_android_session(fake), sources)
    return fake, sources, notebooks


def test_exact_abstract_sets_and_android_adapters_are_concrete() -> None:
    assert NotebooksAPI.__abstractmethods__ == frozenset(
        {
            "_send_create",
            "copy",
            "delete",
            "get",
            "get_description",
            "get_raw",
            "get_source_ids",
            "get_summary",
            "list",
            "remove_from_recent",
            "suggest_next_steps",
            "suggest_prompts",
            "update",
        }
    )
    assert SourcesAPI.__abstractmethods__ == frozenset(
        {
            "add_drive",
            "add_drive_file",
            "add_file",
            "add_play_book",
            "list_play_books",
            "add_text",
            "add_url",
            "add_urls_async",
            "append_text",
            "check_freshness",
            "copy",
            "delete",
            "get_fulltext",
            "get_guide",
            "list",
            "refresh",
            "rename",
            "search",
        }
    )
    assert AndroidNotebooksAPI.__abstractmethods__ == frozenset()
    assert AndroidSourcesAPI.__abstractmethods__ == frozenset()
    assert "_add_urls_batch" in AndroidSourcesAPI.__dict__


def test_notebook_public_callable_manifest_is_exact() -> None:
    for adapter in (NotebooksAPI, AndroidNotebooksAPI):
        assert {
            name
            for name, member in inspect.getmembers(adapter)
            if not name.startswith("_") and callable(member)
        } == EXPECTED_NOTEBOOK_PUBLIC_CALLABLES


def test_direct_graph_requires_and_retains_structural_sources_collaborator() -> None:
    _, sources, notebooks = _graph()
    parameter = inspect.signature(AndroidNotebooksAPI).parameters["sources_api"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.annotation == "NotebookSourceLister"
    assert get_type_hints(AndroidNotebooksAPI.__init__)["sources_api"] is NotebookSourceLister
    assert notebooks._sources is sources


@pytest.mark.asyncio
async def test_notebook_requests_and_projection_are_exact() -> None:
    fake, _, notebooks = _graph()

    listed = await notebooks.list()
    notebook = listed[0]
    assert notebook.id == "notebook-1"
    assert notebook.title == "Android notebook"
    assert notebook.sources_count == 4
    assert notebook.emoji == ""
    assert notebook.created_at is not None
    assert notebook.created_at.isoformat() == "2023-11-14T22:13:20.123000+00:00"
    assert notebook.role is SharePermission.OWNER
    assert notebook.is_owner is True
    assert notebook.last_viewed_at is None
    assert notebook.modified_at is None
    assert notebook.chat_sessions == []
    assert notebook.chat_settings is None
    assert notebook.premium_features is None

    method, request, kwargs = fake.calls[0]
    assert method == LIST_RECENT_PROJECTS_METHOD
    assert request == read_pb2.ListRecentlyViewedProjectsRequest(
        include_own_projects=True,
        include_audio_overview_ids=True,
    )
    assert kwargs == {
        "replay_safe": True,
        "response_type": read_pb2.ListRecentlyViewedProjectsResponse,
    }

    fetched = await notebooks.get("notebook-1")
    assert fetched.id == notebook.id
    assert fetched.title == notebook.title
    assert fetched.chat_settings == ChatSettings(
        goal=ChatGoal.DEFAULT,
        response_length=ChatResponseLength.DEFAULT,
    )
    method, request, kwargs = fake.calls[1]
    assert method == GET_PROJECT_METHOD
    assert request == read_pb2.GetProjectRequest(
        project_id="notebook-1",
        include_audio_overview_ids=True,
    )
    assert kwargs == {
        "replay_safe": True,
        "response_type": wire_notebooks_pb2.WireGetProjectResponse,
    }


@pytest.mark.asyncio
async def test_notebook_get_projects_exact_chat_settings() -> None:
    fake, _, notebooks = _graph()
    response = fake.responses[GET_PROJECT_METHOD]
    response.project.advanced_settings.CopyFrom(
        wire_notebooks_pb2.WireProjectAdvancedSettings(
            goal_settings=wire_notebooks_pb2.WireProjectGoalSettings(
                goal=ChatGoal.CUSTOM.value,
                custom_prompt="Use terse explanations.",
            ),
            response_style_settings=wire_notebooks_pb2.WireProjectResponseStyleSettings(
                response_length=ChatResponseLength.SHORTER.value
            ),
        )
    )

    notebook = await notebooks.get("notebook-1")

    assert notebook.chat_settings == ChatSettings(
        goal=ChatGoal.CUSTOM,
        response_length=ChatResponseLength.SHORTER,
        custom_prompt="Use terse explanations.",
    )


@pytest.mark.asyncio
async def test_notebook_role_mapping_uses_names_and_unknown_degrades_to_none() -> None:
    project = _project()
    project.metadata.user_role = read_pb2.PROJECT_ROLE_WRITER
    _, _, notebooks = _graph(project)
    assert (await notebooks.get(project.id)).role is SharePermission.EDITOR

    unknown_project = _project()
    unknown_project.metadata.user_role = 99
    _, _, unknown_notebooks = _graph(unknown_project)
    assert (await unknown_notebooks.get(unknown_project.id)).role is None


@pytest.mark.asyncio
async def test_notebook_projects_exact_premium_capability_block() -> None:
    project = _project()
    project.premium_feature_info.CopyFrom(
        read_pb2.PremiumFeatureInfo(
            can_edit_advanced_settings=True,
            can_edit_guidebook_config=True,
            can_view_analytics=False,
        )
    )
    _, _, notebooks = _graph(project)

    premium = (await notebooks.get(project.id)).premium_features

    assert premium is not None
    assert premium.can_edit_advanced_settings is True
    assert premium.can_edit_guidebook_config is True
    assert premium.can_view_analytics is False

    partial = _project(project_id="partial-premium")
    partial.premium_feature_info.can_edit_advanced_settings = False
    _, _, partial_notebooks = _graph(partial)
    partial_premium = (await partial_notebooks.get(partial.id)).premium_features
    assert partial_premium is not None
    assert partial_premium.can_edit_advanced_settings is False
    assert partial_premium.can_edit_guidebook_config is None
    assert partial_premium.can_view_analytics is None


@pytest.mark.asyncio
async def test_notebook_required_id_is_never_invented() -> None:
    _, _, notebooks = _graph(_project(project_id=""))
    with pytest.raises(DecodingError, match="did not contain a notebook id") as raised:
        await notebooks.get("requested-id")
    assert raised.value.method_id == GET_PROJECT_METHOD
    assert "Android notebook" not in str(raised.value)


@pytest.mark.asyncio
async def test_get_project_rejects_a_different_echoed_notebook_id() -> None:
    _, sources, notebooks = _graph(_project(project_id="other-notebook"))

    with pytest.raises(DecodingError, match="unexpected notebook id"):
        await notebooks.get("requested-notebook")
    with pytest.raises(DecodingError, match="unexpected notebook id"):
        await sources.list("requested-notebook")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invoke",
    [
        pytest.param(lambda api: api.get("missing-id"), id="get"),
        pytest.param(lambda api: api.get_raw("missing-id"), id="get-raw"),
        pytest.param(lambda api: api.get_source_ids("missing-id"), id="get-source-ids"),
    ],
)
async def test_all_notebook_get_project_reads_translate_grpc_not_found(
    invoke: Callable[[AndroidNotebooksAPI], Awaitable[object]],
) -> None:
    fake, _, notebooks = _graph()
    fake.error = RPCError(
        "sanitized status",
        method_id=GET_PROJECT_METHOD,
        raw_response="bounded-response",
        rpc_code=5,
        found_ids=["other-id"],
    )
    with pytest.raises(NotebookNotFoundError) as raised:
        await invoke(notebooks)
    assert raised.value.notebook_id == "missing-id"
    assert raised.value.rpc_code == 5
    assert raised.value.method_id == GET_PROJECT_METHOD
    assert raised.value.raw_response == "bounded-response"
    assert raised.value.found_ids == ["other-id"]


@pytest.mark.asyncio
async def test_notebook_get_project_reads_preserve_non_not_found_rpc_error() -> None:
    fake, _, notebooks = _graph()

    original = RPCError("permission denied", method_id=GET_PROJECT_METHOD, rpc_code=7)
    fake.error = original
    with pytest.raises(RPCError) as preserved:
        await notebooks.get("forbidden-id")
    assert preserved.value is original


@pytest.mark.asyncio
async def test_source_list_translates_notebook_not_found_and_preserves_other_statuses() -> None:
    fake, sources, _ = _graph()
    fake.error = RPCError("missing", method_id=GET_PROJECT_METHOD, rpc_code=5)
    with pytest.raises(NotebookNotFoundError) as missing:
        await sources.list("missing-notebook")
    assert missing.value.notebook_id == "missing-notebook"
    assert missing.value.rpc_code == 5

    original = RPCError("permission denied", method_id=GET_PROJECT_METHOD, rpc_code=7)
    fake.error = original
    with pytest.raises(RPCError) as preserved:
        await sources.list("forbidden-notebook")
    assert preserved.value is original


@pytest.mark.asyncio
async def test_project_timestamp_failure_is_bounded_decoding_error() -> None:
    project = _project(title="secret title must not escape")
    project.metadata.create_time.seconds = 253_402_300_800
    _, _, notebooks = _graph(project)

    with pytest.raises(DecodingError, match="Could not decode Android project") as raised:
        await notebooks.get(project.id)
    assert raised.value.__cause__ is None
    assert "secret title" not in str(raised.value)


def test_source_projection_failure_is_bounded_decoding_error() -> None:
    class ExplodingSource:
        def HasField(self, field: str) -> bool:
            raise ValueError("secret raw protobuf diagnostic")

    with pytest.raises(DecodingError, match="Could not decode Android source") as raised:
        decode_source(cast(Any, ExplodingSource()), method_id=GET_PROJECT_METHOD, index=7)
    assert raised.value.__cause__ is None
    assert "secret raw" not in str(raised.value)


@pytest.mark.asyncio
async def test_get_or_none_and_share_url_stay_base_concrete() -> None:
    fake, _, notebooks = _graph()
    assert (await notebooks.get_or_none("notebook-1")).id == "notebook-1"  # type: ignore[union-attr]
    fake.error = RPCError("missing", method_id=GET_PROJECT_METHOD, rpc_code=5)
    assert await notebooks.get_or_none("missing") is None
    assert notebooks.get_share_url("id/with spaces").endswith("/notebook/id%2Fwith%20spaces")


@pytest.mark.asyncio
async def test_get_raw_is_known_field_snake_case_message_dict() -> None:
    fake, _, notebooks = _graph()
    response = fake.responses[GET_PROJECT_METHOD]
    response.project.metadata.is_public = True
    response.project.metadata.audio_overview_artifact_ids.append("audio-1")
    response.project.project_tier_limits.max_projects = 100
    response.project.advanced_settings.CopyFrom(
        wire_notebooks_pb2.WireProjectAdvancedSettings(
            goal_settings=wire_notebooks_pb2.WireProjectGoalSettings(
                goal=ChatGoal.LEARNING_GUIDE.value
            ),
            response_style_settings=wire_notebooks_pb2.WireProjectResponseStyleSettings(
                response_length=ChatResponseLength.LONGER.value
            ),
        )
    )
    response.project.sources[0].metadata.expert_intelligence_source_metadata.CopyFrom(
        read_pb2.ExpertIntelligenceSourceMetadata(
            content_id="expert-1",
            title="Expert source",
            authors=["Author"],
        )
    )
    # Retained protobuf unknown fields are intentionally outside the raw API contract.
    response.ParseFromString(response.SerializeToString() + b"\xf8\x07\x01")

    raw = await notebooks.get_raw("notebook-1")

    assert raw["project"]["id"] == "notebook-1"
    assert raw["project"]["metadata"]["user_role"] == "PROJECT_ROLE_OWNER"
    assert raw["project"]["metadata"]["is_public"] is True
    assert raw["project"]["metadata"]["audio_overview_artifact_ids"] == ["audio-1"]
    assert raw["project"]["advanced_settings"]["goal_settings"]["goal"] == 3
    assert raw["project"]["project_tier_limits"]["max_projects"] == 100
    assert (
        raw["project"]["sources"][0]["metadata"]["expert_intelligence_source_metadata"][
            "content_id"
        ]
        == "expert-1"
    )
    assert "premium_feature_info" not in raw["project"]
    assert "127" not in raw


@pytest.mark.asyncio
async def test_get_source_ids_uses_one_read_and_first_duplicate_semantics() -> None:
    first = _source_fixture()[0]
    duplicate = read_pb2.Source()
    duplicate.CopyFrom(first)
    malformed = read_pb2.Source(title="missing id")
    fake, _, notebooks = _graph(_project(sources=[first, duplicate, malformed]))

    assert await notebooks.get_source_ids("notebook-1") == ["url-1"]
    assert [call[0] for call in fake.calls] == [GET_PROJECT_METHOD]


@pytest.mark.asyncio
async def test_source_codec_fixture_projects_status_kind_drive_and_order() -> None:
    fake, sources, _ = _graph()
    decoded = await sources.list("notebook-1")

    assert [source.id for source in decoded] == ["url-1", "pdf-1", "tentative-1", "drive-1"]
    assert [source.kind for source in decoded] == [
        SourceType.WEB_PAGE,
        SourceType.PDF,
        SourceType.MARKDOWN,
        # SOURCE_CONTENT_TYPE_DRIVE was absent from the name map and decoded as
        # UNKNOWN (with a warning). It is code 14, the Drive catch-all.
        SourceType.GOOGLE_DRIVE,
    ]
    assert [source.status for source in decoded] == [
        SourceStatus.READY,
        SourceStatus.PROCESSING,
        SourceStatus.PREPARING,
        SourceStatus.READY,
    ]
    assert decoded[0].url == "https://example.test/article"
    assert decoded[1].url is None
    assert decoded[3].drive_document_id == "drive-document-1"
    assert decoded[3].drive_status is DriveSourceStatus.ACTIVE
    assert decoded[3].content_mime == "application/vnd.google-apps.document"
    assert decoded[3].created_at is None
    assert decoded[3].revision_id is None
    assert decoded[3].download_url is None
    method, request, kwargs = fake.calls[0]
    assert method == GET_PROJECT_METHOD
    assert request == read_pb2.GetProjectRequest(
        project_id="notebook-1",
        include_audio_overview_ids=True,
    )
    assert kwargs == {
        "replay_safe": True,
        "response_type": read_pb2.GetProjectResponse,
    }


@pytest.mark.parametrize(
    ("google_docs_id", "drive_id", "expected"),
    [
        ("docs-id", None, "docs-id"),
        ("docs-id", "descriptor-id", "docs-id"),
        ("", "descriptor-id", "descriptor-id"),
    ],
)
def test_source_codec_prefers_google_docs_id_then_falls_back_to_drive_descriptor(
    google_docs_id: str,
    drive_id: str | None,
    expected: str,
) -> None:
    raw = _source(
        "source-1",
        title="Drive source",
        content_type=read_pb2.SOURCE_CONTENT_TYPE_GOOGLE_DOC,
        status=source_settings_pb2.SOURCE_STATUS_COMPLETE,
        google_docs_document_id=google_docs_id,
        drive_document_id=drive_id,
        content_mime="application/vnd.google-apps.document" if drive_id else None,
    )

    decoded = decode_source(raw, method_id=GET_PROJECT_METHOD)

    assert decoded.drive_document_id == expected
    assert decoded.content_mime == ("application/vnd.google-apps.document" if drive_id else None)


@pytest.mark.asyncio
async def test_checked_in_textproto_fixture_projects_through_both_adapters() -> None:
    response = text_format.Parse(
        (FIXTURES / "get_project_response.textproto").read_text(encoding="utf-8"),
        read_pb2.GetProjectResponse(),
    )
    fake = FakeSession(
        {
            GET_PROJECT_METHOD: response,
            LIST_RECENT_PROJECTS_METHOD: read_pb2.ListRecentlyViewedProjectsResponse(
                projects=[response.project]
            ),
        }
    )
    sources = AndroidSourcesAPI(
        _android_session(fake),
        cast(AndroidUploadPipeline, object()),
    )
    notebooks = AndroidNotebooksAPI(_android_session(fake), sources)

    notebook = await notebooks.get("00000000-0000-4000-8000-000000000000")
    decoded = await sources.list(notebook.id)

    assert notebook.title == "Synthetic read project"
    assert notebook.sources_count == 4
    assert [source.kind for source in decoded] == [
        SourceType.WEB_PAGE,
        SourceType.PDF,
        SourceType.MARKDOWN,
        SourceType.GOOGLE_DOCS,
    ]
    assert decoded[3].status is SourceStatus.ERROR
    assert decoded[3].drive_status is DriveSourceStatus.ACTIVE


@pytest.mark.asyncio
async def test_source_enum_mapping_is_name_based_not_android_integer_copy() -> None:
    sheet = _source(
        "sheet-1",
        title="Sheet",
        content_type=read_pb2.SOURCE_CONTENT_TYPE_GOOGLE_SHEET,
        status=99,
        drive_status=99,
    )
    _, sources, _ = _graph(_project(sources=[sheet]))
    decoded = (await sources.list("notebook-1"))[0]
    assert read_pb2.SOURCE_CONTENT_TYPE_GOOGLE_SHEET == 7
    assert decoded.kind is SourceType.GOOGLE_SPREADSHEET
    # 7 straight through. This used to be remapped to 14 because the public map
    # called 14 GOOGLE_SPREADSHEET; 14 is the Drive catch-all, and both enums
    # agree that 7 is the Sheet.
    assert decoded._type_code == 7
    assert decoded.status is SourceStatus.UNKNOWN
    assert decoded.drive_status is DriveSourceStatus.UNKNOWN


@pytest.mark.asyncio
async def test_source_missing_id_default_skips_with_warning_and_strict_rejects(
    caplog: pytest.LogCaptureFixture,
) -> None:
    malformed = _source(
        "",
        title="sensitive title must not be logged",
        content_type=read_pb2.SOURCE_CONTENT_TYPE_URL,
        status=source_settings_pb2.SOURCE_STATUS_COMPLETE,
    )
    valid = _source_fixture()[0]
    _, sources, _ = _graph(_project(sources=[malformed, valid]))

    with caplog.at_level("WARNING", logger="notebooklm._android.sources"):
        assert [source.id for source in await sources.list("notebook-1")] == ["url-1"]
    assert "index 0" in caplog.text
    assert "sensitive title" not in caplog.text

    with pytest.raises(DecodingError, match="source id at index 0"):
        await sources.list("notebook-1", strict=True)


@pytest.mark.asyncio
async def test_source_duplicates_keep_first_and_strict_only_rejects_conflicts() -> None:
    first = _source_fixture()[0]
    same = read_pb2.Source()
    same.CopyFrom(first)
    conflict = read_pb2.Source()
    conflict.CopyFrom(first)
    conflict.title = "Conflicting title"
    _, sources, _ = _graph(_project(sources=[first, same, conflict]))

    assert [source.title for source in await sources.list("notebook-1")] == ["Website"]

    # Equal duplicates are benign under strict mode; conflict is not.
    _, equal_sources, _ = _graph(_project(sources=[first, same]))
    assert len(await equal_sources.list("notebook-1", strict=True)) == 1
    with pytest.raises(DecodingError, match="Conflicting duplicate"):
        await sources.list("notebook-1", strict=True)


class _MutableStatusFilter(Collection[SourceStatus]):
    def __init__(self) -> None:
        self.values = [SourceStatus.READY]

    def __contains__(self, value: object) -> bool:
        return value in self.values

    def __iter__(self) -> Iterator[SourceStatus]:
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)


@pytest.mark.asyncio
async def test_source_filters_validate_and_snapshot_before_session_entry() -> None:
    fake, sources, _ = _graph()
    with pytest.raises(TypeError, match="statuses must be a collection"):
        await sources.list("notebook-1", statuses=cast(Any, iter([SourceStatus.READY])))
    with pytest.raises(TypeError, match="types must contain only SourceType"):
        await sources.list("notebook-1", types=cast(Any, [SourceType.PDF, "pdf"]))
    assert fake.calls == []

    mutable = _MutableStatusFilter()
    fake.on_call = mutable.values.clear
    filtered = await sources.list("notebook-1", statuses=mutable, types={SourceType.WEB_PAGE})
    assert [source.id for source in filtered] == ["url-1"]


@pytest.mark.asyncio
async def test_source_base_get_get_or_none_and_four_waiters_use_android_reads() -> None:
    _, sources, _ = _graph(_project(sources=[_source_fixture()[0]]))
    assert (await sources.get("notebook-1", "url-1")).id == "url-1"
    assert await sources.get_or_none("notebook-1", "missing") is None
    with pytest.raises(SourceNotFoundError):
        await sources.get("notebook-1", "missing")
    assert (await sources.wait_until_ready("notebook-1", "url-1", timeout=0.1)).id == "url-1"
    assert (await sources.wait_until_registered("notebook-1", "url-1", timeout=0.1)).id == "url-1"
    all_results = await sources.wait_all_until_ready(
        "notebook-1",
        ["url-1"],
        timeout=0.1,
    )
    assert all_results[0].id == "url-1"
    assert [source.id for source in await sources.wait_for_sources("notebook-1", ["url-1"])] == [
        "url-1"
    ]


@pytest.mark.asyncio
async def test_notebook_metadata_composes_exact_android_source_collaborator() -> None:
    _, _, notebooks = _graph()
    metadata = await notebooks.get_metadata("notebook-1")
    assert metadata.notebook.id == "notebook-1"
    assert [(item.kind, item.title, item.url) for item in metadata.sources] == [
        (SourceType.WEB_PAGE, "Website", "https://example.test/article"),
        (SourceType.PDF, "Paper", None),
        (SourceType.MARKDOWN, "Draft", None),
        (SourceType.GOOGLE_DRIVE, "Drive file", None),
    ]


def test_decode_source_populates_expert_intelligence_metadata() -> None:
    """An Android EI source read carries its Play Books provenance (#2292)."""
    metadata = read_pb2.SourceMetadata(
        original_source_content_type=read_pb2.SOURCE_CONTENT_TYPE_EXPERT_INTELLIGENCE,
        expert_intelligence_source_metadata=read_pb2.ExpertIntelligenceSourceMetadata(
            content_id="QhsZEAAAQBAJ",
            title="The Art of War",
            authors=["Sun Tzu"],
            thumbnail_image_url="https://cover",
            description="<p>desc</p>",
            field_type=4.6458335,
        ),
    )
    raw = read_pb2.Source(
        source_id=read_pb2.SourceId(id="ei-1"),
        title="The Art of War",
        metadata=metadata,
        settings=source_settings_pb2.SourceSettings(
            status=source_settings_pb2.SOURCE_STATUS_COMPLETE,
        ),
    )
    decoded = decode_source(raw, method_id=GET_PROJECT_METHOD)
    assert decoded.kind is SourceType.EXPERT_INTELLIGENCE
    ei = decoded.expert_intelligence
    assert ei is not None
    assert ei.content_id == "QhsZEAAAQBAJ"
    assert ei.title == "The Art of War"
    assert ei.authors == ("Sun Tzu",)
    assert ei.thumbnail_image_url == "https://cover"
    assert ei.field_type == pytest.approx(4.6458335)
    # The recovered mobile schema does not carry ContentProvider.
    assert ei.provider is None


def test_decode_source_without_expert_intelligence_leaves_field_none() -> None:
    raw = _source(
        "url-1",
        title="Website",
        content_type=read_pb2.SOURCE_CONTENT_TYPE_URL,
        status=source_settings_pb2.SOURCE_STATUS_COMPLETE,
        url="https://example.test/article",
    )
    assert decode_source(raw, method_id=GET_PROJECT_METHOD).expert_intelligence is None


def test_decode_source_populates_created_at_from_source_added_timestamp() -> None:
    metadata = read_pb2.SourceMetadata(
        original_source_content_type=read_pb2.SOURCE_CONTENT_TYPE_URL,
        webpage_metadata=read_pb2.WebpageMetadata(url="https://example.test/article"),
        source_added_timestamp=Timestamp(seconds=1723890544, nanos=740182000),
    )
    raw = read_pb2.Source(
        source_id=read_pb2.SourceId(id="src-1"),
        title="Article",
        metadata=metadata,
        settings=source_settings_pb2.SourceSettings(
            status=source_settings_pb2.SOURCE_STATUS_COMPLETE,
        ),
    )
    decoded = decode_source(raw, method_id=GET_PROJECT_METHOD)
    assert decoded.created_at is not None
    assert decoded.created_at.isoformat() == "2024-08-17T10:29:04.740182+00:00"


def test_decode_source_without_source_added_timestamp_leaves_created_at_none() -> None:
    metadata = read_pb2.SourceMetadata(
        original_source_content_type=read_pb2.SOURCE_CONTENT_TYPE_URL,
        webpage_metadata=read_pb2.WebpageMetadata(url="https://example.test/article"),
    )
    raw = read_pb2.Source(
        source_id=read_pb2.SourceId(id="src-1"),
        title="Article",
        metadata=metadata,
        settings=source_settings_pb2.SourceSettings(
            status=source_settings_pb2.SOURCE_STATUS_COMPLETE,
        ),
    )
    decoded = decode_source(raw, method_id=GET_PROJECT_METHOD)
    assert decoded.created_at is None
