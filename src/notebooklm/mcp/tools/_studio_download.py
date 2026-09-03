"""Download plumbing shared by the Studio (``studio.py``) tools and the remote
file-transfer route (``_fileroutes.py``).

The downloadable-artifact registry + the ref/transport helpers that back
``studio_download`` live here (rather than inside ``studio.py``) because
``_fileroutes.py`` needs the same ``_DOWNLOAD_SPECS`` / ``_resolve_artifact_id``
to serve a brokered download URL — keeping them in the tool module made it a
de-facto shared module. Split out to honor the ADR-0008 1000-line cap and to name
the coupling.

This module imports NO ``click`` / ``rich`` / ``cli``: it consumes the canonical
neutral registry from ``_app.download_specs`` directly.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NamedTuple, TypeAlias, cast

from fastmcp.server.dependencies import get_http_request
from fastmcp.tools.tool import ToolResult
from mcp.types import ResourceLink, TextContent
from pydantic import AnyUrl, BeforeValidator, WithJsonSchema

from ..._app import download as download_core
from ..._app import download_specs as download_specs_core
from ..._app.resolve import resolve_ref
from ...exceptions import AuthError, NotebookLMError, ValidationError
from .._filelink import DOWNLOAD_TTL, FileTransferConfig

if TYPE_CHECKING:
    from ...client import NotebookLMClient

__all__ = [
    "INLINE_TEXT_MAX_CHARS",
    "DownloadFormat",
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

#: Chunk size (chars) for streaming past the inline prefix to count the remaining
#: length without materializing the whole file in memory.
_INLINE_READ_CHUNK = 65536

#: Cap concurrent inline reads. Each spools an artifact to a private temp dir and
#: fetches it from Google before reading it back, so many parallel report/data-table
#: ``studio_download`` calls could otherwise drive unbounded temp disk + upstream
#: fetch fan-out (mirrors ``_fileroutes._MAX_CONCURRENT_DOWNLOADS``). Because inline
#: text is best-effort, exceeding the cap simply SKIPS the inline body (the tool still
#: returns the link) rather than erroring. A plain counter (mutated only between
#: ``await`` points in this single-process async server) suffices — no lock, and no
#: asyncio primitive that would bind to one event loop.
_MAX_CONCURRENT_INLINE_READS = 4
_inflight_inline_reads = 0


class _InlineText(NamedTuple):
    """The bounded inline body of a text artifact plus the artifact it came from.

    ``artifact_id`` / ``title`` are the CONCRETE artifact ``execute_download`` selected
    (even on the "latest" path where the caller passed no id), so the broker can PIN the
    signed link to the exact artifact whose body was inlined — otherwise a "latest" link
    could resolve to a newer artifact than the inline text if one completes in between.
    """

    content: str
    char_count: int
    truncated: bool
    artifact_id: str | None
    title: str | None


def _read_bounded_text(path: str) -> tuple[str, int, bool]:
    """Read the first :data:`INLINE_TEXT_MAX_CHARS` chars of ``path`` as the inline
    ``content`` and stream the rest only to COUNT it — returning
    ``(content, char_count, truncated)`` without ever holding more than the prefix plus
    one chunk in memory (so a large data-table/report can't OOM the tool call).

    ``char_count`` is the FULL post-decode length (mirroring ``source_read``); read with
    ``utf-8-sig`` so a data-table CSV's BOM is stripped (a report is plain ``utf-8``).
    """
    with open(path, encoding="utf-8-sig") as fh:
        content = fh.read(INLINE_TEXT_MAX_CHARS)
        remainder = 0
        while True:
            chunk = fh.read(_INLINE_READ_CHUNK)
            if not chunk:
                break
            remainder += len(chunk)
    return content, len(content) + remainder, remainder > 0


def _validate_download_type(value: Any) -> str:
    choices = download_specs_core.DOWNLOAD_SPECS_BY_NAME
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"expected one of {sorted(choices)}")
    return value


def _validate_download_format(value: Any) -> str:
    choices = download_specs_core.DOWNLOAD_FORMAT_NAMES
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"expected one of {list(choices)}")
    return value


#: MCP schema aliases derived from the canonical registry. ``WithJsonSchema``
#: keeps the same compact enums that the old hand-written ``Literal`` aliases
#: advertised, while ``BeforeValidator`` preserves boundary validation.
DownloadType: TypeAlias = Annotated[
    str,
    BeforeValidator(_validate_download_type),
    WithJsonSchema({"type": "string", "enum": list(download_specs_core.DOWNLOAD_SPECS_BY_NAME)}),
]
DownloadFormat: TypeAlias = Annotated[
    str,
    BeforeValidator(_validate_download_format),
    WithJsonSchema({"type": "string", "enum": list(download_specs_core.DOWNLOAD_FORMAT_NAMES)}),
]

#: Shared immutable projection. MCP adds no per-adapter fields.
_DOWNLOAD_SPECS: Mapping[str, download_core.DownloadTypeSpec] = (
    download_specs_core.DOWNLOAD_SPECS_BY_NAME
)

#: Reverse of ``_DOWNLOAD_SPECS`` — an artifact's ``ArtifactType`` (``.kind``) → the
#: download-type key. Lets ``studio_download`` derive ``artifact_type`` from an
#: ``artifact`` name-or-id ref (so the caller need not repeat the type).
_KIND_TO_DOWNLOAD_KEY: dict[Any, DownloadType] = {
    spec.kind: cast(DownloadType, key) for key, spec in _DOWNLOAD_SPECS.items()
}


def download_extension(spec: download_core.DownloadTypeSpec, output_format: str | None) -> str:
    """The file extension a download of ``spec`` in ``output_format`` will carry.

    Thin alias for :func:`~notebooklm._app.download.resolve_extension`, kept for
    this module's established name (it is in ``__all__`` and read by
    ``_fileroutes``); the rule itself lives in the neutral core so the REST
    ``/download`` route resolves extensions identically.
    """
    return download_core.resolve_extension(spec, output_format)


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
    """The MIME type for a download of ``spec`` in ``output_format``.

    Both the ``studio_download`` tool payload (:func:`_broker_download`) and the
    ``/files/dl`` route derive their Content-Type from here, so they read the one
    :data:`~notebooklm._app.download.EXTENSION_MIME_TYPES` table the REST
    ``/download`` response uses and can never drift from it.
    """
    return download_core.mime_type_for_extension(download_extension(spec, output_format))


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
) -> _InlineText | None:
    """Download a TEXT artifact server-side and return its bounded inline body plus the
    concrete artifact it was read from (:class:`_InlineText`), or ``None`` when no
    completed artifact of the type is available.

    Used by :func:`_broker_download` to inline a report / data-table body so a
    resource-link-incapable host can read it (#1907). Reuses the SAME
    :func:`~notebooklm._app.download.execute_download` path the ``/files/dl`` route
    serves — spooling to a private temp file and reading it back — so the inline
    text is byte-identical to the file the signed link hands out.

    On the "latest" path (``artifact_id is None``) ``execute_download`` still selects a
    CONCRETE artifact; its id + title ride back in the result so the broker can pin the
    signed link to the same artifact (see :class:`_InlineText`). The body is read with a
    bounded/streaming reader (:func:`_read_bounded_text`) so a large export can't OOM the
    call — only the ``INLINE_TEXT_MAX_CHARS`` prefix is held; the tail is counted, not
    materialized.

    Inline text is strictly **best-effort**: a missing/incomplete artifact, a soft
    download error, OR an upstream list/RPC failure all yield ``None`` so the broker
    still hands out the ``resource_link`` (the guaranteed deliverable — the link needs
    no RPC to mint on the "latest" path, and opening it later re-runs this fetch, so a
    transient hiccup must not fail the whole ``studio_download`` call). Explicit-id
    refs are already validated before this runs, so a swallowed error here can only be
    an infra/transient failure, never a bad-id miss.

    Bounded by :data:`_MAX_CONCURRENT_INLINE_READS`: when too many inline reads are
    already in flight this returns ``None`` (link-only) WITHOUT spooling — so a burst of
    concurrent report/data-table downloads can't exhaust temp disk / upstream fetch
    fan-out (the best-effort contract makes skipping safe).
    """
    global _inflight_inline_reads
    if _inflight_inline_reads >= _MAX_CONCURRENT_INLINE_READS:
        return None
    _inflight_inline_reads += 1
    try:
        return await _do_read_inline_artifact_text(
            client, notebook_id, spec, output_format, artifact_id
        )
    finally:
        _inflight_inline_reads -= 1


async def _do_read_inline_artifact_text(
    client: NotebookLMClient,
    notebook_id: str,
    spec: download_core.DownloadTypeSpec,
    output_format: str | None,
    artifact_id: str | None,
) -> _InlineText | None:
    """Spool + read one inline artifact (the body of :func:`_read_inline_artifact_text`,
    split out so the concurrency-counter increment/decrement wraps it cleanly)."""
    # mkdtemp runs synchronously (it is a quick syscall, and this is how _fileroutes
    # spools) so the dir handle is bound to ``temp_dir`` in the SAME step it is created:
    # an ``await asyncio.to_thread(mkdtemp)`` could be cancelled after the dir exists but
    # before the result is assigned, orphaning it past the ``finally`` cleanup.
    temp_dir = tempfile.mkdtemp(prefix="nblm-mcp-inline-")
    try:
        # Format-resolved like every other spool site, so the invariant "a spooled
        # download is named by ``resolve_extension``" holds everywhere and greps
        # clean. Today's inline kinds (report / data-table) have no format axis, so
        # this is identical to ``spec.extension`` — it is the future format-bearing
        # text type that would otherwise reintroduce the #2034 mislabel here.
        temp_path = os.path.join(temp_dir, f"artifact{download_extension(spec, output_format)}")
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
        try:
            result = await download_core.execute_download(
                plan,
                client,
                notebook_resolver=_passthrough_download_notebook,
                artifact_resolver=_resolve_artifact_id,
            )
        except AuthError:
            # Authentication is actionable and must reach ``mcp_errors`` so
            # the tool returns the typed AUTH envelope instead of link-only.
            raise
        except NotebookLMError:
            # Upstream list/RPC failure (execute_download does not wrap its own list
            # call). Inline text is best-effort, so degrade to link-only rather than
            # failing the whole download.
            return None
        if result.outcome != download_core.DownloadOutcome.SINGLE_DOWNLOADED:
            # No completed artifact / soft download error — return nothing so the
            # broker still hands out the link (which will surface the same state).
            return None
        served = result.output_path or temp_path
        try:
            content, char_count, truncated = await asyncio.to_thread(_read_bounded_text, served)
        except (OSError, ValueError):
            # Best-effort: a local read/decode failure (I/O error, or a malformed file
            # that isn't valid UTF-8 → UnicodeError ⊂ ValueError) degrades to link-only,
            # same as the soft download failures above — it must NOT sink the whole
            # studio_download call and its guaranteed resource_link.
            return None
    finally:
        await asyncio.to_thread(shutil.rmtree, temp_dir, ignore_errors=True)

    # The concrete artifact execute_download selected — id + title — so the broker can
    # PIN the link to it (the "latest" path passed no id but resolved one here).
    selected = result.artifact or {}
    return _InlineText(content, char_count, truncated, selected.get("id"), selected.get("title"))


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
