"""Signed-URL file-transfer routes mounted on the FastMCP http app (ADR-0024).

Three custom routes carry binaries **outside** the MCP JSON-RPC channel so the
claude.ai connector works for local-file upload and artifact download::

    GET      /files/dl/{token}   -> stream the artifact   (FileResponse)
    GET      /files/ul/{token}   -> minimal upload page    (file picker + fetch POST)
    POST|PUT /files/ul/{token}   -> stream a RAW body -> add the source

The HMAC-signed token is the **sole** auth for these routes: a browser opening a
signed link cannot carry the MCP bearer/OAuth credential, and FastMCP does not
wrap custom routes with ``RequireAuthMiddleware`` (only the ``/mcp`` route) — a
regression test pins both facts. The token encodes the operation parameters
(notebook id, title/mime, artifact type/format), so the handlers hold no state.

Upload is a **raw request body** (``request.stream()``), never ``request.form()``:
``python-multipart`` is in the ``server`` extra only, not ``mcp``, and a raw body
also lets a sandbox ``curl``/``PUT`` (the FutureSearch pattern) reuse the same
handler. The real DoS defense is the **running byte cap** while streaming into the
temp file; the ``Content-Length`` check is just an early 413.

This module imports NOTHING from ``server/`` (which pulls ``fastapi`` — absent on
``mcp``-only installs) and NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

import html
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)

from .._app import download as download_core
from .._app import source_add as add_core
from ..exceptions import ValidationError
from ._context import get_client_from_app
from ._filelink import FileLinkError, FileTransferConfig
from .tools.artifacts import _DOWNLOAD_SPECS

if TYPE_CHECKING:
    from fastmcp import FastMCP

#: Max accepted upload size (mirrors the REST route's ``MAX_UPLOAD_BYTES``). Bounds
#: temp-file disk pressure; an upload exceeding it is rejected with 413 — early via
#: ``Content-Length``, and authoritatively via the running byte cap below.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

#: Cap concurrent in-flight uploads. The per-request byte cap bounds ONE upload to
#: 200 MiB, but a leaked/replayable ``ul`` token (valid for its full TTL) could
#: otherwise drive N parallel streams = N×200 MiB of transient temp disk →
#: ENOSPC. This bounds aggregate temp pressure to ``_MAX_CONCURRENT_UPLOADS`` ×
#: ``MAX_UPLOAD_BYTES``; excess uploads get a fast 429 (no disk touched). A single
#: process serves the single tenant, so a plain counter (mutated only between
#: ``await`` points, never concurrently) is sufficient — no lock needed.
_MAX_CONCURRENT_UPLOADS = 4
_inflight_uploads = 0

#: Security headers for the HTML pages. The signed token rides in the URL path, so
#: bound its exposure: ``no-referrer`` keeps it out of any ``Referer``, ``no-store``
#: out of caches, ``DENY`` out of frames, plus a strict CSP (the upload page's only
#: script is its own inline ``fetch``; it posts same-origin).
_HTML_SECURITY_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; form-action 'none'; base-uri 'none'"
    ),
}


def _safe_upload_name(filename: str | None) -> str:
    """Return a safe basename for the spooled upload file.

    The browser's ``fetch(body: file)`` does NOT send the filename, so the page
    passes it as ``?filename=``; NotebookLM 400s on an extensionless name and the
    source-id extraction keys off the real basename+extension, so we must keep the
    caller's name. :func:`os.path.basename` strips directory components (the
    path-traversal guard), and the file lands in a private ``mkdtemp`` dir so an odd
    basename is isolated. An empty/extensionless-default falls back to
    ``"upload.bin"`` (never extensionless). Re-implemented locally on purpose — the
    REST route's twin lives behind the ``server`` extra (``fastapi``), which this
    ``mcp``-only module must not import.
    """
    # Strip control chars (NUL would make ``os.open`` raise ``ValueError``; the rest
    # are never legitimate in a filename), normalize ``\`` so a Windows-style
    # ``C:\dir\x.pdf`` from a sandbox PUT yields its real leaf, then take the
    # basename (the path-traversal guard). Reject the directory-cursor names
    # ``.``/``..`` (which would target an existing dir and fail ``O_EXCL`` → 500) —
    # fall back to a safe extensioned default.
    cleaned = re.sub(r"[\x00-\x1f]", "", filename or "").replace("\\", "/")
    base = os.path.basename(cleaned)
    if not base or base in (".", ".."):
        return "upload.bin"
    if len(base) > 255:
        # Truncate the STEM, not the whole name — lopping a pathological 300-char
        # name to 255 could drop the extension, and NotebookLM 400s on an
        # extensionless upload. Keep the suffix.
        suffix = Path(base).suffix[:255]
        base = Path(base).stem[: 255 - len(suffix)] + suffix
    return base


def _cleanup(path: str) -> None:
    """Remove a temp directory tree, ignoring an already-removed path."""
    shutil.rmtree(path, ignore_errors=True)


async def _passthrough_notebook(notebook_id: str) -> str:
    """Async pass-through notebook resolver (the token carries the full id)."""
    return notebook_id


_UPLOAD_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Upload a source to NotebookLM</title></head>
<body style="font-family:system-ui,sans-serif;max-width:34em;margin:3em auto;padding:0 1em">
<h2>Upload a source to NotebookLM</h2>
<p>Choose a local file to add to your notebook as a source.</p>
<input id="f" type="file">
<button id="btn" style="font-size:1em;padding:.4em 1em;margin-left:.5em">Upload</button>
<p id="out" style="white-space:pre-wrap"></p>
<script>
const f = document.getElementById('f');
const out = document.getElementById('out');
document.getElementById('btn').onclick = async () => {
  const file = f.files && f.files[0];
  if (!file) { out.textContent = 'Pick a file first.'; return; }
  out.textContent = 'Uploading ' + file.name + ' ...';
  try {
    const resp = await fetch(
      location.href + '?filename=' + encodeURIComponent(file.name),
      {method: 'POST',
       headers: {'Content-Type': file.type || 'application/octet-stream'},
       body: file});
    const text = await resp.text();
    out.textContent = '[' + resp.status + '] ' + text;
  } catch (e) {
    out.textContent = 'Upload failed: ' + e;
  }
};
</script>
</body></html>"""


