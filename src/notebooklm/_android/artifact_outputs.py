"""Local decoding and publication helpers for Android artifact representations."""

from __future__ import annotations

import asyncio
import builtins
import csv
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .._artifact.formatters import _extract_app_data
from .._types.artifact_content import ArtifactMediaType
from .._types.enums import INTERACTIVE_MIND_MAP_VARIANT, ArtifactTypeCode
from ..exceptions import ArtifactDownloadError, ArtifactParseError, DecodingError, ValidationError
from ..types import Artifact, ArtifactType, MindMap, MindMapKind
from .artifact_proto import ARTIFACTS_PROTO as _PROTO
from .artifact_proto import table_artifact_projection
from .codecs.artifacts import decode_artifact
from .codecs.documents import structural_element_markdown, tailwind_doc_markdown


def matches_artifact_type(artifact: Artifact, requested: ArtifactType | None) -> bool:
    """Match public artifact kinds while retaining interactive mind-map compatibility."""

    if requested is None:
        return True
    if requested == ArtifactType.MIND_MAP:
        return artifact._artifact_type == ArtifactTypeCode.MIND_MAP.value or (
            artifact._artifact_type == ArtifactTypeCode.QUIZ.value
            and artifact._variant == INTERACTIVE_MIND_MAP_VARIANT
        )
    return artifact.kind == requested


