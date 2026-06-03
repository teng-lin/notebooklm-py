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

import notebooklm._artifact.downloads as _downloads
from notebooklm._artifacts import DownloadResult
from notebooklm.exceptions import ArtifactDownloadError

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
    """Public download result remains available without facade patch targets.

    Post-C2 the artifact-compat tuple and its unused exception-class
    re-exports were removed from ``_artifacts.py``. The artifact
    exception classes now resolve only from their canonical home
    (``notebooklm.exceptions``); ``_artifacts`` no longer carries
    ``ArtifactDownloadError`` as an attribute. ``DownloadResult`` and
    the ``_mind_map`` re-export remain because ``_artifacts.py`` uses
    them internally.
    """
    import notebooklm._artifacts as artifacts_module

    assert artifacts_module.DownloadResult is DownloadResult
    assert artifacts_module._mind_map is not None
    assert not hasattr(artifacts_module, "load_httpx_cookies")
    # The exception classes are no longer reachable through `_artifacts`.
    assert not hasattr(artifacts_module, "ArtifactDownloadError")


# ---------------------------------------------------------------------------
# Integration with _download_urls_batch
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_artifacts_api(tmp_path):
    """Minimal ArtifactsAPI with a mocked core for download tests."""
    from notebooklm._artifacts import ArtifactsAPI
    from notebooklm._mind_map import NoteBackedMindMapService
    from notebooklm._note_service import NoteService

    mock_core = MagicMock()
    api = ArtifactsAPI(
        rpc=mock_core,
        drain=mock_core,
        lifecycle=mock_core,
        notebooks=MagicMock(),
        mind_maps=MagicMock(spec=NoteBackedMindMapService),
        note_service=MagicMock(spec=NoteService),
        storage_path=tmp_path / "storage.json",
    )
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
        patch.object(_downloads, "load_httpx_cookies", return_value={}),
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
    """Each failed URL emits a WARNING (URL redaction applies automatically)."""
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
async def test_html_response_aggregated_into_failed(mock_artifacts_api, tmp_path):
    """``ArtifactDownloadError`` raised for an HTML payload is aggregated.

    Pre-fix the batch loop only caught ``(httpx.HTTPError, ValueError)``
    so an HTML-instead-of-media response (e.g. an auth-expired
    redirect) aborted the entire batch. Post-fix the policy
    ``ArtifactDownloadError`` lands in ``result.failed`` alongside any
    sibling successes — single bad URL no longer drops the rest of
    the batch. The single-download path (``download_url``) still
    raises this error; see
    ``tests/integration/test_artifacts_integration.py``'s download-URL
    contract tests for that surface.
    """
    api, _ = mock_artifacts_api
    html_response = _mock_response(b"<html>...</html>", "text/html")

    # Use return_value style: a single response for the lone request.
    with _patched_httpx_client(None) as mock_client:
        mock_client.get = AsyncMock(return_value=html_response)

        result = await api._download_urls_batch(
            [(f"{TRUSTED_URL_PREFIX}file.mp4", str(tmp_path / "file.mp4"))]
        )

    assert result.succeeded == []
    assert len(result.failed) == 1
    failed_url, failed_exc = result.failed[0]
    assert failed_url == f"{TRUSTED_URL_PREFIX}file.mp4"
    assert isinstance(failed_exc, ArtifactDownloadError)


@pytest.mark.asyncio
async def test_untrusted_domain_aggregated_into_failed(mock_artifacts_api, tmp_path):
    """Untrusted-domain ``ArtifactDownloadError`` lands in ``failed``, not raised."""
    api, _ = mock_artifacts_api

    with patch.object(_downloads, "load_httpx_cookies", return_value={}):
        result = await api._download_urls_batch(
            [("https://evil.example.com/file.mp4", str(tmp_path / "x.mp4"))]
        )

    assert result.succeeded == []
    assert len(result.failed) == 1
    failed_url, failed_exc = result.failed[0]
    assert failed_url == "https://evil.example.com/file.mp4"
    assert isinstance(failed_exc, ArtifactDownloadError)
    assert "Untrusted" in str(failed_exc)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.evil\\.google.com/file.mp4",
        "https://attacker.evil%5c.google.com/file.mp4",
        "https://attacker.evil%5C.google.com/file.mp4",
        "https://attacker.evil%2f.google.com/file.mp4",
        "https://attacker.evil%2F.google.com/file.mp4",
    ],
)
async def test_download_batch_rejects_backslash_hostname_confusion(
    mock_artifacts_api, tmp_path, url
):
    """Batch downloads use parsed hostname for the same allowlist guard."""
    api, _ = mock_artifacts_api

    with _patched_httpx_client([]) as mock_client:
        result = await api._download_urls_batch([(url, str(tmp_path / "file.mp4"))])

    assert result.succeeded == []
    assert len(result.failed) == 1
    failed_url, failed_exc = result.failed[0]
    assert failed_url == url
    assert isinstance(failed_exc, ArtifactDownloadError)
    assert "Untrusted download domain" in str(failed_exc)
    mock_client.get.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://storage.googleapis.com:443/file.mp4",
        "https://user:pass@storage.googleapis.com:443/file.mp4",
    ],
)
async def test_download_batch_allows_trusted_hostname_with_userinfo_or_port(
    mock_artifacts_api, tmp_path, url
):
    """Port and userinfo components must not participate in batch host matching."""
    api, _ = mock_artifacts_api
    output_path = tmp_path / "file.mp4"

    with _patched_httpx_client([_mock_response(b"payload bytes")]) as mock_client:
        result = await api._download_urls_batch([(url, str(output_path))])

    assert result.succeeded == [str(output_path)]
    assert output_path.read_bytes() == b"payload bytes"
    assert result.failed == []
    mock_client.get.assert_awaited_once_with(url)


