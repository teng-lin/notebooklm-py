"""Download plumbing shared by the Studio (``studio.py``) tools and the remote
file-transfer route (``_fileroutes.py``).

The downloadable-artifact registry + the ref/transport helpers that back
``studio_download`` live here (rather than inside ``studio.py``) because
``_fileroutes.py`` needs the same ``_DOWNLOAD_SPECS`` / ``_resolve_artifact_id``
to serve a brokered download URL — keeping them in the tool module made it a
de-facto shared module. Split out to honor the ADR-0008 1000-line cap and to name
the coupling.

This module imports NO ``click`` / ``rich`` / ``cli``: the ``DownloadTypeSpec``
rows are rebuilt from the neutral ``_app.download`` types rather than imported
from ``cli/_download_specs.py``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from fastmcp.server.dependencies import get_http_request
from fastmcp.tools.tool import ToolResult
from mcp.types import ResourceLink, TextContent
from pydantic import AnyUrl

from ..._app import download as download_core
from ..._app.resolve import resolve_ref
from ...exceptions import ValidationError
from ...types import ArtifactType
from .._filelink import DOWNLOAD_TTL, FileTransferConfig

if TYPE_CHECKING:
    from ...client import NotebookLMClient

__all__ = [
    "INLINE_TEXT_MAX_CHARS",
    "DownloadType",
    "_DOWNLOAD_SPECS",
    "_INLINE_TEXT_TYPES",
    "_KIND_TO_DOWNLOAD_KEY",
    "_broker_download",
    "_is_http_transport",
    "_passthrough_download_notebook",
    "_read_inline_artifact_text",
    "_resolve_artifact_id",
    "download_extension",
    "download_filename",
    "download_mime_type",
]

#: Download-type keys whose file is UTF-8 text safe to ALSO return inline in the
#: ``studio_download`` tool result (alongside the ``resource_link``), so a host that
#: cannot open a ``resource_link`` — e.g. the claude.ai remote connector — can still
#: read the body. Every other kind is binary (audio/video/pdf/png) or structured
#: JSON best consumed as a file. #1907.
_INLINE_TEXT_TYPES: frozenset[str] = frozenset({"report", "data-table"})

#: Max chars of inline artifact text returned alongside the download link, mirroring
#: ``source_read``'s 10k content cap (ADR-0025). A longer body is truncated (the tool
#: marks ``truncated`` and appends a marker to the inline block); the full file stays
#: reachable via the ``resource_link`` / signed URL.
INLINE_TEXT_MAX_CHARS = 10_000

#: Appended to the inline TEXT block (not the structured ``content`` prefix) when the
#: body was truncated, pointing the reader at the link for the complete file.
_INLINE_TRUNCATION_MARKER = "\n\n[… truncated — open the download link above for the full file …]"

#: The downloadable artifact-type keys (the ``artifact_type`` param's enum).
DownloadType = Literal[
    "audio",
    "video",
    "slide-deck",
    "infographic",
    "report",
    "mind-map",
    "data-table",
    "quiz",
    "flashcards",
]

#: Download type registry, rebuilt from the neutral ``_app.download`` types so this
#: module never imports the Click-coupled ``cli/_download_specs.py``. Each row
#: mirrors the corresponding CLI ``DownloadTypeSpec`` (name / kind / extension /
#: download method / optional ``--format`` wiring).
_DOWNLOAD_SPECS: dict[str, download_core.DownloadTypeSpec] = {
    "audio": download_core.DownloadTypeSpec(
        name="audio",
        kind=ArtifactType.AUDIO,
        extension=".mp3",
        default_dir="./audio",
        download_attr="download_audio",
        help_summary="",
        help_examples="",
    ),
    "video": download_core.DownloadTypeSpec(
        name="video",
        kind=ArtifactType.VIDEO,
        extension=".mp4",
        default_dir="./video",
        download_attr="download_video",
        help_summary="",
        help_examples="",
    ),
    "slide-deck": download_core.DownloadTypeSpec(
        name="slide-deck",
        kind=ArtifactType.SLIDE_DECK,
        extension=".pdf",
        default_dir="./slide-decks",
        download_attr="download_slide_deck",
        format_choices=("pdf", "pptx"),
        format_default="pdf",
        format_extension_map={"pdf": ".pdf", "pptx": ".pptx"},
        format_kwarg="output_format",
        forward_format_only_if_set=True,
        help_summary="",
        help_examples="",
    ),
    "infographic": download_core.DownloadTypeSpec(
        name="infographic",
        kind=ArtifactType.INFOGRAPHIC,
        extension=".png",
        default_dir="./infographic",
        download_attr="download_infographic",
        help_summary="",
        help_examples="",
    ),
    "report": download_core.DownloadTypeSpec(
        name="report",
        kind=ArtifactType.REPORT,
        extension=".md",
        default_dir="./reports",
        download_attr="download_report",
        help_summary="",
        help_examples="",
    ),
    "mind-map": download_core.DownloadTypeSpec(
        name="mind-map",
        kind=ArtifactType.MIND_MAP,
        extension=".json",
        default_dir="./mind-maps",
        download_attr="download_mind_map",
        help_summary="",
        help_examples="",
    ),
    "data-table": download_core.DownloadTypeSpec(
        name="data-table",
        kind=ArtifactType.DATA_TABLE,
        extension=".csv",
        default_dir="./data-tables",
        download_attr="download_data_table",
        help_summary="",
        help_examples="",
    ),
    "quiz": download_core.DownloadTypeSpec(
        name="quiz",
        kind=ArtifactType.QUIZ,
        extension=".json",
        default_dir="./quizzes",
        download_attr="download_quiz",
        format_choices=("json", "markdown", "html"),
        format_default="json",
        format_extension_map=dict(download_core.FORMAT_EXTENSIONS),
        format_kwarg="output_format",
        help_summary="",
        help_examples="",
    ),
    "flashcards": download_core.DownloadTypeSpec(
        name="flashcards",
        kind=ArtifactType.FLASHCARDS,
        extension=".json",
        default_dir="./flashcards",
        download_attr="download_flashcards",
        format_choices=("json", "markdown", "html"),
        format_default="json",
        format_extension_map=dict(download_core.FORMAT_EXTENSIONS),
        format_kwarg="output_format",
        help_summary="",
        help_examples="",
    ),
}

#: Reverse of ``_DOWNLOAD_SPECS`` — an artifact's ``ArtifactType`` (``.kind``) → the
#: download-type key. Lets ``studio_download`` derive ``artifact_type`` from an
#: ``artifact`` name-or-id ref (so the caller need not repeat the type).
_KIND_TO_DOWNLOAD_KEY: dict[Any, DownloadType] = {
    spec.kind: cast(DownloadType, key) for key, spec in _DOWNLOAD_SPECS.items()
}

#: The ONE file-extension → MIME-type table. Both the ``studio_download`` tool
#: payload (:func:`_broker_download`) and the ``/files/dl`` route derive their
#: Content-Type from this via :func:`download_mime_type`, so the advertised
#: ``mime_type`` and the byte stream's ``Content-Type`` can never drift. Keyed by
#: the extension the spec+format already resolve to, so a new download type only
#: needs its extension mapped here.
_EXTENSION_MIME_TYPES: dict[str, str] = {
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".pdf": "application/pdf",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".png": "image/png",
    ".md": "text/markdown",
    ".json": "application/json",
    ".csv": "text/csv",
    ".html": "text/html",
}

#: Fallback when an extension isn't in the table (unreachable for minted tokens —
#: every spec extension is mapped — but keeps the helpers total).
_DEFAULT_MIME = "application/octet-stream"


def download_extension(spec: download_core.DownloadTypeSpec, output_format: str | None) -> str:
    """The file extension a download of ``spec`` in ``output_format`` will carry.

    ``output_format`` selects the extension for the format-bearing types
    (slide-deck pdf/pptx; quiz/flashcards json/markdown/html) via the spec's
    ``format_extension_map``; ``None`` (or a leaf with no format axis) yields the
    spec's default ``extension`` (which is already the default format's extension).
    """
    if output_format:
        return spec.format_extension_map.get(output_format, spec.extension)
    return spec.extension


def download_filename(
    spec: download_core.DownloadTypeSpec, title: str | None, output_format: str | None
) -> str:
    """The download filename for ``spec`` — the artifact ``title`` (falling back to
    the type name when unknown, e.g. the latest-by-type path) plus the
    format-resolved extension, sanitized by the shared
    :func:`~notebooklm._app.download.artifact_title_to_filename`.
    """
    base = title if title else spec.name
    return download_core.artifact_title_to_filename(
        base, download_extension(spec, output_format), set()
    )


def download_mime_type(spec: download_core.DownloadTypeSpec, output_format: str | None) -> str:
    """The MIME type for a download of ``spec`` in ``output_format`` (central table)."""
    return _EXTENSION_MIME_TYPES.get(download_extension(spec, output_format), _DEFAULT_MIME)


async def _passthrough_download_notebook(notebook_id: str) -> str:
    """Async pass-through notebook resolver for the download core."""
    return notebook_id


def _resolve_artifact_id(artifacts: list[Any], artifact_id: str) -> str:
    """Resolve a full / partial / UUID artifact id against the type-filtered list.

    Wraps the transport-neutral :func:`resolve_ref` (full-UUID fast-path, exact
    match, unique prefix; ambiguous / no-match prefixes raise ``ValidationError`` /
    ``AmbiguousIdError``). The fast-path returns a canonical UUID **verbatim**
    without scanning ``artifacts``, so we match it case-insensitively against the
    pre-fetched list and return the list's own id. This:

    * fixes uppercase full UUIDs — ``select_artifact`` compares ids
      case-sensitively, so returning the token's casing would spuriously miss; and
    * makes a not-found full UUID raise the SAME hard error as a not-found /
      ambiguous prefix (→ ``ToolError`` on stdio, 400 on the remote route) instead
      of falling through to the download core's soft ``ERROR`` outcome — matching
      how ``_resolve.py`` resolves notebooks / sources (every miss is ``NOT_FOUND``).
    """
    resolved = resolve_ref(
        artifact_id,
        artifacts,
        id_of=lambda a: a["id"],
        title_of=lambda a: a.get("title"),
    ).id
    # The full-UUID fast-path returns the caller's casing verbatim; for a prefix
    # match ``resolved`` is already the list's canonical id. A single
    # case-insensitive scan normalizes both and confirms membership.
    resolved_lower = resolved.lower()
    for artifact in artifacts:
        if str(artifact["id"]).lower() == resolved_lower:
            return str(artifact["id"])
    # Mirror ``select_artifact``'s "Artifact <id> not found" wording so the message
    # is uniform whether the miss is caught here or by the core.
    raise ValidationError(f"Artifact {artifact_id} not found")


def _is_http_transport() -> bool:
    """Whether the current tool call arrived over the http transport.

    A remote (http) call has an active Starlette request; stdio does not
    (:func:`get_http_request` raises ``RuntimeError``). Lets a remote download
    *without* file transfer configured report a clean "not configured" error
    instead of the stdio "requires path" error.
    """
    try:
        get_http_request()
    except RuntimeError:
        return False
    return True


async def _read_inline_artifact_text(
    client: NotebookLMClient,
    notebook_id: str,
    spec: download_core.DownloadTypeSpec,
    output_format: str | None,
    artifact_id: str | None,
) -> tuple[str, int, bool] | None:
    """Download a TEXT artifact server-side and return ``(content, char_count,
    truncated)`` bounded to :data:`INLINE_TEXT_MAX_CHARS`, or ``None`` when no
    completed artifact of the type is available.

    Used by :func:`_broker_download` to inline a report / data-table body so a
    resource-link-incapable host can read it (#1907). Reuses the SAME
    :func:`~notebooklm._app.download.execute_download` path the ``/files/dl`` route
    serves — spooling to a private temp file and reading it back — so the inline
    text is byte-identical to the file the signed link hands out. A missing artifact
    or a soft download error yields ``None`` (the link still stands); an upstream RPC
    failure propagates (the link would fail identically).

    ``content`` is the clean first-``INLINE_TEXT_MAX_CHARS``-chars prefix; ``char_count``
    is the FULL length; ``truncated`` says whether the prefix omits any tail. Read with
    ``utf-8-sig`` so a data-table CSV's BOM is stripped (a report is plain ``utf-8``).
    """
    temp_dir = await asyncio.to_thread(tempfile.mkdtemp, prefix="nblm-mcp-inline-")
    try:
        temp_path = os.path.join(temp_dir, f"artifact{spec.extension}")
        args: dict[str, Any] = {
            "notebook_id": notebook_id,
            "output_path": temp_path,
            "latest": artifact_id is None,
        }
        if artifact_id is not None:
            args["artifact_id"] = artifact_id
        if output_format is not None:
            args[spec.format_param_name] = output_format
        plan = download_core.build_download_plan(spec, args, cwd=Path.cwd())
        result = await download_core.execute_download(
            plan,
            client,
            notebook_resolver=_passthrough_download_notebook,
            artifact_resolver=_resolve_artifact_id,
        )
        if result.outcome != download_core.DownloadOutcome.SINGLE_DOWNLOADED:
            # No completed artifact / soft download error — return nothing so the
            # broker still hands out the link (which will surface the same state).
            return None
        served = result.output_path or temp_path
        text = await asyncio.to_thread(Path(served).read_text, encoding="utf-8-sig")
    finally:
        await asyncio.to_thread(shutil.rmtree, temp_dir, ignore_errors=True)

    char_count = len(text)
    truncated = char_count > INLINE_TEXT_MAX_CHARS
    return text[:INLINE_TEXT_MAX_CHARS], char_count, truncated


def _broker_download(
    cfg: FileTransferConfig,
    notebook_id: str,
    artifact_type: str,
    output_format: str | None,
    artifact_id: str | None = None,
    *,
    title: str | None = None,
    inline: tuple[str, int, bool] | None = None,
) -> ToolResult:
    """Mint a signed download URL + a clickable ``resource_link`` for a remote
    ``studio_download``.

    Returns a :class:`ToolResult` carrying BOTH a ``resource_link`` content item
    (claude.ai renders it clickable) and the structured ``download_ready`` payload.
    The signer injects expiry; ``expires_at`` mirrors the download TTL.

    The payload is self-describing so a client can render a download affordance
    before opening the URL: ``filename`` (the artifact ``title`` — falling back to
    the type name on the latest-by-type path where no id was resolved — plus the
    format-resolved extension) and ``mime_type`` both come from the SAME central
    helpers the ``/files/dl`` route serves with, so the advertised metadata and the
    streamed bytes can't drift. ``size_bytes`` is ``None``: it can't be known
    without eagerly fetching the artifact, which this must not do.

    ``inline`` (``(content, char_count, truncated)``, from
    :func:`_read_inline_artifact_text` for text kinds — report / data-table) adds the
    bounded body to the payload AND as a ``TextContent`` block, so a host that cannot
    open the ``resource_link`` can still read it (#1907). ``content`` is the bounded
    prefix, ``char_count`` the full length, ``truncated`` whether the prefix omits a
    tail; the inline block appends a truncation marker when truncated.
    """
    spec = _DOWNLOAD_SPECS[artifact_type]
    payload: dict[str, Any] = {
        "nb": notebook_id,
        "atype": artifact_type,
    }  # op stamped by download_url
    if artifact_id is not None:
        payload["aid"] = artifact_id
    if output_format is not None:
        payload["fmt"] = output_format
    url = cfg.download_url(payload)
    structured: dict[str, Any] = {
        "status": "download_ready",
        "notebook_id": notebook_id,
        "artifact_type": artifact_type,
        "filename": download_filename(spec, title, output_format),
        "mime_type": download_mime_type(spec, output_format),
        # Unknown without eagerly downloading (which we refuse to do); the route
        # sets the real Content-Length when the link is opened.
        "size_bytes": None,
        "url": url,
        "expires_at": int(time.time()) + DOWNLOAD_TTL,
    }
    if artifact_id is not None:
        # Echo the targeted id the link was brokered for, so the agent's response
        # records what it asked for (the token carries it, but the structured
        # payload should be self-describing).
        structured["artifact_id"] = artifact_id
        desc = f"Download {artifact_type} artifact {artifact_id} (link expires)."
    else:
        desc = f"Download the latest {artifact_type} artifact (link expires)."
    link = ResourceLink(
        type="resource_link",
        name=f"{artifact_type} download",
        # ResourceLink.uri is an AnyUrl — construct it explicitly rather than
        # passing the raw str (keeps mypy happy across pydantic-stub versions:
        # a bare str needed a [arg-type] ignore that CI's stubs flagged unused).
        uri=AnyUrl(url),
        description=desc,
    )
    content: list[Any] = [link]
    if inline is not None:
        inline_content, char_count, truncated = inline
        structured["content"] = inline_content
        structured["char_count"] = char_count
        structured["truncated"] = truncated
        block = inline_content + _INLINE_TRUNCATION_MARKER if truncated else inline_content
        content.append(TextContent(type="text", text=block))
    return ToolResult(content=content, structured_content=structured)