def register_file_routes(mcp: FastMCP, config: FileTransferConfig) -> None:
    """Register the three ``/files/*`` routes on ``mcp`` (called only on http with a
    public URL configured). ``config`` is closed over; the live client is fetched
    per-request via :func:`get_client_from_app`."""

    @mcp.custom_route("/files/dl/{token}", methods=["GET"])
    async def download_route(request: Request) -> Response:
        token = request.path_params["token"]
        try:
            payload = config.signer.verify(token, op="dl")
        except FileLinkError:
            return PlainTextResponse(
                "This download link is invalid or has expired.",
                status_code=403,
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
        spec = _DOWNLOAD_SPECS.get(str(payload.get("atype")))
        if spec is None:  # pragma: no cover - tokens are minted only for known types
            return PlainTextResponse("Unknown artifact type.", status_code=400)
        try:
            client = get_client_from_app(request)
        except RuntimeError:
            return PlainTextResponse("Server is not ready.", status_code=500)

        temp_dir = tempfile.mkdtemp(prefix="nblm-mcp-dl-")
        temp_path = os.path.join(temp_dir, f"artifact{spec.extension}")
        try:
            args: dict[str, object] = {
                "notebook_id": payload.get("nb"),
                "output_path": temp_path,
                "latest": True,
            }
            fmt = payload.get("fmt")
            if fmt is not None:
                args[spec.format_param_name] = fmt
            plan = download_core.build_download_plan(spec, args, cwd=Path.cwd())
            result = await download_core.execute_download(
                plan,
                client,
                notebook_resolver=_passthrough_notebook,
                # download selects by ``latest``, so the artifact resolver is never
                # invoked — supply the required identity stub inline.
                artifact_resolver=lambda _artifacts, artifact_id: artifact_id,
            )
        except BaseException:
            _cleanup(temp_dir)
            raise

        if result.outcome != download_core.DownloadOutcome.SINGLE_DOWNLOADED:
            _cleanup(temp_dir)
            return PlainTextResponse(
                f"No completed {spec.name} artifact is available yet.",
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )
        # The core may resolve a conflict to a different name, but it must stay
        # inside our private dir — anything else is a bug, not a file we serve.
        served = result.output_path or temp_path
        if Path(temp_dir).resolve() not in Path(served).resolve().parents:
            _cleanup(temp_dir)
            return PlainTextResponse(
                "Download produced an unexpected output path.", status_code=500
            )
        # Hand the user a meaningful name (the core wrote ``artifact<ext>``): the
        # artifact title + the served file's actual extension.
        title = str((result.artifact or {}).get("title") or spec.name)
        download_name = download_core.artifact_title_to_filename(title, Path(served).suffix, set())
        return FileResponse(
            served,
            filename=download_name,
            background=BackgroundTask(_cleanup, temp_dir),
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @mcp.custom_route("/files/ul/{token}", methods=["GET"])
    async def upload_page_route(request: Request) -> Response:
        token = request.path_params["token"]
        try:
            config.signer.verify(token, op="ul")
        except FileLinkError:
            return HTMLResponse(
                "<!doctype html><html><body style='font-family:system-ui'>"
                "<h2>This upload link is invalid or has expired.</h2>"
                "<p>Re-run the tool from your assistant to get a fresh link.</p>"
                "</body></html>",
                status_code=403,
                headers=_HTML_SECURITY_HEADERS,
            )
        # The page is fully static (the token already lives in location.href), so
        # there is nothing attacker-controlled to interpolate.
        return HTMLResponse(_UPLOAD_PAGE, headers=_HTML_SECURITY_HEADERS)

    @mcp.custom_route("/files/ul/{token}", methods=["POST", "PUT"])
    async def upload_route(request: Request) -> Response:
        token = request.path_params["token"]
        try:
            payload = config.signer.verify(token, op="ul")
        except FileLinkError:
            return PlainTextResponse("This upload link is invalid or has expired.", status_code=403)
        # Early 413 on a declared over-cap body (the running cap below is the real
        # defense — a chunked / under-stated Content-Length slips past this).
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > MAX_UPLOAD_BYTES:
                    return PlainTextResponse("Upload exceeds the size limit.", status_code=413)
            except ValueError:
                pass
        try:
            client = get_client_from_app(request)
        except RuntimeError:
            return PlainTextResponse("Server is not ready.", status_code=500)

        # Bound aggregate temp-disk: reject (fast, no disk touched) when too many
        # uploads are already streaming. The counter is mutated only between awaits
        # in this single-process async server, so no lock is needed.
        global _inflight_uploads
        if _inflight_uploads >= _MAX_CONCURRENT_UPLOADS:
            return PlainTextResponse(
                "Too many concurrent uploads in progress; retry shortly.", status_code=429
            )
        _inflight_uploads += 1
        try:
            # Filename: the raw fetch body omits it, so it arrives sanitized via
            # ?filename=. Content-type: the signed token's mime WINS; the request
            # Content-Type header is the fallback. Strip any ``; charset=…`` params
            # off the header so only the bare MIME type reaches the backend.
            filename = _safe_upload_name(request.query_params.get("filename"))
            raw_mime = payload.get("mime") or request.headers.get("content-type")
            mime = raw_mime.split(";")[0].strip() if raw_mime else None

            temp_dir = tempfile.mkdtemp(prefix="nblm-mcp-ul-")  # mkdtemp is 0o700
            temp_path = os.path.join(temp_dir, filename)
            try:
                fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                total = 0
                with os.fdopen(fd, "wb") as out:
                    async for chunk in request.stream():
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_UPLOAD_BYTES:
                            return PlainTextResponse(
                                "Upload exceeds the size limit.", status_code=413
                            )
                        out.write(chunk)
                plan = add_core.build_source_add_plan(
                    content=os.path.realpath(temp_path),
                    source_type="file",
                    title=payload.get("title"),
                    mime_type=str(mime) if mime is not None else None,
                    follow_symlinks=False,
                    validate_path=add_core.validate_upload_path,
                    looks_path_shaped=add_core.looks_like_path,
                )
                result = await add_core.execute_source_add(
                    client,
                    add_core.SourceAddExecutionPlan(notebook_id=str(payload.get("nb")), plan=plan),
                )
                source_id = str(result.source.id)
                # The documented sandbox-`curl`/PUT path (an agent uploading a file
                # it holds) gets clean JSON when it asks for it; a human browser gets
                # the HTML page.
                if "application/json" in request.headers.get("accept", ""):
                    return JSONResponse(
                        {"status": "added", "source_id": source_id},
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
                    )
                return HTMLResponse(
                    "<!doctype html><html><body style='font-family:system-ui'>"
                    f"<h2>Source added</h2><p>id = <code>{html.escape(source_id)}</code></p>"
                    "<p>You can close this tab and return to your assistant.</p>"
                    "</body></html>",
                    headers=_HTML_SECURITY_HEADERS,
                )
            except ValidationError as exc:
                return PlainTextResponse(f"Upload rejected: {exc}", status_code=400)
            except OSError:
                # A bad filename / fs error (e.g. a name that survives sanitization
                # but the fs rejects) is a clean 400, not a bare 500.
                return PlainTextResponse("Upload could not be processed.", status_code=400)
            finally:
                # Always remove the temp dir — on success (bytes already uploaded), a
                # rejection, an fs error, or a mid-stream client disconnect.
                _cleanup(temp_dir)
        finally:
            _inflight_uploads -= 1
