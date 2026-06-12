"""Coverage-focused tests for ``_artifact.downloads`` error/edge branches.
These tests target branches not exercised by ``test_artifact_downloads.py`` /
``test_download_url.py``: module-level helpers, ``UnknownRPCMethodError`` /
missing-URL handling per download type, format validation, interactive
artifact-id lookup, parse-error wrapping, the batch non-HTTPS reject path,
the streaming HTML-payload reject, the ``_await_writer_exit`` cancellation
shield-loop, and the producer ``except``-block queue-drain.
The ``ArtifactDownloadService`` is constructed directly with ``MagicMock``
collaborators (mirroring ``TestStoragePathEncapsulation`` in
``test_artifact_downloads.py``), and ``_select_artifact`` is stubbed to
return a row whose typed accessors raise/return the value under test.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import httpx
import pytest

from notebooklm._artifact import downloads as artifact_downloads
from notebooklm._artifact.downloads import (
    ArtifactDownloadService,
    _await_writer_exit,
    _download_display_host,
    _is_trusted_download_host,
)
from notebooklm.exceptions import UnknownRPCMethodError
from notebooklm.types import (
    ArtifactDownloadError,
    ArtifactNotFoundError,
    ArtifactNotReadyError,
    ArtifactParseError,
)


def _make_service(**overrides):
    """Build a service with inert MagicMock collaborators."""
    listing = MagicMock()
    # ``download_mind_map`` now consults the studio list (via ``_list_raw``) to
    # detect interactive mind maps before the note-backed path; default it to an
    # awaitable empty list so the inert service skips that guard.
    listing.list_raw = AsyncMock(return_value=[])
    kwargs = {
        "rpc": MagicMock(),
        "listing": listing,
        "mind_maps": MagicMock(),
    }
    kwargs.update(overrides)
    return ArtifactDownloadService(**kwargs)


def _row_with(prop_name, *, value=None, exc=None):
    """Return a MagicMock row whose ``prop_name`` returns value or raises."""
    row = MagicMock()
    if exc is not None:
        type(row)  # noqa: B018 -- ensure attribute access path
        setattr(type(row), prop_name, PropertyMock(side_effect=exc))
    else:
        setattr(row, prop_name, value)
    return row


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------
def test_is_trusted_download_host_none_returns_false():
    """``None`` hostname is never trusted."""
    assert _is_trusted_download_host(None) is False


@pytest.mark.parametrize(
    "host",
    [
        "evil%2egoogleapis.com",  # %2e decodes to '.' -> evil.googleapis.com
        "evil%2Egoogleapis.com",  # uppercase variant
        "storage.googleapis.com%2eevil.example",  # encoded dot mid-host
        "storage.googleapis.com%00",  # any percent-escape is suspect
    ],
)
def test_is_trusted_download_host_rejects_percent_encoded(host):
    """Percent-encoded hosts are never trusted (parser-differential bypass, #1521).
    httpx connects to the RAW host; the guard must validate that same raw
    string and never percent-decode it, or ``evil%2egoogleapis.com`` would be
    judged trusted while the connection goes to a non-Google host.
    """
    assert _is_trusted_download_host(host) is False


@pytest.mark.parametrize(
    "host",
    [
        "storage.googleapis.com",
        "lh3.googleusercontent.com",
        "www.google.com",
        "google.com",
        "googleapis.com",
    ],
)
def test_is_trusted_download_host_accepts_legitimate_google_hosts(host):
    """Removing the percent-decode must not reject any real Google download host."""
    assert _is_trusted_download_host(host) is True


def test_download_display_host_falls_back_to_netloc():
    """with no parsed hostname, strip userinfo from netloc."""
    from urllib.parse import urlparse

    # A URL whose netloc is only userinfo yields hostname=None, so the
    # netloc fallback (stripping the ``user@`` prefix) is exercised.
    p2 = urlparse("scheme://user@")
    assert p2.hostname is None
    assert _download_display_host(p2) == ""
    # And the normal case returns the hostname directly.
    assert _download_display_host(urlparse("https://host.example/x")) == "host.example"


# ---------------------------------------------------------------------------
# _list_mind_maps delegation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_mind_maps_delegates_to_service():
    mind_maps = MagicMock()
    mind_maps.list_mind_maps = AsyncMock(return_value=["mm"])
    service = _make_service(mind_maps=mind_maps)
    result = await service._list_mind_maps("nb_1")
    assert result == ["mm"]
    mind_maps.list_mind_maps.assert_awaited_once_with("nb_1")


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_download_audio_unknown_rpc_method_wrapped():
    service = _make_service()
    service._list_raw = AsyncMock(return_value=[])
    row = _row_with("audio_url", exc=UnknownRPCMethodError("boom"))
    service._select_artifact = MagicMock(return_value=row)
    with pytest.raises(ArtifactParseError, match="Failed to parse structure"):
        await service.download_audio("nb", "/tmp/a.mp4")


@pytest.mark.asyncio
async def test_download_audio_missing_url_raises():
    service = _make_service()
    service._list_raw = AsyncMock(return_value=[])
    service._select_artifact = MagicMock(return_value=_row_with("audio_url", value=None))
    with pytest.raises(ArtifactParseError, match="Could not extract download URL"):
        await service.download_audio("nb", "/tmp/a.mp4")


# ---------------------------------------------------------------------------
# Video
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_download_video_unknown_rpc_method_wrapped():
    service = _make_service()
    service._list_raw = AsyncMock(return_value=[])
    service._select_artifact = MagicMock(
        return_value=_row_with("video_url", exc=UnknownRPCMethodError("boom"))
    )
    with pytest.raises(ArtifactParseError, match="Failed to parse structure"):
        await service.download_video("nb", "/tmp/v.mp4")


@pytest.mark.asyncio
async def test_download_video_missing_url_raises():
    service = _make_service()
    service._list_raw = AsyncMock(return_value=[])
    service._select_artifact = MagicMock(return_value=_row_with("video_url", value=""))
    with pytest.raises(ArtifactParseError, match="Could not extract download URL"):
        await service.download_video("nb", "/tmp/v.mp4")


# ---------------------------------------------------------------------------
# Infographic
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_download_infographic_missing_url_raises():
    """empty infographic URL -> ArtifactParseError."""
    service = _make_service()
    service._list_raw = AsyncMock(return_value=[])
    service._select_artifact = MagicMock(return_value=_row_with("infographic_url", value=None))
    with pytest.raises(ArtifactParseError, match="Could not find metadata"):
        await service.download_infographic("nb", "/tmp/i.png")


@pytest.mark.asyncio
async def test_download_infographic_index_error_wrapped():
    """structural IndexError/TypeError wrapped as parse error."""
    service = _make_service()
    service._list_raw = AsyncMock(return_value=[])
    service._select_artifact = MagicMock(
        return_value=_row_with("infographic_url", exc=TypeError("bad shape"))
    )
    with pytest.raises(ArtifactParseError, match="Failed to parse structure"):
        await service.download_infographic("nb", "/tmp/i.png")


# ---------------------------------------------------------------------------
# Slide deck
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_download_slide_deck_invalid_format_rejected():
    """only pdf/pptx are accepted."""
    service = _make_service()
    with pytest.raises(Exception, match="Invalid format"):
        await service.download_slide_deck("nb", "/tmp/s.bin", output_format="docx")


@pytest.mark.asyncio
async def test_download_slide_deck_pptx_url_missing():
    """missing PPTX URL -> ArtifactDownloadError."""
    service = _make_service()
    service._list_raw = AsyncMock(return_value=[])
    service._select_artifact = MagicMock(return_value=_row_with("slide_deck_pptx_url", value=None))
    with pytest.raises(ArtifactDownloadError, match="PPTX URL not available"):
        await service.download_slide_deck("nb", "/tmp/s.pptx", output_format="pptx")


@pytest.mark.asyncio
async def test_download_slide_deck_pdf_url_missing():
    """missing PDF URL -> ArtifactDownloadError."""
    service = _make_service()
    service._list_raw = AsyncMock(return_value=[])
    service._select_artifact = MagicMock(return_value=_row_with("slide_deck_pdf_url", value=None))
    with pytest.raises(ArtifactDownloadError, match="Could not find PDF download URL"):
        await service.download_slide_deck("nb", "/tmp/s.pdf", output_format="pdf")


@pytest.mark.asyncio
async def test_download_slide_deck_unknown_rpc_method_wrapped():
    service = _make_service()
    service._list_raw = AsyncMock(return_value=[])
    service._select_artifact = MagicMock(
        return_value=_row_with("slide_deck_pdf_url", exc=UnknownRPCMethodError("boom"))
    )
    with pytest.raises(ArtifactParseError, match="Failed to parse structure"):
        await service.download_slide_deck("nb", "/tmp/s.pdf")


# ---------------------------------------------------------------------------
# Interactive artifact
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_interactive_invalid_format_rejected():
    service = _make_service()
    with pytest.raises(Exception, match="Invalid output_format"):
        await service.download_interactive_artifact("nb", "/tmp/q.json", None, "xml", "quiz")


@pytest.mark.asyncio
async def test_interactive_specific_id_not_found():
    """requested artifact_id absent among completed."""
    completed = MagicMock(id="other", is_completed=True, created_at=None, title="Q")
    service = _make_service()
    service._list_artifacts = AsyncMock(return_value=[completed])
    with pytest.raises(ArtifactNotFoundError):
        await service.download_interactive_artifact(
            "nb", "/tmp/q.json", "wanted_id", "json", "quiz"
        )


@pytest.mark.asyncio
async def test_interactive_json_decode_error_wrapped(monkeypatch):
    """JSONDecodeError from app-data extraction -> ArtifactParseError."""
    artifact = MagicMock(id="q1", is_completed=True, created_at=None, title="Q")
    service = _make_service()
    service._list_artifacts = AsyncMock(return_value=[artifact])
    service._get_artifact_content = AsyncMock(return_value="<html></html>")
    monkeypatch.setattr(
        artifact_downloads,
        "_extract_app_data",
        MagicMock(side_effect=json.JSONDecodeError("bad", "doc", 0)),
    )
    with pytest.raises(ArtifactParseError, match="Failed to parse content"):
        await service.download_interactive_artifact("nb", "/tmp/q.json", None, "json", "quiz")


@pytest.mark.asyncio
async def test_interactive_no_completed_raises_not_ready():
    service = _make_service()
    service._list_artifacts = AsyncMock(return_value=[MagicMock(is_completed=False)])
    with pytest.raises(ArtifactNotReadyError):
        await service.download_interactive_artifact("nb", "/tmp/q.json", None, "json", "quiz")


@pytest.mark.asyncio
async def test_interactive_empty_content_raises_download_error():
    artifact = MagicMock(id="q1", is_completed=True, created_at=None, title="Q")
    service = _make_service()
    service._list_artifacts = AsyncMock(return_value=[artifact])
    service._get_artifact_content = AsyncMock(return_value=None)
    with pytest.raises(ArtifactDownloadError, match="Failed to fetch content"):
        await service.download_interactive_artifact("nb", "/tmp/q.json", None, "json", "quiz")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_download_report_non_string_markdown_raises():
    """non-str report markdown -> ArtifactParseError."""
    service = _make_service()
    service._list_raw = AsyncMock(return_value=[])
    service._select_artifact = MagicMock(return_value=_row_with("report_markdown", value=[1, 2, 3]))
    with pytest.raises(ArtifactParseError, match="Invalid structure"):
        await service.download_report("nb", "/tmp/r.md")


@pytest.mark.asyncio
async def test_download_report_unknown_rpc_method_wrapped():
    """UnknownRPCMethodError -> ArtifactParseError."""
    service = _make_service()
    service._list_raw = AsyncMock(return_value=[])
    service._select_artifact = MagicMock(
        return_value=_row_with("report_markdown", exc=UnknownRPCMethodError("boom"))
    )
    with pytest.raises(ArtifactParseError, match="Failed to parse structure"):
        await service.download_report("nb", "/tmp/r.md")


# ---------------------------------------------------------------------------
# Mind map
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_download_mind_map_none_content_raises():
    """extract_content returning None -> ArtifactParseError."""
    mind_maps = MagicMock()
    mind_maps.list_mind_maps = AsyncMock(return_value=[["mm_1", None, None, None, "Title"]])
    mind_maps.extract_content = MagicMock(return_value=None)
    service = _make_service(mind_maps=mind_maps)
    with pytest.raises(ArtifactParseError, match="Invalid structure"):
        await service.download_mind_map("nb", "/tmp/m.json")


@pytest.mark.asyncio
async def test_download_mind_map_bad_json_wrapped():
    """invalid JSON string -> ArtifactParseError."""
    mind_maps = MagicMock()
    mind_maps.list_mind_maps = AsyncMock(return_value=[["mm_1", None, None, None, "Title"]])
    mind_maps.extract_content = MagicMock(return_value="{not valid json")
    service = _make_service(mind_maps=mind_maps)
    with pytest.raises(ArtifactParseError, match="Failed to parse structure"):
        await service.download_mind_map("nb", "/tmp/m.json")


# ---------------------------------------------------------------------------
# Data table
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_download_data_table_parse_error_wrapped(monkeypatch):
    """ValueError from parsing -> ArtifactParseError."""
    service = _make_service()
    service._list_raw = AsyncMock(return_value=[])
    service._select_artifact = MagicMock(
        return_value=_row_with("data_table_raw_payload", value=["raw"])
    )
    monkeypatch.setattr(
        artifact_downloads,
        "_parse_data_table",
        MagicMock(side_effect=ValueError("bad table")),
    )
    with pytest.raises(ArtifactParseError, match="Failed to parse structure"):
        await service.download_data_table("nb", "/tmp/t.csv")


# ---------------------------------------------------------------------------
# Quiz/flashcards delegation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_download_quiz_delegates():
    service = _make_service()
    service.download_interactive_artifact = AsyncMock(return_value="/tmp/q.json")
    result = await service.download_quiz("nb", "/tmp/q.json")
    assert result == "/tmp/q.json"
    service.download_interactive_artifact.assert_awaited_once_with(
        "nb", "/tmp/q.json", None, "json", "quiz", artifacts=None
    )


@pytest.mark.asyncio
async def test_download_flashcards_delegates():
    service = _make_service()
    service.download_interactive_artifact = AsyncMock(return_value="/tmp/f.json")
    result = await service.download_flashcards("nb", "/tmp/f.json", output_format="markdown")
    assert result == "/tmp/f.json"
    service.download_interactive_artifact.assert_awaited_once_with(
        "nb", "/tmp/f.json", None, "markdown", "flashcards", artifacts=None
    )


# ---------------------------------------------------------------------------
# Batch non-HTTPS reject
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_download_urls_batch_non_https_recorded_as_failure(tmp_path):
    """http:// URL is rejected and aggregated into ``failed``."""
    service = _make_service(storage_path=tmp_path / "storage.json", cookie_loader=lambda _path: {})
    result = await service.download_urls_batch(
        [("http://storage.googleapis.com/x.bin", str(tmp_path / "out.bin"))]
    )
    assert result.succeeded == []
    assert len(result.failed) == 1
    url, exc = result.failed[0]
    assert url == "http://storage.googleapis.com/x.bin"
    assert isinstance(exc, ArtifactDownloadError)
    assert "must use HTTPS" in str(exc)


# ---------------------------------------------------------------------------
# Streaming HTML payload reject
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_download_url_html_payload_rejected(tmp_path):
    """an HTML content-type aborts with an auth-hint error."""
    service = _make_service(storage_path=tmp_path / "storage.json")

    async def mock_aiter_bytes(chunk_size: int = 8192):
        if False:  # pragma: no cover
            yield b""

    mock_response = MagicMock()
    mock_response.headers = {"content-type": "text/html; charset=utf-8"}
    mock_response.raise_for_status = MagicMock()
    mock_response.aiter_bytes = mock_aiter_bytes
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)
    mock_client = AsyncMock()
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    output_path = tmp_path / "out.bin"
    with (
        patch.object(httpx, "AsyncClient", return_value=mock_client),
        patch.object(artifact_downloads, "load_httpx_cookies", return_value=MagicMock()),
        pytest.raises(ArtifactDownloadError, match="received HTML instead of media"),
    ):
        await service.download_url("https://storage.googleapis.com/x.bin", str(output_path))
    assert not output_path.exists()
    assert list(tmp_path.glob("out.bin.*.tmp")) == []


