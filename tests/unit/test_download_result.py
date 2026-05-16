"""Tests for DownloadResult dataclass and _download_urls_batch return shape."""

from __future__ import annotations

import contextlib
import dataclasses
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from notebooklm._artifacts import DownloadResult

# A trusted-domain prefix accepted by `_download_urls_batch`'s domain check.
TRUSTED_URL_PREFIX = "https://storage.googleapis.com/"

# ---------------------------------------------------------------------------
# Pure-data tests
# ---------------------------------------------------------------------------


def test_dataclass_shape():
    """DownloadResult is a dataclass with succeeded/failed lists."""
    assert dataclasses.is_dataclass(DownloadResult)
    r = DownloadResult()
    assert r.succeeded == []
    assert r.failed == []


def test_all_succeeded_property():
    """all_succeeded reflects empty failed list."""
    assert DownloadResult(succeeded=["a", "b"]).all_succeeded
    assert not DownloadResult(succeeded=["a"], failed=[("u", Exception("x"))]).all_succeeded
    assert DownloadResult().all_succeeded  # empty batch trivially all-succeeded


def test_partial_property():
    """partial = succeeded AND failed both non-empty."""
    assert not DownloadResult().partial
    assert not DownloadResult(succeeded=["a"]).partial
    assert not DownloadResult(failed=[("u", Exception())]).partial
    assert DownloadResult(succeeded=["a"], failed=[("u", Exception())]).partial


def test_failed_preserves_url_and_exception():
    """failed entries carry both URL and exception object."""
    exc = httpx.ConnectError("boom")
    r = DownloadResult(failed=[("https://example/x", exc)])
    url, err = r.failed[0]
    assert url == "https://example/x"
    assert err is exc
    assert isinstance(err, httpx.HTTPError)


def test_artifacts_module_preserves_download_patch_targets():
    """Private compatibility names remain available through _artifacts."""
    import notebooklm._artifacts as artifacts_module

    assert artifacts_module.DownloadResult is DownloadResult
    assert artifacts_module.ArtifactDownloadError is not None
    assert artifacts_module.load_httpx_cookies is not None
    assert artifacts_module._mind_map is not None
    assert artifacts_module.json.dump is not None


# ---------------------------------------------------------------------------
# Integration with _download_urls_batch
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_artifacts_api(tmp_path):
    """Minimal ArtifactsAPI with a mocked core for download tests."""
    from notebooklm._artifacts import ArtifactsAPI

    mock_core = MagicMock()
    mock_notes = MagicMock()
    api = ArtifactsAPI(mock_core, notes_api=mock_notes)
    api._storage_path = str(tmp_path / "storage.json")
    return api, mock_core


def _mock_response(content: bytes, content_type: str = "video/mp4") -> MagicMock:
    resp = MagicMock()
    resp.content = content
    resp.headers = {"content-type": content_type}
    resp.raise_for_status = MagicMock()
    return resp


