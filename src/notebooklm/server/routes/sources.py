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

import os
import shutil
import tempfile
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel

from ..._app import source_add as add_core
from ..._app.serialize import to_jsonable
from ...client import NotebookLMClient
from .._context import get_client, get_pending
from .._pending import PendingRegistry

__all__ = ["MAX_UPLOAD_BYTES", "router"]

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


def _safe_upload_name(filename: str | None) -> str:
    """Return a safe basename for the spooled upload file.

    The resumable-upload init derives the upload filename from the temp path and
    the source-id extraction keys off it, so the file must keep the caller's
    *real* name (with its extension — the API 400s on an extensionless one).
    :func:`os.path.basename` strips any directory components (the path-traversal
    guard); the file is then created inside a private ``mkdtemp`` directory, so
    even an odd basename is isolated. Falls back to ``"upload"`` for an empty
    name and bounds the length.
    """
    return (os.path.basename(filename or "") or "upload")[:255]


class SourceAddUrl(BaseModel):
    """Request body for adding a URL source."""

    url: str
    allow_internal: bool = False


class SourceAddText(BaseModel):
    """Request body for adding a text source."""

    text: str
    title: str | None = None


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
    return to_jsonable(result.source)


@router.get("")
async def list_sources(notebook_id: str, client: ClientDep) -> dict[str, Any]:
    """List a notebook's sources."""
    sources = await client.sources.list(notebook_id)
    return {"notebook_id": notebook_id, "sources": to_jsonable(sources)}


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
    return to_jsonable(source)


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


@router.delete("/{source_id}", status_code=204)
async def delete_source(
    notebook_id: str, source_id: str, client: ClientDep, pending: PendingDep
) -> Response:
    """Delete a source (idempotent)."""
    await client.sources.delete(notebook_id, source_id)
    pending.drop(notebook_id, source_id)
    return Response(status_code=204)