# ---------------------------------------------------------------------------
# _await_writer_exit cancellation shield-loop
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_await_writer_exit_reraises_cancel_after_join():
    """A cancellation during writer join is re-raised after the writer exits."""
    release = threading.Event()

    def _block():
        release.wait(timeout=5)

    writer = threading.Thread(target=_block, daemon=True)
    writer.start()
    task = asyncio.ensure_future(_await_writer_exit(writer, re_raise_cancel=True))
    # Let the helper start awaiting the shielded join, then cancel it.
    await asyncio.sleep(0.05)
    task.cancel()
    # The shield keeps the inner join alive; release the thread so the
    # join completes and the loop exits, then the preserved cancel is
    # re-raised.
    await asyncio.sleep(0.05)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not writer.is_alive()


@pytest.mark.asyncio
async def test_await_writer_exit_cleanup_path_swallows_cancel():
    """re_raise_cancel=False (cleanup path) returns normally even after a
    cancellation was observed mid-join."""
    release = threading.Event()

    def _block():
        release.wait(timeout=5)

    writer = threading.Thread(target=_block, daemon=True)
    writer.start()
    task = asyncio.ensure_future(_await_writer_exit(writer, re_raise_cancel=False))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.sleep(0.05)
    release.set()
    # No CancelledError surfaces because re_raise_cancel is False.
    await task
    assert not writer.is_alive()


