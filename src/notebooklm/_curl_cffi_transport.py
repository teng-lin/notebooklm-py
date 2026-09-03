"""PoC: an ``httpx.AsyncClient``-shaped adapter backed by ``curl_cffi``.

Lets the transport kernel speak to Google over a connection that impersonates a
real browser's TLS/JA3/HTTP-2 fingerprint (``curl_cffi``'s reason to exist),
while every downstream consumer keeps seeing ``httpx.Response`` objects and
``httpx`` exception types.

Scope: implements the slice of ``httpx.AsyncClient`` the authenticated surface uses
— ``.cookies``, ``.get()``, ``.post()``, ``.stream()``, ``.aclose()`` — plus
``.stream_upload()`` (low-level libcurl streaming upload, no full-file buffer).
Selected at runtime via ``NOTEBOOKLM_TRANSPORT=curl_cffi`` (see
``_web/transport/init._resolve_async_client_factory`` / ``resolve_transport_factory``).

ponytail: PoC, deliberately minimal. Known gaps: httpx ``limits`` ignored
(curl_cffi pools internally); the 4-slot
``httpx.Timeout`` is folded to curl's ``(connect, read)`` model (write/pool have
no libcurl equivalent — see ``_to_curl_timeout``); gzip handling assumes
``aiter_content``/``content`` yield already-decoded bytes (true for libcurl's
auto-decompress — verify against real gzip'd RPC before production).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import io
import os
import threading
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urljoin, urlparse

import httpx

from ._hop_credentials import CredentialPolicy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
    from http.cookiejar import CookieJar
    from typing import IO

# HTTP status codes that carry a ``Location`` we must re-validate before following.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

DEFAULT_IMPERSONATE = "chrome"
# Recognized values for NOTEBOOKLM_TRANSPORT; anything else (non-empty) is a typo.
_KNOWN_TRANSPORTS = frozenset({"curl_cffi", "httpx"})

# Streaming-upload fallbacks when the configured timeout doesn't pin one (seconds).
_DEFAULT_CONNECT_TIMEOUT = 30
_DEFAULT_STALL_TIMEOUT = 300

# Headers that must not survive onto a Response rebuilt from already-decoded
# bytes — same rationale as ``_streaming_post._STRIP_HEADERS_ON_REBUFFER``.
_STRIP_HEADERS = frozenset({"content-encoding", "content-length"})


def _to_curl_timeout(timeout: Any) -> float | tuple[float, float] | None:
    """Map an httpx.Timeout (or float/None) to curl_cffi's timeout model.

    curl_cffi takes a single total float or a ``(connect, read)`` tuple, which it
    applies as CONNECTTIMEOUT=connect and overall TIMEOUT=connect+read. httpx's
    4-slot Timeout (connect/read/write/pool) has no separate write/pool in
    libcurl, so those fold into the total — preserving the two slots curl can act
    on (connect + read) instead of collapsing everything to one window.
    """
    # ``bool`` is an ``int`` subclass — exclude it so a stray ``True``/``False``
    # isn't silently treated as a 1s/0s timeout.
    if timeout is None or (isinstance(timeout, (int, float)) and not isinstance(timeout, bool)):
        return timeout
    connect = getattr(timeout, "connect", None)
    read = getattr(timeout, "read", None)
    if connect is not None and read is not None:
        return (connect, read)
    return read if read is not None else connect


def _strip(headers: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _STRIP_HEADERS}


def _strip_preserving_duplicates(headers: Mapping[str, str]) -> list[tuple[str, str]]:
    """Strip rebuffer-invalid headers without collapsing repeated fields."""
    multi_items = getattr(headers, "multi_items", None)
    items = multi_items() if callable(multi_items) else headers.items()
    return [(key, value) for key, value in items if key.lower() not in _STRIP_HEADERS]


async def _materialize(content: Any) -> bytes | None:
    """Collapse an httpx-style request body (bytes / sync- or async-iterable) to bytes.

    curl_cffi's async ``data=`` accepts only ``bytes``/``str``/``BytesIO``/``dict``
    — never a (async) generator — so a streamed upload body must be buffered here.
    This is a curl_cffi API limitation, not a buffer we can stream around; it is
    bounded by NotebookLM's per-source upload size limit. For very large uploads,
    prefer the default httpx transport (which streams the body).
    """
    if content is None or isinstance(content, (bytes, bytearray)):
        return bytes(content) if content is not None else None
    if isinstance(content, str):
        return content.encode()
    if isinstance(content, io.IOBase):  # e.g. BytesIO — read it out
        return content.read()
    buf = bytearray()
    if hasattr(content, "__aiter__"):
        async for chunk in content:
            buf.extend(chunk)
        return bytes(buf)
    if hasattr(content, "__iter__"):
        for chunk in content:
            buf.extend(chunk)
        return bytes(buf)
    # Explicit contract: an unsupported body type would otherwise reach curl_cffi
    # and surface as a cryptic error. Fail clearly instead.
    raise TypeError(f"_materialize: unsupported content type {type(content).__name__!r}")


class _StreamedResponse:
    """Wraps a live curl_cffi streamed response in the shape ``stream_post_with_size_cap`` needs."""

    def __init__(self, curl_resp: Any, url: str, abort_callback: Callable[[], None]) -> None:
        self._r = curl_resp
        self._abort_callback = abort_callback
        self._aborted = False
        self.status_code: int = curl_resp.status_code
        self.headers = curl_resp.headers
        # Downstream rebuilds httpx.Response(request=...); give it a real one.
        self.request = httpx.Request("POST", url)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            resp = httpx.Response(
                status_code=self.status_code,
                headers=_strip(self.headers),
                request=self.request,
            )
            raise httpx.HTTPStatusError(
                f"Server error '{self.status_code}' for url '{self.request.url}'",
                request=self.request,
                response=resp,
            )

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        async for chunk in self._r.aiter_content():
            yield chunk

    def abort(self) -> None:
        """Interrupt the live curl handle before response-context teardown."""

        if self._aborted:
            return
        self._aborted = True
        self._abort_callback()

    async def aclose(self) -> None:
        """Abort a partial body, then settle curl_cffi's stream task."""

        self.abort()
        stream_task = self._r.astream_task
        try:
            await self._r.aclose()
        except asyncio.CancelledError:
            if stream_task is None or not stream_task.cancelled():
                raise


