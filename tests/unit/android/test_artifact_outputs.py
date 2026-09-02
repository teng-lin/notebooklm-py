"""Unit coverage for the Android artifact decode/render helpers.

``tests/unit/android/test_artifacts.py`` drives these through the full adapter
graph and covers the happy paths. These cases call the helpers directly to
reach the rejection branches — malformed mind-map trees, non-typed prefetch
rows, ragged/headerless tables, and the citation-label variants of
``report_doc_markdown``.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from notebooklm._android import artifact_outputs as outputs
from notebooklm._android.artifact_outputs import (
    data_table_csv,
    decode_interactive_app_data,
    decode_interactive_mind_map_tree,
    decode_prefetched_artifacts,
    report_doc_markdown,
    select_note_backed_mind_map,
    select_single_file_media_url,
    validate_artifact_language,
    validate_echoed_source_ids,
    write_text_atomic,
)
from notebooklm._android.artifact_proto import ARTIFACTS_PROTO as _PROTO
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    chat_pb2,
)
from notebooklm._android.proto.notebooklm.internal.android.wire.v1 import (
    artifacts_pb2 as wire_pb2,
)
from notebooklm._types.artifact_content import ArtifactMedia, ArtifactMediaType
from notebooklm.exceptions import (
    ArtifactDownloadError,
    ArtifactParseError,
    DecodingError,
    ValidationError,
)
from notebooklm.types import Artifact, MindMap, MindMapKind

METHOD_ID = "test-method"


def _artifact(artifact_id: str = "art-1", **kwargs: Any) -> Artifact:
    kwargs.setdefault("_artifact_type", 1)
    kwargs.setdefault("status", 3)
    return Artifact(id=artifact_id, title="Title", **kwargs)


# ---------------------------------------------------------------------------
# Small validators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, 7, "", "   "])
def test_validate_artifact_language_rejects_blank_and_non_string(value: Any) -> None:
    with pytest.raises(ValidationError, match="language must be a non-empty string"):
        validate_artifact_language(value)


def test_validate_artifact_language_passes_a_tag_through() -> None:
    assert validate_artifact_language("fr") == "fr"


def test_validate_echoed_source_ids_rejects_a_mismatched_echo() -> None:
    artifact = _artifact(source_ids=("s1", "s2"))

    with pytest.raises(DecodingError, match="different source ids"):
        validate_echoed_source_ids(artifact, ["s1"], "report", METHOD_ID)


@pytest.mark.parametrize(
    "source_ids",
    [pytest.param((), id="empty-echo-is-tolerated"), pytest.param(("s2", "s1"), id="reordered")],
)
def test_validate_echoed_source_ids_accepts_matching_or_absent_echoes(
    source_ids: tuple[str, ...],
) -> None:
    validate_echoed_source_ids(_artifact(source_ids=source_ids), ["s1", "s2"], "report", METHOD_ID)


# ---------------------------------------------------------------------------
# select_single_file_media_url
# ---------------------------------------------------------------------------


def _media(kind: ArtifactMediaType, url: str) -> ArtifactMedia:
    return ArtifactMedia(url=url, kind=kind)


def test_progressive_media_wins_over_download() -> None:
    artifact = _artifact(
        media_urls=(
            _media(ArtifactMediaType.DOWNLOAD, "https://x.invalid/dl"),
            _media(ArtifactMediaType.PROGRESSIVE, "https://x.invalid/prog"),
        )
    )

    assert select_single_file_media_url(artifact) == "https://x.invalid/prog"


def test_download_media_is_the_fallback() -> None:
    artifact = _artifact(media_urls=(_media(ArtifactMediaType.DOWNLOAD, "https://x.invalid/dl"),))

    assert select_single_file_media_url(artifact) == "https://x.invalid/dl"


def test_no_admitted_media_representation_returns_none() -> None:
    assert select_single_file_media_url(_artifact()) is None


# ---------------------------------------------------------------------------
# decode_interactive_mind_map_tree
# ---------------------------------------------------------------------------


def test_mind_map_tree_decodes_a_nested_tree() -> None:
    payload = {"name": "root", "children": [{"name": "leaf", "children": []}]}

    assert decode_interactive_mind_map_tree(json.dumps(payload), artifact_id="a") == payload


def test_mind_map_tree_rejects_invalid_json() -> None:
    with pytest.raises(ArtifactParseError, match="not valid JSON"):
        decode_interactive_mind_map_tree("{not json", artifact_id="a")


def test_mind_map_tree_rejects_a_non_object_root() -> None:
    with pytest.raises(ArtifactParseError, match="not a JSON object"):
        decode_interactive_mind_map_tree("[]", artifact_id="a")


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"children": []}, id="missing-name"),
        pytest.param({"name": "", "children": []}, id="empty-name"),
        pytest.param({"name": 1, "children": []}, id="non-string-name"),
        pytest.param({"name": "root", "children": {}}, id="non-list-children"),
    ],
)
def test_mind_map_tree_rejects_invalid_nodes(payload: dict[str, Any]) -> None:
    with pytest.raises(ArtifactParseError, match="invalid node"):
        decode_interactive_mind_map_tree(json.dumps(payload), artifact_id="a")


def test_mind_map_tree_rejects_a_non_object_child() -> None:
    payload = {"name": "root", "children": ["leaf"]}

    with pytest.raises(ArtifactParseError, match="structural bounds"):
        decode_interactive_mind_map_tree(json.dumps(payload), artifact_id="a")


def test_mind_map_tree_rejects_nesting_past_the_depth_bound() -> None:
    node: dict[str, Any] = {"name": "leaf", "children": []}
    for _ in range(70):
        node = {"name": "n", "children": [node]}

    with pytest.raises(ArtifactParseError, match="structural bounds"):
        decode_interactive_mind_map_tree(json.dumps(node), artifact_id="a")


def test_mind_map_tree_rejects_more_than_ten_thousand_nodes() -> None:
    children = [{"name": f"n{index}", "children": []} for index in range(10_001)]

    with pytest.raises(ArtifactParseError, match="structural bounds"):
        decode_interactive_mind_map_tree(
            json.dumps({"name": "root", "children": children}), artifact_id="a"
        )


# ---------------------------------------------------------------------------
# decode_prefetched_artifacts
# ---------------------------------------------------------------------------


def test_prefetched_typed_artifacts_pass_through_unchanged() -> None:
    artifact = _artifact("art-9")

    assert decode_prefetched_artifacts([artifact], method_id=METHOD_ID) == [artifact]


def test_prefetched_protobufs_are_decoded() -> None:
    message = _PROTO.Artifact(artifact_id="art-9", title="Deck")

    [decoded] = decode_prefetched_artifacts([message], method_id=METHOD_ID)

    assert decoded.id == "art-9"
    assert decoded.title == "Deck"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(["art-9", "Deck"], id="web-wire-list"),
        pytest.param({"id": "art-9"}, id="mapping"),
        pytest.param(None, id="none"),
    ],
)
def test_prefetched_rows_of_other_shapes_are_rejected(value: Any) -> None:
    with pytest.raises(ValidationError, match="Android Artifact objects or protobufs"):
        decode_prefetched_artifacts([value], method_id=METHOD_ID)


# ---------------------------------------------------------------------------
# select_note_backed_mind_map
# ---------------------------------------------------------------------------


def _mind_map(
    mind_map_id: str,
    *,
    kind: MindMapKind = MindMapKind.NOTE_BACKED,
    created_at: datetime | None = None,
) -> MindMap:
    return MindMap(
        id=mind_map_id,
        notebook_id="nb1",
        title=mind_map_id,
        kind=kind,
        created_at=created_at,
    )


def test_select_note_backed_mind_map_rejects_untyped_rows() -> None:
    with pytest.raises(ValidationError, match="typed MindMap objects"):
        select_note_backed_mind_map([{"id": "mm-1"}], mind_map_id=None)


def test_select_note_backed_mind_map_returns_the_requested_id() -> None:
    wanted = _mind_map("mm-2")

    selected = select_note_backed_mind_map([_mind_map("mm-1"), wanted], mind_map_id="mm-2")

    assert selected is wanted


def test_select_note_backed_mind_map_returns_none_for_an_unknown_id() -> None:
    assert select_note_backed_mind_map([_mind_map("mm-1")], mind_map_id="absent") is None


def test_select_note_backed_mind_map_prefers_the_newest_candidate() -> None:
    older = _mind_map("mm-1", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    newer = _mind_map("mm-2", created_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    undated = _mind_map("mm-3")

    assert select_note_backed_mind_map([older, undated, newer], mind_map_id=None) is newer


def test_select_note_backed_mind_map_skips_interactive_rows() -> None:
    interactive = _mind_map("mm-1", kind=MindMapKind.INTERACTIVE)

    assert select_note_backed_mind_map([interactive], mind_map_id=None) is None


# ---------------------------------------------------------------------------
# decode_interactive_app_data
# ---------------------------------------------------------------------------


def test_app_data_json_is_preferred_over_the_embedded_html() -> None:
    decoded = decode_interactive_app_data(
        "<html>ignored</html>",
        '{"cards": 3}',
        artifact_type="quiz",
        artifact_id="a",
    )

    assert decoded == {"cards": 3}


def test_app_data_falls_back_to_the_embedded_html_payload() -> None:
    html = '<div data-app-data="{&quot;cards&quot;: 1}"></div>'

    decoded = decode_interactive_app_data(html, "", artifact_type="quiz", artifact_id="a")

    assert decoded == {"cards": 1}


def test_app_data_rejects_unparseable_json() -> None:
    with pytest.raises(ArtifactParseError, match="Failed to parse"):
        decode_interactive_app_data("", "{nope", artifact_type="quiz", artifact_id="a")


def test_app_data_rejects_html_without_an_embedded_payload() -> None:
    with pytest.raises(ArtifactParseError, match="Failed to parse"):
        decode_interactive_app_data(
            "<html>no payload</html>", "", artifact_type="quiz", artifact_id="a"
        )


def test_app_data_rejects_a_non_object_payload() -> None:
    with pytest.raises(ArtifactParseError, match="not a JSON object"):
        decode_interactive_app_data("", "[1, 2]", artifact_type="quiz", artifact_id="a")


# ---------------------------------------------------------------------------
# report_doc_markdown
# ---------------------------------------------------------------------------


def _document_with_body(text: str = "Body") -> chat_pb2.TailwindDoc:
    document = chat_pb2.TailwindDoc()
    document.body.content.add().paragraph.elements.add().text_run.content = text
    return document


def test_report_doc_markdown_without_a_body_is_empty() -> None:
    assert report_doc_markdown(chat_pb2.TailwindDoc()) == ""


def test_report_doc_markdown_skips_objects_without_a_citation() -> None:
    document = _document_with_body()
    document.objects.add().object_id.id = "obj-1"

    assert report_doc_markdown(document) == "Body"


def test_report_doc_markdown_renders_an_attributed_citation() -> None:
    document = _document_with_body()
    item = document.objects.add()
    item.object_id.id = "obj-1"
    item.citation.fragment.elements.add().paragraph.elements.add().text_run.content = "quoted"
    item.citation.source_attribution.ingested_source.source.id = "src-7"

    assert report_doc_markdown(document) == ("Body\n\n> **Citation obj-1 (src-7):** quoted")


def test_report_doc_markdown_falls_back_to_the_citation_object_id() -> None:
    document = _document_with_body()
    item = document.objects.add()
    item.citation.object_id.id = "cite-2"
    item.citation.fragment.elements.add().paragraph.elements.add().text_run.content = "quoted"

    assert report_doc_markdown(document) == "Body\n\n> **Citation cite-2:** quoted"


def test_report_doc_markdown_labels_an_anonymous_empty_citation() -> None:
    document = _document_with_body()
    document.objects.add().citation.SetInParent()

    assert report_doc_markdown(document) == "Body\n\n> **Citation:**"


# ---------------------------------------------------------------------------
# data_table_csv
# ---------------------------------------------------------------------------


def _table_message(rows: list[list[str]]) -> Any:
    """Build an exact ``Artifact`` carrying a single-table wire projection."""
    projection = wire_pb2.WireArtifactTableProjection()
    table = projection.table.document.body.content.add().table
    for values in rows:
        row = table.table_rows.add()
        for value in values:
            cell = row.table_cells.add()
            cell.content.add().paragraph.elements.add().text_run.content = value
    message = _PROTO.Artifact(artifact_id="art-1")
    message.MergeFromString(projection.SerializeToString())
    return message


def test_data_table_csv_writes_a_bom_prefixed_rfc_table() -> None:
    message = _table_message([["Name", "Note"], ["Alpha", "one"]])

    assert data_table_csv(message, artifact_id="a") == "﻿Name,Note\r\nAlpha,one\r\n"


def test_data_table_csv_rejects_an_artifact_without_a_table_document() -> None:
    with pytest.raises(ArtifactParseError, match="omitted its table document"):
        data_table_csv(_PROTO.Artifact(artifact_id="art-1"), artifact_id="a")


def test_data_table_csv_rejects_a_ragged_table() -> None:
    message = _table_message([["Name", "Note"], ["Alpha"]])

    with pytest.raises(ArtifactParseError, match="invalid rectangular table"):
        data_table_csv(message, artifact_id="a")


def test_data_table_csv_rejects_a_table_with_no_columns() -> None:
    projection = wire_pb2.WireArtifactTableProjection()
    projection.table.document.body.content.add().table.table_rows.add()
    message = _PROTO.Artifact(artifact_id="art-1")
    message.MergeFromString(projection.SerializeToString())

    with pytest.raises(ArtifactParseError, match="invalid rectangular table"):
        data_table_csv(message, artifact_id="a")


@pytest.mark.parametrize(
    "heading", [pytest.param("", id="empty"), pytest.param("   ", id="whitespace-only")]
)
def test_data_table_csv_rejects_a_missing_header_cell(heading: str) -> None:
    message = _table_message([["Name", heading], ["Alpha", "one"]])

    with pytest.raises(ArtifactParseError, match="missing header cell"):
        data_table_csv(message, artifact_id="a")


def test_data_table_csv_flattens_thought_wrapped_cells() -> None:
    projection = wire_pb2.WireArtifactTableProjection()
    table = projection.table.document.body.content.add().table
    header = table.table_rows.add()
    header.table_cells.add().content.add().paragraph.elements.add().text_run.content = "Head"
    thought = table.table_rows.add().table_cells.add().content.add().thought
    thought.elements.add().paragraph.elements.add().text_run.content = "inner"
    message = _PROTO.Artifact(artifact_id="art-1")
    message.MergeFromString(projection.SerializeToString())

    assert data_table_csv(message, artifact_id="a") == "﻿Head\r\ninner\r\n"


def test_data_table_csv_rejects_a_non_text_paragraph_element() -> None:
    projection = wire_pb2.WireArtifactTableProjection()
    table = projection.table.document.body.content.add().table
    header = table.table_rows.add()
    header.table_cells.add().content.add().paragraph.elements.add().text_run.content = "Head"
    cell = table.table_rows.add().table_cells.add().content.add()
    cell.paragraph.elements.add().image.url = "https://x.invalid/i"
    message = _PROTO.Artifact(artifact_id="art-1")
    message.MergeFromString(projection.SerializeToString())

    with pytest.raises(ArtifactParseError, match="unsupported cell structure"):
        data_table_csv(message, artifact_id="a")


def test_data_table_csv_rejects_an_unsupported_block_variant() -> None:
    projection = wire_pb2.WireArtifactTableProjection()
    table = projection.table.document.body.content.add().table
    header = table.table_rows.add()
    header.table_cells.add().content.add().paragraph.elements.add().text_run.content = "Head"
    table.table_rows.add().table_cells.add().content.add().horizontal_rule.SetInParent()
    message = _PROTO.Artifact(artifact_id="art-1")
    message.MergeFromString(projection.SerializeToString())

    with pytest.raises(ArtifactParseError, match="unsupported cell structure"):
        data_table_csv(message, artifact_id="a")


# ---------------------------------------------------------------------------
# write_text_atomic
# ---------------------------------------------------------------------------


def _staging_leftovers(directory: Path) -> list[Path]:
    return [path for path in directory.iterdir() if path.name.endswith(".tmp")]


@pytest.mark.asyncio
async def test_write_text_atomic_publishes_bytes_verbatim(tmp_path: Path) -> None:
    """Line endings survive publication — no platform newline translation."""
    destination = tmp_path / "nested" / "out.csv"

    result = await write_text_atomic(
        str(destination), "a,b\r\n1,2\r\n", artifact_type="data_table", artifact_id="art-1"
    )

    assert result == str(destination)
    assert destination.read_bytes() == b"a,b\r\n1,2\r\n"
    assert _staging_leftovers(destination.parent) == []


@pytest.mark.asyncio
async def test_write_text_atomic_reports_a_failed_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "out.csv"

    def _failing_replace(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("cross-device link")

    monkeypatch.setattr(outputs.os, "replace", _failing_replace)

    with pytest.raises(ArtifactDownloadError, match="Could not publish"):
        await write_text_atomic(
            str(destination), "body", artifact_type="report", artifact_id="art-1"
        )

    assert not destination.exists()
    # The staging file is removed rather than left behind as a partial artifact.
    assert _staging_leftovers(tmp_path) == []


@pytest.mark.asyncio
async def test_write_text_atomic_removes_staging_when_encoding_fails(
    tmp_path: Path,
) -> None:
    """An unencodable payload leaves neither the destination nor a temp file."""
    destination = tmp_path / "out.md"

    with pytest.raises(ArtifactDownloadError, match="Could not publish"):
        await write_text_atomic(
            str(destination), "\ud800", artifact_type="report", artifact_id="art-1"
        )

    assert not destination.exists()
    assert _staging_leftovers(tmp_path) == []


@pytest.fixture
def blocking_mkstemp(monkeypatch: pytest.MonkeyPatch):
    """Hold the worker thread inside ``mkstemp`` until the test releases it."""
    started = threading.Event()
    release = threading.Event()
    real_mkstemp = tempfile.mkstemp

    def _blocking(*args: Any, **kwargs: Any):
        started.set()
        release.wait(10)
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(outputs.tempfile, "mkstemp", _blocking)
    yield started, release
    release.set()


async def _await_worker_started(started: threading.Event) -> None:
    await asyncio.to_thread(started.wait, 10)


@pytest.mark.asyncio
async def test_cancelled_write_settles_the_worker_and_leaves_no_files(
    tmp_path: Path, blocking_mkstemp
) -> None:
    """Cancellation cannot abandon a live filesystem thread mid-write."""
    started, release = blocking_mkstemp
    destination = tmp_path / "out.md"
    task = asyncio.create_task(
        write_text_atomic(str(destination), "body", artifact_type="report", artifact_id="art-1")
    )
    await _await_worker_started(started)

    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not destination.exists()
    assert _staging_leftovers(tmp_path) == []


@pytest.mark.asyncio
async def test_repeated_cancellation_still_settles_the_worker(
    tmp_path: Path, blocking_mkstemp
) -> None:
    """A second cancel during the shielded retry re-enters the wait, not the caller."""
    started, release = blocking_mkstemp
    destination = tmp_path / "out.md"
    task = asyncio.create_task(
        write_text_atomic(str(destination), "body", artifact_type="report", artifact_id="art-1")
    )
    await _await_worker_started(started)

    task.cancel()
    # Let the coroutine take the first CancelledError and re-enter the shield.
    for _ in range(3):
        await asyncio.sleep(0)
    task.cancel()
    for _ in range(3):
        await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not destination.exists()
    assert _staging_leftovers(tmp_path) == []


@pytest.mark.asyncio
async def test_cancellation_racing_a_failed_worker_reports_the_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker OSError during cancellation does not mask the CancelledError."""
    started = threading.Event()
    release = threading.Event()

    def _failing(*_args: Any, **_kwargs: Any):
        started.set()
        release.wait(10)
        raise OSError("no space left on device")

    monkeypatch.setattr(outputs.tempfile, "mkstemp", _failing)
    destination = tmp_path / "out.md"
    task = asyncio.create_task(
        write_text_atomic(str(destination), "body", artifact_type="report", artifact_id="art-1")
    )
    await _await_worker_started(started)

    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not destination.exists()