# ---------------------------------------------------------------------------
# Producer except-block queue drain
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_download_url_error_path_drains_full_queue(tmp_path):
    """when the producer fails mid-stream while the bounded
    queue is already full (the writer is parked and not consuming), the
    ``except`` block must drop one buffered item (``get_nowait``) to make
    room for the ``None`` sentinel, otherwise ``put_nowait(None)`` keeps
    raising ``queue.Full`` and the writer stays blocked forever.
    To force the queue-full path deterministically the writer thread is
    made to block on its very first ``write`` (it never drains the queue),
    so the producer saturates the bounded queue. ``aiter_bytes`` then
    raises a network error, driving the producer into the failure handler
    with a full queue. Releasing the blocked write lets the sentinel land
    and the writer exit so cleanup can unlink the temp file.
    """
    service = _make_service(storage_path=tmp_path / "storage.json")
    real_open = open
    release_writer = threading.Event()

    class _BlockingHandle:
        """File-like wrapper whose first ``write`` blocks until released.
        The writer thread does one ``chunk_q.get()`` and then parks on its
        first ``write``, so it never drains the rest of the queue — letting
        the test hold the bounded queue at full capacity while the producer
        enters its failure handler.
        """

        def __init__(self, fh):
            self._fh = fh
            self._blocked_once = False

        def write(self, data):
            if not self._blocked_once:
                self._blocked_once = True
                release_writer.wait(timeout=5)
            return self._fh.write(data)

        def __enter__(self):
            self._fh.__enter__()
            return self

        def __exit__(self, *exc):
            return self._fh.__exit__(*exc)

    def blocking_open(path, *args, **kwargs):
        if str(path).endswith(".tmp"):
            return _BlockingHandle(real_open(path, *args, **kwargs))
        return real_open(path, *args, **kwargs)

    # Capture the bounded queue the producer/writer share so the test can
    # deterministically fill it to capacity before forcing the failure.
    created_queues: list[queue.Queue] = []
    real_queue_cls = queue.Queue

    def _capturing_queue(*args, **kwargs):
        q = real_queue_cls(*args, **kwargs)
        created_queues.append(q)
        return q

    # A non-httpx error so it propagates straight through the outer
    # ``except httpx.*`` handlers (which would otherwise re-wrap it) and
    # out to the temp-file cleanup, keeping the assertion on the original
    # exception type.
    boom = RuntimeError("connection dropped mid-stream")

    async def mock_aiter_bytes(chunk_size: int = 8192):
        # First, yield a single chunk so the writer's ``open`` runs and the
        # writer thread parks on its first ``write`` holding one item.
        yield b"x" * 1024
        # Wait until the shared queue object exists and the writer has
        # parked (so it will not consume further), then top the queue off
        # to its max so the producer's failure-handler ``put_nowait(None)``
        # is guaranteed to hit ``queue.Full`` and drive the ``get_nowait``
        # drain .
        await asyncio.to_thread(_fill_queue_to_full)
        raise boom

    def _fill_queue_to_full() -> None:
        # Busy-wait briefly for the queue to be created and the writer to
        # park, then saturate it.
        import time

        deadline = time.monotonic() + 5
        while not created_queues and time.monotonic() < deadline:
            time.sleep(0.005)
        q = created_queues[0]
        # Give the writer a beat to perform its single ``get`` and park.
        time.sleep(0.05)
        while True:
            try:
                q.put_nowait(b"pad")
            except queue.Full:
                break

    mock_response = MagicMock()
    mock_response.headers = {"content-type": "video/mp4"}
    mock_response.raise_for_status = MagicMock()
    mock_response.aiter_bytes = mock_aiter_bytes
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)
    mock_client = AsyncMock()
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    # Release the parked writer shortly after the failure handler starts so
    # the writer can exit once the sentinel lands; the producer's
    # synchronous drain loop (no awaits) has already executed by then.
    async def _release_soon():
        await asyncio.sleep(0.2)
        release_writer.set()

    output_path = tmp_path / "out.bin"
    releaser = asyncio.ensure_future(_release_soon())
    try:
        with (
            patch.object(httpx, "AsyncClient", return_value=mock_client),
            patch.object(artifact_downloads, "load_httpx_cookies", return_value=MagicMock()),
            patch.object(artifact_downloads, "open", blocking_open, create=True),
            patch.object(artifact_downloads.queue, "Queue", _capturing_queue),
            pytest.raises(RuntimeError, match="connection dropped"),
        ):
            await service.download_url("https://storage.googleapis.com/x.bin", str(output_path))
    finally:
        release_writer.set()
        await releaser
    # Temp file cleaned up; writer exited.
    assert not output_path.exists()
    assert list(tmp_path.glob("out.bin.*.tmp")) == []
    # Sanity: the bounded queue size constant is what we relied on.
    assert artifact_downloads._DOWNLOAD_WRITER_QUEUE_SIZE < 50


