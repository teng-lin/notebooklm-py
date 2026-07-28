"""Unit tests for the auto-route add-from-Drive service (#1884).

Injection-based, NO live Drive: the ``DriveFetcher`` HTTP leg is driven by a fake
streaming client, and ``DriveImportService`` is driven by a fake ``fetch`` +
``add_file``. Covers the routing table per extension, id/URL + resourcekey parse,
the r3 confirm-form discriminator, header-first close-before-body, the error
taxonomy (auth / rate-limit / server / network / validation), the authuser +
resourcekey URL routing, temp-file cleanup on every path, and the second defense.

Temp downloads are pointed at each test's own ``tmp_path`` (never the shared
system temp dir), so the leak assertions stay deterministic under parallel
(xdist) workers.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from notebooklm._source.drive_import import (
    _DRIVE_DOWNLOAD_URL,
    DriveDownload,
    DriveFetcher,
    DriveImportService,
    DriveRef,
    _filename_from_disposition,
    _find_confirm_params,
    extract_drive_file_id,
    parse_drive_ref,
)
from notebooklm.exceptions import (
    AuthError,
    NetworkError,
    RateLimitError,
    ServerError,
    ValidationError,
)

_FILE_ID = "1W20RJpJUD2JqXSEiM9Il48_fsdOtZ5fD"


# ---------------------------------------------------------------------------
# Fake streaming HTTP client
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        url: str = _DRIVE_DOWNLOAD_URL,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self.url = httpx.URL(url)
        self.body_reads = 0  # number of aiter_bytes() iterations started
        # Thread names alive while the body is being produced — lets a test prove
        # the disk writes run on a dedicated writer thread (off the event loop).
        self.threads_during_stream: set[str] = set()

    async def aiter_bytes(self, chunk_size: int = 65536) -> Any:
        self.body_reads += 1
        for start in range(0, len(self._body), chunk_size):
            self.threads_during_stream.update(t.name for t in threading.enumerate())
            yield self._body[start : start + chunk_size]


class _FakeStream:
    def __init__(self, response: _FakeResponse | BaseException) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        # A transport fault surfaces when the streaming GET is opened.
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeClient:
    def __init__(self, response: _FakeResponse | BaseException) -> None:
        self._response = response
        self.stream_urls: list[str] = []

    def stream(self, method: str, url: str) -> _FakeStream:
        self.stream_urls.append(url)
        return _FakeStream(self._response)

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _factory_for(*responses: _FakeResponse | BaseException):
    """Return a (factory, clients) pair yielding one fresh client per request."""
    clients = [_FakeClient(r) for r in responses]
    calls = iter(clients)

    def factory(_cookies: httpx.Cookies, _timeout: httpx.Timeout) -> _FakeClient:
        return next(calls)

    return factory, clients


def _fetcher(
    *responses: _FakeResponse | BaseException,
    max_bytes: int = 200 * 1024 * 1024,
    temp_dir: Path | None = None,
    authuser: str | None = None,
) -> DriveFetcher:
    factory, _clients = _factory_for(*responses)
    return DriveFetcher(
        cookies_provider=httpx.Cookies,
        client_factory=factory,
        max_bytes=max_bytes,
        authuser=authuser,
        temp_dir=temp_dir,
    )


def _attachment_headers(filename: str, **extra: str) -> dict[str, str]:
    headers = {
        "content-type": "application/octet-stream",
        "content-disposition": f'attachment; filename="{filename}"',
    }
    headers.update(extra)
    return headers


def _leaked_temps(temp_dir: Path) -> set[Path]:
    """The ``nlm-drive-*`` temp files in ``temp_dir`` (a per-test isolated dir)."""
    return set(temp_dir.glob("nlm-drive-*"))


# ===========================================================================
# parse_drive_ref / extract_drive_file_id
# ===========================================================================


class TestParseDriveRef:
    def test_raw_id(self) -> None:
        assert extract_drive_file_id(_FILE_ID) == _FILE_ID
        assert parse_drive_ref(_FILE_ID) == DriveRef(file_id=_FILE_ID, resource_key=None)

    @pytest.mark.parametrize(
        "url",
        [
            f"https://drive.google.com/file/d/{_FILE_ID}/view?usp=sharing",
            f"https://drive.google.com/open?id={_FILE_ID}",
            f"https://docs.google.com/document/d/{_FILE_ID}/edit",
            f"https://drive.usercontent.google.com/download?id={_FILE_ID}&export=download",
        ],
    )
    def test_url_forms(self, url: str) -> None:
        assert extract_drive_file_id(url) == _FILE_ID

    @pytest.mark.parametrize(
        "url",
        [
            f"https://drive.google.com/file/d/{_FILE_ID}/view?resourcekey=0-abcDEF",
            f"https://drive.google.com/open?id={_FILE_ID}&resourcekey=0-abcDEF",
        ],
    )
    def test_resource_key_preserved(self, url: str) -> None:
        ref = parse_drive_ref(url)
        assert ref.file_id == _FILE_ID
        assert ref.resource_key == "0-abcDEF"

    @pytest.mark.parametrize("bad", ["", "   ", "short", "not a url", "https://example.com/x"])
    def test_rejects_unparseable(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            parse_drive_ref(bad)

    @pytest.mark.parametrize(
        "url",
        [
            f"https://evil.example/file/d/{_FILE_ID}/view",
            f"https://drive.google.com.evil.example/open?id={_FILE_ID}",
            f"http://notgoogle.test/d/{_FILE_ID}",
        ],
    )
    def test_rejects_id_shaped_url_on_non_google_host(self, url: str) -> None:
        # A valid-looking id under a non-Google host must NOT be treated as a Drive
        # reference (host hardening) — it raises the clean parse ValidationError.
        with pytest.raises(ValidationError):
            parse_drive_ref(url)


# ===========================================================================
# _find_confirm_params — r3 Fix A discriminator
# ===========================================================================


_CONFIRM_HTML = (
    "<html><body><form action='https://drive.usercontent.google.com/download' method='get'>"
    f"<input type='hidden' name='id' value='{_FILE_ID}'>"
    "<input type='hidden' name='export' value='download'>"
    "<input type='hidden' name='confirm' value='t'>"
    "<input type='hidden' name='uuid' value='abc-uuid-123'>"
    "</form></body></html>"
)


class TestFindConfirmParams:
    def test_matching_signature(self) -> None:
        params = _find_confirm_params(_CONFIRM_HTML, _FILE_ID)
        assert params is not None
        assert params["confirm"] == "t"
        assert params["uuid"] == "abc-uuid-123"
        assert params["id"] == _FILE_ID
        assert params["export"] == "download"

    def test_wrong_host_rejected(self) -> None:
        html = _CONFIRM_HTML.replace("drive.usercontent.google.com", "evil.example.com")
        assert _find_confirm_params(html, _FILE_ID) is None

    def test_wrong_id_rejected(self) -> None:
        assert _find_confirm_params(_CONFIRM_HTML, "some-other-id-000000000") is None

    def test_empty_confirm_rejected(self) -> None:
        html = _CONFIRM_HTML.replace("value='t'", "value=''")
        assert _find_confirm_params(html, _FILE_ID) is None

    def test_plain_login_html_has_no_form(self) -> None:
        html = "<html><body>Sign in to continue</body></html>"
        assert _find_confirm_params(html, _FILE_ID) is None


# ===========================================================================
# _filename_from_disposition — Content-Disposition parsing (incl. RFC 5987)
# ===========================================================================


class TestFilenameFromDisposition:
    def test_plain_filename(self) -> None:
        assert _filename_from_disposition('attachment; filename="book.epub"') == "book.epub"

    def test_unquoted_filename(self) -> None:
        assert _filename_from_disposition("attachment; filename=book.epub") == "book.epub"

    def test_rfc5987_no_language_tag(self) -> None:
        assert _filename_from_disposition("attachment; filename*=UTF-8''plain.txt") == "plain.txt"

    def test_rfc5987_with_language_tag_and_pct_encoding(self) -> None:
        # A non-empty language tag (``en``) between the quotes must not defeat the
        # match, and percent-encoding is decoded.
        got = _filename_from_disposition("attachment; filename*=UTF-8'en'my%20book.epub")
        assert got == "my book.epub"

    def test_rfc5987_takes_precedence_over_plain(self) -> None:
        disposition = "attachment; filename=\"fallback.txt\"; filename*=UTF-8'en'real.epub"
        assert _filename_from_disposition(disposition) == "real.epub"

    def test_empty_disposition(self) -> None:
        assert _filename_from_disposition("") == ""


# ===========================================================================
# DriveFetcher — routing table per extension
# ===========================================================================


@pytest.mark.asyncio
class TestDriveFetcherRouting:
    @pytest.mark.parametrize(
        "ext", ["epub", "docx", "txt", "md", "rtf", "odt", "csv", "tsv", "pdf"]
    )
    async def test_supported_ext_downloads(self, ext: str, tmp_path: Path) -> None:
        body = b"payload-bytes-" + ext.encode()
        response = _FakeResponse(headers=_attachment_headers(f"book.{ext}"), body=body)
        download = await _fetcher(response, temp_dir=tmp_path)(DriveRef(_FILE_ID))
        assert download.filename == f"book.{ext}"
        assert download.path.suffix == f".{ext}"
        assert download.path.read_bytes() == body

    @pytest.mark.parametrize("ext", ["html", "htm", "xhtml"])
    async def test_html_extension_rejected_before_body(self, ext: str, tmp_path: Path) -> None:
        response = _FakeResponse(headers=_attachment_headers(f"page.{ext}"), body=b"<html>")
        with pytest.raises(ValidationError, match="HTML"):
            await _fetcher(response, temp_dir=tmp_path)(DriveRef(_FILE_ID))
        assert response.body_reads == 0  # header-first: body never streamed

    @pytest.mark.parametrize("ext", ["png", "exe", "zip", "bin"])
    async def test_unsupported_ext_rejected_before_body(self, ext: str, tmp_path: Path) -> None:
        response = _FakeResponse(headers=_attachment_headers(f"file.{ext}"), body=b"xxxx")
        with pytest.raises(ValidationError, match="unsupported|Accepted"):
            await _fetcher(response, temp_dir=tmp_path)(DriveRef(_FILE_ID))
        assert response.body_reads == 0

    async def test_over_cap_content_length_rejected_before_body(self, tmp_path: Path) -> None:
        response = _FakeResponse(
            headers=_attachment_headers("big.pdf", **{"content-length": str(500 * 1024 * 1024)}),
            body=b"x",
        )
        with pytest.raises(ValidationError, match="cap"):
            await _fetcher(response, temp_dir=tmp_path)(DriveRef(_FILE_ID))
        assert response.body_reads == 0

    async def test_running_byte_cap_aborts_and_cleans(self, tmp_path: Path) -> None:
        # No Content-Length header, but the streamed body exceeds the cap mid-stream.
        response = _FakeResponse(headers=_attachment_headers("book.epub"), body=b"x" * 5000)
        with pytest.raises(ValidationError, match="cap"):
            await _fetcher(response, max_bytes=1000, temp_dir=tmp_path)(DriveRef(_FILE_ID))
        assert _leaked_temps(tmp_path) == set()  # temp unlinked on the abort

    async def test_large_multichunk_body_streams_off_the_event_loop(self, tmp_path: Path) -> None:
        """A body far larger than one chunk streams byte-exact via the writer thread.

        The ~11-chunk body (> the 8-slot queue) exercises the ``queue.Full``
        back-pressure branch, and the assertion proves the blocking disk writes ran
        on a dedicated ``drive-dl-writer-*`` thread — NOT inline on the event loop
        (the #1873-class starvation this fix removes).
        """
        body = b"".join(bytes([i % 256]) for i in range(700_000))
        response = _FakeResponse(headers=_attachment_headers("big.epub"), body=body)
        download = await _fetcher(response, temp_dir=tmp_path)(DriveRef(_FILE_ID))
        assert download.path.read_bytes() == body
        assert any(
            name.startswith("drive-dl-writer-") for name in response.threads_during_stream
        ), "disk writes must run on a dedicated writer thread, not the event loop"

    async def test_zero_byte_download_rejected(self, tmp_path: Path) -> None:
        response = _FakeResponse(headers=_attachment_headers("book.epub"), body=b"")
        with pytest.raises(ValidationError, match="0 bytes"):
            await _fetcher(response, temp_dir=tmp_path)(DriveRef(_FILE_ID))
        assert _leaked_temps(tmp_path) == set()  # empty temp unlinked

    async def test_authuser_and_resourcekey_threaded_into_url(self, tmp_path: Path) -> None:
        response = _FakeResponse(headers=_attachment_headers("book.epub"), body=b"data")
        factory, clients = _factory_for(response)
        fetcher = DriveFetcher(
            cookies_provider=httpx.Cookies,
            client_factory=factory,
            authuser="7",
            temp_dir=tmp_path,
        )
        await fetcher(DriveRef(_FILE_ID, resource_key="0-abcDEF"))
        url = clients[0].stream_urls[0]
        assert "authuser=7" in url
        assert "resourcekey=0-abcDEF" in url
        assert f"id={_FILE_ID}" in url


# ===========================================================================
# DriveFetcher — HTML classification (r3 Fix A) + confirm re-request
# ===========================================================================


@pytest.mark.asyncio
class TestDriveFetcherHtmlClassification:
    async def test_auth_expired_on_service_login_redirect(self, tmp_path: Path) -> None:
        response = _FakeResponse(
            headers={"content-type": "text/html; charset=utf-8"},
            body=b"<html>login</html>",
            url="https://accounts.google.com/ServiceLogin",
        )
        with pytest.raises(AuthError, match="authentication expired"):
            await _fetcher(response, temp_dir=tmp_path)(DriveRef(_FILE_ID))

    async def test_401_maps_to_auth_error(self, tmp_path: Path) -> None:
        response = _FakeResponse(status_code=401, headers={"content-type": "text/html"})
        with pytest.raises(AuthError, match="authentication expired"):
            await _fetcher(response, temp_dir=tmp_path)(DriveRef(_FILE_ID))

    async def test_403_html_falls_through_to_pointer_not_auth(self, tmp_path: Path) -> None:
        # A 403 permission page must NOT be declared auth-expired; it goes to the
        # discriminator → non-committal pointer (permissions/not-found/native).
        response = _FakeResponse(
            status_code=403,
            headers={"content-type": "text/html"},
            body=b"<html><body>Access denied</body></html>",
            url=_DRIVE_DOWNLOAD_URL,
        )
        with pytest.raises(ValidationError, match="did not return downloadable bytes"):
            await _fetcher(response, temp_dir=tmp_path)(DriveRef(_FILE_ID))

    async def test_429_maps_to_rate_limit(self, tmp_path: Path) -> None:
        response = _FakeResponse(status_code=429)
        with pytest.raises(RateLimitError):
            await _fetcher(response, temp_dir=tmp_path)(DriveRef(_FILE_ID))

    async def test_5xx_maps_to_server_error(self, tmp_path: Path) -> None:
        response = _FakeResponse(status_code=503)
        with pytest.raises(ServerError):
            await _fetcher(response, temp_dir=tmp_path)(DriveRef(_FILE_ID))

    async def test_transport_failure_maps_to_network_error(self, tmp_path: Path) -> None:
        boom = httpx.ConnectError("dns boom")
        with pytest.raises(NetworkError):
            await _fetcher(boom, temp_dir=tmp_path)(DriveRef(_FILE_ID))

    async def test_non_committal_pointer_for_native_doc(self, tmp_path: Path) -> None:
        response = _FakeResponse(
            headers={"content-type": "text/html"},
            body=b"<html><body>Google Docs</body></html>",
            url=_DRIVE_DOWNLOAD_URL,
        )
        with pytest.raises(ValidationError, match="native Google Doc"):
            await _fetcher(response, temp_dir=tmp_path)(DriveRef(_FILE_ID))

    async def test_confirm_interstitial_re_requests_then_downloads(self, tmp_path: Path) -> None:
        first = _FakeResponse(
            headers={"content-type": "text/html"},
            body=_CONFIRM_HTML.encode(),
            url=_DRIVE_DOWNLOAD_URL,
        )
        second = _FakeResponse(headers=_attachment_headers("big.epub"), body=b"epub-bytes")
        factory, clients = _factory_for(first, second)
        fetcher = DriveFetcher(
            cookies_provider=httpx.Cookies, client_factory=factory, authuser="3", temp_dir=tmp_path
        )
        download = await fetcher(DriveRef(_FILE_ID, resource_key="rk-9"))
        assert download.path.read_bytes() == b"epub-bytes"
        # The confirmed re-request carried the parsed token + the account/resource routing.
        confirm_url = clients[1].stream_urls[0]
        assert "confirm=t" in confirm_url
        assert "uuid=abc-uuid-123" in confirm_url
        assert "authuser=3" in confirm_url
        assert "resourcekey=rk-9" in confirm_url


# ===========================================================================
# DriveImportService — orchestration, cleanup, second defense
# ===========================================================================


def _tmp_download(tmp_path: Path, name: str = "book.epub", body: bytes = b"data") -> DriveDownload:
    path = tmp_path / f"{name}.part"
    path.write_bytes(body)
    return DriveDownload(path=path, filename=name, content_type="application/epub+zip")


@pytest.mark.asyncio
class TestDriveImportService:
    async def test_success_downloads_uploads_and_cleans(self, tmp_path: Path) -> None:
        download = _tmp_download(tmp_path)
        added = object()
        calls: dict[str, Any] = {}

        async def fetch(ref: DriveRef) -> DriveDownload:
            calls["ref"] = ref
            return download

        async def add_file(notebook_id: str, path: Path, **kwargs: Any) -> Any:
            calls["add"] = (notebook_id, path, kwargs)
            assert path.exists()  # file still present during the upload
            return added

        service = DriveImportService(fetch=fetch, add_file=add_file)
        result = await service.add_drive_file(
            "nb_1", f"https://drive.google.com/file/d/{_FILE_ID}/view", title="My Book"
        )
        assert result is added
        assert calls["ref"].file_id == _FILE_ID
        nb, _path, kwargs = calls["add"]
        assert nb == "nb_1"
        assert kwargs["title"] == "My Book"
        assert not download.path.exists()  # unlinked in finally

    async def test_resource_key_threaded_to_fetch(self, tmp_path: Path) -> None:
        download = _tmp_download(tmp_path)
        seen: dict[str, DriveRef] = {}

        async def fetch(ref: DriveRef) -> DriveDownload:
            seen["ref"] = ref
            return download

        async def add_file(*_a: Any, **_k: Any) -> Any:
            return object()

        service = DriveImportService(fetch=fetch, add_file=add_file)
        await service.add_drive_file(
            "nb_1", f"https://drive.google.com/file/d/{_FILE_ID}/view?resourcekey=0-xyz"
        )
        assert seen["ref"].resource_key == "0-xyz"

    async def test_default_title_is_drive_filename(self, tmp_path: Path) -> None:
        download = _tmp_download(tmp_path, name="Report.pdf")

        async def fetch(_ref: DriveRef) -> DriveDownload:
            return download

        captured: dict[str, Any] = {}

        async def add_file(_nb: str, _path: Path, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return object()

        service = DriveImportService(fetch=fetch, add_file=add_file)
        await service.add_drive_file("nb_1", _FILE_ID)
        assert captured["title"] == "Report.pdf"

    async def test_temp_cleaned_on_upload_failure(self, tmp_path: Path) -> None:
        download = _tmp_download(tmp_path)

        async def fetch(_ref: DriveRef) -> DriveDownload:
            return download

        async def add_file(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("upload boom")

        service = DriveImportService(fetch=fetch, add_file=add_file)
        with pytest.raises(RuntimeError, match="upload boom"):
            await service.add_drive_file("nb_1", _FILE_ID)
        assert not download.path.exists()

    async def test_temp_cleaned_on_cancellation(self, tmp_path: Path) -> None:
        download = _tmp_download(tmp_path)

        async def fetch(_ref: DriveRef) -> DriveDownload:
            return download

        async def add_file(*_a: Any, **_k: Any) -> Any:
            raise asyncio.CancelledError()

        service = DriveImportService(fetch=fetch, add_file=add_file)
        with pytest.raises(asyncio.CancelledError):
            await service.add_drive_file("nb_1", _FILE_ID)
        assert not download.path.exists()

    async def test_html_mislabel_hits_second_defense_before_upload(self, tmp_path: Path) -> None:
        # A fetch that (hypothetically) returned an HTML file must still be rejected
        # by _validate_upload_file_supported BEFORE add_file is ever called.
        html_path = tmp_path / "page.html"
        html_path.write_bytes(b"<html></html>")
        download = DriveDownload(path=html_path, filename="page.html", content_type="text/html")
        add_called = False

        async def fetch(_ref: DriveRef) -> DriveDownload:
            return download

        async def add_file(*_a: Any, **_k: Any) -> Any:
            nonlocal add_called
            add_called = True
            return object()

        service = DriveImportService(fetch=fetch, add_file=add_file)
        with pytest.raises(ValidationError, match="HTML"):
            await service.add_drive_file("nb_1", _FILE_ID)
        assert add_called is False
        assert not html_path.exists()  # still cleaned up

    async def test_unparseable_id_never_fetches(self, tmp_path: Path) -> None:
        fetched = False

        async def fetch(_ref: DriveRef) -> DriveDownload:
            nonlocal fetched
            fetched = True
            return _tmp_download(tmp_path)

        async def add_file(*_a: Any, **_k: Any) -> Any:
            return object()

        service = DriveImportService(fetch=fetch, add_file=add_file)
        with pytest.raises(ValidationError):
            await service.add_drive_file("nb_1", "not-a-valid-id")
        assert fetched is False
