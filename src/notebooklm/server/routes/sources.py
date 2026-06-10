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
import tempfile
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
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


class SourceAddUrl(BaseModel):
    """Request body for adding a URL source."""

    url: str
    allow_internal: bool = False


class SourceAddText(BaseModel):
    """Request body for adding a text source."""

    text: str
    title: str | None = None


def _record_and_serialize(
    pending: PendingRegistry, notebook_id: str, result: add_core.SourceAddResult
) -> dict[str, Any]:
    """Record the new source id in the registry and project it to the wire."""
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
    plan = add_core.build_source_add_plan(
        content=body.url,
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
    return _record_and_serialize(pending, notebook_id, result)


@router.post("/text", status_code=201)
async def add_text(
    notebook_id: str, body: SourceAddText, client: ClientDep, pending: PendingDep
) -> dict[str, Any]:
    """Add an inline-text source."""
    plan = add_core.build_source_add_plan(
        content=body.text,
        source_type="text",
        title=body.title,
        mime_type=None,
        follow_symlinks=False,
        validate_path=add_core.validate_upload_path,
        looks_path_shaped=add_core.looks_like_path,
    )
    result = await add_core.execute_source_add(
        client, add_core.SourceAddExecutionPlan(notebook_id=notebook_id, plan=plan)
    )
    return _record_and_serialize(pending, notebook_id, result)


@router.post("/file", status_code=201)
async def add_file(
    notebook_id: str,
    client: ClientDep,
    pending: PendingDep,
    file: Annotated[UploadFile, File()],
    title: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Add a file source by spooling the multipart upload to a temp file.

    The temp file is created ``0o600`` with a unique ``mkstemp`` name, written
    under :data:`MAX_UPLOAD_BYTES`, and removed in a ``finally`` (so a mid-stream
    disconnect or a downstream error still cleans up). The upload safety here is
    the ``0o600`` / size-limit / unique-name discipline — ``validate_upload_path``
    guards a *caller-supplied* path string, not the server-generated temp path.
    """
    suffix = os.path.splitext(file.filename or "")[1]
    fd, temp_path = tempfile.mkstemp(suffix=suffix, prefix="nblm-upload-")
    try:
        os.fchmod(fd, 0o600)
        total = 0
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(_UPLOAD_CHUNK):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Upload exceeds the size limit")
                out.write(chunk)
        plan = add_core.build_source_add_plan(
            content=temp_path,
            source_type="file",
            title=title,
            mime_type=None,
            follow_symlinks=False,
            validate_path=add_core.validate_upload_path,
            looks_path_shaped=add_core.looks_like_path,
        )
        result = await add_core.execute_source_add(
            client, add_core.SourceAddExecutionPlan(notebook_id=notebook_id, plan=plan)
        )
        return _record_and_serialize(pending, notebook_id, result)
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:  # pragma: no cover - already gone
            pass


@router.delete("/{source_id}", status_code=204)
async def delete_source(
    notebook_id: str, source_id: str, client: ClientDep, pending: PendingDep
) -> JSONResponse:
    """Delete a source (idempotent)."""
    await client.sources.delete(notebook_id, source_id)
    pending.drop(notebook_id, source_id)
    return JSONResponse(status_code=204, content=None)