def validate_artifact_language(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("language must be a non-empty string")
    return value


def validate_echoed_source_ids(
    artifact: Artifact,
    requested_source_ids: builtins.list[str],
    family_label: str,
    method_id: str,
) -> None:
    """Reject a populated creation echo that belongs to different sources."""

    if artifact.source_ids and set(artifact.source_ids) != set(requested_source_ids):
        raise DecodingError(
            f"Android {family_label} creation returned different source ids.",
            method_id=method_id,
        )


async def write_text_atomic(
    output_path: str,
    content: str,
    *,
    artifact_type: str,
    artifact_id: str,
) -> str:
    """Publish generated text without exposing partial files or payloads on failure."""

    destination = Path(output_path)
    failure: bool = False
    staging: Path | None = None

    def _write_staging(payload: str) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, staging_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        try:
            # ``content`` already owns its line-ending contract (CSV uses
            # RFC-compatible CRLF; markdown/JSON renderers use LF). Disable
            # platform translation so Windows does not turn an existing CRLF
            # into CRCRLF and publish different bytes from POSIX platforms.
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            return Path(staging_name)
        except BaseException:
            try:
                os.unlink(staging_name)
            except FileNotFoundError:
                pass
            raise

    worker = asyncio.create_task(asyncio.to_thread(_write_staging, content))
    try:
        try:
            # Shield the worker so caller cancellation cannot abandon a live
            # filesystem thread. Publication stays on the event-loop thread,
            # after the final cancellation point, so a cancelled caller can
            # only leave a fully settled and removed staging file.
            staging = await asyncio.shield(worker)
        except asyncio.CancelledError as cancellation:
            while True:
                try:
                    staging = await asyncio.shield(worker)
                    break
                except asyncio.CancelledError:
                    if worker.done():
                        try:
                            staging = worker.result()
                        except (OSError, UnicodeError):
                            pass
                        break
                    continue
                except (OSError, UnicodeError):
                    break
            if staging is not None:
                try:
                    staging.unlink(missing_ok=True)
                except OSError:
                    pass
                staging = None
            raise cancellation
        os.replace(staging, destination)
        staging = None
    except (OSError, UnicodeError):
        failure = True
    finally:
        if staging is not None:
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                pass
        del content
    if failure:
        raise ArtifactDownloadError(
            artifact_type,
            artifact_id=artifact_id,
            details="Could not publish the decoded artifact.",
            cause=None,
        ) from None
    return str(destination)


def decode_interactive_mind_map_tree(content: str, *, artifact_id: str) -> dict[str, Any]:
    """Decode the live AppArtifact field-4 tree with bounded recursive validation."""

    try:
        tree = json.loads(content)
    except json.JSONDecodeError:
        raise ArtifactParseError(
            "mind_map",
            artifact_id=artifact_id,
            details="Interactive mind-map content is not valid JSON",
            cause=None,
        ) from None
    if not isinstance(tree, dict):
        raise ArtifactParseError(
            "mind_map",
            artifact_id=artifact_id,
            details="Interactive mind-map content is not a JSON object",
            cause=None,
        ) from None

    stack: builtins.list[tuple[Any, int]] = [(tree, 0)]
    nodes = 0
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > 10_000 or depth > 64 or not isinstance(node, dict):
            raise ArtifactParseError(
                "mind_map",
                artifact_id=artifact_id,
                details="Interactive mind-map tree exceeds its structural bounds",
                cause=None,
            )
        name = node.get("name")
        children = node.get("children", [])
        if not isinstance(name, str) or not name or not isinstance(children, builtins.list):
            raise ArtifactParseError(
                "mind_map",
                artifact_id=artifact_id,
                details="Interactive mind-map tree has an invalid node",
                cause=None,
            )
        stack.extend((child, depth + 1) for child in children)
    return tree


def select_single_file_media_url(artifact: Artifact) -> str | None:
    """Prefer the live-verified progressive representation, then the download fallback."""

    media = next(
        (item for item in artifact.media_urls if item.kind is ArtifactMediaType.PROGRESSIVE),
        None,
    ) or next(
        (item for item in artifact.media_urls if item.kind is ArtifactMediaType.DOWNLOAD),
        None,
    )
    return None if media is None else media.url


def decode_prefetched_artifacts(
    values: builtins.list[Any], *, method_id: str
) -> builtins.list[Artifact]:
    """Accept only this adapter's typed or exact-protobuf prefetch representations."""

    decoded: builtins.list[Artifact] = []
    for value in values:
        if isinstance(value, Artifact):
            decoded.append(value)
        elif isinstance(value, _PROTO.Artifact):
            decoded.append(decode_artifact(value, method_id=method_id))
        else:
            raise ValidationError(
                "artifacts_data must contain Android Artifact objects or protobufs"
            )
    return decoded


def select_note_backed_mind_map(
    values: builtins.list[Any],
    *,
    mind_map_id: str | None,
) -> MindMap | None:
    """Select one typed note-backed prefetch row without accepting Web wire lists."""

    if any(not isinstance(value, MindMap) for value in values):
        raise ValidationError("mind_maps must contain typed MindMap objects")
    candidates = [value for value in values if value.kind is MindMapKind.NOTE_BACKED]
    if mind_map_id is not None:
        return next((value for value in candidates if value.id == mind_map_id), None)
    return max(
        candidates,
        key=lambda value: value.created_at.timestamp() if value.created_at is not None else 0,
        default=None,
    )


def decode_interactive_app_data(
    html_content: str,
    app_data_json: str,
    *,
    artifact_type: str,
    artifact_id: str,
) -> dict[str, Any]:
    """Decode the exact templatized payload or the legacy embedded-HTML fallback."""

    try:
        app_data = json.loads(app_data_json) if app_data_json else _extract_app_data(html_content)
    except (json.JSONDecodeError, ArtifactParseError):
        raise ArtifactParseError(
            artifact_type,
            artifact_id=artifact_id,
            details="Failed to parse Android app artifact content",
            cause=None,
        ) from None
    if not isinstance(app_data, dict):
        raise ArtifactParseError(
            artifact_type,
            artifact_id=artifact_id,
            details="Android app artifact content is not a JSON object",
            cause=None,
        )
    return app_data


def report_doc_markdown(document: Any) -> str:
    """Render the admitted TailwindDoc structural closure as Markdown."""

    if not document.HasField("body"):
        return ""
    blocks = [tailwind_doc_markdown(document)]
    for item in document.objects:
        if not item.HasField("citation"):
            continue
        citation = item.citation
        fragment = " ".join(
            block
            for block in (
                structural_element_markdown(element) for element in citation.fragment.elements
            )
            if block
        )
        source_id = ""
        if citation.HasField("source_attribution"):
            source_id = citation.source_attribution.ingested_source.source.id
        object_id = item.object_id.id or citation.object_id.id
        label = f"Citation {object_id}" if object_id else "Citation"
        if source_id:
            label = f"{label} ({source_id})"
        blocks.append(f"> **{label}:** {fragment}".rstrip())
    return "\n\n".join(block for block in blocks if block).strip()


def _invalid_data_table_cell(*, artifact_id: str) -> ArtifactParseError:
    return ArtifactParseError(
        "data_table",
        artifact_id=artifact_id,
        details="Android table artifact contains an unsupported cell structure",
        cause=None,
    )


def _data_table_structural_text(block: Any, *, artifact_id: str) -> str:
    variants = [
        name
        for name in (
            "paragraph",
            "table",
            "image",
            "code_block",
            "a2ui_block",
            "thought",
            "horizontal_rule",
        )
        if block.HasField(name)
    ]
    variant = next(iter(variants), None)
    if len(variants) != 1 or variant not in {"paragraph", "thought", "code_block"}:
        raise _invalid_data_table_cell(artifact_id=artifact_id)
    kind = variant
    if kind == "code_block":
        return block.code_block.content
    if kind == "thought":
        return "".join(
            _data_table_structural_text(child, artifact_id=artifact_id)
            for child in block.thought.elements
        )
    parts: builtins.list[str] = []
    for element in block.paragraph.elements:
        element_variants = [
            name for name in ("text_run", "image", "resource") if element.HasField(name)
        ]
        if element_variants != ["text_run"]:
            raise _invalid_data_table_cell(artifact_id=artifact_id)
        parts.append(element.text_run.content)
    return "".join(parts)


def _data_table_cell_text(cell: Any, *, artifact_id: str) -> str:
    parts: builtins.list[str] = []
    for block in cell.content:
        parts.append(_data_table_structural_text(block, artifact_id=artifact_id))
    return "".join(parts)


def data_table_csv(message: Any, *, artifact_id: str) -> str:
    """Render one live table document as BOM-prefixed RFC-compatible CSV."""

    table_artifact = table_artifact_projection(message)
    document = None if table_artifact is None else table_artifact.document
    tables = (
        []
        if document is None or not document.HasField("body")
        else [block.table for block in document.body.content if block.HasField("table")]
    )
    if len(tables) != 1:
        raise ArtifactParseError(
            "data_table",
            artifact_id=artifact_id,
            details=(
                "Android table artifact omitted its table document"
                if not tables
                else "Android table artifact contains multiple top-level tables"
            ),
            cause=None,
        )
    table, *_ = tables
    rows = [
        [_data_table_cell_text(cell, artifact_id=artifact_id) for cell in row.table_cells]
        for row in table.table_rows
    ]
    first_row = next(iter(rows), [])
    width = len(first_row)
    if width == 0 or any(len(row) != width for row in rows):
        raise ArtifactParseError(
            "data_table",
            artifact_id=artifact_id,
            details="Android table artifact has an invalid rectangular table",
            cause=None,
        )
    if any(not heading.strip() for heading in first_row):
        raise ArtifactParseError(
            "data_table",
            artifact_id=artifact_id,
            details="Android table artifact contains a missing header cell",
            cause=None,
        )
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerows(rows)
    return "\ufeff" + output.getvalue()