# ---------------------------------------------------------------------------
# Writer fails immediately: short-circuit + surfaced error
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_download_url_writer_failure_surfaced(tmp_path):
    """Writer thread fails on ``open`` -> captures the error and sets
    ``writer_failed``. The producer short-circuits and
    re-raises the writer's captured exception , exercising the
    writer ``except`` block  too.
    """
    service = _make_service(storage_path=tmp_path / "storage.json")
    boom = OSError("disk full")
    real_open = open

    def failing_open(path, *args, **kwargs):
        if str(path).endswith(".tmp"):
            raise boom
        return real_open(path, *args, **kwargs)

    async def mock_aiter_bytes(chunk_size: int = 8192):
        for _ in range(50):
            yield b"x" * 1024
            await asyncio.sleep(0)

    mock_response = MagicMock()
    mock_response.headers = {"content-type": "video/mp4"}
    mock_response.raise_for_status = MagicMock()
    mock_response.aiter_bytes = mock_aiter_bytes
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)
    mock_client = AsyncMock()
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    output_path = tmp_path / "out.bin"
    with (
        patch.object(httpx, "AsyncClient", return_value=mock_client),
        patch.object(artifact_downloads, "load_httpx_cookies", return_value=MagicMock()),
        patch.object(artifact_downloads, "open", failing_open, create=True),
        pytest.raises(OSError, match="disk full"),
    ):
        await service.download_url("https://storage.googleapis.com/x.bin", str(output_path))
    assert not output_path.exists()
    assert list(tmp_path.glob("out.bin.*.tmp")) == []