class _StreamCtx:
    """Async context manager mirroring ``httpx.AsyncClient.stream(...)``."""

    def __init__(self, client: CurlCffiAsyncClient, method: str, url: str, kwargs: dict[str, Any]):
        self._client = client
        self._method = method
        self._url = url
        self._kwargs = kwargs
        self._cm: Any = None
        self._response: Any = None
        self._aborted = False

    def _abort_response(self) -> None:
        """Remove an active response from curl's multi handle without waiting for I/O."""

        response = self._response
        if response is None:
            return
        self._aborted = True
        quit_now = response.quit_now
        if quit_now is not None:
            quit_now.set()
        stream_task = response.astream_task
        if stream_task is None or stream_task.done():
            return
        curl = response.curl
        if curl is not None:
            # ``remove_handle`` synchronously detaches the socket and cancels
            # the future awaited by ``astream_task``.  Merely setting
            # ``quit_now`` is insufficient while a peer sends no more bytes.
            self._client._curl.acurl.remove_handle(curl)

    async def __aenter__(self) -> _StreamedResponse:
        from curl_cffi.requests import RequestsError

        self._cm = self._client._curl.stream(self._method, self._url, **self._kwargs)
        try:
            curl_resp = await self._cm.__aenter__()
        except RequestsError as exc:  # transport failure -> httpx.RequestError for the mapper
            # __aexit__ is NOT auto-called when __aenter__ raises, so close the
            # curl stream handle ourselves before re-raising.
            try:
                await self._cm.__aexit__(type(exc), exc, exc.__traceback__)
            except Exception:  # noqa: BLE001 — cleanup must not mask the original error
                pass
            raise httpx.RequestError(
                str(exc), request=httpx.Request(self._method, self._url)
            ) from exc
        self._response = curl_resp
        return _StreamedResponse(curl_resp, self._url, self._abort_response)

    async def __aexit__(self, *exc: object) -> None:
        try:
            if self._cm is not None:
                try:
                    await self._cm.__aexit__(*exc)
                except asyncio.CancelledError:
                    stream_task = getattr(self._response, "astream_task", None)
                    if not (self._aborted and stream_task is not None and stream_task.cancelled()):
                        raise
        finally:
            self._client._sync_cookies_back()


