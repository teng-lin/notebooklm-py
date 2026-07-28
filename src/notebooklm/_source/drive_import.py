"""Auto-route "add from Drive": download + upload the upload-only Drive types.

NotebookLM's native Drive import (``client.sources.add_drive``) only ingests
Google-native Docs/Slides/Sheets + PDF. Everything else NotebookLM *can* accept
as an uploaded file (epub/docx/txt/md/rtf/odt/csv/tsv/…) has to be fetched from
Drive and pushed through the resumable-upload leg instead. This module owns that
route (#1884).

Design (see ``.sisyphus/plans/1884-drive-auto-route.md``):

* **Server-side download.** The fetch runs where the profile + cookies already
  live (the client host / MCP server), authenticated by the SAME ``.google.com``
  master jar the upload leg uses. So it works in stdio AND remote MCP mode with
  no ``upload_required`` detour — a Drive source has no client-side bytes, so
  both the Drive→server fetch and the server→NotebookLM push are server-local.

* **One cookie-authed request classifies AND downloads.** A single GET to
  ``drive.usercontent.google.com/download?id=<id>&export=download`` returns the
  bytes with a ``Content-Disposition: attachment; filename="X.ext"`` for a
  directly-downloadable file; the extension routes it. A native Google Doc (or a
  permissions/expired-auth failure) comes back as HTML instead — classified via
  the :func:`_find_confirm_params` discriminator (r3 Fix A) into the >25 MB
  virus-scan interstitial (re-request with the confirm token), an expired-auth
  redirect, or a non-committal "not downloadable" pointer error.

* **Header-first streaming (r3 Fix B).** The fetch inspects headers BEFORE the
  body: an unsupported/HTML extension or an over-cap ``Content-Length`` closes
  the stream immediately (no download); otherwise the body streams to a 0600
  temp file under a running byte cap. The temp file is unlinked on every exit
  path (success, upload failure, cancellation).

All I/O sits behind injected seams (``fetch`` + ``add_file``) so the routing
table, id/URL parsing, and the confirm-form discriminator are unit-tested with
no live Drive access.
"""

from __future__ import annotations

import asyncio
import os
import queue
import re
import tempfile
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .._artifact._download_client import _is_trusted_download_host
from .._artifact._redirect_guard import redirect_revalidation_hooks
from .._artifact.downloads import _await_writer_exit
from ..exceptions import (
    ArtifactDownloadError,
    AuthError,
    NetworkError,
    RateLimitError,
    ServerError,
    ValidationError,
)
from ._upload_decode import _validate_upload_file_supported

if TYPE_CHECKING:
    from ..types import Source

# The cookie-authed download endpoint that returns BOTH the type (via the
# Content-Disposition filename) and the bytes in one request (validated live).
_DRIVE_DOWNLOAD_URL = "https://drive.usercontent.google.com/download"

# Hosts a legitimate confirm-form action may target (r3 Fix A). A form pointing
# anywhere else is NOT the virus-scan interstitial and must not be followed.
_DRIVE_DOWNLOAD_HOSTS = frozenset({"drive.usercontent.google.com", "drive.google.com"})

# Size cap for a Drive download (matches the MCP file-transfer cap in
# ``mcp/tools/_fileupload.py``). Enforced header-first via Content-Length and
# again as a running byte cap for unknown-length bodies.
_MAX_DRIVE_DOWNLOAD_MIB = 200
_MAX_DRIVE_DOWNLOAD_BYTES = _MAX_DRIVE_DOWNLOAD_MIB * 1024 * 1024

# How much of an HTML classification body to read before deciding (r3 Fix B: read
# only the small body needed for the form parse, then close — never stream HTML
# to a file). The confirm interstitial is a few KiB; 256 KiB is generous slack.
_HTML_SNIFF_CAP_BYTES = 256 * 1024

_STREAM_CHUNK_BYTES = 65536

# Bounded queue between the async chunk producer and the single writer thread that
# drains it to disk — so ``handle.write()`` never runs on the event loop (this
# fetch is SERVER-SIDE on a possibly-shared loop, streaming up to the cap). Small
# enough to keep back-pressure (the producer awaits when the writer falls behind),
# large enough to stay hot across a brief read stall. Mirrors
# ``_artifact/downloads.py::download_url``'s writer/queue split.
_DRIVE_WRITER_QUEUE_SIZE = 8