# ---------------------------------------------------------------------------
# Back-pressure: producer falls back to to_thread(put) when the queue is
# full
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_download_url_backpressure_to_thread_put(tmp_path):
    """A slow writer forces the queue full so the producer's chunk ``put``
    and the final ``None`` sentinel ``put`` both fall back to
    ``asyncio.to_thread(chunk_q.put, ...)``.
    The writer sleeps briefly per write so the bounded queue saturates
    while the producer streams many small chunks, but the download still
    completes successfully.
    """
    service = _make_service(storage_path=tmp_path / "storage.json")
    real_open = open

    class _SlowHandle:
        def __init__(self, fh):
            self._fh = fh

        def write(self, data):
            # Slow the writer so the bounded queue fills and the producer
            # must block on ``to_thread(put)``.
            import time

            time.sleep(0.01)
            return self._fh.write(data)

        def __enter__(self):
            self._fh.__enter__()
            return self

        def __exit__(self, *exc):
            return self._fh.__exit__(*exc)

    def slow_open(path, *args, **kwargs):
        if str(path).endswith(".tmp"):
            return _SlowHandle(real_open(path, *args, **kwargs))
        return real_open(path, *args, **kwargs)

    content_chunks = [b"y" * 1024 for _ in range(40)]

    async def mock_aiter_bytes(chunk_size: int = 8192):
        for chunk in content_chunks:
            yield chunk

    mock_response = MagicMock()
    mock_response.headers = {"content-type": "video/mp4"}
    mock_response.raise_for_status = MagicMock()
    mock_response.aiter_bytes = mock_aiter_bytes
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)
    mock_client = AsyncMock()
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    output_path = tmp_path / "out.bin"
    with (
        patch.object(httpx, "AsyncClient", return_value=mock_client),
        patch.object(artifact_downloads, "load_httpx_cookies", return_value=MagicMock()),
        patch.object(artifact_downloads, "open", slow_open, create=True),
    ):
        result = await service.download_url(
            "https://storage.googleapis.com/x.bin", str(output_path)
        )
    assert result == str(output_path)
    assert output_path.read_bytes() == b"".join(content_chunks)
    assert list(tmp_path.glob("out.bin.*.tmp")) == []


