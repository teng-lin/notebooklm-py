"""Regression test for T7.H3 — configurable upload timeouts.

Audit item #20 (`thread-safety-concurrency-audit.md` §20):
Pre-fix, the resumable-upload `_start_resumable_upload` helper and the
finalize POST in `_upload_file_streaming` instantiated
`httpx.AsyncClient(timeout=httpx.Timeout(...))` with hardcoded values
(10.0s connect / 60.0s read for start, 10.0s connect / 300.0s read for
finalize). Callers uploading very large files on slow networks (or
testing with deliberately short timeouts) had no way to override.

Post-fix (T7.H3): `NotebookLMClient.__init__` / `from_storage` accept
`upload_timeout: httpx.Timeout | None = None`, threaded to
`SourcesAPI`, and used at both hardcoded sites. ``None`` (default)
preserves the original hardcoded values for back-compat — defaults
are NOT changed silently.

The test asserts the timeout passed to ``httpx.AsyncClient`` at the
upload sites matches the configured value, and that the default
unchanged when no override is supplied.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from notebooklm import NotebookLMClient

# Mock-only tests (no real HTTP, no cassette) — opt out of the T8.D11
# tier-enforcement hook in ``tests/integration/conftest.py``. Marker
# was missed when this file landed (PR #618 T7.H3 merged the same day
# as PR #622 T8.D11 tier-enforcement).
pytestmark = pytest.mark.allow_no_vcr


@pytest.fixture
def tmp_upload_file(tmp_path: Path) -> Path:
    """Tiny payload for streaming uploads — content doesn't matter."""
    path = tmp_path / "upload.txt"
    path.write_bytes(b"x" * 256)
    return path


def _make_capturing_async_client(
    captured: list[httpx.Timeout | None],
) -> type[httpx.AsyncClient]:
    """Build an ``httpx.AsyncClient`` subclass that records the ``timeout`` kwarg.

    Returns a class so ``async with httpx.AsyncClient(...)`` continues to
    work — replacing only the constructor argument capture, not the
    instance behavior.
    """
    real_async_client = httpx.AsyncClient

    class CapturingClient(real_async_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured.append(kwargs.get("timeout"))  # type: ignore[arg-type]
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    return CapturingClient


async def test_custom_upload_timeout_propagates_to_start(
    auth_tokens, tmp_upload_file: Path
) -> None:
    """``upload_timeout=Timeout(5.0, read=10.0)`` reaches ``_start_resumable_upload``."""
    custom = httpx.Timeout(5.0, read=10.0)
    captured: list[httpx.Timeout | None] = []
    capturing = _make_capturing_async_client(captured)

    async with NotebookLMClient(auth_tokens, upload_timeout=custom) as client:
        with patch("notebooklm._sources.httpx.AsyncClient", capturing):
            # Call the helper directly — exercises the start-resumable-upload
            # site in isolation. The actual network call will fail (no real
            # server), but we only care that the timeout kwarg was captured
            # at construction time, before any I/O.
            with pytest.raises((httpx.HTTPError, OSError)):
                await client.sources._start_resumable_upload(
                    notebook_id="nb-test",
                    filename=tmp_upload_file.name,
                    file_size=tmp_upload_file.stat().st_size,
                    source_id="src-test",
                )

    assert captured, "Expected at least one httpx.AsyncClient construction"
    timeout = captured[0]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 5.0
    assert timeout.read == 10.0


async def test_default_upload_timeout_preserves_back_compat_start(auth_tokens) -> None:
    """No override -> ``_start_resumable_upload`` still uses the original 10.0/60.0 hardcode."""
    captured: list[httpx.Timeout | None] = []
    capturing = _make_capturing_async_client(captured)

    async with NotebookLMClient(auth_tokens) as client:  # no upload_timeout
        with patch("notebooklm._sources.httpx.AsyncClient", capturing):
            with pytest.raises((httpx.HTTPError, OSError)):
                await client.sources._start_resumable_upload(
                    notebook_id="nb-test",
                    filename="dummy.txt",
                    file_size=256,
                    source_id="src-test",
                )

    assert captured
    timeout = captured[0]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 10.0
    assert timeout.read == 60.0


async def test_custom_upload_timeout_propagates_to_finalize(
    auth_tokens, tmp_upload_file: Path
) -> None:
    """``upload_timeout=Timeout(5.0, read=10.0)`` reaches the finalize POST site."""
    custom = httpx.Timeout(5.0, read=10.0)
    captured: list[httpx.Timeout | None] = []
    capturing = _make_capturing_async_client(captured)

    async with NotebookLMClient(auth_tokens, upload_timeout=custom) as client:
        with patch("notebooklm._sources.httpx.AsyncClient", capturing):
            with pytest.raises((httpx.HTTPError, OSError)):
                await client.sources._upload_file_streaming(
                    upload_url="https://example.invalid/upload/abc",
                    file_obj=tmp_upload_file,
                )

    assert captured, "Expected at least one httpx.AsyncClient construction"
    finalize_timeout = captured[-1]
    assert isinstance(finalize_timeout, httpx.Timeout)
    assert finalize_timeout.connect == 5.0
    assert finalize_timeout.read == 10.0


async def test_default_upload_timeout_preserves_back_compat_finalize(
    auth_tokens, tmp_upload_file: Path
) -> None:
    """No override -> finalize POST still uses the original 10.0/300.0 hardcode."""
    captured: list[httpx.Timeout | None] = []
    capturing = _make_capturing_async_client(captured)

    async with NotebookLMClient(auth_tokens) as client:  # no upload_timeout
        with patch("notebooklm._sources.httpx.AsyncClient", capturing):
            with pytest.raises((httpx.HTTPError, OSError)):
                await client.sources._upload_file_streaming(
                    upload_url="https://example.invalid/upload/abc",
                    file_obj=tmp_upload_file,
                )

    assert captured
    finalize_timeout = captured[-1]
    assert isinstance(finalize_timeout, httpx.Timeout)
    assert finalize_timeout.connect == 10.0
    assert finalize_timeout.read == 300.0


async def test_from_storage_accepts_upload_timeout(monkeypatch, auth_tokens) -> None:
    """``from_storage`` honors the ``upload_timeout`` kwarg and threads it to SourcesAPI."""
    from notebooklm import auth as auth_module

    async def _fake_from_storage(*args: object, **kwargs: object):
        return auth_tokens

    monkeypatch.setattr(auth_module.AuthTokens, "from_storage", _fake_from_storage)

    custom = httpx.Timeout(7.0, read=14.0)
    # Context not entered — only inspecting constructor-level wiring.
    # ``__aenter__`` / ``ClientCore.open()`` never run, so there are no
    # background tasks or open sockets to clean up.
    client = await NotebookLMClient.from_storage(upload_timeout=custom)

    assert client.sources._upload_timeout is custom