@contextlib.contextmanager
def _patched_httpx_client(get_behavior: Any) -> Iterator[AsyncMock]:
    """Patch `httpx.AsyncClient` + `load_httpx_cookies` for download tests.

    `get_behavior` is assigned to ``mock_client.get.side_effect`` so callers can
    pass a list (sequential responses/exceptions) or a single exception.
    Yields the inner mock_client for further customization if needed.
    """
    with (
        patch("notebooklm._artifacts.load_httpx_cookies", return_value={}),
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        mock_client = AsyncMock()
        mock_client.get.side_effect = get_behavior
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client
        yield mock_client


@pytest.mark.asyncio
async def test_download_batch_all_fail(mock_artifacts_api, tmp_path):
    """All three URLs fail → succeeded=[], failed has 3 entries."""
    api, _ = mock_artifacts_api
    urls = [f"{TRUSTED_URL_PREFIX}{name}.mp4" for name in ("a", "b", "c")]

    with _patched_httpx_client(
        [
            httpx.ConnectError("net down"),
            httpx.ReadTimeout("slow"),
            httpx.HTTPError("misc"),
        ]
    ):
        result = await api._download_urls_batch(
            [(u, str(tmp_path / f"{i}.mp4")) for i, u in enumerate(urls)]
        )

    assert result.succeeded == []
    assert len(result.failed) == 3
    assert not result.all_succeeded
    assert not result.partial  # partial requires BOTH succeeded and failed
    assert {url for url, _ in result.failed} == set(urls)


@pytest.mark.asyncio
async def test_download_batch_logs_warning_per_failure(mock_artifacts_api, tmp_path, caplog):
    """Each failed URL emits a WARNING (Phase 0 redaction applies automatically)."""
    api, _ = mock_artifacts_api

    success = _mock_response(b"ok")
    with (
        _patched_httpx_client([success, httpx.ConnectError("net down")]),
        caplog.at_level(logging.WARNING, logger="notebooklm"),
    ):
        result = await api._download_urls_batch(
            [
                (f"{TRUSTED_URL_PREFIX}ok.mp4", str(tmp_path / "ok.mp4")),
                (f"{TRUSTED_URL_PREFIX}bad.mp4", str(tmp_path / "bad.mp4")),
            ]
        )

    assert result.partial
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Download failed" in r.message for r in warnings)


@pytest.mark.asyncio
async def test_html_response_still_raises(mock_artifacts_api, tmp_path):
    """ArtifactDownloadError (security signal) propagates — NOT swallowed into failed."""
    from notebooklm._artifacts import ArtifactDownloadError

    api, _ = mock_artifacts_api
    html_response = _mock_response(b"<html>...</html>", "text/html")

    # Use return_value style: a single response for the lone request.
    with _patched_httpx_client(None) as mock_client:
        mock_client.get = AsyncMock(return_value=html_response)

        with pytest.raises(ArtifactDownloadError):
            await api._download_urls_batch(
                [(f"{TRUSTED_URL_PREFIX}file.mp4", str(tmp_path / "file.mp4"))]
            )


@pytest.mark.asyncio
async def test_untrusted_domain_still_raises(mock_artifacts_api, tmp_path):
    """Untrusted-domain ArtifactDownloadError propagates."""
    from notebooklm._artifacts import ArtifactDownloadError

    api, _ = mock_artifacts_api

    with (
        patch("notebooklm._artifacts.load_httpx_cookies", return_value={}),
        pytest.raises(ArtifactDownloadError, match="Untrusted"),
    ):
        await api._download_urls_batch(
            [("https://evil.example.com/file.mp4", str(tmp_path / "x.mp4"))]
        )


@pytest.mark.asyncio
async def test_download_warning_log_does_not_leak_url_via_exception_str(
    mock_artifacts_api, tmp_path, caplog
):
    """str(httpx exception) may include the full URL with capability tokens.
    The warning log must use a safe identifier (status code or class name),
    not the raw exception."""
    api, _ = mock_artifacts_api

    # Build a 503 error whose str() includes a fake-tokenized URL.
    request = httpx.Request("GET", "https://storage.googleapis.com/file.mp4?capability_token=LEAKY")
    response = httpx.Response(503, request=request)
    boom = httpx.HTTPStatusError("Service Unavailable", request=request, response=response)

    with (
        _patched_httpx_client(None) as mock_client,
        caplog.at_level(logging.WARNING, logger="notebooklm"),
    ):
        mock_client.get = AsyncMock(side_effect=boom)
        result = await api._download_urls_batch(
            [
                (
                    f"{TRUSTED_URL_PREFIX}file.mp4?capability_token=LEAKY",
                    str(tmp_path / "file.mp4"),
                )
            ]
        )

    # Failure recorded with the raw URL+exception for the caller, BUT...
    assert len(result.failed) == 1
    # ...the log line must not contain the token.
    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    joined = " ".join(warning_messages)
    assert "LEAKY" not in joined, f"capability token leaked: {joined!r}"
    assert "HTTP 503" in joined


def test_no_docs_callers():
    """_download_urls_batch is private — no public docs reference it."""
    repo_root = Path(__file__).resolve().parents[2]
    docs_dir = repo_root / "docs"
    for md in docs_dir.rglob("*.md"):
        text = md.read_text(encoding="utf-8")
        assert "_download_urls_batch" not in text, f"unexpected docs ref in {md}"