# ---------------------------------------------------------------------------
# download_url scheme/host policy rejects
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_download_url_non_https_rejected(tmp_path):
    """a single-URL ``download_url`` rejects non-HTTPS schemes
    (the batch surface absorbs, but ``download_url`` raises)."""
    service = _make_service(storage_path=tmp_path / "storage.json")
    with pytest.raises(ArtifactDownloadError, match="must use HTTPS"):
        await service.download_url("http://storage.googleapis.com/x.bin", str(tmp_path / "out.bin"))


# ---------------------------------------------------------------------------
# _list_raw delegation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_raw_delegates_to_listing():
    listing = MagicMock()
    listing.list_raw = AsyncMock(return_value=["raw"])
    service = _make_service(listing=listing)
    result = await service._list_raw("nb_1")
    assert result == ["raw"]
    listing.list_raw.assert_awaited_once()


# ---------------------------------------------------------------------------
# _get_artifact_content safe_index path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_artifact_content_indexes_result():
    """a non-null RPC result is indexed via ``safe_index``."""
    # safe_index(result, 0, 9, 0) -> result[0][9][0] is the HTML string.
    inner = [None] * 10
    inner[9] = ["<html>quiz</html>"]
    # Configure rpc_call at construction (ADR-0007 forbids dynamic AsyncMock
    # attribute assignment onto a duck-typed collaborator).
    rpc = MagicMock(rpc_call=AsyncMock(return_value=[inner]))
    service = _make_service(rpc=rpc)
    result = await service._get_artifact_content("nb_1", "quiz_1")
    assert result == "<html>quiz</html>"


