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
from ..exceptions import ArtifactDownloadError, ArtifactParseError, ValidationError
from ..types import Artifact, MindMap, MindMapKind
from .artifact_proto import ARTIFACTS_PROTO as _PROTO
from .artifact_proto import table_artifact_projection
from .codecs.artifacts import decode_artifact


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

    def _write(payload: str) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, staging_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(staging_name, destination)
        except BaseException:
            try:
                os.unlink(staging_name)
            except FileNotFoundError:
                pass
            raise

    try:
        await asyncio.to_thread(_write, content)
    except (OSError, UnicodeError):
        failure = True
    finally:
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


def _report_inline(paragraph: Any) -> str:
    parts: builtins.list[str] = []
    for element in paragraph.elements:
        if element.HasField("text_run"):
            text = element.text_run.content
            if element.text_run.HasField("text_style"):
                style = element.text_run.text_style
                if style.code:
                    text = f"`{text}`"
                if style.bold:
                    text = f"**{text}**"
                if style.italic:
                    text = f"*{text}*"
                if style.strikethrough:
                    text = f"~~{text}~~"
                if style.underline:
                    text = f"<u>{text}</u>"
                if style.math:
                    text = f"${text}$"
                if style.url:
                    text = f"[{text}]({style.url})"
            parts.append(text)
        elif element.HasField("image") and element.image.url:
            parts.append(f"![image]({element.image.url})")
        elif element.HasField("resource") and element.resource.id:
            parts.append(f"[resource: {element.resource.id}]")
    return "".join(parts).strip()


def _report_structural_markdown(element: Any) -> str:
    if element.HasField("paragraph"):
        paragraph = element.paragraph
        text = _report_inline(paragraph)
        if not text:
            return ""
        if paragraph.HasField("bullet_info"):
            bullet = paragraph.bullet_info
            indent = "  " * max(0, min(int(bullet.nesting_level), 12))
            if int(bullet.list_type) == 2:
                ordinal = int(bullet.absolute_ordinal or bullet.ordinal or 1)
                return f"{indent}{ordinal}. {text}"
            return f"{indent}- {text}"
        if paragraph.HasField("paragraph_style"):
            named_style = int(paragraph.paragraph_style.named_style_type)
            if named_style == 2:
                return f"# {text}"
            if named_style == 3:
                return f"## {text}"
            if 4 <= named_style <= 9:
                return f"{'#' * (named_style - 3)} {text}"
        return text
    if element.HasField("table"):
        rows: builtins.list[builtins.list[str]] = []
        for row in element.table.table_rows:
            cells: builtins.list[str] = []
            for cell in row.table_cells:
                cell_blocks = [_report_structural_markdown(block) for block in cell.content]
                cells.append(
                    "<br>".join(block for block in cell_blocks if block).replace("|", "\\|")
                )
            rows.append(cells)
        if not rows:
            return ""
        width = max(len(row) for row in rows)
        normalized = [row + [""] * (width - len(row)) for row in rows]
        lines = ["| " + " | ".join(row) + " |" for row in normalized]
        lines.insert(1, "| " + " | ".join("---" for _ in range(width)) + " |")
        return "\n".join(lines)
    if element.HasField("horizontal_rule"):
        return "---"
    if element.HasField("code_block"):
        hint = element.code_block.language_hint
        return f"```{hint}\n{element.code_block.content}\n```"
    if element.HasField("a2ui_block"):
        return f"```json\n{element.a2ui_block.json}\n```"
    if element.HasField("image") and element.image.url:
        return f"![image]({element.image.url})"
    if element.HasField("thought"):
        return "\n\n".join(
            block
            for block in (_report_structural_markdown(child) for child in element.thought.elements)
            if block
        )
    return ""


def report_doc_markdown(document: Any) -> str:
    """Render the admitted TailwindDoc structural closure as Markdown."""

    if not document.HasField("body"):
        return ""
    blocks = [_report_structural_markdown(element) for element in document.body.content]
    for item in document.objects:
        if not item.HasField("citation"):
            continue
        citation = item.citation
        fragment = " ".join(
            block
            for block in (
                _report_structural_markdown(element) for element in citation.fragment.elements
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
