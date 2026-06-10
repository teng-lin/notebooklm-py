"""Unit tests for ``ArtifactsAPI._download_url`` httpx-error wrapping.

These tests pin the contract: every httpx failure (auth, generic HTTP,
timeout, connection error) is surfaced as :class:`ArtifactDownloadError`,
never as a raw ``httpx`` subclass. 401/403 carry an explicit
``Authentication required ... try `notebooklm login``` hint plus the
``status_code`` attribute on the exception; other HTTP errors keep their
``status_code``; transport errors leave ``status_code`` ``None``.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import notebooklm._artifact.downloads as _downloads_mod
from notebooklm._artifacts import ArtifactsAPI
from notebooklm.types import ArtifactDownloadError


@pytest.fixture
def mock_artifacts_api():
    """ArtifactsAPI wired to MagicMocks -- no real I/O."""
    from notebooklm._mind_map import NoteBackedMindMapService
    from notebooklm._note_service import NoteService
    from tests._fixtures.fake_core import make_fake_core

    mock_core = make_fake_core(
        rpc_call=AsyncMock(),
        get_source_ids=AsyncMock(return_value=[]),
    )
    mind_maps = MagicMock(spec=NoteBackedMindMapService)
    mind_maps.list_mind_maps = AsyncMock(return_value=[])
    note_service = MagicMock(spec=NoteService)
    api = ArtifactsAPI(
        rpc=mock_core,
        drain=mock_core,
        lifecycle=mock_core,
        notebooks=MagicMock(),
        mind_maps=mind_maps,
        note_service=note_service,
    )
    return api


def _build_mock_response(
    *,
    raise_for_status_exc: Exception | None = None,
    content: bytes = b"",
    content_type: str = "video/mp4",
) -> MagicMock:
    """Build a mock streaming response for ``client.stream()``.

    If ``raise_for_status_exc`` is provided, ``raise_for_status`` raises it.
    Otherwise the response streams ``content`` in a single chunk.
    """

    async def mock_aiter_bytes(chunk_size: int = 8192):
        if content:
            yield content

    mock_response = MagicMock()
    mock_response.headers = {"content-type": content_type}
    if raise_for_status_exc is not None:
        mock_response.raise_for_status = MagicMock(side_effect=raise_for_status_exc)
    else:
        mock_response.raise_for_status = MagicMock()
    mock_response.aiter_bytes = mock_aiter_bytes
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)
    return mock_response


def _patch_httpx_client(
    mock_response: MagicMock | None = None, *, stream_exc: Exception | None = None
):
    """Return ctx managers patching httpx.AsyncClient and load_httpx_cookies.

    ``stream_exc``: if set, ``client.stream(...)`` raises this exception when
    entered (covers httpx.ConnectError / TimeoutException raised during
    connection establishment, before any response arrives).
    """
    mock_client = AsyncMock()
    if stream_exc is not None:
        # Make ``async with client.stream(...) as response`` raise on enter.
        failing_cm = MagicMock()
        failing_cm.__aenter__ = AsyncMock(side_effect=stream_exc)
        failing_cm.__aexit__ = AsyncMock(return_value=None)
        mock_client.stream = MagicMock(return_value=failing_cm)
    else:
        assert mock_response is not None
        mock_client.stream = MagicMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    return (
        patch.object(httpx, "AsyncClient", return_value=mock_client),
        patch.object(_downloads_mod, "load_httpx_cookies", return_value=MagicMock()),
    )


def _make_http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """Build a real httpx.HTTPStatusError carrying ``status_code``."""
    request = httpx.Request("GET", "https://storage.googleapis.com/file.mp4")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"HTTP {status_code}",
        request=request,
        response=response,
    )


class TestDownloadUrlErrorWrapping:
    """Pin the contract that httpx errors become ArtifactDownloadError."""

    @pytest.mark.asyncio
    async def test_happy_path_returns_output_path(self, mock_artifacts_api):
        """200 OK with body -> returns output_path, file written."""
        api = mock_artifacts_api

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "file.mp4")
            content = b"binary media payload"
            response = _build_mock_response(content=content)
            client_patch, cookies_patch = _patch_httpx_client(response)

            with client_patch, cookies_patch as mock_load_cookies:
                result = await api._download_url(
                    "https://storage.googleapis.com/file.mp4", output_path
                )

            assert result == output_path
            with open(output_path, "rb") as f:
                assert f.read() == content
            # The locally-imported seam alias was actually patched and
            # invoked -- guards against a silently-no-op object patch.
            mock_load_cookies.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            "https://attacker.evil\\.google.com/file.mp4",
            "https://attacker.evil%5c.google.com/file.mp4",
            "https://attacker.evil%5C.google.com/file.mp4",
            "https://attacker.evil%2f.google.com/file.mp4",
            "https://attacker.evil%2F.google.com/file.mp4",
            "https://storage.googleapis.com@attacker.evil/file.mp4",
        ],
    )
    async def test_untrusted_hostname_shapes_rejected_before_streaming(
        self, mock_artifacts_api, tmp_path, url
    ):
        """Host allowlist uses parsed hostname, not the display netloc."""
        api = mock_artifacts_api
        output_path = tmp_path / "file.mp4"

        with pytest.raises(ArtifactDownloadError) as exc_info:
            await api._download_url(url, str(output_path))

        assert "Untrusted download domain" in str(exc_info.value)
        assert "storage.googleapis.com@" not in str(exc_info.value)
        assert not output_path.exists()
        assert list(tmp_path.glob("file.mp4.*.tmp")) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            "https://storage.googleapis.com:443/file.mp4",
            "https://user:pass@storage.googleapis.com:443/file.mp4",
        ],
    )
    async def test_trusted_hostname_with_userinfo_or_port_allowed(
        self, mock_artifacts_api, tmp_path, url
    ):
        """Port and userinfo components must not participate in host matching."""
        api = mock_artifacts_api
        output_path = tmp_path / "file.mp4"
        response = _build_mock_response(content=b"binary media payload")
        client_patch, cookies_patch = _patch_httpx_client(response)

        with client_patch, cookies_patch:
            result = await api._download_url(url, str(output_path))

        assert result == str(output_path)
        assert output_path.read_bytes() == b"binary media payload"

    @pytest.mark.asyncio
    async def test_401_raises_artifact_download_error_with_auth_hint(self, mock_artifacts_api):
        """401 -> ArtifactDownloadError mentioning re-auth, status_code=401."""
        api = mock_artifacts_api

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "file.mp4")
            err = _make_http_status_error(401)
            response = _build_mock_response(raise_for_status_exc=err)
            client_patch, cookies_patch = _patch_httpx_client(response)

            with (
                client_patch,
                cookies_patch,
                pytest.raises(ArtifactDownloadError) as exc_info,
            ):
                await api._download_url("https://storage.googleapis.com/file.mp4", output_path)

            assert exc_info.value.status_code == 401
            assert "Authentication required" in str(exc_info.value)
            assert "notebooklm login" in str(exc_info.value)
            # Cause preserved for diagnostics.
            assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)
            # Partial temp file cleaned up.
            assert not os.path.exists(output_path)
            assert not os.path.exists(output_path + ".tmp")

    @pytest.mark.asyncio
    async def test_403_raises_artifact_download_error_with_auth_hint(self, mock_artifacts_api):
        """403 follows the same auth-hint path as 401, with status_code=403."""
        api = mock_artifacts_api

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "file.mp4")
            err = _make_http_status_error(403)
            response = _build_mock_response(raise_for_status_exc=err)
            client_patch, cookies_patch = _patch_httpx_client(response)

            with (
                client_patch,
                cookies_patch,
                pytest.raises(ArtifactDownloadError) as exc_info,
            ):
                await api._download_url("https://storage.googleapis.com/file.mp4", output_path)

            assert exc_info.value.status_code == 403
            assert "Authentication required" in str(exc_info.value)
            assert "notebooklm login" in str(exc_info.value)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (_make_http_status_error(403), "Authentication required"),
            (_make_http_status_error(500), "HTTP error downloading"),
            (httpx.ReadTimeout("read timed out"), "Network error"),
        ],
    )
    async def test_error_details_redact_userinfo(self, mock_artifacts_api, error, expected):
        """Trusted URLs may contain userinfo, but diagnostics must not echo it."""
        api = mock_artifacts_api

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "file.mp4")
            if isinstance(error, httpx.HTTPStatusError):
                response = _build_mock_response(raise_for_status_exc=error)
                client_patch, cookies_patch = _patch_httpx_client(response)
            else:
                client_patch, cookies_patch = _patch_httpx_client(stream_exc=error)

            with (
                client_patch,
                cookies_patch,
                pytest.raises(ArtifactDownloadError) as exc_info,
            ):
                await api._download_url(
                    "https://user:pass@storage.googleapis.com:443/file.mp4",
                    output_path,
                )

            message = str(exc_info.value)
            assert expected in message
            assert "user:pass" not in message
            assert "storage.googleapis.com/file.mp4" in message

    @pytest.mark.asyncio
    async def test_500_raises_artifact_download_error_generic_http(self, mock_artifacts_api):
        """500 -> ArtifactDownloadError without auth hint, status_code=500."""
        api = mock_artifacts_api

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "file.mp4")
            err = _make_http_status_error(500)
            response = _build_mock_response(raise_for_status_exc=err)
            client_patch, cookies_patch = _patch_httpx_client(response)

            with (
                client_patch,
                cookies_patch,
                pytest.raises(ArtifactDownloadError) as exc_info,
            ):
                await api._download_url("https://storage.googleapis.com/file.mp4", output_path)

            # ``status_code`` rides on the exception attribute, so the
            # message text no longer repeats it. The message uses
            # a sanitized host plus ``parsed.path`` so capability tokens in query
            # params can't leak into log lines.
            assert exc_info.value.status_code == 500
            assert "HTTP error downloading" in str(exc_info.value)
            assert "storage.googleapis.com/file.mp4" in str(exc_info.value)
            assert "Authentication required" not in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_timeout_raises_artifact_download_error_no_status(self, mock_artifacts_api):
        """httpx.TimeoutException -> ArtifactDownloadError, status_code=None."""
        api = mock_artifacts_api

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "file.mp4")
            client_patch, cookies_patch = _patch_httpx_client(
                stream_exc=httpx.ReadTimeout("read timed out"),
            )

            with (
                client_patch,
                cookies_patch,
                pytest.raises(ArtifactDownloadError) as exc_info,
            ):
                await api._download_url("https://storage.googleapis.com/file.mp4", output_path)

            assert exc_info.value.status_code is None
            assert "Network error" in str(exc_info.value)
            assert isinstance(exc_info.value.__cause__, httpx.ReadTimeout)

    @pytest.mark.asyncio
    async def test_connect_error_raises_artifact_download_error(self, mock_artifacts_api):
        """httpx.ConnectError -> ArtifactDownloadError, status_code=None."""
        api = mock_artifacts_api

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "file.mp4")
            client_patch, cookies_patch = _patch_httpx_client(
                stream_exc=httpx.ConnectError("dns resolution failed"),
            )

            with (
                client_patch,
                cookies_patch,
                pytest.raises(ArtifactDownloadError) as exc_info,
            ):
                await api._download_url("https://storage.googleapis.com/file.mp4", output_path)

            assert exc_info.value.status_code is None
            assert "Network error" in str(exc_info.value)
            assert isinstance(exc_info.value.__cause__, httpx.ConnectError)

    @pytest.mark.asyncio
    async def test_cancellation_during_write_removes_temp_file(self, mock_artifacts_api, tmp_path):
        """Cancelled streaming writes must not leave the mkstemp temp file behind.

        After the review-fix refactor (PR #981 r2: gemini findings), the
        producer uses ``put_nowait`` whenever the bounded queue has
        space, falling back to ``to_thread(put, ...)`` only under back-
        pressure. For small payloads that fit in the queue, no
        ``to_thread(put)`` call fires — so the test cannot gate on it.
        Instead we gate on the producer's ``aiter_bytes`` await, which
        is the canonical async-cancellable yield point inside the chunk
        loop. The invariant under test — no leftover ``mkstemp`` temp
        file after cancellation — is unchanged.
        """
        api = mock_artifacts_api
        output_path = tmp_path / "file.mp4"

        chunk_yielded = asyncio.Event()
        allow_finish = asyncio.Event()

        async def mock_aiter_bytes(chunk_size: int = 8192):
            # Yield one real chunk so the producer enters the chunk
            # loop body and pushes a chunk onto the queue, then park
            # at a cancellable ``await`` so the test can deliver
            # ``task.cancel()`` while the producer is mid-stream.
            yield b"partial media payload"
            chunk_yielded.set()
            await allow_finish.wait()

        response = _build_mock_response(content=b"partial media payload")
        response.aiter_bytes = mock_aiter_bytes
        client_patch, cookies_patch = _patch_httpx_client(response)

        with client_patch, cookies_patch:
            task = asyncio.create_task(
                api._download_url("https://storage.googleapis.com/file.mp4", str(output_path))
            )
            # Wait until the producer has yielded once and is parked
            # on ``allow_finish.wait()`` inside the mocked
            # ``aiter_bytes``.
            await asyncio.wait_for(chunk_yielded.wait(), timeout=2)
            task.cancel()
            # Release the mock so the cancel-propagation path unwinds
            # cleanly. Without this the awaited Event would briefly
            # delay the cancellation visitor; asyncio still cancels,
            # but the release makes the unwind deterministic.
            allow_finish.set()

            with pytest.raises(asyncio.CancelledError):
                await task

        assert not output_path.exists()
        assert list(tmp_path.glob("file.mp4.*.tmp")) == []
