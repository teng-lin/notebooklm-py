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
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.types import Receive, Scope, Send

from .._app import download as download_core
from .._app import source_add as add_core
from .._app.errors import ErrorCategory, classify
from ..exceptions import NotebookLMError, ValidationError
from ._context import get_client_from_app
from ._errors import redact
from ._filelink import FileLinkError, FileTransferConfig
from .tools._studio_download import (
    _DOWNLOAD_SPECS,
    _resolve_artifact_id,
    download_filename,
    download_mime_type,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

#: Max accepted upload size (mirrors the REST route's ``MAX_UPLOAD_BYTES``). Bounds
#: temp-file disk pressure; an upload exceeding it is rejected with 413 — early via
#: ``Content-Length``, and authoritatively via the running byte cap below.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

#: One opaque 403 message shared by every ``/files/ul`` POST rejection — a bad
#: signature/expiry, a missing/malformed ``jti``, or an already-consumed / mid-flight
#: single-use token. Keeping them identical means the response never reveals WHICH
#: check failed, so a probing client gets no oracle.
_UPLOAD_LINK_REJECTED = "This upload link is invalid or has expired."

#: Cap concurrent in-flight uploads. The per-request byte cap bounds ONE upload to
#: 200 MiB, but a leaked/replayable ``ul`` token (valid for its full TTL) could
#: otherwise drive N parallel streams = N×200 MiB of transient temp disk →
#: ENOSPC. This bounds aggregate temp pressure to ``_MAX_CONCURRENT_UPLOADS`` ×
#: ``MAX_UPLOAD_BYTES``; excess uploads get a fast 429 (no disk touched). A single
#: process serves the single tenant, so a plain counter (mutated only between
#: ``await`` points, never concurrently) is sufficient — no lock needed.
_MAX_CONCURRENT_UPLOADS = 4
_inflight_uploads = 0

#: Cap concurrent in-flight downloads. Each accepted download spools the artifact
#: to a private temp dir and fetches it from Google before streaming it out, so a
#: leaked/replayable ``dl`` token (valid for its full TTL) could otherwise drive N
#: parallel streams = N× transient temp disk + N× Google-fetch amplification. This
#: bounds aggregate temp pressure + upstream fetch fan-out to
#: ``_MAX_CONCURRENT_DOWNLOADS``; excess downloads get a fast 429 (no disk touched,
#: no fetch issued). Single process / single tenant, so a plain counter (mutated
#: only between ``await`` points, never concurrently) is sufficient — no lock.
_MAX_CONCURRENT_DOWNLOADS = 4
_inflight_downloads = 0

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

#: CORS for the ``/files/ul`` POST — the in-app MCP-App widget (Phase 3) uploads cross-origin
#: from its sandboxed iframe, so the browser sends a preflight and needs an allow-origin on the
#: response. ``*`` is safe here: the signed single-use token is the sole auth (no cookies /
#: ambient credentials, ADR-0024), so a hostile page that can't mint a token gains nothing.
_UPLOAD_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, PUT, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Accept",
    "Access-Control-Max-Age": "600",
}
#: Just the allow-origin, for the POST route's own responses (success AND error) — without it a
#: cross-origin widget can't READ the response, so an expired-link 403 surfaces as an opaque
#: "Failed to fetch" instead of a message the user can act on. Safe for the same reason (#1889).
_CORS_ORIGIN = {"Access-Control-Allow-Origin": "*"}