# A raw Drive file id, or the id embedded in a share URL. Drive ids are long
# base64url-ish tokens; the 20-char floor rejects obviously-too-short junk.
_DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_DRIVE_URL_PATH_ID_RE = re.compile(r"/(?:file/)?d/([A-Za-z0-9_-]{20,})")

# Extensions NotebookLM's resumable upload accepts → download + upload route.
_UPLOAD_SUPPORTED_EXTS = frozenset(
    {"epub", "docx", "doc", "txt", "md", "markdown", "rtf", "odt", "csv", "tsv", "pdf"}
)
# HTML-family extensions the upload endpoint rejects (kept explicit so the fetch
# gives the convert-first guidance instead of the generic unsupported error).
_HTML_EXTS = frozenset({"html", "htm", "xhtml", "xht"})

# A plausible browser UA — the Drive download endpoint is a browser surface.
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# Derived from the supported set (never hand-maintained) so the user-facing
# accepted-types message can't drift from what the router actually accepts.
_ACCEPTED_EXTS_HINT = ", ".join(sorted(_UPLOAD_SUPPORTED_EXTS))


@dataclass(frozen=True)
class DriveDownload:
    """A Drive file streamed to a local temp path, ready for resumable upload.

    ``path`` carries the source file's extension (so ``add_file``'s content-type
    resolution + HTML-reject gate fire as a second defense); the caller unlinks
    it after the upload completes.
    """

    path: Path
    filename: str
    content_type: str | None


#: The fetch seam: classify + (on success) download a Drive ref to a temp path.
#: Raises the typed library errors — ``AuthError`` (expired session),
#: ``RateLimitError`` / ``ServerError`` / ``NetworkError`` (transient), or
#: ``ValidationError`` (unsupported / native-doc / permission pointer). Injected
#: so the routing table + discriminator are testable with a fake HTTP client.
DriveFetch = Callable[["DriveRef"], Awaitable[DriveDownload]]


class AddFile(Protocol):
    """The resumable-upload seam (``SourcesAPI.add_file``)."""

    async def __call__(
        self,
        notebook_id: str,
        file_path: Path,
        *,
        title: str | None,
        wait: bool,
        wait_timeout: float,
    ) -> Source: ...


#: Builds the streaming download client. Always httpx (never the buffering
#: curl_cffi ``get_guarded`` — Fix B forces a potentially 200 MiB body through
#: streaming httpx) with the #1521 per-hop redirect-revalidation hooks + the
#: shared ``.google.com`` trusted-host allowlist. Injected for testing.
StreamingClientFactory = Callable[[httpx.Cookies, httpx.Timeout], httpx.AsyncClient]


def _default_streaming_client(cookies: httpx.Cookies, timeout: httpx.Timeout) -> httpx.AsyncClient:
    """Build the httpx streaming download client (reuses the download wiring).

    Mirrors the httpx branch of ``_artifact/_download_client.py::_make_download_client``
    (auto-follow redirects + the #1521 revalidation event hook + the shared
    ``.google.com`` trusted-host guard) but is consumed via ``client.stream(...)``
    rather than the buffering GET, so an over-cap body is never buffered.
    """
    return httpx.AsyncClient(
        cookies=cookies,
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": _BROWSER_UA},
        event_hooks=redirect_revalidation_hooks(_is_trusted_download_host),
    )


@dataclass(frozen=True)
class DriveRef:
    """A parsed Drive reference: the file id plus an optional resource key.

    Link-shared files carry a ``resourcekey`` in the share URL that the download
    request MUST echo back, or Drive refuses the fetch (403 / permission page).
    """

    file_id: str
    resource_key: str | None = None


