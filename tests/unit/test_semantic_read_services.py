"""Focused tests for transport-neutral read services and public projections."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._backend import BackendErrorReason
from notebooklm._deadline import RuntimeDeadline
from notebooklm._notebooks import NotebooksAPI
from notebooklm._operations import Operation
from notebooklm._read_services import NotebookReadService, SourceReadService
from notebooklm._semantic.projectors import project_notebook, project_source
from notebooklm._semantic.records import (
    NOTEBOOK_GET_DEF,
    NOTEBOOK_LIST_DEF,
    SOURCE_GET_DEF,
    SOURCE_LIST_DEF,
    NotebookChatSessionRecord,
    NotebookChatSettingsRecord,
    NotebookGetInput,
    NotebookGetResult,
    NotebookListInput,
    NotebookListResult,
    NotebookPremiumFeaturesRecord,
    NotebookRecord,
    SourceGetInput,
    SourceGetResult,
    SourceListInput,
    SourceListResult,
    SourceRecord,
)
from notebooklm._sources import SourcesAPI
from notebooklm.exceptions import NetworkError
from notebooklm.types import (
    ChatGoal,
    ChatResponseLength,
    DriveSourceStatus,
    Notebook,
    SharePermission,
    Source,
    SourceStatus,
    SourceType,
    UnknownTypeWarning,
)
from tests._fixtures.recording_backend import (
    BackendInvocation,
    RecordingBackend,
    scripted_error,
)


def _notebook_record() -> NotebookRecord:
    created = datetime(2025, 1, 2, 3, 4, tzinfo=timezone.utc)
    viewed = datetime(2026, 5, 6, 7, 8, tzinfo=timezone.utc)
    return NotebookRecord(
        id="notebook-id",
        title="Notebook title",
        created_at=created,
        sources_count=7,
        is_owner=True,
        role="editor",
        last_viewed_at=viewed,
        emoji="🧪",
        premium_features=NotebookPremiumFeaturesRecord(True, False, None),
        chat_sessions=(
            NotebookChatSessionRecord("session-one"),
            NotebookChatSessionRecord("session-two"),
        ),
        chat_settings=NotebookChatSettingsRecord("custom", "long", "Be precise"),
    )


def _source_record() -> SourceRecord:
    created = datetime(2025, 2, 3, 4, 5, tzinfo=timezone.utc)
    revised = datetime(2025, 3, 4, 5, 6, tzinfo=timezone.utc)
    modified = datetime(2025, 4, 5, 6, 7, tzinfo=timezone.utc)
    return SourceRecord(
        id="source-id",
        title="Source title",
        url="https://example.com/source.pdf",
        kind="pdf",
        created_at=created,
        status="preparing",
        drive_document_id="drive-id",
        drive_status="syncing",
        download_url="https://example.com/download",
        viewer_url="https://example.com/view",
        content_mime="application/pdf",
        word_count=123,
        revision_id="revision-id",
        revision_timestamp=revised,
        last_modified_at=modified,
    )


def test_project_notebook_preserves_all_neutral_record_fields_and_invariants() -> None:
    record = _notebook_record()

    notebook = project_notebook(record)

    assert notebook.id == record.id
    assert notebook.title == record.title
    assert notebook.created_at == record.created_at
    assert notebook.sources_count == record.sources_count
    assert notebook.role is SharePermission.EDITOR
    assert not notebook.is_owner
    assert notebook.last_viewed_at == record.last_viewed_at
    assert notebook.modified_at == record.last_viewed_at
    assert notebook.emoji == record.emoji
    assert notebook.premium_features is not None
    assert notebook.premium_features.can_edit_advanced_settings is True
    assert notebook.premium_features.can_edit_guidebook_config is False
    assert notebook.premium_features.can_view_analytics is None
    assert [session.id for session in notebook.chat_sessions] == ["session-one", "session-two"]
    assert notebook.chat_settings is not None
    assert notebook.chat_settings.goal is ChatGoal.CUSTOM
    assert notebook.chat_settings.response_length is ChatResponseLength.LONGER
    assert notebook.chat_settings.custom_prompt == "Be precise"


def test_project_notebook_preserves_legacy_owner_fallback_when_role_is_unknown() -> None:
    record = NotebookRecord("notebook-id", "Notebook", is_owner=False, role="future-role")

    notebook = project_notebook(record)

    assert notebook.role is None
    assert notebook.is_owner is False


def test_project_source_preserves_every_neutral_field_and_known_semantics() -> None:
    record = _source_record()

    source = project_source(record)

    assert source.id == record.id
    assert source.title == record.title
    assert source.url == record.url
    assert source.kind is SourceType.PDF
    assert source.created_at == record.created_at
    assert source.status is SourceStatus.PREPARING
    assert source.drive_document_id == record.drive_document_id
    assert source.drive_status is DriveSourceStatus.SYNCING
    assert source.download_url == record.download_url
    assert source.viewer_url == record.viewer_url
    assert source.content_mime == record.content_mime
    assert source.word_count == record.word_count
    assert source.revision_id == record.revision_id
    assert source.revision_timestamp == record.revision_timestamp
    assert source.last_modified_at == record.last_modified_at


def test_project_source_preserves_absent_kind_distinct_from_wire_unknown_zero() -> None:
    absent = project_source(SourceRecord("absent", kind_present=False))
    wire_unknown = project_source(SourceRecord("unknown", kind="unknown"))

    assert absent._type_code is None
    assert wire_unknown._type_code == 0


@pytest.mark.parametrize("opaque_kind", [938_475, "future-source-kind"])
def test_project_source_preserves_unknown_backend_kind_discriminator(
    opaque_kind: int | str,
) -> None:
    source = project_source(
        SourceRecord(
            "source-id",
            kind="unknown",
            unrecognized_kind=opaque_kind,
        )
    )

    assert source._type_code == opaque_kind
    with pytest.warns(UnknownTypeWarning, match=str(opaque_kind)):
        assert source.kind is SourceType.UNKNOWN


@pytest.mark.asyncio
async def test_read_services_invoke_typed_operations_and_preserve_backend_order() -> None:
    notebook = _notebook_record()
    source = _source_record()
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)
    backend = RecordingBackend()
    backend.set_result(NOTEBOOK_LIST_DEF, NotebookListResult((notebook,)))
    backend.set_result(NOTEBOOK_GET_DEF, NotebookGetResult(notebook, ("source-a", "source-b")))
    backend.set_result(SOURCE_LIST_DEF, SourceListResult((source,)))
    backend.set_result(SOURCE_GET_DEF, SourceGetResult(source))
    notebooks = NotebookReadService(backend)
    sources = SourceReadService(backend)

    listed_notebooks = await notebooks.list(deadline=deadline)
    fetched_notebook = await notebooks.get("notebook-id", deadline=deadline)
    source_ids = await notebooks.get_source_ids("notebook-id", deadline=deadline)
    listed_sources = await sources.list(
        "notebook-id",
        strict=True,
        statuses=frozenset({"ready", "preparing"}),
        kinds=frozenset({"pdf"}),
        deadline=deadline,
    )
    fetched_source = await sources.get("notebook-id", "source-id", deadline=deadline)

    # R6.1: the read services hand back the backend's neutral records
    # untouched; projection to Notebook/Source belongs to the facades and is
    # pinned in the NotebooksAPI/SourcesAPI tests below.
    assert listed_notebooks == [notebook]
    assert fetched_notebook is notebook
    assert source_ids == ["source-a", "source-b"]
    assert listed_sources == [source]
    assert fetched_source is source
    assert backend.invocations == [
        BackendInvocation(Operation.NOTEBOOK_LIST, NotebookListInput(), deadline),
        BackendInvocation(
            Operation.NOTEBOOK_GET,
            NotebookGetInput("notebook-id"),
            deadline,
        ),
        BackendInvocation(
            Operation.NOTEBOOK_GET,
            NotebookGetInput("notebook-id", include_notebook=False),
            deadline,
        ),
        BackendInvocation(
            Operation.SOURCE_LIST,
            SourceListInput(
                "notebook-id",
                strict=True,
                statuses=frozenset({"ready", "preparing"}),
                kinds=frozenset({"pdf"}),
            ),
            deadline,
        ),
        BackendInvocation(
            Operation.SOURCE_GET,
            SourceGetInput("notebook-id", "source-id"),
            deadline,
        ),
    ]


@pytest.mark.asyncio
async def test_read_services_preserve_semantic_not_found_results() -> None:
    backend = RecordingBackend()
    backend.set_result(NOTEBOOK_GET_DEF, NotebookGetResult(None))
    backend.set_result(SOURCE_GET_DEF, SourceGetResult(None))

    assert await NotebookReadService(backend).get("missing-notebook") is None
    assert await SourceReadService(backend).get("notebook-id", "missing-source") is None


@pytest.mark.asyncio
async def test_notebooks_facade_delegates_live_reads_without_parallel_rpc_calls() -> None:
    notebook = _notebook_record()
    backend = RecordingBackend()
    backend.set_result(NOTEBOOK_LIST_DEF, NotebookListResult((notebook,)))
    backend.set_result(NOTEBOOK_GET_DEF, NotebookGetResult(notebook))
    rpc_call = AsyncMock()
    api = NotebooksAPI(
        sources_api=MagicMock(),
        _backend=backend,
    )

    listed = await api.list()
    fetched = await api.get("notebook-id")
    optional = await api.get_or_none("notebook-id")

    # The facade owns the record -> public model projection (R6.1).
    expected = project_notebook(notebook)
    assert listed == [expected]
    assert isinstance(fetched, Notebook) and fetched == expected
    assert optional == expected
    assert [invocation.operation for invocation in backend.invocations] == [
        Operation.NOTEBOOK_LIST,
        Operation.NOTEBOOK_GET,
        Operation.NOTEBOOK_GET,
    ]
    rpc_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_notebooks_get_raw_reads_the_undecoded_payload_through_the_row() -> None:
    """R6.2: ``get_raw`` is a ``NOTEBOOK_GET`` invocation, not a raw ``rpc_call``."""
    payload = [["Test Notebook", [["src1"], ["src2"]], "notebook-id", "📘"], ["extra"]]
    backend = RecordingBackend()
    backend.set_result(NOTEBOOK_GET_DEF, NotebookGetResult(None, (), payload))
    api = NotebooksAPI(_backend=backend)

    assert await api.get_raw("notebook-id") == payload
    assert backend.invocations == [
        BackendInvocation(
            Operation.NOTEBOOK_GET,
            NotebookGetInput("notebook-id", include_notebook=False, include_raw=True),
            None,
        )
    ]


@pytest.mark.asyncio
async def test_notebooks_get_raw_projects_a_backend_failure_to_its_public_error() -> None:
    """The raw helper raised transport exceptions before; it still does."""
    backend = RecordingBackend()
    backend.set_error(
        NOTEBOOK_GET_DEF,
        scripted_error(BackendErrorReason.NETWORK, operation=Operation.NOTEBOOK_GET),
    )
    api = NotebooksAPI(_backend=backend)

    with pytest.raises(NetworkError):
        await api.get_raw("notebook-id")


@pytest.mark.asyncio
async def test_direct_notebooks_metadata_uses_two_semantic_reads_without_raw_transport() -> None:
    notebook = _notebook_record()
    source = _source_record()
    backend = RecordingBackend()
    backend.set_result(NOTEBOOK_GET_DEF, NotebookGetResult(notebook))
    backend.set_result(SOURCE_LIST_DEF, SourceListResult((source,)))
    rpc_call = AsyncMock()
    api = NotebooksAPI(_backend=backend)

    metadata = await api.get_metadata("notebook-id")

    assert metadata.notebook.id == "notebook-id"
    assert [item.title for item in metadata.sources] == ["Source title"]
    assert backend.invocations == [
        BackendInvocation(
            Operation.NOTEBOOK_GET,
            NotebookGetInput("notebook-id"),
            None,
        ),
        BackendInvocation(
            Operation.SOURCE_LIST,
            SourceListInput("notebook-id"),
            None,
        ),
    ]
    rpc_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_notebooks_metadata_keeps_get_late_bound_with_semantic_lister() -> None:
    source = _source_record()
    backend = RecordingBackend()
    backend.set_result(SOURCE_LIST_DEF, SourceListResult((source,)))
    rpc_call = AsyncMock()
    api = NotebooksAPI(_backend=backend)
    replacement_get = AsyncMock(
        return_value=project_notebook(
            NotebookRecord("notebook-id", "Late-bound notebook", sources_count=1)
        )
    )
    api.get = replacement_get

    metadata = await api.get_metadata("notebook-id")

    assert metadata.notebook.title == "Late-bound notebook"
    replacement_get.assert_awaited_once_with("notebook-id")
    assert backend.invocations == [
        BackendInvocation(
            Operation.SOURCE_LIST,
            SourceListInput("notebook-id"),
            None,
        )
    ]
    rpc_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_sources_facade_projects_read_service_records_into_public_models() -> None:
    """The Source read projection lives on SourcesAPI, not the read service (R6.1)."""
    record = _source_record()
    backend = RecordingBackend()
    backend.set_result(SOURCE_LIST_DEF, SourceListResult((record,)))
    backend.set_result(SOURCE_GET_DEF, SourceGetResult(record))
    api = SourcesAPI(MagicMock(), uploader=MagicMock(), _backend=backend)

    listed = await api.list("notebook-id")
    fetched = await api.get_or_none("notebook-id", "source-id")

    expected = project_source(record)
    assert listed == [expected]
    assert isinstance(listed[0], Source)
    assert fetched == expected


@pytest.mark.asyncio
async def test_sources_facade_preserves_the_none_on_miss_lookup_after_projection() -> None:
    backend = RecordingBackend()
    backend.set_result(SOURCE_GET_DEF, SourceGetResult(None))
    api = SourcesAPI(MagicMock(), uploader=MagicMock(), _backend=backend)

    assert await api.get_or_none("notebook-id", "missing-source") is None
