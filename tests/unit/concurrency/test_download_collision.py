"""Regression test for the concurrent download temp-file collision.

Audit item #1 (`thread-safety-concurrency-audit.md` §1):
Pre-fix, two concurrent `_download_url(...)` calls targeting the same
`output_path` shared a single `<output>.tmp` file, interleaving bytes
and racing on `temp_file.rename(output_file)`. Post-fix, each
call uses `tempfile.mkstemp(...)` for a unique temp path and commits
via `os.replace`, so concurrent writers cannot corrupt the final file.

This test exercises the previously-broken path directly via
`client.artifacts._download_url`, intercepting the internal
`httpx.AsyncClient` with the `httpx_mock` fixture.
"""

from __future__ import annotations

import asyncio

import pytest
from pytest_httpx import HTTPXMock

from notebooklm import NotebookLMClient


@pytest.fixture(autouse=True)
def _stub_storage_cookies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass on-disk storage_state.json lookup in `_download_url`.

    `_download_url` calls `load_httpx_cookies(path=self._storage_path)`
    which raises `FileNotFoundError` on a clean CI runner that hasn't
    been through `notebooklm login`. We don't care about the cookies for
    this test (the mock transport doesn't validate auth) — return an
    empty dict.
    """
    monkeypatch.setattr(
        "notebooklm._artifacts.load_httpx_cookies",
        lambda path=None: {},
    )


async def test_concurrent_downloads_to_same_output_path_no_corruption(
    auth_tokens,
    httpx_mock: HTTPXMock,
    tmp_path,
) -> None:
    """Two concurrent _download_url calls writing to the same path produce
    valid bytes (one URL wins cleanly), never interleaved."""
    url_a = "https://storage.googleapis.com/file_a.bin"
    url_b = "https://storage.googleapis.com/file_b.bin"
    body_a = b"AAAA" * 4096  # 16 KB of 'A'
    body_b = b"BBBB" * 4096  # 16 KB of 'B'

    httpx_mock.add_response(url=url_a, content=body_a)
    httpx_mock.add_response(url=url_b, content=body_b)

    output_path = tmp_path / "out.bin"

    async with NotebookLMClient(auth_tokens) as client:
        results = await asyncio.gather(
            client.artifacts._download_url(url_a, str(output_path)),
            client.artifacts._download_url(url_b, str(output_path)),
            return_exceptions=True,
        )

    # Both calls should return the output_path string (not raise) — pre-fix
    # one could raise FileNotFoundError on the rename if the other had
    # already moved its temp file.
    assert all(isinstance(r, str) for r in results), f"unexpected exception: {results}"

    # The final file must contain EXACTLY one of the two URL's bytes,
    # NOT a mix or partial content. Pre-fix, the shared `<output>.tmp`
    # could be open()ed twice with mode "wb" (truncating), so the
    # observed bytes were a non-deterministic interleave of A's and B's.
    final_bytes = output_path.read_bytes()
    assert final_bytes in (body_a, body_b), (
        f"output bytes corrupted: not equal to body_a or body_b. "
        f"len={len(final_bytes)}, head={final_bytes[:16]!r}"
    )


async def test_concurrent_downloads_to_distinct_paths_both_succeed(
    auth_tokens,
    httpx_mock: HTTPXMock,
    tmp_path,
) -> None:
    """Sanity: distinct output paths still work — no regression on the
    common case (each URL ends up at its own destination)."""
    url_a = "https://storage.googleapis.com/file_a.bin"
    url_b = "https://storage.googleapis.com/file_b.bin"
    body_a = b"DISTINCT-A" * 1024
    body_b = b"DISTINCT-B" * 1024

    httpx_mock.add_response(url=url_a, content=body_a)
    httpx_mock.add_response(url=url_b, content=body_b)

    out_a = tmp_path / "a.bin"
    out_b = tmp_path / "b.bin"

    async with NotebookLMClient(auth_tokens) as client:
        await asyncio.gather(
            client.artifacts._download_url(url_a, str(out_a)),
            client.artifacts._download_url(url_b, str(out_b)),
        )

    assert out_a.read_bytes() == body_a
    assert out_b.read_bytes() == body_b


async def test_no_leftover_tmp_files_after_concurrent_downloads(
    auth_tokens,
    httpx_mock: HTTPXMock,
    tmp_path,
) -> None:
    """Both calls succeed and leave zero tempfiles behind.

    With the mock transport, both `_download_url` invocations always
    succeed and atomically `os.replace` their unique mkstemp temps onto
    `output_path` (one wins, the other clobbers — both get a final
    state). No tempfile should remain in `tmp_path` after the gather.
    """
    url_a = "https://storage.googleapis.com/file_a.bin"
    url_b = "https://storage.googleapis.com/file_b.bin"
    httpx_mock.add_response(url=url_a, content=b"a" * 1024)
    httpx_mock.add_response(url=url_b, content=b"b" * 1024)

    output_path = tmp_path / "out.bin"

    async with NotebookLMClient(auth_tokens) as client:
        results = await asyncio.gather(
            client.artifacts._download_url(url_a, str(output_path)),
            client.artifacts._download_url(url_b, str(output_path)),
            return_exceptions=True,
        )

    # Both calls must succeed (no exception slipped through return_exceptions).
    assert all(isinstance(r, str) for r in results), f"unexpected exception: {results}"
    assert output_path.exists(), "expected final output file to exist after both downloads"

    # mkstemp temp names have shape `out.bin.<random>.tmp`. With deterministic
    # mock-transport completion, both renames succeed and zero leftovers remain.
    leftovers = sorted(p for p in tmp_path.iterdir() if p != output_path)
    assert leftovers == [], f"unexpected leftover temp files: {leftovers}"


@pytest.fixture
def non_mocked_hosts() -> list[str]:
    """Empty list: intercept all hosts via pytest-httpx. No real network."""
    return []