def parse_drive_ref(id_or_url: str) -> DriveRef:
    """Parse a raw Drive file id or a Drive share URL into a :class:`DriveRef`.

    Accepts a raw id, or a ``https://…`` URL (on a Google host) of the ``/d/<id>``,
    ``/file/d/<id>/…``, or ``?id=<id>`` shapes, preserving a ``resourcekey`` query
    param when present. A URL on a NON-Google host is rejected — an id-shaped path
    segment under ``evil.example`` is not a Drive reference. Rejects anything that
    does not yield a valid id.
    """
    candidate = (id_or_url or "").strip()
    if not candidate:
        raise ValidationError("A Google Drive file id or share URL is required.")
    if _DRIVE_ID_RE.fullmatch(candidate):
        return DriveRef(file_id=candidate)

    parsed = urlparse(candidate)
    # Only extract from a URL that is actually a Google host (reuses the download
    # trusted-host allowlist: *.google.com / *.googleusercontent.com / …); a bare
    # id with no scheme/host stays accepted via the fullmatch above.
    if parsed.scheme in ("http", "https") and _is_trusted_download_host(parsed.hostname):
        query = parse_qs(parsed.query)
        resource_key = next((v for v in query.get("resourcekey", []) if v), None)
        for value in query.get("id", []):
            if _DRIVE_ID_RE.fullmatch(value):
                return DriveRef(file_id=value, resource_key=resource_key)
        path_match = _DRIVE_URL_PATH_ID_RE.search(parsed.path)
        if path_match:
            return DriveRef(file_id=path_match.group(1), resource_key=resource_key)

    raise ValidationError(
        f"Could not parse a Google Drive file id from {id_or_url!r}. Pass a raw file id "
        "or a Drive URL like https://drive.google.com/file/d/<id>/view."
    )


def extract_drive_file_id(id_or_url: str) -> str:
    """Parse a raw Drive file id or a Drive share URL into the bare file id."""
    return parse_drive_ref(id_or_url).file_id


def _download_url(
    file_id: str, *, authuser: str | None = None, resource_key: str | None = None
) -> str:
    """Build the cookie-authed download URL, routing to the SELECTED account.

    ``authuser`` disambiguates the account in a multi-login cookie jar (else Drive
    serves authuser=0's view); ``resourcekey`` is required for link-shared files.
    """
    params = {"id": file_id, "export": "download"}
    if authuser is not None:
        params["authuser"] = authuser
    if resource_key:
        params["resourcekey"] = resource_key
    return f"{_DRIVE_DOWNLOAD_URL}?{urlencode(params)}"