@pytest.mark.asyncio
async def test_download_batch_error_details_redact_userinfo(mock_artifacts_api, tmp_path):
    """Batch auth errors should report a host without echoing userinfo."""
    api, _ = mock_artifacts_api
    url = "https://user:pass@storage.googleapis.com:443/file.mp4"
    response = _mock_response(b"")
    response.status_code = 403

    with _patched_httpx_client([response]):
        result = await api._download_urls_batch([(url, str(tmp_path / "file.mp4"))])

    assert result.succeeded == []
    assert len(result.failed) == 1
    _, failed_exc = result.failed[0]
    message = str(failed_exc)
    assert "Authentication failed (HTTP 403)" in message
    assert "user:pass" not in message
    assert "storage.googleapis.com/file.mp4" in message


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


@pytest.mark.asyncio
async def test_download_warning_log_redacts_userinfo(mock_artifacts_api, tmp_path, caplog):
    """Batch failure logs should use the sanitized host, not the URL netloc."""
    api, _ = mock_artifacts_api
    url = "https://user:pass@storage.googleapis.com:443/file.mp4"

    with (
        _patched_httpx_client([httpx.ConnectError("net down")]),
        caplog.at_level(logging.WARNING, logger="notebooklm"),
    ):
        result = await api._download_urls_batch([(url, str(tmp_path / "file.mp4"))])

    assert len(result.failed) == 1
    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    joined = " ".join(warning_messages)
    assert "user:pass" not in joined
    assert "storage.googleapis.com/file.mp4" in joined


@pytest.mark.asyncio
async def test_download_batch_isolates_bad_url_from_good_one(mock_artifacts_api, tmp_path):
    """A policy violation on one URL must not abort sibling downloads.

    Mixed batch: one untrusted-domain URL (raises ``ArtifactDownloadError``
    inside the per-URL try-block) and one trusted-domain URL (succeeds).
    Pre-fix the policy error escapes the batch loop and the good URL is
    never attempted. Post-fix the policy error lands in ``failed`` while
    the good URL still completes — caller can pick up the partial result
    and retry just the failure.
    """
    api, _ = mock_artifacts_api
    bad_url = "https://untrusted.example/x.mp4"
    good_url = f"{TRUSTED_URL_PREFIX}y.mp4"
    bad_path = tmp_path / "x.mp4"
    good_path = tmp_path / "y.mp4"

    # The good URL's GET should succeed. The bad URL never reaches GET
    # because the trusted-domain check raises before the HTTP call.
    success = _mock_response(b"payload bytes")
    with _patched_httpx_client([success]):
        result = await api._download_urls_batch(
            [(bad_url, str(bad_path)), (good_url, str(good_path))]
        )

    # Good URL completed.
    assert result.succeeded == [str(good_path)]
    # Bad URL ended up in failed with an ArtifactDownloadError, NOT propagated.
    assert len(result.failed) == 1
    failed_url, failed_exc = result.failed[0]
    assert failed_url == bad_url
    assert isinstance(failed_exc, ArtifactDownloadError)
    assert result.partial


def test_no_docs_callers():
    """_download_urls_batch is private — no public docs reference it."""
    repo_root = Path(__file__).resolve().parents[2]
    docs_dir = repo_root / "docs"
    for md in docs_dir.rglob("*.md"):
        text = md.read_text(encoding="utf-8")
        assert "_download_urls_batch" not in text, f"unexpected docs ref in {md}"