#: HTTP status each neutral :class:`ErrorCategory` projects onto for the
#: ``/files/*`` routes. Covers EVERY category (pinned by ``test_fileroutes.py``).
#: This mirrors the REST server's ``CATEGORY_STATUS`` but is defined locally — the
#: MCP layer must NOT import ``notebooklm.server`` (it pulls ``fastapi``; the
#: boundary is enforced by ``tests/_guardrails/test_mcp_boundary.py``). Deliberate
#: deviations from the REST table (because these routes are a *gateway* to the
#: NotebookLM backend, not the backend itself): ``AUTH`` / ``CONFIG`` → **502**, not
#: 401/500 — they are authenticated by the signed token, so a *server-side* broken
#: Google session is an upstream-dependency failure (Bad Gateway) the token-bearing
#: caller cannot fix by re-authenticating (401 would be misleading); and
#: ``LIBRARY`` → **502**, not 500, for the same gateway reason (an unclassified
#: library error reaching here is still an upstream failure, not an internal bug of
#: the route). ``UNEXPECTED`` stays 500 (a genuine route bug) but is unreachable via
#: :func:`_upstream_error_response`, which only takes ``NotebookLMError``.
_FILE_ROUTE_STATUS: dict[ErrorCategory, int] = {
    ErrorCategory.NOT_FOUND: 404,
    ErrorCategory.AUTH: 502,
    ErrorCategory.RATE_LIMITED: 429,
    ErrorCategory.VALIDATION: 400,
    ErrorCategory.CONFIG: 502,
    ErrorCategory.NETWORK: 502,
    ErrorCategory.NOTEBOOK_LIMIT: 409,
    ErrorCategory.ARTIFACT_TIMEOUT: 504,
    ErrorCategory.TIMEOUT: 504,
    ErrorCategory.SERVER: 502,
    ErrorCategory.RPC: 502,
    ErrorCategory.SOURCE_MUTATION: 422,
    ErrorCategory.LIBRARY: 502,
    ErrorCategory.UNEXPECTED: 500,
}


def _upstream_error_response(exc: NotebookLMError, *, note: str = "") -> PlainTextResponse:
    """Project an upstream ``NotebookLMError`` onto a classified, redacted response.

    A ``NotebookLMError`` raised inside a ``/files/*`` handler (e.g. the artifact
    ``list`` RPC inside ``execute_download``, which is not wrapped by the core, or
    ``execute_source_add``) would otherwise escape to a raw Starlette 500. Classify
    it via the shared :func:`_app.errors.classify`, map the category to an HTTP
    status, and return the secret-scrubbed message (the same :func:`redact`
    chokepoint the MCP tool errors use). ``.get(..., 502)`` is defense-in-depth —
    every category is in the table (pinned by a coverage test).

    An optional ``note`` is prepended as a human-facing prefix; the helper owns the
    separating space and runs it through :func:`redact` too (defense-in-depth — it
    should already be a static, secret-free label, e.g. the upload route's
    bytes-arrived-but-add-failed hint, but redacting uniformly means a future
    dynamic caller can't leak through it).
    """
    status = _FILE_ROUTE_STATUS.get(classify(exc).category, 502)
    prefix = f"{redact(note)} " if note else ""
    return PlainTextResponse(
        f"{prefix}Upstream NotebookLM error: {redact(str(exc))}",
        status_code=status,
        # ACAO so the cross-origin widget can read the (redacted) failure, not "Failed to fetch".
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", **_CORS_ORIGIN},
    )


#: Safe-basename sanitizer for a spooled upload. Aliased to the shared neutral
#: helper (:func:`notebooklm._app.source_add.safe_upload_name`) so the MCP
#: ``/files/ul`` route and the REST ``add_file`` route sanitize identically
#: (strip control chars, reject ``.``/``..``, basename the path, stem-truncate).
_safe_upload_name = add_core.safe_upload_name


def _cleanup(path: str) -> None:
    """Remove a temp directory tree, ignoring an already-removed path."""
    shutil.rmtree(path, ignore_errors=True)