class CurlCffiAsyncClient:
    """Minimal ``httpx.AsyncClient`` look-alike backed by ``curl_cffi.AsyncSession``."""

    def __init__(
        self,
        *,
        headers: Mapping[str, str] | None = None,
        cookies: httpx.Cookies | CookieJar | None = None,
        timeout: Any = None,
        follow_redirects: bool = True,
        limits: Any = None,  # noqa: ARG002 — accepted for httpx parity; curl_cffi pools internally
        impersonate: str | None = None,
    ) -> None:
        from curl_cffi.requests import AsyncSession

        # Match httpx.AsyncClient's cookie semantics: copy the caller's cookies
        # into our own httpx.Cookies jar so server Set-Cookie (rotated PSIDTS etc.)
        # never mutates the caller's jar — the runtime re-syncs rotations via
        # ``auth.cookie_jar = client.cookies``. ``httpx.Cookies(...)`` copies an
        # httpx.Cookies, wraps a raw http.cookiejar.CookieJar, and accepts None.
        self.cookies = httpx.Cookies(cookies)
        self._follow_redirects = follow_redirects
        self._timeout = _to_curl_timeout(timeout)
        # ``Any`` so it satisfies curl_cffi's ``impersonate: Literal[...]`` param
        # whether or not curl_cffi's stubs are installed — avoids a `type: ignore`
        # that mypy flags as unused in the (no-impersonate-extra) CI type-check.
        impersonate_value: Any = impersonate or os.environ.get(
            "NOTEBOOKLM_IMPERSONATE", DEFAULT_IMPERSONATE
        )
        self._impersonate = impersonate_value  # reused by the low-level streaming upload
        self._curl: Any = AsyncSession(
            headers=dict(headers) if headers else None,
            cookies=self.cookies.jar,
            impersonate=impersonate_value,
        )

    def _sync_cookies_back(self) -> None:
        """Merge server Set-Cookie (rotated PSIDTS etc.) back into the httpx jar."""
        for cookie in self._curl.cookies.jar:
            self.cookies.jar.set_cookie(cookie)

    def _redirects(self, kwargs: dict[str, Any]) -> bool:
        # httpx callers may pass ``follow_redirects`` per-request; curl_cffi uses
        # ``allow_redirects``. Translate so secondary auth clients work verbatim.
        return bool(kwargs.pop("follow_redirects", self._follow_redirects))

    def _timeout_for(self, kwargs: dict[str, Any]) -> Any:
        # Fall back to the session default ONLY when the caller omitted timeout;
        # an explicit ``timeout=0``/``None`` is honored (httpx treats those as
        # immediate / no-timeout, not "use default").
        if "timeout" not in kwargs:
            return self._timeout
        return _to_curl_timeout(kwargs.pop("timeout"))

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        from curl_cffi.requests import RequestsError

        allow_redirects = self._redirects(kwargs)
        timeout = self._timeout_for(kwargs)
        try:
            r = await self._curl.get(
                url,
                allow_redirects=allow_redirects,
                timeout=timeout,
                **kwargs,
            )
        except RequestsError as exc:
            raise httpx.RequestError(str(exc), request=httpx.Request("GET", url)) from exc
        self._sync_cookies_back()
        # curl_cffi .content is already decoded; build a real httpx.Response so
        # callers get .text/.url/.raise_for_status() unchanged.
        return httpx.Response(
            status_code=r.status_code,
            headers=_strip(r.headers),
            content=r.content,
            request=httpx.Request("GET", r.url),
        )

    async def get_guarded(
        self,
        url: str,
        *,
        is_trusted_host: Callable[[str | None], bool],
        credential_for: CredentialPolicy | None = None,
        max_redirects: int = 20,  # match httpx.AsyncClient's default for cross-transport parity
        **kwargs: Any,
    ) -> httpx.Response:
        """GET that follows redirects MANUALLY, re-validating every hop's host.

        The curl_cffi/libcurl equivalent of the httpx #1521 redirect-revalidation
        event hook: libcurl's *internal* auto-follow loop can't host a per-hop
        Python policy callback, so we disable it (``allow_redirects=False``) and
        follow ``Location`` ourselves, checking each hop's scheme + host against
        ``is_trusted_host`` *before* connecting.

        SECURITY — this is an SSRF boundary, so two things are deliberate:

        * We validate the **raw** URL host (via ``urlparse``), never curl_cffi's
          ``requote_uri``'d form. ``requote_uri`` un-escapes ``%2e`` to ``.``
          (``.`` is "unreserved"), so ``evil%2egoogleapis.com`` would otherwise
          decode to a trusted-looking ``evil.googleapis.com`` *after* the check.
          ``is_trusted_host`` already rejects any host containing ``%`` / ``/`` /
          ``\\`` (#1521); we keep that gate ahead of the request.
        * We pass ``quote=False`` so curl_cffi hands libcurl the exact URL we
          validated — otherwise it would rewrite the host before connecting and
          the host we checked would not be the host libcurl dials.

        Next-hop targets come from the raw ``Location`` header (not curl_cffi's
        decoded ``redirect_url``), resolved against the current URL.
        """
        from curl_cffi.requests import RequestsError

        timeout = self._timeout_for(kwargs)
        # We hardcode allow_redirects=False and follow manually; drop any
        # caller-supplied follow_redirects so it can't collide with that.
        kwargs.pop("follow_redirects", None)
        current = url
        managed_header_names = {"authorization", "proxy-authorization"}
        for _ in range(max_redirects + 1):
            parsed = urlparse(current)
            if parsed.scheme != "https" or not is_trusted_host(parsed.hostname):
                raise httpx.RequestError(
                    f"untrusted or non-HTTPS download hop: {parsed.hostname or '<unknown>'}",
                    request=httpx.Request("GET", current),
                )
            try:
                hop_kwargs = dict(kwargs)
                credentials = None
                if credential_for is not None:
                    credentials = await credential_for(current)
                    if credentials is not None:
                        managed_header_names.update(name.lower() for name in credentials.headers)

                    # A policy-managed request uses the callback as the sole
                    # credential source, including on hop 1. curl_cffi otherwise
                    # merges constructor/session cookies and headers into every
                    # request before applying the per-call values.
                    self._curl.cookies.clear()
                    for name in managed_header_names:
                        self._curl.headers.pop(name, None)

                    hop_kwargs["cookies"] = (
                        credentials.cookies.jar
                        if credentials is not None and credentials.cookies is not None
                        else {}
                    )
                    hop_headers = httpx.Headers(hop_kwargs.get("headers") or {})
                    for name in managed_header_names:
                        hop_headers.pop(name, None)
                    if credentials is not None:
                        hop_headers.update(credentials.headers)
                    if hop_headers or "headers" in hop_kwargs:
                        hop_kwargs["headers"] = dict(hop_headers)
                    # curl_cffi promotes per-call request cookies into its
                    # AsyncSession cookie store unless response parsing is told
                    # not to. A policy-managed session must remain credential-free
                    # so a later None result cannot inherit an earlier hop's jar.
                    hop_kwargs["discard_cookies"] = True
                r = await self._curl.get(
                    current,
                    allow_redirects=False,
                    quote=False,
                    timeout=timeout,
                    **hop_kwargs,
                )
            except RequestsError as exc:
                raise httpx.RequestError(str(exc), request=httpx.Request("GET", current)) from exc
            if (
                credential_for is not None
                and credentials is not None
                and credentials.cookies is not None
            ):
                # ``discard_cookies=True`` prevents curl_cffi from promoting
                # request cookies into session state. Preserve normal redirect
                # cookie rotation explicitly in the policy-selected jar, using
                # the current request URL for correct host-only cookie scoping.
                credentials.cookies.extract_cookies(
                    httpx.Response(
                        status_code=r.status_code,
                        headers=_strip_preserving_duplicates(r.headers),
                        request=httpx.Request("GET", current),
                    )
                )
            self._sync_cookies_back()
            if r.status_code in _REDIRECT_STATUSES:
                location = r.headers.get("location")
                if not location:
                    # Malformed redirect (3xx with no target): fail closed rather
                    # than return a 3xx the caller's raise_for_status won't catch.
                    raise httpx.RequestError(
                        f"redirect status {r.status_code} without a Location header",
                        request=httpx.Request("GET", current),
                    )
                current = urljoin(current, location)
                continue
            return httpx.Response(
                status_code=r.status_code,
                headers=_strip(r.headers),
                content=r.content,
                request=httpx.Request("GET", r.url),
            )
        raise httpx.RequestError(
            f"exceeded {max_redirects} redirects", request=httpx.Request("GET", url)
        )

    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: Any = None,
        **kwargs: Any,
    ) -> httpx.Response:
        from curl_cffi.requests import RequestsError

        body = await _materialize(content)
        allow_redirects = self._redirects(kwargs)
        timeout = self._timeout_for(kwargs)
        try:
            r = await self._curl.post(
                url,
                headers=dict(headers) if headers else None,
                data=body,
                allow_redirects=allow_redirects,
                timeout=timeout,
                **kwargs,
            )
        except RequestsError as exc:
            raise httpx.RequestError(str(exc), request=httpx.Request("POST", url)) from exc
        self._sync_cookies_back()
        # Preserve response headers (e.g. ``x-goog-upload-url``); only strip the
        # decode-confusing ones, same as ``.get()``.
        return httpx.Response(
            status_code=r.status_code,
            headers=_strip(r.headers),
            content=r.content,
            # Use the final (post-redirect) URL, consistent with ``get()``.
            request=httpx.Request("POST", r.url),
        )

    async def delete(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """DELETE, for the Drive staging cleanup in ``_android.drive_staging``.

        Without it that cleanup raises ``AttributeError`` under
        ``NOTEBOOKLM_TRANSPORT=curl_cffi`` and, because cleanup failures are
        best-effort, silently leaves the staged file in the user's Drive.
        """
        from curl_cffi.requests import RequestsError

        allow_redirects = self._redirects(kwargs)
        timeout = self._timeout_for(kwargs)
        try:
            r = await self._curl.delete(
                url,
                headers=dict(headers) if headers else None,
                allow_redirects=allow_redirects,
                timeout=timeout,
                **kwargs,
            )
        except RequestsError as exc:
            raise httpx.RequestError(str(exc), request=httpx.Request("DELETE", url)) from exc
        self._sync_cookies_back()
        return httpx.Response(
            status_code=r.status_code,
            headers=_strip(r.headers),
            content=r.content,
            request=httpx.Request("DELETE", r.url),
        )

    def stream(self, method: str, url: str, **kwargs: Any) -> _StreamCtx:
        stream_kwargs: dict[str, Any] = {
            "allow_redirects": self._redirects(kwargs),
            "timeout": self._timeout_for(kwargs),
        }
        # Map httpx's stream kwargs onto curl_cffi's: body content + headers.
        # NOTE: ``content`` must be bytes here — curl_cffi's stream ``data=`` can't
        # consume a (async) generator. A streamed body goes through ``post()``
        # (which buffers via ``_materialize``); the kernel's RPC stream is bytes.
        if "content" in kwargs:
            stream_kwargs["data"] = kwargs.pop("content")
        if kwargs.get("headers"):
            stream_kwargs["headers"] = dict(kwargs.pop("headers"))
        stream_kwargs.update(kwargs)
        return _StreamCtx(self, method, url, stream_kwargs)

    def _cookie_header_for(self, url: str) -> str:
        """Build the ``Cookie:`` header for ``url`` from the authoritative jar."""
        req = httpx.Request("GET", url)
        self.cookies.set_cookie_header(req)
        return req.headers.get("cookie", "")

    def _connect_and_stall_timeouts(self) -> tuple[int, int]:
        """Derive (connect, stall) seconds from the configured timeout.

        Returns the connect cap and a stall window. No *overall* cap is applied —
        a large upload that keeps progressing must not be killed — but a hung
        connection is bounded by a low-speed (stall) guard set to the stall window.
        Both are floored to a non-zero default: libcurl treats ``LOW_SPEED_TIME=0``
        as "disabled", which (with no overall cap) would let a hung upload hang
        forever — so a ``0``/``None``/sub-second timeout must NOT disable the guard.
        """
        timeout = self._timeout
        if isinstance(timeout, tuple):  # (connect, read)
            connect, read = timeout
        elif isinstance(timeout, (int, float)):
            connect = read = timeout  # httpx scalar timeout applies to every slot
        else:
            connect, read = _DEFAULT_CONNECT_TIMEOUT, _DEFAULT_STALL_TIMEOUT
        return (int(connect) or _DEFAULT_CONNECT_TIMEOUT, int(read) or _DEFAULT_STALL_TIMEOUT)

    async def stream_upload(
        self,
        url: str,
        source: IO[bytes] | str | os.PathLike[str],
        *,
        total_bytes: int,
        headers: Mapping[str, str],
        method: str = "POST",
        on_chunk: Callable[[int], Awaitable[None]] | None = None,
        overall_timeout: float | None = None,
        stop_on_cancel: bool = False,
    ) -> httpx.Response:
        """Stream a request body from disk via libcurl — no full-body buffering.

        The high-level curl_cffi API always buffers ``data`` into ``CURLOPT_POSTFIELDS``;
        this drops to the low-level ``Curl`` with ``CURLOPT_READFUNCTION`` so libcurl
        pulls the body in chunks. It impersonates the SAME fingerprint as the session
        (the upload endpoint correlates it) and runs the blocking ``perform()`` in a
        thread. ``source`` is a file path (str/PathLike, opened/closed here) or an open
        binary file (read, not closed — the caller owns it). Returns an ``httpx.Response``.
        """
        from curl_cffi import Curl, CurlError, CurlInfo, CurlOpt

        cookie_header = self._cookie_header_for(url)
        header_list = [f"{k}: {v}".encode() for k, v in headers.items()]
        owns_handle = isinstance(source, (str, os.PathLike))  # a path we open
        connect_timeout, stall_timeout = self._connect_and_stall_timeouts()

        event_loop = asyncio.get_running_loop()
        cancel_requested = threading.Event()

        def _run() -> tuple[int, bytes, bytes]:
            # ``fh`` is Any: a path we open, or the caller's already-open binary file.
            # Independent cleanup: the file (if we opened it) must close even if
            # Curl() construction or curl.close() raises — hence nested try/finally,
            # not one shared finally.
            if owns_handle:
                fh: Any = open(cast("str | os.PathLike[str]", source), "rb")  # noqa: SIM115
            else:
                fh = source
            try:
                body = io.BytesIO()
                response_headers = io.BytesIO()
                curl = Curl()
                try:
                    curl.impersonate(self._impersonate)
                    curl.setopt(CurlOpt.URL, url.encode())
                    curl.setopt(CurlOpt.UPLOAD, 1)
                    curl.setopt(CurlOpt.CUSTOMREQUEST, method.encode())  # UPLOAD defaults to PUT
                    curl.setopt(CurlOpt.INFILESIZE_LARGE, total_bytes)

                    def _read(size: int) -> bytes:
                        if cancel_requested.is_set():
                            return b""
                        chunk = fh.read(size)
                        if chunk and on_chunk is not None:

                            async def _invoke_callback() -> None:
                                await on_chunk(len(chunk))

                            callback: concurrent.futures.Future[None] = (
                                asyncio.run_coroutine_threadsafe(_invoke_callback(), event_loop)
                            )
                            callback.result()
                        return chunk

                    curl.setopt(CurlOpt.READFUNCTION, _read)  # libcurl pulls chunks from disk
                    curl.setopt(CurlOpt.HTTPHEADER, header_list)
                    if cookie_header:
                        curl.setopt(CurlOpt.COOKIE, cookie_header.encode())
                    curl.setopt(CurlOpt.WRITEDATA, body)
                    curl.setopt(CurlOpt.HEADERFUNCTION, response_headers.write)
                    curl.setopt(CurlOpt.CONNECTTIMEOUT, connect_timeout)
                    # No overall cap (large uploads keep progressing), but bound a hung
                    # connection: abort if throughput stays < 1 byte/s for the stall window.
                    curl.setopt(CurlOpt.LOW_SPEED_LIMIT, 1)
                    curl.setopt(CurlOpt.LOW_SPEED_TIME, stall_timeout)
                    if overall_timeout is not None:
                        curl.setopt(
                            CurlOpt.TIMEOUT_MS,
                            max(1, int(float(overall_timeout) * 1000)),
                        )
                    curl.perform()
                    status_raw: Any = curl.getinfo(CurlInfo.RESPONSE_CODE)
                    return int(status_raw), body.getvalue(), response_headers.getvalue()
                finally:
                    curl.close()
            finally:
                if owns_handle:
                    fh.close()

        # A thread can't be cancelled, so if this coroutine is cancelled we drain
        # the worker to completion (re-shielding through repeated cancellations)
        # rather than orphan a live authenticated upload, then propagate the cancel.
        task = asyncio.ensure_future(asyncio.to_thread(_run))
        try:
            status, content, raw_headers = await asyncio.shield(task)
        except asyncio.CancelledError:
            if stop_on_cancel:
                cancel_requested.set()
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue  # cancelled again — keep draining
                except BaseException:  # noqa: BLE001 — worker finished (with error); done draining
                    break
            raise
        except CurlError as exc:  # low-level Curl raises CurlError (RequestsError subclasses it)
            raise httpx.RequestError(str(exc), request=httpx.Request(method, url)) from exc
        # No cookie sync-back: the resumable upload leg doesn't rotate auth cookies,
        # and this used a standalone Curl (not the session jar).
        blocks = [block for block in raw_headers.split(b"\r\n\r\n") if block.strip()]
        header_items: list[tuple[str, str]] = []
        if blocks:
            final_header_block = next(reversed(blocks))
            for line in final_header_block.split(b"\r\n")[1:]:
                if b":" not in line:
                    continue
                name, value = line.split(b":", 1)
                header_items.append((name.decode("latin-1"), value.strip().decode("latin-1")))
        return httpx.Response(
            status_code=status,
            headers=header_items,
            content=content,
            request=httpx.Request(method, url),
        )

    async def aclose(self) -> None:
        await self._curl.close()

    async def __aenter__(self) -> CurlCffiAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


def make_curl_cffi_factory(impersonate: str | None = None) -> Any:
    """Return an ``async_client_factory`` that builds :class:`CurlCffiAsyncClient`."""

    def factory(**kwargs: Any) -> CurlCffiAsyncClient:
        return CurlCffiAsyncClient(impersonate=impersonate, **kwargs)

    return factory


def resolve_transport_factory() -> Any:
    """Pick the HTTP client factory for the current transport opt-in.

    The single source of truth for ``NOTEBOOKLM_TRANSPORT=curl_cffi``: returns the
    curl_cffi factory when set, else ``httpx.AsyncClient``. Used by every
    authenticated-Google client site (main RPC kernel, upload, account, refresh, and
    artifact downloads) so the whole API surface shares ONE TLS fingerprint —
    including the download's cookie-bearing first hop.

    ``httpx.AsyncClient`` is resolved at CALL time (not bound as a default arg) so
    tests that ``patch("httpx.AsyncClient")`` still intercept the opt-out path.

    NOTE: importing this module does not import curl_cffi — that happens lazily only
    when the returned factory is actually called, so the opt-out path stays pure-httpx
    with no hard dependency.

    Downloads keep the #1521 redirect-host SSRF guard on both transports: httpx uses
    response ``event_hooks``; the curl_cffi path can't inject a hook into libcurl's
    internal auto-follow loop, so ``CurlCffiAsyncClient.get_guarded`` disables
    auto-follow and re-validates each hop manually (see ``_artifact/_download_client``).

    A non-empty ``NOTEBOOKLM_TRANSPORT`` that isn't a known transport raises, so a
    typo (e.g. ``curlcffi``) fails loudly instead of silently using httpx while the
    operator believes impersonation is on.
    """
    transport = os.environ.get("NOTEBOOKLM_TRANSPORT", "").strip()
    if transport == "curl_cffi":
        return make_curl_cffi_factory()
    if transport and transport not in _KNOWN_TRANSPORTS:
        raise ValueError(
            f"Unknown NOTEBOOKLM_TRANSPORT={transport!r}; "
            f"expected one of {sorted(_KNOWN_TRANSPORTS)} or unset."
        )
    return httpx.AsyncClient