def _filename_from_disposition(disposition: str) -> str:
    """Extract the filename from a Content-Disposition header, if present."""
    if not disposition:
        return ""
    # RFC 5987 ``filename*=<charset>'<lang>'<pct-encoded>`` takes precedence over a
    # plain ``filename=``. The language tag between the quotes is OPTIONAL and may be
    # non-empty (e.g. ``UTF-8'en'file.txt``), so match ``[^']*`` for it, not ``''``.
    ext_match = re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", disposition, re.IGNORECASE)
    if ext_match:
        from urllib.parse import unquote

        return unquote(ext_match.group(1).strip()).strip('"')
    match = re.search(r'filename\s*=\s*"?([^";]+)"?', disposition, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return ""


def _extension(filename: str) -> str:
    suffix = Path(filename).suffix
    return suffix[1:].lower() if suffix else ""


class _DownloadFormParser(HTMLParser):
    """Collect ``<form>`` actions + their hidden-input name→value pairs."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[tuple[str, dict[str, str]]] = []
        self._current: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key: (value or "") for key, value in attrs}
        if tag == "form":
            self._current = {}
            self.forms.append((attr.get("action", ""), self._current))
        elif tag == "input" and self._current is not None:
            name = attr.get("name")
            if name:
                self._current[name] = attr.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._current = None


def _find_confirm_params(html_text: str, file_id: str) -> dict[str, str] | None:
    """Detect the >25 MB virus-scan interstitial and return its re-request params.

    Qualifies as the confirm page ONLY when a form's action resolves to a Drive
    download host AND its hidden inputs carry ``id`` (== the requested id),
    ``export=download``, and a NON-EMPTY ``confirm`` token (r3 Fix A). Arbitrary
    hidden fields do not qualify. Carries ``uuid`` when present.
    """
    parser = _DownloadFormParser()
    parser.feed(html_text)
    for action, inputs in parser.forms:
        host = (urlparse(action).hostname or "").lower()
        if host not in _DRIVE_DOWNLOAD_HOSTS:
            continue
        if inputs.get("id") != file_id or inputs.get("export") != "download":
            continue
        confirm = inputs.get("confirm", "")
        if not confirm:
            continue
        params = {"id": file_id, "export": "download", "confirm": confirm}
        uuid = inputs.get("uuid")
        if uuid:
            params["uuid"] = uuid
        return {"__action__": action or _DRIVE_DOWNLOAD_URL, **params}
    return None


def _confirm_url(
    confirm_params: dict[str, str], *, authuser: str | None = None, resource_key: str | None = None
) -> str:
    params = dict(confirm_params)
    action = params.pop("__action__")
    # The interstitial form rarely echoes authuser/resourcekey, so re-attach the
    # account routing + resource key for the confirmed re-request (unless the form
    # already carried them).
    if authuser is not None:
        params.setdefault("authuser", authuser)
    if resource_key:
        params.setdefault("resourcekey", resource_key)
    return f"{action}?{urlencode(params)}"


@dataclass(frozen=True)
class _ConfirmRedirect:
    """Sentinel: the response was the confirm interstitial; re-request this URL."""

    url: str


async def _read_capped_text(response: httpx.Response, cap: int) -> str:
    """Read at most ``cap`` bytes of a streamed body, then decode as text."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes(_STREAM_CHUNK_BYTES):
        # Slice the final chunk so the buffer never exceeds ``cap`` by up to one
        # whole chunk (a body far larger than the sniff cap would otherwise pull a
        # full extra 64 KiB into memory).
        if total + len(chunk) > cap:
            chunk = chunk[: cap - total]
        chunks.append(chunk)
        total += len(chunk)
        if total >= cap:
            break
    return b"".join(chunks).decode("utf-8", errors="replace")


class DriveFetcher:
    """Default :data:`DriveFetch`: cookie-authed header-first streaming fetch.

    Holds a cookie provider (the live kernel jar) and a streaming-client factory
    (both injected). Never buffers the body: HTML is read only far enough to
    classify, an unsupported/over-cap binary is rejected before the body is read,
    and a supported binary streams to a 0600 temp file under a running byte cap.
    """

    def __init__(
        self,
        *,
        cookies_provider: Callable[[], httpx.Cookies],
        client_factory: StreamingClientFactory = _default_streaming_client,
        max_bytes: int = _MAX_DRIVE_DOWNLOAD_BYTES,
        authuser: str | None = None,
        temp_dir: Path | None = None,
    ) -> None:
        self._cookies_provider = cookies_provider
        self._client_factory = client_factory
        self._max_bytes = max_bytes
        # Routes a multi-login cookie jar to the SELECTED account (else authuser=0).
        self._authuser = authuser
        # Where temp downloads land; ``None`` = the system temp dir. Injectable so
        # concurrent downloads (and tests) can scope their own directory.
        self._temp_dir = temp_dir

    async def __call__(self, ref: DriveRef) -> DriveDownload:
        url = _download_url(ref.file_id, authuser=self._authuser, resource_key=ref.resource_key)
        result = await self._request(url, ref, allow_confirm=True)
        if isinstance(result, _ConfirmRedirect):
            # Re-request with the confirm token; a second interstitial is treated
            # as a hard failure (no unbounded confirm loop).
            result = await self._request(result.url, ref, allow_confirm=False)
        if isinstance(result, _ConfirmRedirect):  # pragma: no cover - defensive
            raise ValidationError(
                f"Drive kept returning the download-confirmation page for {ref.file_id}; "
                "it may be too large to fetch or temporarily unavailable."
            )
        return result

    async def _request(
        self, url: str, ref: DriveRef, *, allow_confirm: bool
    ) -> DriveDownload | _ConfirmRedirect:
        file_id = ref.file_id
        timeout = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=30.0)
        client = self._client_factory(self._cookies_provider(), timeout)
        try:
            async with client:  # noqa: SIM117 - stream() nested so the client is entered first
                async with client.stream("GET", url) as response:
                    return await self._handle_response(response, ref, allow_confirm=allow_confirm)
        except (httpx.HTTPError, ArtifactDownloadError) as exc:
            # Transport-level faults (timeout, DNS, connection reset, or a
            # redirect-guard policy rejection escaping the streaming GET) map to a
            # retriable NETWORK error — never a bare httpx traceback surfacing as
            # UNEXPECTED in MCP / an internal-bug CLI exit.
            raise NetworkError(
                f"Network error fetching Drive file {file_id} ({exc.__class__.__name__})",
                original_error=exc if isinstance(exc, Exception) else None,
            ) from exc

    async def _handle_response(
        self, response: httpx.Response, ref: DriveRef, *, allow_confirm: bool
    ) -> DriveDownload | _ConfirmRedirect:
        file_id = ref.file_id
        status = response.status_code
        # 401 → expired session (re-auth path). 403 is NOT decided here: Drive
        # returns 403 + an HTML permission page, which the discriminator handles.
        if status == 401:
            raise AuthError("Drive authentication expired — run `notebooklm login`, then retry.")
        if status == 429:
            raise RateLimitError(
                f"Drive throttled the download (HTTP 429) for {file_id}; retry after a delay."
            )
        if status >= 500:
            raise ServerError(
                f"Drive returned HTTP {status} while fetching {file_id}; retry after a delay.",
                status_code=status,
            )

        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type.lower():
            return await self._classify_html(response, ref, allow_confirm=allow_confirm)
        if status >= 400:
            # A non-HTML 4xx (e.g. 403/404) is a hard caller/permission error.
            raise ValidationError(
                f"Drive returned HTTP {status} while fetching file {file_id}; confirm the "
                "id is correct and the file is accessible to this account."
            )

        # Attachment / binary route — inspect headers BEFORE the body.
        filename = _filename_from_disposition(response.headers.get("content-disposition", ""))
        extension = _extension(filename)
        if extension in _HTML_EXTS:
            raise ValidationError(
                "HTML isn't supported by NotebookLM upload; convert the page to "
                ".txt, .md, or .pdf first, then retry."
            )
        if extension not in _UPLOAD_SUPPORTED_EXTS:
            raise ValidationError(
                f"Drive file {filename or file_id!r} has an unsupported type for "
                f"NotebookLM upload. Accepted: {_ACCEPTED_EXTS_HINT}."
            )
        self._reject_oversize_header(response, file_id)
        path = await self._stream_to_temp(response, filename, extension)
        return DriveDownload(path=path, filename=filename, content_type=content_type or None)

    async def _classify_html(
        self, response: httpx.Response, ref: DriveRef, *, allow_confirm: bool
    ) -> _ConfirmRedirect:
        """Discriminate an HTML response: expired-auth / confirm page / not-downloadable."""
        file_id = ref.file_id
        final_host = (response.url.host or "").lower()
        if final_host == "accounts.google.com":
            raise AuthError("Drive authentication expired — run `notebooklm login`, then retry.")
        body = await _read_capped_text(response, _HTML_SNIFF_CAP_BYTES)
        if allow_confirm:
            confirm_params = _find_confirm_params(body, file_id)
            if confirm_params is not None:
                return _ConfirmRedirect(
                    url=_confirm_url(
                        confirm_params,
                        authuser=self._authuser,
                        resource_key=ref.resource_key,
                    )
                )
        raise ValidationError(
            f"Drive did not return downloadable bytes for {file_id}. If it's a native "
            "Google Doc/Slides/Sheet, add it with source_add(source_type='drive', "
            "mime_type='google-doc'|'google-slides'|'google-sheets') (or the `add-drive` "
            "CLI); if it's a permissions/not-found issue, confirm the file is accessible "
            "to this account."
        )

    def _reject_oversize_header(self, response: httpx.Response, file_id: str) -> None:
        raw = response.headers.get("content-length")
        if raw is None:
            return
        try:
            declared = int(raw)
        except ValueError:
            return
        if declared > self._max_bytes:
            raise ValidationError(
                f"Drive file {file_id} is {declared} bytes, over the "
                f"{self._max_bytes // (1024 * 1024)} MiB download cap."
            )

    async def _stream_to_temp(
        self, response: httpx.Response, filename: str, extension: str
    ) -> Path:
        """Stream a supported binary body to a 0600 temp file under a running cap.

        The temp file keeps the source extension so ``add_file``'s content-type
        resolution + HTML-reject gate fire. Unlinked on any failure; the caller
        unlinks it after the upload on success.
        """
        suffix = f".{extension}" if extension else ""
        temp_dir = str(self._temp_dir) if self._temp_dir is not None else None
        fd, temp_name = tempfile.mkstemp(prefix="nlm-drive-", suffix=suffix, dir=temp_dir)
        os.close(fd)  # mkstemp already created it 0600
        temp_path = Path(temp_name)
        try:
            await self._drain_to_temp(response, temp_path, filename)
            return temp_path
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    async def _drain_to_temp(
        self, response: httpx.Response, temp_path: Path, filename: str
    ) -> None:
        """Drain the streamed body to ``temp_path`` via a dedicated writer thread.

        A single writer thread performs every blocking ``handle.write()`` off the
        event loop, draining a bounded queue the async producer feeds — so this
        server-side fetch never starves a shared loop even while streaming up to
        the cap. Modelled on ``_artifact/downloads.py::download_url``. The running
        byte cap is enforced producer-side (before the queue) so an over-cap body
        aborts + cleans up without ever hitting disk beyond one queued chunk.
        """
        chunk_q: queue.Queue[bytes | None] = queue.Queue(maxsize=_DRIVE_WRITER_QUEUE_SIZE)
        writer_failed = threading.Event()
        writer_error: list[BaseException] = []

        def _writer_loop() -> None:
            # On failure, drain the queue in ``finally`` so a producer parked in
            # ``queue.put`` unblocks and can observe ``writer_failed``.
            try:
                with open(temp_path, "wb") as handle:
                    while True:
                        item = chunk_q.get()
                        if item is None:
                            return
                        handle.write(item)
            except BaseException as exc:  # noqa: BLE001 - surfaced via writer_error below
                writer_error.append(exc)
                writer_failed.set()
            finally:
                while True:
                    try:
                        chunk_q.get_nowait()
                    except queue.Empty:
                        break

        writer_thread = threading.Thread(
            target=_writer_loop,
            name=f"drive-dl-writer-{temp_path.name}",
            daemon=True,
        )
        writer_thread.start()
        total = 0
        try:
            async for chunk in response.aiter_bytes(_STREAM_CHUNK_BYTES):
                if writer_failed.is_set():
                    break
                total += len(chunk)
                if total > self._max_bytes:
                    raise ValidationError(
                        f"Drive download exceeded the {self._max_bytes // (1024 * 1024)} MiB "
                        f"cap for {filename or 'the file'}."
                    )
                # ``put_nowait`` fast-paths the common case; fall back to a
                # ``to_thread(put)`` only when the queue is full so the producer
                # suspends cleanly under back-pressure (never blocking the loop).
                try:
                    chunk_q.put_nowait(chunk)
                except queue.Full:
                    await asyncio.to_thread(chunk_q.put, chunk)
            if not writer_failed.is_set():
                try:
                    chunk_q.put_nowait(None)
                except queue.Full:
                    await asyncio.to_thread(chunk_q.put, None)
            await _await_writer_exit(writer_thread, re_raise_cancel=True)
            if writer_error:
                raise next(iter(writer_error))  # one-slot exception box
        except BaseException:
            # Ensure the writer sees a sentinel and exits even if the queue is
            # saturated (drop one item to make room, then put the sentinel) before
            # the outer handler unlinks the temp file — a bare unlink would race
            # the writer's still-open file handle.
            while True:
                try:
                    chunk_q.put_nowait(None)
                    break
                except queue.Full:
                    pass
                try:
                    chunk_q.get_nowait()
                except queue.Empty:
                    pass
            await _await_writer_exit(writer_thread)
            raise
        if total == 0:
            raise ValidationError("Drive returned 0 bytes — the file may be empty or inaccessible.")


class DriveImportService:
    """Route a Drive file id/URL: download the upload-only types, then upload.

    All I/O is behind the injected ``fetch`` + ``add_file`` seams; this class
    owns only the id parse, the pre-upload HTML second-defense, and the
    guaranteed temp-file cleanup.
    """

    def __init__(self, *, fetch: DriveFetch, add_file: AddFile) -> None:
        self._fetch = fetch
        self._add_file = add_file

    async def add_drive_file(
        self,
        notebook_id: str,
        id_or_url: str,
        *,
        title: str | None = None,
        wait: bool = False,
        wait_timeout: float = 120.0,
    ) -> Source:
        ref = parse_drive_ref(id_or_url)
        download = await self._fetch(ref)
        try:
            # Second defense: even if the fetch mislabeled the type, an HTML file
            # (by extension or content-type) is rejected before it reaches upload.
            _validate_upload_file_supported(download.path, download.content_type or "")
            return await self._add_file(
                notebook_id,
                download.path,
                title=title if title else (download.filename or None),
                wait=wait,
                wait_timeout=wait_timeout,
            )
        finally:
            # Temp file unlinked on every exit path (success, upload failure,
            # cancellation) — the upload ran inside this guard.
            download.path.unlink(missing_ok=True)