class _SlotHeldFileResponse(FileResponse):
    """A ``FileResponse`` that releases its download slot + temp dir only once the
    body has finished streaming — or the client disconnected / the stream aborted.

    Releasing at the END of the ASGI send loop (a ``finally``), not at handler
    return, means the slot stays counted for the whole time the spooled artifact
    occupies temp disk, so slow or deliberately-held streams still count against
    ``_MAX_CONCURRENT_DOWNLOADS`` (a replayed token cannot accumulate uncounted
    on-disk artifacts). The ``finally`` also guarantees the release when a client
    disconnect or bad ``Range`` aborts the stream mid-flight, so a slot can never
    leak and permanently wedge the cap. Error/early-return paths never construct
    this response — the route's own ``finally`` releases those.
    """

    def __init__(self, *args: Any, temp_dir: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._temp_dir = temp_dir

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        global _inflight_downloads
        try:
            await super().__call__(scope, receive, send)
        finally:
            _inflight_downloads -= 1
            _cleanup(self._temp_dir)


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

        # ``aid`` rides inside the HMAC-signed token, so a non-string value should be
        # unreachable in practice — but the route treats the token as its source of
        # truth, and a non-string ``aid`` would make ``_resolve_artifact_id`` raise a
        # raw ``AttributeError`` (not ``ValidationError``) → a 500. Guard the shape so
        # a malformed token fails as a clean 400 like any other bad ``aid``.
        aid = payload.get("aid")
        if aid is not None and not isinstance(aid, str):
            return PlainTextResponse(
                "This download link is invalid.",
                status_code=400,
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )

        # Bound aggregate temp-disk + upstream Google-fetch fan-out: reject (fast,
        # no disk touched, no fetch issued) when too many downloads are already in
        # flight. Placed AFTER the cheap token/spec/client/aid-shape rejects (those
        # must not count against the cap) and BEFORE the temp dir / fetch. The
        # counter is mutated only between awaits in this single-process async server,
        # so no lock is needed.
        global _inflight_downloads
        if _inflight_downloads >= _MAX_CONCURRENT_DOWNLOADS:
            return PlainTextResponse(
                "Too many concurrent downloads in progress; retry shortly.",
                status_code=429,
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
        _inflight_downloads += 1
        # Slot accounting has two disjoint owners, so the counter is decremented
        # exactly once per accepted request:
        #  * NON-success (mkdtemp failure, any error/early return, or an unexpected
        #    exception in the post-fetch code like ``FileResponse`` init /
        #    ``artifact_title_to_filename``) → the outer ``finally`` below releases
        #    the slot AND cleans the temp dir. ``mkdtemp`` failure is the exact
        #    temp-disk exhaustion this cap defends against, so it must release.
        #  * SUCCESS → ownership passes to the returned ``_SlotHeldFileResponse``,
        #    which releases the slot + cleans the temp dir only after the body has
        #    finished streaming (or a disconnect/bad-Range aborts it). Holding the
        #    slot for the whole stream keeps slow/held downloads counted (temp disk
        #    stays bounded), and its ``finally`` guarantees release on disconnect so
        #    a slot can never leak and wedge the cap.
        temp_dir: str | None = None
        success = False
        try:
            try:
                temp_dir = tempfile.mkdtemp(prefix="nblm-mcp-dl-")
            except OSError:
                # Temp-dir creation failed (e.g. ENOSPC) — the exact temp-disk
                # exhaustion this cap defends against. Scoped to mkdtemp ONLY (not the
                # whole try) so it stays distinct from the post-fetch paths; without it
                # the OSError escapes as a RAW Starlette 500 (the outer finally still
                # releases the slot, but the response is bare). Return a clean 500,
                # mirroring the upload route. No dir was created, so the outer finally's
                # temp_dir cleanup is a no-op and success stays False → slot released.
                return PlainTextResponse(
                    "Download could not be prepared (server storage error).",
                    status_code=500,
                    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
                )
            temp_path = os.path.join(temp_dir, f"artifact{spec.extension}")
            try:
                args: dict[str, object] = {
                    "notebook_id": payload.get("nb"),
                    "output_path": temp_path,
                    "latest": aid is None,
                }
                if aid is not None:
                    args["artifact_id"] = aid
                fmt = payload.get("fmt")
                if fmt is not None:
                    args[spec.format_param_name] = fmt
                plan = download_core.build_download_plan(spec, args, cwd=Path.cwd())
                result = await download_core.execute_download(
                    plan,
                    client,
                    notebook_resolver=_passthrough_notebook,
                    artifact_resolver=_resolve_artifact_id,
                )
            except ValidationError as exc:
                # A bad ``aid`` in the token — a no-match id (full UUID or prefix) or
                # an ambiguous prefix (AmbiguousIdError) — surfaces here from
                # ``_resolve_artifact_id``. The catch also covers
                # ``build_download_plan``'s ``DownloadPlanValidationError`` (a
                # ValidationError subclass), which a broker-minted token won't trigger
                # but is correctly a 400 too. Map it to a clean 400 instead of letting
                # it bubble up as a Starlette 500. (The 409 below stays for the
                # latest-by-type path when no completed artifact of that type exists.)
                return PlainTextResponse(
                    str(exc),
                    status_code=400,
                    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
                )
            except NotebookLMError as exc:
                # An upstream error raised out of the core (e.g. the artifact ``list``
                # RPC inside ``execute_download`` is not wrapped) would otherwise become
                # a raw 500. Classify + redact it instead. (Failures that the core
                # *returns* as a non-success ``DownloadResult`` fall through to the
                # generic 409 below — that path already leaks nothing.)
                return _upstream_error_response(exc)

            if result.outcome != download_core.DownloadOutcome.SINGLE_DOWNLOADED:
                return PlainTextResponse(
                    f"No completed {spec.name} artifact is available yet.",
                    status_code=409,
                    headers={"Cache-Control": "no-store"},
                )
            # The core may resolve a conflict to a different name, but it must stay
            # inside our private dir — anything else is a bug, not a file we serve.
            served = result.output_path or temp_path
            if Path(temp_dir).resolve() not in Path(served).resolve().parents:
                return PlainTextResponse(
                    "Download produced an unexpected output path.", status_code=500
                )
            # Hand the user a meaningful name + Content-Type via the SAME shared
            # helpers the studio_download tool payload advertises, so the browser
            # download matches the ``filename`` / ``mime_type`` the agent was told
            # (the core wrote ``artifact<ext>``; the title falls back to the type
            # name when the artifact carries none).
            title = (result.artifact or {}).get("title")
            download_name = download_filename(spec, title, fmt)
            response = _SlotHeldFileResponse(
                served,
                filename=download_name,
                media_type=download_mime_type(spec, fmt),
                temp_dir=temp_dir,
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
            success = True
            return response
        finally:
            # Success hands slot + temp ownership to ``_SlotHeldFileResponse`` (released
            # at end-of-stream); every other exit releases here.
            if not success:
                _inflight_downloads -= 1
                if temp_dir is not None:
                    _cleanup(temp_dir)

    @mcp.custom_route("/files/ul/{token}", methods=["OPTIONS"])
    async def upload_preflight(request: Request) -> Response:
        # CORS preflight for the in-app widget's cross-origin upload (Phase 3). A preflight
        # carries no body/credentials and grants nothing — the POST itself stays token-gated.
        return PlainTextResponse("", status_code=204, headers=_UPLOAD_CORS_HEADERS)

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

    @mcp.custom_route("/u/{shortid}", methods=["GET"])
    async def short_upload_redirect(request: Request) -> Response:
        # Tap-friendly ``/u/<shortid>`` → 302 to the canonical ``/files/ul/<token>`` page.
        # A short id survives mobile-chat corruption that mangles the long opaque token
        # (tap-truncation, model re-typing, autocorrect — all live-confirmed). The redirect
        # keeps the upload page + POST route as the single source of truth. The resolved
        # token still carries the full HMAC + single-use jti, so the short id grants nothing
        # extra; an unknown/expired id is a flat 404 (a probe learns nothing).
        token = config.short_links.get(request.path_params["shortid"])
        if token is None:
            return HTMLResponse(
                "<!doctype html><html><body style='font-family:system-ui'>"
                "<h2>This upload link is invalid or has expired.</h2>"
                "<p>Re-run the tool from your assistant to get a fresh link.</p>"
                "</body></html>",
                status_code=404,
                headers=_HTML_SECURITY_HEADERS,
            )
        return RedirectResponse(
            f"/files/ul/{token}",
            status_code=302,
            headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"},
        )

    @mcp.custom_route("/files/ul/{token}", methods=["POST", "PUT"])
    async def upload_route(request: Request) -> Response:
        token = request.path_params["token"]
        try:
            payload = config.signer.verify(token, op="ul")
        except FileLinkError:
            return PlainTextResponse(_UPLOAD_LINK_REJECTED, status_code=403, headers=_CORS_ORIGIN)
        # Single-use (jti) guard — ``ul`` only (ADR-0024, #1746). A leaked upload token
        # is a content-agnostic WRITE primitive (anyone can POST arbitrary bytes as a
        # source), so unlike ``dl`` (which stays multi-use for Range/resume) an upload
        # token authorizes exactly one *successful* add. ``sign`` always injects a str
        # ``jti``, so a missing/non-str one means a malformed/hand-built token → 403.
        jti = payload.get("jti")
        if not isinstance(jti, str) or not jti:
            return PlainTextResponse(_UPLOAD_LINK_REJECTED, status_code=403, headers=_CORS_ORIGIN)
        # Early 413 on a declared over-cap body (the running cap below is the real
        # defense — a chunked / under-stated Content-Length slips past this).
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > MAX_UPLOAD_BYTES:
                    return PlainTextResponse(
                        "Upload exceeds the size limit.", status_code=413, headers=_CORS_ORIGIN
                    )
            except ValueError:
                pass
        try:
            client = get_client_from_app(request)
        except RuntimeError:
            return PlainTextResponse("Server is not ready.", status_code=500)

        # Atomically claim the single-use jti BEFORE any concurrency slot / spool: a
        # sequential replay (jti already consumed) or a concurrent duplicate (jti
        # already mid-upload) is rejected here with a flat 403. Placed after the cheap
        # validations above (which never mark the token active, so nothing to release)
        # and before the slot. ``try_begin`` runs no ``await``, so the check-and-mark
        # is atomic on the one event loop.
        if not config.jti_store.try_begin(jti):
            return PlainTextResponse(_UPLOAD_LINK_REJECTED, status_code=403, headers=_CORS_ORIGIN)
        committed = False
        try:
            # Bound aggregate temp-disk: reject (fast, no disk touched) when too many
            # uploads are already streaming. The counter is mutated only between awaits
            # in this single-process async server, so no lock is needed.
            global _inflight_uploads
            if _inflight_uploads >= _MAX_CONCURRENT_UPLOADS:
                return PlainTextResponse(
                    "Too many concurrent uploads in progress; retry shortly.",
                    status_code=429,
                    headers=_CORS_ORIGIN,  # let the cross-origin widget read the status to retry
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

                try:
                    temp_dir = tempfile.mkdtemp(prefix="nblm-mcp-ul-")  # mkdtemp is 0o700
                except OSError:
                    # Temp-dir creation failed (e.g. ENOSPC) — the exact temp-disk
                    # exhaustion the concurrency cap guards against. mkdtemp sits OUTSIDE
                    # the spool ``try`` below (whose ``except OSError`` maps a bad
                    # *filename* to a 400), so without this its OSError would escape as a
                    # raw Starlette 500. A failed dir-create is a SERVER storage problem,
                    # not bad input, so 500 (mirrors the download route's status) — kept
                    # distinct from the 400 os.open path. Accounting stays safe: the outer
                    # finallys release the slot + roll back the jti; no dir was made, so
                    # nothing to clean.
                    return PlainTextResponse(
                        "Upload could not be stored (server storage error).",
                        status_code=500,
                        headers={
                            "Cache-Control": "no-store",
                            "Referrer-Policy": "no-referrer",
                            **_CORS_ORIGIN,
                        },
                    )
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
                                    "Upload exceeds the size limit.",
                                    status_code=413,
                                    headers=_CORS_ORIGIN,  # cross-origin widget reads the status
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
                        add_core.SourceAddExecutionPlan(
                            notebook_id=str(payload.get("nb")), plan=plan
                        ),
                    )
                    source_id = str(result.source.id)
                    # Burn the single-use jti now the add SUCCEEDED — before the
                    # JSON-vs-HTML branch so BOTH return paths consume it. Recording only
                    # on success means a failed/aborted upload (handled by the outer
                    # ``finally`` rollback) leaves the link usable for retry, honoring
                    # ADR-0024's large-file retry window. ``exp`` was validated as an int
                    # by ``verify``. The ``result`` payload feeds the in-process completion
                    # map so a same-process ``await_upload`` poll surfaces the added source
                    # (Phase 1); it is success-only by construction — a failed upload writes
                    # nothing, so ``await_upload`` stays "pending" and the model falls back
                    # to ``source_list``. ``sha256`` is intentionally absent until the
                    # deferred client/stream hashing (Phase 4) lands.
                    config.jti_store.commit(
                        jti,
                        payload["exp"],
                        result={
                            "source_id": source_id,
                            "name": filename,
                            "size": total,
                            "mime": mime,
                        },
                    )
                    committed = True
                    # The documented sandbox-`curl`/PUT path (an agent uploading a file
                    # it holds) gets clean JSON when it asks for it; a human browser gets
                    # the HTML page.
                    if "application/json" in request.headers.get("accept", ""):
                        return JSONResponse(
                            {"status": "added", "source_id": source_id},
                            headers={
                                "Cache-Control": "no-store",
                                "Referrer-Policy": "no-referrer",
                                # Let the cross-origin widget read the result (ADR-0024: token-auth,
                                # not cookies, so allow-origin * is safe).
                                "Access-Control-Allow-Origin": "*",
                            },
                        )
                    return HTMLResponse(
                        "<!doctype html><html><body style='font-family:system-ui'>"
                        f"<h2>Source added</h2><p>id = <code>{html.escape(source_id)}</code></p>"
                        "<p>You can close this tab and return to your assistant.</p>"
                        "</body></html>",
                        headers=_HTML_SECURITY_HEADERS,
                    )
                except ValidationError as exc:
                    # ValidationError ⊂ NotebookLMError, so this MUST precede the
                    # NotebookLMError handler. ``validate_upload_path`` rejections can
                    # embed the local file path, so the detail is redacted.
                    return PlainTextResponse(
                        f"Upload rejected: {redact(str(exc))}",
                        status_code=400,
                        headers={
                            "Cache-Control": "no-store",
                            "Referrer-Policy": "no-referrer",
                            **_CORS_ORIGIN,
                        },
                    )
                except NotebookLMError as exc:
                    # An upstream auth/server/rate-limit error from execute_source_add
                    # (add_file → RPC) would otherwise escape as a raw 500. The bytes
                    # already finished uploading by here, so tell the user a retry
                    # re-sends the whole file (vs a mid-stream failure that did not).
                    return _upstream_error_response(
                        exc,
                        note="Your file uploaded, but adding it as a source failed "
                        "(a retry re-uploads it).",
                    )
                except OSError:
                    # A bad filename / fs error (e.g. a name that survives sanitization
                    # but the fs rejects) is a clean 400, not a bare 500.
                    return PlainTextResponse(
                        "Upload could not be processed.",
                        status_code=400,
                        headers=_CORS_ORIGIN,  # cross-origin widget reads the status
                    )
                finally:
                    # Always remove the temp dir — on success (bytes already uploaded), a
                    # rejection, an fs error, or a mid-stream client disconnect.
                    _cleanup(temp_dir)
            finally:
                _inflight_uploads -= 1
        finally:
            # Release a claimed-but-not-committed jti so the link can be retried: this
            # covers the 429, the validation / upstream / OSError returns, and a
            # mid-stream disconnect (``CancelledError`` ⊂ ``BaseException``, which
            # ``finally`` still runs on — an ``except Exception`` would miss it and wedge
            # the jti in the active set). A committed upload keeps the jti burned.
            if not committed:
                config.jti_store.rollback(jti)
