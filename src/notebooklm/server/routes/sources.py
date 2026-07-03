"""Source routes — ``/v1/notebooks/{id}/sources`` list / get / add / delete.

Adapters over the transport-neutral ``_app.source_add`` core and the public
``client.sources`` namespace, with poll-the-resource status backed by the
in-process provenance registry (:mod:`.._pending`).

``add`` accepts ``url`` / ``text`` / ``file``:

* ``url`` / ``text`` flow through ``build_source_add_plan`` +
  ``execute_source_add`` (which runs the SSRF / upload-path validation).
* ``file`` spools the multipart body to a uniquely-named ``0o600`` temp file
  (under a max-upload-size limit), then runs the same core, and deletes the temp
  file in a ``finally`` (including on a mid-stream client disconnect).

A successful create records the source id in the pending registry. The GET poll
consults it to resolve the 200-vs-404 ambiguity that the client's
``get_or_none``-returns-``None`` alone cannot: a registry-known id returning
``None`` (the not-yet-listable window) → ``200`` pending; an unknown id → ``404``.
Once the source is ``READY`` it is dropped from the registry (now listable).

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

import asyncio
import math
import os
import shutil
import tempfile
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel

from ..._app import source_add as add_core
from ..._app import source_content as content_core
from ..._app import source_mutations as mut_core
from ..._app import source_wait as wait_core
from ..._app.errors import classify
from ..._app.views import source_view
from ...client import NotebookLMClient
from ...exceptions import ValidationError
from .._context import get_client, get_pending
from .._errors import CATEGORY_STATUS, error_item, safe_detail
from .._pagination import MAX_LIMIT, paginate_envelope
from .._pending import PendingRegistry
from ._passthrough import passthrough_source_id

__all__ = ["MAX_UPLOAD_BYTES", "MAX_WAIT_TIMEOUT", "router"]

router = APIRouter(prefix="/notebooks/{notebook_id}/sources", tags=["sources"])

ClientDep = Annotated[NotebookLMClient, Depends(get_client)]
PendingDep = Annotated[PendingRegistry, Depends(get_pending)]

#: Max accepted upload size. Bounds temp-file disk pressure under concurrent
#: uploads; an upload exceeding it is rejected with 413 before it is spooled to
#: completion. 200 MiB comfortably covers documents/audio while staying
#: single-user-safe.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

#: Chunk size when streaming an upload to the temp file.
_UPLOAD_CHUNK = 1024 * 1024

#: Upper bound on a ``source_wait`` timeout (seconds). Bounds how long a single
#: request can hold a worker; a caller wanting to wait longer re-polls. Also the
#: backstop that turns a ``timeout=inf`` (valid JSON) into a clean 400 rather
#: than a request that never returns.
MAX_WAIT_TIMEOUT = 3600.0


#: Safe-basename sanitizer for a spooled upload. Aliased to the shared neutral
#: helper (:func:`notebooklm._app.source_add.safe_upload_name`) so the REST
#: ``add_file`` route and the MCP ``/files/ul`` route sanitize identically —
#: control chars stripped, ``.``/``..`` and slashes rejected, extension preserved
#: on stem-truncation (the old ``basename(...)[:255]`` could drop the extension).
_safe_upload_name = add_core.safe_upload_name


class SourceAddUrl(BaseModel):
    """Request body for adding a URL source."""

    url: str
    allow_internal: bool = False


class SourceAddText(BaseModel):
    """Request body for adding a text source."""

    text: str
    title: str | None = None


class SourceAddDrive(BaseModel):
    """Request body for adding a Google Drive document source."""

    document_id: str
    title: str | None = None
    mime_type: mut_core.DriveMimeChoice = "google-doc"


class SourceAddBatch(BaseModel):
    """Request body for adding many http(s) URL sources in one call."""

    urls: list[str]
    allow_internal: bool = False


class SourceRename(BaseModel):
    """Request body for renaming a source (title only)."""

    title: str


class SourceWaitBody(BaseModel):
    """Request body for waiting on source readiness.

    Omitting ``source_ids`` waits for EVERY source in the notebook (mirroring the
    MCP ``source_wait`` all-sources mode).
    """

    source_ids: list[str] | None = None
    timeout: float = 120.0
    interval: float = 1.0


async def _add_source(
    client: NotebookLMClient,
    pending: PendingRegistry,
    notebook_id: str,
    *,
    content: str,
    source_type: add_core.SourceAddType,
    title: str | None,
    mime_type: str | None = None,
    allow_internal: bool = False,
) -> dict[str, Any]:
    """Build + execute a source-add, then record the new id and project it.

    Shared by the ``url`` / ``text`` / ``file`` handlers: each supplies its own
    ``content`` / ``source_type`` / ``title`` (and the URL handler its
    ``allow_internal`` flag), while the SSRF / upload-path validators and the
    execute → record → serialize tail live here once.
    """
    plan = add_core.build_source_add_plan(
        content=content,
        source_type=source_type,
        title=title,
        mime_type=mime_type,
        follow_symlinks=False,
        validate_path=add_core.validate_upload_path,
        looks_path_shaped=add_core.looks_like_path,
        allow_internal=allow_internal,
    )
    result = await add_core.execute_source_add(
        client, add_core.SourceAddExecutionPlan(notebook_id=notebook_id, plan=plan)
    )
    pending.record(notebook_id, result.source.id)
    # Project with the shared enriched view (string ``kind`` / ``status_label``
    # alongside the raw codes) so the create path matches ``GET`` — a raw
    # ``to_jsonable`` here would leak bare ``status`` / ``_type_code`` integers.
    return source_view(result.source)


@router.get("")
async def list_sources(
    notebook_id: str,
    client: ClientDep,
    limit: Annotated[int | None, Query(ge=1, le=MAX_LIMIT)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """List a notebook's sources.

    Each source carries string ``kind`` / ``status_label`` labels alongside the
    raw type/status codes (shared with the MCP ``source_list`` surface). Defaults
    to the full collection under ``sources`` (unchanged); supply ``?limit=`` to
    slice and add a ``meta`` block, ``?offset=`` to page forward.
    """
    sources = await client.sources.list(notebook_id)
    data = [source_view(s) for s in sources]
    return paginate_envelope(
        data, key="sources", limit=limit, offset=offset, notebook_id=notebook_id
    )


@router.get("/{source_id}")
async def get_source(
    notebook_id: str, source_id: str, client: ClientDep, pending: PendingDep
) -> dict[str, Any]:
    """Poll one source.

    A registry-known id returning ``None`` (the not-yet-listable window) → 200
    ``pending``; an unknown id → 404. A ``READY`` source is dropped from the
    registry and returned.
    """
    source = await client.sources.get_or_none(notebook_id, source_id)
    if source is None:
        if pending.knows(notebook_id, source_id):
            return {"notebook_id": notebook_id, "source_id": source_id, "status": "pending"}
        raise HTTPException(status_code=404, detail="Source not found")
    if source.is_ready:
        pending.drop(notebook_id, source_id)
    # Enriched view: string ``kind`` / ``status_label`` alongside the raw codes
    # (shared with the MCP source surface).
    return source_view(source)


@router.post("/url", status_code=201)
async def add_url(
    notebook_id: str, body: SourceAddUrl, client: ClientDep, pending: PendingDep
) -> dict[str, Any]:
    """Add a URL source (SSRF-validated via the neutral core)."""
    return await _add_source(
        client,
        pending,
        notebook_id,
        content=body.url,
        source_type="url",
        title=None,
        allow_internal=body.allow_internal,
    )


@router.post("/text", status_code=201)
async def add_text(
    notebook_id: str, body: SourceAddText, client: ClientDep, pending: PendingDep
) -> dict[str, Any]:
    """Add an inline-text source."""
    return await _add_source(
        client,
        pending,
        notebook_id,
        content=body.text,
        source_type="text",
        title=body.title,
    )


@router.post("/file", status_code=201)
async def add_file(
    notebook_id: str,
    client: ClientDep,
    pending: PendingDep,
    file: Annotated[UploadFile, File()],
    title: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Add a file source by spooling the multipart upload to a temp file.

    The multipart request MUST send a ``Content-Length`` header: a chunked
    (no-Content-Length) multipart upload is rejected with ``411`` by design, so
    the size can be bounded before any part is spooled to disk (see the app
    middleware in ``app.py``).

    The upload is spooled into a private ``0o700`` ``mkdtemp`` directory, named
    after the caller's basename (see :func:`_safe_upload_name`). The real name
    matters: the resumable-upload init derives the upload filename from the path,
    the source-id extraction keys off it, and the API 400s on an extensionless
    name — a random temp name breaks all three. ``basename`` strips directory
    components (traversal guard) and the unique directory isolates the file, so
    the caller's name is reproduced safely. The file is ``0o600`` and the whole
    directory is removed in a ``finally`` (so a mid-stream disconnect or a
    downstream error still cleans up). ``content_type`` is passed as the explicit
    upload mime.

    ``validate_upload_path`` guards a *caller-supplied* path string; our temp
    path is trusted, so we canonicalize it with ``realpath`` first — keeping the
    symlink-parent guard from tripping on a symlinked temp root (e.g. macOS
    ``/var`` → ``/private/var``).

    The per-chunk size check below caps the copy into *our* temp file; the
    primary disk-exhaustion guard is the Content-Length pre-check in the app
    middleware (see ``app.py``). For a chunked (no-Content-Length) upload that
    bypasses the pre-check, Starlette has already spooled the part before this
    runs, so the check is a backstop on our own write, not on Starlette's spool.
    """
    temp_dir = tempfile.mkdtemp(prefix="nblm-upload-")
    temp_path = os.path.join(temp_dir, _safe_upload_name(file.filename))
    try:
        # O_EXCL + 0o600: we own the unique dir, so the create cannot clobber.
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        total = 0
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(_UPLOAD_CHUNK):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Upload exceeds the size limit")
                out.write(chunk)
        return await _add_source(
            client,
            pending,
            notebook_id,
            content=os.path.realpath(temp_path),
            source_type="file",
            title=title,  # explicit override only; the upload already uses the real name
            mime_type=file.content_type,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@router.get("/{source_id}/content")
async def get_source_content(
    notebook_id: str,
    source_id: str,
    client: ClientDep,
    detail: Annotated[Literal["full", "summary"], Query()] = "full",
    output_format: Annotated[Literal["text", "markdown"], Query()] = "text",
    max_chars: Annotated[int | None, Query(ge=0)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Read a source's content (distinct from the status-poll ``GET /{source_id}``).

    ``detail=full`` (default) returns the extracted body, bounded by ``max_chars``
    (default 10,000) and windowed by ``offset``, with the FULL ``char_count`` and a
    ``truncated`` flag. ``content`` is ``null`` (``char_count`` 0) when the source
    is not READY yet or has no extractable text. A resolved id the backend no
    longer has is a 404 (existence-gated), never a false empty success. Unlike the
    status-poll ``GET /{source_id}`` route, this requires a *listable* source: a
    known-but-not-yet-listable (pending) id is a 404 here, not a pending indicator.

    ``output_format`` (``text`` default / ``markdown``) selects the extracted-body
    format for ``detail=full`` (ignored for ``summary``); ``markdown`` needs the
    server's ``markdownify`` extra and otherwise fails with a deterministic
    ``config`` error. Mirrors the MCP ``source_read`` tool's ``output_format``.

    ``detail=summary`` returns the AI source-guide digest ``{summary, keywords}``
    for cheap low-token triage.
    """
    if detail == "summary":
        # Existence guard so a missing source is a 404, not an empty guide.
        guard = await content_core.execute_source_get(
            client, content_core.SourceGetPlan(notebook_id=notebook_id, source_id=source_id)
        )
        if guard.source is None:
            raise HTTPException(status_code=404, detail="Source not found")
        guide = await content_core.execute_source_guide(
            client, content_core.SourceGuidePlan(notebook_id=notebook_id, source_id=source_id)
        )
        return {
            "notebook_id": notebook_id,
            "source_id": guide.source_id,
            "summary": guide.summary,
            "keywords": list(guide.keywords),
        }

    read = await content_core.execute_source_read(
        client,
        content_core.SourceReadPlan(
            notebook_id=notebook_id,
            source_id=source_id,
            output_format=output_format,
            max_chars=max_chars,
            offset=offset,
        ),
    )
    return {
        "notebook_id": notebook_id,
        "source_id": source_id,
        "content": read.content,
        "char_count": read.char_count,
        "truncated": read.truncated,
        "output_format": output_format,
    }


@router.post("/drive", status_code=201)
async def add_drive(
    notebook_id: str, body: SourceAddDrive, client: ClientDep, pending: PendingDep
) -> dict[str, Any]:
    """Add a Google Drive document as a source.

    ``mime_type`` is one of ``google-doc`` (default) / ``google-slides`` /
    ``google-sheets`` / ``pdf`` (validated by the neutral core, which 400s on an
    unknown value). Flows through ``_app.source_mutations.execute_source_add_drive``
    (the neutral ``source_add`` core has no Drive path).
    """
    result = await mut_core.execute_source_add_drive(
        client,
        mut_core.SourceAddDrivePlan(
            notebook_id=notebook_id,
            file_id=body.document_id,
            title=body.title or "",
            mime_type=body.mime_type,
        ),
    )
    pending.record(notebook_id, result.source.id)
    return source_view(result.source)


def _batch_item_is_fatal(exc: BaseException) -> bool:
    """Decide whether a per-item add failure must abort the whole batch.

    Per-item isolation is correct ONLY for per-URL, user-input failures (a bad
    URL / SSRF-blocked host / not-found → 4xx-input): those are properly reported
    as a per-entry ``error`` while the rest of the batch proceeds. A service /
    infrastructure failure (expired auth, rate limiting, an upstream 5xx /
    transport error) is NOT specific to the one URL — folding it into a per-item
    result would return a ``200``/``201`` batch envelope for what is really a
    top-level ``401`` / ``429`` / ``5xx``. So classify the exception and treat it
    as fatal (re-raise, letting the top-level handler map it) when its projected
    HTTP status is ``401`` / ``429`` or a server error (``>= 500``); everything
    else (``400`` bad URL, ``404`` not-found, ``409`` conflict, ``422``) stays
    isolated. ``CancelledError`` is a ``BaseException`` and is never passed here.
    """
    status = CATEGORY_STATUS[classify(exc).category]
    return status in (401, 429) or status >= 500


@router.post("/batch", status_code=201)
async def add_batch(
    notebook_id: str,
    body: SourceAddBatch,
    client: ClientDep,
    pending: PendingDep,
    response: Response,
) -> dict[str, Any]:
    """Add many http(s) URL sources in one call (mirrors MCP ``source_add`` batch).

    The shared notebook / auth context is validated ONCE up front (a bad
    ``notebook_id`` or stale auth surfaces as the normal top-level 404 / 401),
    so a whole-batch failure is never masked as ``201`` with every item errored.
    Only per-entry URL / add failures inside the loop are isolated — recorded as
    an ``error`` item and skipped, never aborting the batch — so partial failure
    stays visible. Each entry is added SEQUENTIALLY (concurrent bulk writes
    invite backend rate-limiting) with ``source_type="url"`` so the http/https
    SSRF guard runs per item. Results are positional (``results[i]`` ↔
    ``urls[i]``).

    The top-level ``status`` is ``"added"`` once at least one source was added,
    else ``"error"`` (every item failed) — so the envelope can't claim success
    while every ``results[].status`` says error (MCP ``_add_url_batch`` parity).
    An all-failed batch returns ``200`` (nothing was created) rather than ``201``.
    """
    if not body.urls:
        raise ValidationError("urls must contain at least one URL")
    # Validate the SHARED context once, before the per-item loop: a missing
    # notebook or stale auth would otherwise fail every entry identically and be
    # swallowed into a 201-all-errored body. Letting it raise here routes it
    # through the normal classify → 404 / 401 contract.
    await client.notebooks.get(notebook_id)
    results: list[dict[str, Any]] = []
    for entry in body.urls:
        try:
            plan = add_core.build_source_add_plan(
                content=entry,
                source_type="url",
                title=None,
                mime_type=None,
                follow_symlinks=False,
                validate_path=add_core.validate_upload_path,
                looks_path_shaped=add_core.looks_like_path,
                allow_internal=body.allow_internal,
            )
            result = await add_core.execute_source_add(
                client, add_core.SourceAddExecutionPlan(notebook_id=notebook_id, plan=plan)
            )
        except Exception as exc:  # noqa: BLE001 - per-item isolation; CancelledError still propagates
            # Re-raise service/infra failures (auth / rate-limit / server /
            # transport) so the top-level handler maps them to the correct
            # 401 / 429 / 5xx instead of masking them as a 200/201 batch
            # envelope; keep per-item isolation only for per-URL input failures.
            if _batch_item_is_fatal(exc):
                raise
            # ``error_item`` routes ``str(exc)`` through the shared ``_redact``
            # chokepoint (same scrubber as ``safe_detail``), so the per-item text
            # carries no raw exception/stack detail (CodeQL information-exposure).
            results.append({"input": entry, "status": "error", "error": error_item(exc)})
        else:
            pending.record(notebook_id, result.source.id)
            view = source_view(result.source)
            results.append(
                {
                    "input": entry,
                    "status": "added",
                    "source_id": result.source.id,
                    "title": result.source.title,
                    "status_label": view["status_label"],
                }
            )
    added = sum(1 for item in results if item["status"] == "added")
    # Nothing created → 200 (not 201). ``status`` mirrors the MCP batch envelope:
    # "added" when ≥1 succeeded, "error" when every item failed.
    if not added:
        response.status_code = 200
    return {
        "status": "added" if added else "error",
        "notebook_id": notebook_id,
        "added": added,
        "failed": len(results) - added,
        "results": results,
    }


@router.post("/wait")
async def wait_sources(notebook_id: str, body: SourceWaitBody, client: ClientDep) -> dict[str, Any]:
    """Wait for source(s) to finish processing (mirrors MCP ``source_wait``).

    Waits for the given ``source_ids``, or — when omitted — for EVERY source in
    the notebook. Both modes return the SAME aggregate::

        {"notebook_id", "ok", "ready", "timed_out", "failed", "not_found"}

    ``ready`` holds the sources that reached READY (each with ``kind`` /
    ``status_label`` labels); the error buckets hold ``{"source_id", "error"}``
    entries. ``ok`` is true iff all three error buckets are empty — the
    all-sources mode reports partial progress rather than discarding the sources
    that did become ready.
    """
    # Reject non-finite bounds first: JSON allows ``inf`` / ``NaN`` (Python's
    # json module parses ``Infinity`` / ``NaN``), and ``timeout=inf`` would wait
    # forever while ``NaN`` breaks every comparison. ``math.isfinite`` is False
    # for both.
    if not math.isfinite(body.timeout):
        raise ValidationError(f"timeout must be a finite number; got {body.timeout}")
    if not math.isfinite(body.interval):
        raise ValidationError(f"interval must be a finite number; got {body.interval}")
    if body.timeout < 0:
        raise ValidationError(f"timeout must be >= 0; got {body.timeout}")
    if body.timeout > MAX_WAIT_TIMEOUT:
        raise ValidationError(f"timeout must be <= {MAX_WAIT_TIMEOUT}; got {body.timeout}")
    if body.interval <= 0:
        raise ValidationError(f"interval must be > 0; got {body.interval}")
    if body.source_ids is not None:
        # An EXPLICIT empty list is rejected: it would otherwise return an
        # immediate ``ok:true`` with nothing waited on — a false "ready" for a
        # caller that serialized "all sources" as ``[]``. Omit ``source_ids``
        # (None) to wait for every source.
        if not body.source_ids:
            raise ValidationError(
                "source_ids must not be empty; omit it entirely to wait for all sources"
            )
        ids = list(body.source_ids)
    else:
        ids = [s.id for s in await client.sources.list(notebook_id)]
    outcomes = await _wait_all_sources(
        client, notebook_id, ids, timeout=body.timeout, interval=body.interval
    )
    return _aggregate_wait_outcomes(notebook_id, outcomes)


@router.patch("/{source_id}")
async def rename_source(
    notebook_id: str, source_id: str, body: SourceRename, client: ClientDep
) -> dict[str, Any]:
    """Rename a source (title only). Returns the enriched source view."""
    result = await mut_core.execute_source_rename(
        client,
        mut_core.SourceRenamePlan(
            notebook_id=notebook_id, source_id=source_id, new_title=body.title, json_output=True
        ),
        resolve_source_id=passthrough_source_id,
    )
    return source_view(result.source)


@router.delete("/{source_id}", status_code=204)
async def delete_source(
    notebook_id: str, source_id: str, client: ClientDep, pending: PendingDep
) -> Response:
    """Delete a source (idempotent)."""
    await client.sources.delete(notebook_id, source_id)
    pending.drop(notebook_id, source_id)
    return Response(status_code=204)


async def _wait_all_sources(
    client: NotebookLMClient,
    notebook_id: str,
    source_ids: list[str],
    *,
    timeout: float,
    interval: float,
) -> list[wait_core.SourceWaitOutcome]:
    """Wait for every source concurrently, one typed outcome per source.

    Each per-source wait runs through ``execute_source_wait`` (which maps the
    three handled ``SourceWait*`` failures to a typed outcome instead of raising),
    so a slow or failed source never discards its siblings' progress. An
    UNEXPECTED escape (auth/transport ``RPCError``, a bug) cancels + drains the
    still-running sibling pollers before re-raising (it then flows through the
    server's classify-once handler) rather than leaking coroutines.
    """
    tasks = [
        asyncio.create_task(
            wait_core.execute_source_wait(
                client,
                wait_core.SourceWaitPlan(
                    notebook_id=notebook_id, source_id=sid, timeout=timeout, interval=interval
                ),
            )
        )
        for sid in source_ids
    ]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _aggregate_wait_outcomes(
    notebook_id: str, outcomes: list[wait_core.SourceWaitOutcome]
) -> dict[str, Any]:
    """Project per-source wait outcomes onto the unified aggregate wire shape.

    Ready sources (enriched via :func:`source_view`) are returned alongside the
    ones that timed out / failed / went missing. ``ok`` is true iff nothing landed
    in an error bucket. (The MCP surface additionally annotates thin web-page
    content here; REST deliberately omits that costly extra fetch — Phase 1's
    scope decision — so this is warning-free.)
    """
    ready: list[dict[str, Any]] = []
    timed_out: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    not_found: list[dict[str, str]] = []
    for outcome in outcomes:
        if isinstance(outcome, wait_core.SourceWaitReady):
            ready.append(source_view(outcome.source))
        elif isinstance(outcome, wait_core.SourceWaitTimeout):
            timed_out.append(_wait_bucket_entry(outcome.error))
        elif isinstance(outcome, wait_core.SourceWaitProcessingError):
            failed.append(_wait_bucket_entry(outcome.error))
        elif isinstance(outcome, wait_core.SourceWaitNotFound):
            not_found.append(_wait_bucket_entry(outcome.error))
        else:  # exhaustive over the closed SourceWaitOutcome union
            raise AssertionError(f"unhandled SourceWaitOutcome: {outcome!r}")
    return {
        "notebook_id": notebook_id,
        "ok": not (timed_out or failed or not_found),
        "ready": ready,
        "timed_out": timed_out,
        "failed": failed,
        "not_found": not_found,
    }


def _wait_bucket_entry(error: Any) -> dict[str, str]:
    """Project a handled wait failure onto its ``{source_id, error}`` bucket entry.

    The message is scrubbed + length-capped via :func:`safe_detail` (the same
    ``_redact`` chokepoint the rest of the server error projection uses) so the
    bucket entry cannot leak a credential or a multi-kilobyte dump.
    """
    return {"source_id": error.source_id, "error": safe_detail(str(error))}