# ---------------------------------------------------------------------------
# Interactive artifact success write path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_interactive_artifact_writes_output(tmp_path, monkeypatch):
    """the happy path formats content and writes the file."""
    artifact = MagicMock(id="q1", is_completed=True, created_at=None, title="My Quiz")
    service = _make_service()
    service._list_artifacts = AsyncMock(return_value=[artifact])
    service._get_artifact_content = AsyncMock(return_value="<html>quiz</html>")
    monkeypatch.setattr(
        artifact_downloads, "_extract_app_data", MagicMock(return_value={"quiz": []})
    )
    monkeypatch.setattr(
        artifact_downloads,
        "_format_interactive_content",
        MagicMock(return_value="FORMATTED"),
    )
    out = tmp_path / "nested" / "quiz.json"
    result = await service.download_interactive_artifact("nb", str(out), None, "json", "quiz")
    assert result == str(out)
    assert out.read_text(encoding="utf-8") == "FORMATTED"


@pytest.mark.asyncio
async def test_interactive_artifact_default_title_when_untitled(tmp_path, monkeypatch):
    """A flashcards artifact with no title uses the flashcards default title."""
    artifact = MagicMock(id="f1", is_completed=True, created_at=None, title=None)
    service = _make_service()
    service._list_artifacts = AsyncMock(return_value=[artifact])
    service._get_artifact_content = AsyncMock(return_value="<html>cards</html>")
    monkeypatch.setattr(artifact_downloads, "_extract_app_data", MagicMock(return_value={}))
    captured_titles: list[str] = []

    def _fmt(app_data, title, fmt, html, is_quiz):
        captured_titles.append(title)
        return "OUT"

    monkeypatch.setattr(artifact_downloads, "_format_interactive_content", _fmt)
    out = tmp_path / "cards.md"
    await service.download_interactive_artifact("nb", str(out), None, "markdown", "flashcards")
    assert captured_titles == ["Untitled Flashcards"]


# ---------------------------------------------------------------------------
# _list_artifacts delegation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_artifacts_delegates_to_listing():
    from notebooklm.types import ArtifactType

    listing = MagicMock()
    listing.list_artifacts = AsyncMock(return_value=["typed"])
    service = _make_service(listing=listing)
    result = await service._list_artifacts("nb_1", ArtifactType.QUIZ)
    assert result == ["typed"]
    listing.list_artifacts.assert_awaited_once()
    _, kwargs = listing.list_artifacts.call_args
    assert kwargs["list_raw"] == service._list_raw
    assert kwargs["list_mind_maps"] == service._list_mind_maps


# ---------------------------------------------------------------------------
# Success-path branch arcs that fall through to download_url / file write
# (349->369, 399->404, 486->491)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_slide_deck_pptx_success_falls_through(tmp_path):
    """Branch 349->369: a present PPTX URL skips the error and downloads."""
    service = _make_service()
    service._list_raw = AsyncMock(return_value=[])
    service._select_artifact = MagicMock(
        return_value=_row_with("slide_deck_pptx_url", value="https://x.google.com/s.pptx")
    )
    service.download_url = AsyncMock(return_value=str(tmp_path / "s.pptx"))
    result = await service.download_slide_deck("nb", str(tmp_path / "s.pptx"), output_format="pptx")
    assert result == str(tmp_path / "s.pptx")
    service.download_url.assert_awaited_once_with(
        "https://x.google.com/s.pptx", str(tmp_path / "s.pptx")
    )


