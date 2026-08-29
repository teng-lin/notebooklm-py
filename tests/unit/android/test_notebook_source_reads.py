"""Offline contract tests for the B1 Android notebook/source read graph."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Collection, Iterator
from typing import Any, cast

import pytest
from google.protobuf.timestamp_pb2 import Timestamp

from notebooklm._android.notebooks import (
    GET_PROJECT_METHOD,
    LIST_RECENT_PROJECTS_METHOD,
    AndroidNotebooksAPI,
)
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    b1_read_pb2,
)
from notebooklm._android.proto.google.internal.labs.tailwind.v1 import source_settings_pb2
from notebooklm._android.session import AndroidSession
from notebooklm._android.sources import AndroidSourcesAPI
from notebooklm._notebooks import NotebooksAPI
from notebooklm._sources import SourcesAPI
from notebooklm.exceptions import (
    DecodingError,
    NotebookNotFoundError,
    RPCError,
    SourceNotFoundError,
    UnsupportedOperationError,
)
from notebooklm.types import (
    DriveSourceStatus,
    SharePermission,
    SourceStatus,
    SourceType,
)


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
    drive_document_id: str | None = None,
    drive_status: int = 0,
    content_mime: str | None = None,
) -> b1_read_pb2.Source:
    metadata = b1_read_pb2.SourceMetadata(original_source_content_type=content_type)
    if url is not None:
        metadata.webpage_metadata.url = url
    if drive_document_id is not None or content_mime is not None:
        metadata.google_drive_source_metadata.document_id = drive_document_id or ""
        metadata.google_drive_source_metadata.mime_type = content_mime or ""
    return b1_read_pb2.Source(
        source_id=b1_read_pb2.SourceId(id=source_id),
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
    sources: list[b1_read_pb2.Source] | None = None,
) -> b1_read_pb2.Project:
    return b1_read_pb2.Project(
        id=project_id,
        title=title,
        emoji="",
        metadata=b1_read_pb2.ProjectMetadata(
            user_role=b1_read_pb2.PROJECT_ROLE_OWNER,
            create_time=_timestamp(1_700_000_000, 123_000_000),
        ),
        sources=sources or [],
    )


def _source_fixture() -> list[b1_read_pb2.Source]:
    return [
        _source(
            "url-1",
            title="Website",
            content_type=b1_read_pb2.SOURCE_CONTENT_TYPE_URL,
            status=source_settings_pb2.SOURCE_STATUS_COMPLETE,
            url="https://example.test/article",
        ),
        _source(
            "pdf-1",
            title="Paper",
            content_type=b1_read_pb2.SOURCE_CONTENT_TYPE_PDF,
            status=source_settings_pb2.SOURCE_STATUS_PENDING,
        ),
        _source(
            "tentative-1",
            title="Draft",
            content_type=b1_read_pb2.SOURCE_CONTENT_TYPE_MARKDOWN,
            status=source_settings_pb2.SOURCE_STATUS_TENTATIVE,
        ),
        _source(
            "drive-1",
            title="Drive file",
            content_type=b1_read_pb2.SOURCE_CONTENT_TYPE_DRIVE,
            status=source_settings_pb2.SOURCE_STATUS_COMPLETE,
            drive_document_id="drive-document-1",
            drive_status=source_settings_pb2.DRIVE_SOURCE_STATUS_ACTIVE,
            content_mime="application/vnd.google-apps.document",
        ),
    ]


def _graph(
    project: b1_read_pb2.Project | None = None,
) -> tuple[FakeSession, AndroidSourcesAPI, AndroidNotebooksAPI]:
    selected = project or _project(sources=_source_fixture())
    fake = FakeSession(
        {
            GET_PROJECT_METHOD: b1_read_pb2.GetProjectResponse(project=selected),
            LIST_RECENT_PROJECTS_METHOD: b1_read_pb2.ListRecentlyViewedProjectsResponse(
                projects=[selected]
            ),
        }
    )
    sources = AndroidSourcesAPI(_android_session(fake))
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
            "suggest_prompts",
            "update",
        }
    )
    assert SourcesAPI.__abstractmethods__ == frozenset(
        {
            "add_drive",
            "add_drive_file",
            "add_file",
            "add_text",
            "add_url",
            "check_freshness",
            "delete",
            "get_fulltext",
            "get_guide",
            "list",
            "refresh",
            "rename",
        }
    )
    assert AndroidNotebooksAPI.__abstractmethods__ == frozenset()
    assert AndroidSourcesAPI.__abstractmethods__ == frozenset()
    assert "_add_urls_batch" in AndroidSourcesAPI.__dict__


def test_direct_graph_requires_and_retains_exact_sources_collaborator() -> None:
    _, sources, notebooks = _graph()
    assert (
        inspect.signature(AndroidNotebooksAPI).parameters["sources_api"].default
        is inspect.Parameter.empty
    )
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
    assert request == b1_read_pb2.ListRecentlyViewedProjectsRequest(
        include_own_projects=True,
        include_audio_overview_ids=True,
    )
    assert kwargs == {
        "replay_safe": True,
        "response_type": b1_read_pb2.ListRecentlyViewedProjectsResponse,
    }

    assert await notebooks.get("notebook-1") == notebook
    method, request, kwargs = fake.calls[1]
    assert method == GET_PROJECT_METHOD
    assert request == b1_read_pb2.GetProjectRequest(
        project_id="notebook-1",
        include_audio_overview_ids=True,
    )
    assert kwargs == {
        "replay_safe": True,
        "response_type": b1_read_pb2.GetProjectResponse,
    }


@pytest.mark.asyncio
async def test_notebook_role_mapping_uses_names_and_unknown_degrades_to_none() -> None:
    project = _project()
    project.metadata.user_role = b1_read_pb2.PROJECT_ROLE_WRITER
    _, _, notebooks = _graph(project)
    assert (await notebooks.get(project.id)).role is SharePermission.EDITOR

    unknown_project = _project()
    unknown_project.metadata.user_role = 99
    _, _, unknown_notebooks = _graph(unknown_project)
    assert (await unknown_notebooks.get(unknown_project.id)).role is None


@pytest.mark.asyncio
async def test_notebook_required_id_is_never_invented() -> None:
    _, _, notebooks = _graph(_project(project_id=""))
    with pytest.raises(DecodingError, match="did not contain a notebook id") as raised:
        await notebooks.get("requested-id")
    assert raised.value.method_id == GET_PROJECT_METHOD
    assert "Android notebook" not in str(raised.value)


@pytest.mark.asyncio
async def test_get_translates_only_grpc_not_found() -> None:
    fake, _, notebooks = _graph()
    fake.error = RPCError("sanitized status", method_id=GET_PROJECT_METHOD, rpc_code=5)
    with pytest.raises(NotebookNotFoundError) as raised:
        await notebooks.get("missing-id")
    assert raised.value.notebook_id == "missing-id"
    assert raised.value.rpc_code == 5
    assert raised.value.method_id == GET_PROJECT_METHOD

    original = RPCError("permission denied", method_id=GET_PROJECT_METHOD, rpc_code=7)
    fake.error = original
    with pytest.raises(RPCError) as preserved:
        await notebooks.get("forbidden-id")
    assert preserved.value is original


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
    # Retained protobuf unknown fields are intentionally outside the raw API contract.
    response.ParseFromString(response.SerializeToString() + b"\xf8\x07\x01")

    raw = await notebooks.get_raw("notebook-1")

    assert raw["project"]["id"] == "notebook-1"
    assert raw["project"]["metadata"]["user_role"] == "PROJECT_ROLE_OWNER"
    assert "premium_feature_info" not in raw["project"]
    assert "127" not in raw


@pytest.mark.asyncio
async def test_get_source_ids_uses_one_read_and_first_duplicate_semantics() -> None:
    first = _source_fixture()[0]
    duplicate = b1_read_pb2.Source()
    duplicate.CopyFrom(first)
    malformed = b1_read_pb2.Source(title="missing id")
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
        SourceType.UNKNOWN,
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
    assert request == b1_read_pb2.GetProjectRequest(
        project_id="notebook-1",
        include_audio_overview_ids=True,
    )
    assert kwargs == {
        "replay_safe": True,
        "response_type": b1_read_pb2.GetProjectResponse,
    }


@pytest.mark.asyncio
async def test_source_enum_mapping_is_name_based_not_android_integer_copy() -> None:
    sheet = _source(
        "sheet-1",
        title="Sheet",
        content_type=b1_read_pb2.SOURCE_CONTENT_TYPE_GOOGLE_SHEET,
        status=99,
        drive_status=99,
    )
    _, sources, _ = _graph(_project(sources=[sheet]))
    decoded = (await sources.list("notebook-1"))[0]
    assert b1_read_pb2.SOURCE_CONTENT_TYPE_GOOGLE_SHEET == 7
    assert decoded.kind is SourceType.GOOGLE_SPREADSHEET
    assert decoded._type_code == 14
    assert decoded.status is SourceStatus.UNKNOWN
    assert decoded.drive_status is DriveSourceStatus.UNKNOWN


@pytest.mark.asyncio
async def test_source_missing_id_default_skips_with_warning_and_strict_rejects(
    caplog: pytest.LogCaptureFixture,
) -> None:
    malformed = _source(
        "",
        title="sensitive title must not be logged",
        content_type=b1_read_pb2.SOURCE_CONTENT_TYPE_URL,
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
    same = b1_read_pb2.Source()
    same.CopyFrom(first)
    conflict = b1_read_pb2.Source()
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
        (SourceType.UNKNOWN, "Drive file", None),
    ]


NotebookUnsupportedCall = Callable[[AndroidNotebooksAPI], Awaitable[object]]
SourceUnsupportedCall = Callable[[AndroidSourcesAPI], Awaitable[object]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invoke",
    [
        pytest.param(lambda api: api.create("title"), id="create"),
        pytest.param(lambda api: api._send_create("title"), id="send-create"),
        pytest.param(lambda api: api.copy("notebook", "copy"), id="copy"),
        pytest.param(lambda api: api.suggest_prompts("notebook"), id="suggest-prompts"),
        pytest.param(lambda api: api.delete("notebook"), id="delete"),
        pytest.param(lambda api: api.update("notebook", title="new"), id="update"),
        pytest.param(lambda api: api.rename("notebook", "new"), id="rename"),
        pytest.param(lambda api: api.set_emoji("notebook", "📘"), id="set-emoji"),
        pytest.param(lambda api: api.get_summary("notebook"), id="get-summary"),
        pytest.param(lambda api: api.get_description("notebook"), id="get-description"),
        pytest.param(lambda api: api.remove_from_recent("notebook"), id="remove-recent"),
    ],
)
async def test_every_unsupported_notebook_method_fails_before_io(
    invoke: NotebookUnsupportedCall,
) -> None:
    fake, _, notebooks = _graph()
    with pytest.raises(UnsupportedOperationError, match='backend="web"'):
        await invoke(notebooks)
    assert fake.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invoke",
    [
        pytest.param(lambda api: api.add_url("notebook", "https://example.test"), id="add-url"),
        pytest.param(
            lambda api: api._add_urls_batch("notebook", ["https://example.test"]),
            id="add-urls-batch",
        ),
        pytest.param(lambda api: api.add_text("notebook", "title", "body"), id="add-text"),
        pytest.param(lambda api: api.add_file("notebook", "document.pdf"), id="add-file"),
        pytest.param(
            lambda api: api.add_drive("notebook", "file", "title"),
            id="add-drive",
        ),
        pytest.param(
            lambda api: api.add_drive_file("notebook", "document"),
            id="add-drive-file",
        ),
        pytest.param(lambda api: api.delete("notebook", "source"), id="delete"),
        pytest.param(lambda api: api.rename("notebook", "source", "new"), id="rename"),
        pytest.param(lambda api: api.refresh("notebook", "source"), id="refresh"),
        pytest.param(
            lambda api: api.check_freshness("notebook", "source"),
            id="check-freshness",
        ),
        pytest.param(lambda api: api.get_guide("notebook", "source"), id="get-guide"),
        pytest.param(lambda api: api.get_fulltext("notebook", "source"), id="get-fulltext"),
    ],
)
async def test_every_unsupported_source_method_fails_before_io(
    invoke: SourceUnsupportedCall,
) -> None:
    fake, sources, _ = _graph()
    with pytest.raises(UnsupportedOperationError, match='backend="web"'):
        await invoke(sources)
    assert fake.calls == []