@pytest.mark.asyncio
async def test_interactive_specific_id_found_branch(tmp_path, monkeypatch):
    """Branch 399->404: a matching ``artifact_id`` is selected and used."""
    wanted = MagicMock(id="wanted", is_completed=True, created_at=None, title="Q")
    other = MagicMock(id="other", is_completed=True, created_at=None, title="Q2")
    service = _make_service()
    service._list_artifacts = AsyncMock(return_value=[other, wanted])
    captured: list[str] = []

    async def _get_content(nb, art_id):
        captured.append(art_id)
        return "<html>quiz</html>"

    service._get_artifact_content = _get_content
    monkeypatch.setattr(artifact_downloads, "_extract_app_data", MagicMock(return_value={}))
    monkeypatch.setattr(
        artifact_downloads, "_format_interactive_content", MagicMock(return_value="OUT")
    )
    out = tmp_path / "q.json"
    await service.download_interactive_artifact("nb", str(out), "wanted", "json", "quiz")
    assert captured == ["wanted"]
    assert out.read_text(encoding="utf-8") == "OUT"


@pytest.mark.asyncio
async def test_mind_map_specific_id_found_branch(tmp_path):
    """Branch 486->491: a matching mind-map ``artifact_id`` is selected."""
    mind_maps = MagicMock()
    mind_maps.list_mind_maps = AsyncMock(
        return_value=[
            ["other", None, None, None, "Other"],
            ["wanted", None, None, None, "Wanted"],
        ]
    )
    mind_maps.extract_content = MagicMock(return_value='{"name": "Root"}')
    service = _make_service(mind_maps=mind_maps)
    out = tmp_path / "mm.json"
    result = await service.download_mind_map("nb", str(out), artifact_id="wanted")
    assert result == str(out)
    # The matching row (id == "wanted") is the one passed to extract_content.
    (selected_row,), _ = mind_maps.extract_content.call_args
    assert selected_row[0] == "wanted"
    assert json.loads(out.read_text(encoding="utf-8")) == {"name": "Root"}


# ---------------------------------------------------------------------------
# Producer except-block: queue.Empty race branch
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_download_url_error_path_drain_observes_empty_queue(tmp_path):
    """when the producer fails and tries to enqueue the
    ``None`` sentinel, the first ``put_nowait`` can hit ``queue.Full`` and
    the follow-up ``get_nowait`` can find the queue already drained by the
    writer (``queue.Empty``). The loop then retries the put successfully.
    This race is reproduced deterministically with a ``queue.Queue``
    subclass that, for the sentinel only, raises ``Full`` on the first put
    and ``Empty`` on the immediately following get, then accepts the
    sentinel on the retry — exactly the interleaving the production comment
    describes.
    """
    service = _make_service(storage_path=tmp_path / "storage.json")

    class _RacyQueue(queue.Queue):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._sentinel_full_pending = True
            self._empty_pending = True

        def put_nowait(self, item):
            # Force the sentinel's first put to look full so the drain
            # branch (846-852) runs, then accept it on the retry.
            if item is None and self._sentinel_full_pending:
                self._sentinel_full_pending = False
                raise queue.Full
            return super().put_nowait(item)

        def get_nowait(self):
            if self._empty_pending:
                self._empty_pending = False
                raise queue.Empty
            return super().get_nowait()

    boom = RuntimeError("connection dropped mid-stream")

    async def mock_aiter_bytes(chunk_size: int = 8192):
        yield b"x" * 1024
        await asyncio.sleep(0)
        raise boom

    mock_response = MagicMock()
    mock_response.headers = {"content-type": "video/mp4"}
    mock_response.raise_for_status = MagicMock()
    mock_response.aiter_bytes = mock_aiter_bytes
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)
    mock_client = AsyncMock()
    mock_client.stream = MagicMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    output_path = tmp_path / "out.bin"
    with (
        patch.object(httpx, "AsyncClient", return_value=mock_client),
        patch.object(artifact_downloads, "load_httpx_cookies", return_value=MagicMock()),
        patch.object(artifact_downloads.queue, "Queue", _RacyQueue),
        pytest.raises(RuntimeError, match="connection dropped"),
    ):
        await service.download_url("https://storage.googleapis.com/x.bin", str(output_path))
    assert not output_path.exists()
    assert list(tmp_path.glob("out.bin.*.tmp")) == []
