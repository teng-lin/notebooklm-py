"""Unit tests for the ``/files/*`` side-channel routes (``mcp/_fileroutes.py``).

Driven through a Starlette ``TestClient`` over the real FastMCP ``http_app()``,
with the client bound by the server lifespan (a mocked ``NotebookLMClient`` via the
``client_factory`` seam) — and crucially **no bearer header**: the signed token is
the sole auth for these routes (a regression tripwire if a FastMCP upgrade starts
gating custom routes). Covers download/upload happy paths, token rejection, the
running byte cap, ``?filename`` handling, temp cleanup, and the lifespan-unset 500.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py.
pytest.importorskip("fastmcp")
starlette_testclient = pytest.importorskip("starlette.testclient")

from notebooklm.mcp import _fileroutes  # noqa: E402 - after importorskip guard
from notebooklm.mcp._auth import build_auth_provider  # noqa: E402 - after importorskip guard
from notebooklm.mcp._filelink import (  # noqa: E402 - after importorskip guard
    FileLinkSigner,
    FileTransferConfig,
)
from notebooklm.mcp.server import create_server  # noqa: E402 - after importorskip guard

from .conftest import AsyncMock  # noqa: E402 - after importorskip guard

BASE = "https://files.test"
NB = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def config() -> FileTransferConfig:
    return FileTransferConfig(signer=FileLinkSigner(b"k" * 32), base_url=BASE)


def _path(url: str) -> str:
    """Strip the public origin → the route path the TestClient hits."""
    return url[len(BASE) :]


def _build(mock_client: MagicMock, config: FileTransferConfig, *, auth: object | None = None):
    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[MagicMock]:
        yield mock_client

    server = create_server(client_factory=factory, file_transfer=config, auth=auth)  # type: ignore[arg-type]
    return server.http_app()


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
def _fake_download_writing(content: bytes, title: str = "My Podcast"):
    """An ``execute_download`` stand-in that writes bytes to the plan's output path."""

    async def fake(plan, client, *, notebook_resolver, artifact_resolver, progress=None):
        await notebook_resolver(plan.notebook_id)
        Path(plan.output_path).write_bytes(content)
        return _fileroutes.download_core.DownloadResult(
            outcome=_fileroutes.download_core.DownloadOutcome.SINGLE_DOWNLOADED,
            artifact={"id": "a1", "title": title, "selection_reason": "latest"},
            output_path=plan.output_path,
        )

    return fake


def test_download_good_token_streams_bytes_no_bearer(monkeypatch, mock_client, config) -> None:
    monkeypatch.setattr(
        _fileroutes.download_core, "execute_download", _fake_download_writing(b"AUDIO")
    )
    app = _build(mock_client, config)
    url = config.download_url({"op": "dl", "nb": NB, "atype": "audio"})
    with starlette_testclient.TestClient(app) as client:
        # No Authorization header at all — the token is the auth.
        resp = client.get(_path(url))
    assert resp.status_code == 200
    assert resp.content == b"AUDIO"
    # The Content-Disposition uses the artifact title + the real extension, NOT
    # the core's internal "artifact.mp3" (RFC 5987 percent-encodes the space).
    assert "My%20Podcast.mp3" in resp.headers.get("content-disposition", "")
    assert resp.headers["cache-control"] == "no-store"


def test_download_route_forwards_fmt_from_token(monkeypatch, mock_client, config) -> None:
    # The `fmt` carried in a dl token must reach build_download_plan (route-level
    # round trip; the tool-level test only checks the token encodes it).
    captured: dict[str, object] = {}

    async def fake(plan, client, *, notebook_resolver, artifact_resolver, progress=None):
        captured["format_choice"] = plan.format_choice
        await notebook_resolver(plan.notebook_id)
        Path(plan.output_path).write_bytes(b"QUIZ")
        return _fileroutes.download_core.DownloadResult(
            outcome=_fileroutes.download_core.DownloadOutcome.SINGLE_DOWNLOADED,
            artifact={"id": "a1", "title": "Quiz", "selection_reason": "latest"},
            output_path=plan.output_path,
        )

    monkeypatch.setattr(_fileroutes.download_core, "execute_download", fake)
    app = _build(mock_client, config)
    url = config.download_url({"nb": NB, "atype": "quiz", "fmt": "markdown"})
    with starlette_testclient.TestClient(app) as client:
        resp = client.get(_path(url))
    assert resp.status_code == 200
    assert captured["format_choice"] == "markdown"


@pytest.mark.parametrize(
    "token",
    [
        "bogus.token",  # malformed (two segments, bad MAC)
        "x",  # single segment, no '.'
        "é.bm9wZQ",  # non-ASCII body → FileLinkError, not a 500
    ],
)
def test_download_bad_token_403(monkeypatch, mock_client, config, token) -> None:
    app = _build(mock_client, config)
    with starlette_testclient.TestClient(app) as client:
        resp = client.get(f"/files/dl/{token}")
    assert resp.status_code == 403


def test_download_wrong_op_token_403(mock_client, config) -> None:
    # An UPLOAD token replayed against the download route must be rejected.
    upload_url = config.upload_url({"op": "ul", "nb": NB})
    app = _build(mock_client, config)
    with starlette_testclient.TestClient(app) as client:
        resp = client.get(_path(upload_url).replace("/files/ul/", "/files/dl/"))
    assert resp.status_code == 403


def test_download_not_ready_409(monkeypatch, mock_client, config) -> None:
    async def fake(plan, client, *, notebook_resolver, artifact_resolver, progress=None):
        return _fileroutes.download_core.DownloadResult(
            outcome=_fileroutes.download_core.DownloadOutcome.NO_ARTIFACTS,
            error="none yet",
        )

    monkeypatch.setattr(_fileroutes.download_core, "execute_download", fake)
    app = _build(mock_client, config)
    url = config.download_url({"op": "dl", "nb": NB, "atype": "audio"})
    with starlette_testclient.TestClient(app) as client:
        resp = client.get(_path(url))
    assert resp.status_code == 409


def test_download_served_path_must_stay_in_tempdir(monkeypatch, mock_client, config) -> None:
    # A core that resolves a path OUTSIDE our private temp dir is a bug, not a file
    # we serve → 500 (pins the inside-tempdir assertion).
    fd, outside_path = tempfile.mkstemp(suffix=".mp3")
    os.write(fd, b"X")
    os.close(fd)

    async def fake(plan, client, *, notebook_resolver, artifact_resolver, progress=None):
        return _fileroutes.download_core.DownloadResult(
            outcome=_fileroutes.download_core.DownloadOutcome.SINGLE_DOWNLOADED,
            artifact={"id": "a1", "title": "T", "selection_reason": "latest"},
            output_path=outside_path,
        )

    monkeypatch.setattr(_fileroutes.download_core, "execute_download", fake)
    app = _build(mock_client, config)
    url = config.download_url({"op": "dl", "nb": NB, "atype": "audio"})
    try:
        with starlette_testclient.TestClient(app) as client:
            resp = client.get(_path(url))
        assert resp.status_code == 500
    finally:
        os.unlink(outside_path)


# --------------------------------------------------------------------------- #
# Upload page (GET)
# --------------------------------------------------------------------------- #
def test_upload_page_returns_html_with_security_headers(mock_client, config) -> None:
    app = _build(mock_client, config)
    url = config.upload_url({"op": "ul", "nb": NB})
    with starlette_testclient.TestClient(app) as client:
        resp = client.get(_path(url))
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["x-frame-options"] == "DENY"
    assert "<input" in resp.text and "fetch(" in resp.text


def test_upload_page_bad_token_403(mock_client, config) -> None:
    app = _build(mock_client, config)
    with starlette_testclient.TestClient(app) as client:
        resp = client.get("/files/ul/bogus.token")
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Upload POST
# --------------------------------------------------------------------------- #
def test_upload_post_adds_source_with_title_and_mime_from_token(mock_client, config) -> None:
    add_file = AsyncMock(return_value=MagicMock(id="src-99"))
    mock_client.sources.add_file = add_file
    app = _build(mock_client, config)
    url = config.upload_url(
        {"op": "ul", "nb": NB, "title": "Signed Title", "mime": "application/pdf"}
    )
    with starlette_testclient.TestClient(app) as client:
        resp = client.post(
            _path(url) + "?filename=paper.pdf",
            content=b"PDFDATA",
            headers={"Content-Type": "text/plain"},  # token mime must WIN over this
        )
    assert resp.status_code == 200
    assert "src-99" in resp.text
    add_file.assert_awaited_once()
    args, kwargs = add_file.call_args
    notebook_id, file_path, mime = args
    assert notebook_id == NB
    assert file_path.endswith("paper.pdf")  # ?filename extension preserved
    assert mime == "application/pdf"  # token mime won
    assert kwargs["title"] == "Signed Title"


def test_upload_post_filename_is_sanitized_to_basename(mock_client, config) -> None:
    add_file = AsyncMock(return_value=MagicMock(id="src-1"))
    mock_client.sources.add_file = add_file
    app = _build(mock_client, config)
    url = config.upload_url({"op": "ul", "nb": NB})
    with starlette_testclient.TestClient(app) as client:
        resp = client.post(
            _path(url) + "?filename=" + "../../etc/x.pdf",
            content=b"DATA",
        )
    assert resp.status_code == 200
    file_path = add_file.call_args.args[1]
    # Traversal stripped to a basename inside our private temp dir.
    assert os.path.basename(file_path) == "x.pdf"
    assert "/etc/x.pdf" not in file_path


def test_upload_post_missing_filename_defaults_to_extensioned_name(mock_client, config) -> None:
    add_file = AsyncMock(return_value=MagicMock(id="src-1"))
    mock_client.sources.add_file = add_file
    app = _build(mock_client, config)
    url = config.upload_url({"op": "ul", "nb": NB})
    with starlette_testclient.TestClient(app) as client:
        resp = client.post(_path(url), content=b"DATA")
    assert resp.status_code == 200
    assert add_file.call_args.args[1].endswith("upload.bin")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\x00b.pdf", "ab.pdf"),  # NUL stripped (would make os.open raise)
        ("a\x01\x1fb.pdf", "ab.pdf"),  # other control chars stripped too
        ("..", "upload.bin"),  # directory cursor → safe default
        (".", "upload.bin"),
        ("", "upload.bin"),
        (None, "upload.bin"),
        ("../../etc/passwd", "passwd"),  # traversal → basename
        (r"C:\Users\me\report.pdf", "report.pdf"),  # Windows path → leaf
    ],
)
def test_safe_upload_name_hardening(raw, expected) -> None:
    # Security: odd filenames must normalize to a harmless leaf, never reach
    # os.open as a NUL/cursor name (which would 500).
    assert _fileroutes._safe_upload_name(raw) == expected


def test_upload_dotdot_filename_defaults_cleanly_not_500(mock_client, config) -> None:
    add_file = AsyncMock(return_value=MagicMock(id="src-2"))
    mock_client.sources.add_file = add_file
    app = _build(mock_client, config)
    url = config.upload_url({"op": "ul", "nb": NB})
    with starlette_testclient.TestClient(app) as client:
        resp = client.post(_path(url) + "?filename=..", content=b"DATA")
    assert resp.status_code == 200  # clean default, not an uncaught 500
    assert add_file.call_args.args[1].endswith("upload.bin")


def test_upload_concurrency_cap_returns_429(monkeypatch, mock_client, config) -> None:
    # Security: a leaked/replayable ul token must not drive unbounded parallel
    # 200 MiB spools. At the in-flight cap, the next upload is a fast 429 (no disk).
    add_file = AsyncMock(return_value=MagicMock(id="src-x"))
    mock_client.sources.add_file = add_file
    monkeypatch.setattr(_fileroutes, "_inflight_uploads", _fileroutes._MAX_CONCURRENT_UPLOADS)
    app = _build(mock_client, config)
    url = config.upload_url({"op": "ul", "nb": NB})
    with starlette_testclient.TestClient(app) as client:
        resp = client.post(_path(url) + "?filename=a.pdf", content=b"DATA")
    assert resp.status_code == 429
    add_file.assert_not_awaited()  # rejected before any source-add / disk write


def test_upload_post_wrong_op_token_403(mock_client, config) -> None:
    # A download token replayed against the upload route is rejected.
    dl = config.download_url({"op": "dl", "nb": NB, "atype": "audio"})
    app = _build(mock_client, config)
    with starlette_testclient.TestClient(app) as client:
        resp = client.post(_path(dl).replace("/files/dl/", "/files/ul/"), content=b"X")
    assert resp.status_code == 403


def test_upload_post_content_length_over_cap_413_no_temp(monkeypatch, mock_client, config) -> None:
    monkeypatch.setattr(_fileroutes, "MAX_UPLOAD_BYTES", 4)
    made: list[str] = []
    real_mkdtemp = tempfile.mkdtemp
    monkeypatch.setattr(
        _fileroutes.tempfile,
        "mkdtemp",
        lambda *a, **k: made.append("x") or real_mkdtemp(*a, **k),
    )
    add_file = AsyncMock()
    mock_client.sources.add_file = add_file
    app = _build(mock_client, config)
    url = config.upload_url({"op": "ul", "nb": NB})
    with starlette_testclient.TestClient(app) as client:
        # A truthful Content-Length over the cap is rejected early.
        resp = client.post(_path(url), content=b"abcdefghij")
    assert resp.status_code == 413
    assert made == []  # no temp dir created
    add_file.assert_not_awaited()


def test_upload_post_streams_past_cap_413_midstream_and_cleans_up(
    monkeypatch, mock_client, config
) -> None:
    monkeypatch.setattr(_fileroutes, "MAX_UPLOAD_BYTES", 5)
    cleaned: list[str] = []
    real_cleanup = _fileroutes._cleanup
    monkeypatch.setattr(_fileroutes, "_cleanup", lambda p: cleaned.append(p) or real_cleanup(p))
    add_file = AsyncMock()
    mock_client.sources.add_file = add_file
    app = _build(mock_client, config)
    url = config.upload_url({"op": "ul", "nb": NB})

    def body() -> Iterator[bytes]:
        # A chunked body (no/under-stated Content-Length) that streams past the cap.
        yield b"abcd"
        yield b"efgh"

    with starlette_testclient.TestClient(app) as client:
        resp = client.post(_path(url), content=body())
    assert resp.status_code == 413
    add_file.assert_not_awaited()
    assert cleaned, "temp dir must be removed on a mid-stream abort"


def test_upload_post_cleans_temp_on_success(monkeypatch, mock_client, config) -> None:
    cleaned: list[str] = []
    real_cleanup = _fileroutes._cleanup
    monkeypatch.setattr(_fileroutes, "_cleanup", lambda p: cleaned.append(p) or real_cleanup(p))
    mock_client.sources.add_file = AsyncMock(return_value=MagicMock(id="src-1"))
    app = _build(mock_client, config)
    url = config.upload_url({"op": "ul", "nb": NB})
    with starlette_testclient.TestClient(app) as client:
        resp = client.post(_path(url) + "?filename=a.pdf", content=b"DATA")
    assert resp.status_code == 200
    assert cleaned and not Path(cleaned[0]).exists()


# --------------------------------------------------------------------------- #
# No-bearer reachability (regression tripwire) + lifespan-unset 500
# --------------------------------------------------------------------------- #
def test_custom_routes_bypass_the_bearer_gate(mock_client, config) -> None:
    # Build the server WITH a bearer auth provider: the /mcp route would 401 without
    # a token, but the signed /files/* routes must still be reachable (custom routes
    # are not wrapped by RequireAuthMiddleware). Pins the FastMCP auth model.
    mock_client.sources.add_file = AsyncMock(return_value=MagicMock(id="src-1"))
    app = _build(mock_client, config, auth=build_auth_provider("a-strong-token"))
    url = config.upload_url({"op": "ul", "nb": NB})
    with starlette_testclient.TestClient(app) as client:
        # No Authorization header — reaches the handler (200), not a 401.
        resp = client.get(_path(url))
    assert resp.status_code == 200


def test_lifespan_not_set_returns_500(mock_client, config) -> None:
    # No `with` → the lifespan never runs → _lifespan_result_set is False. The
    # download route's client accessor must surface 500, not crash (pins the
    # private-attr access).
    app = _build(mock_client, config)
    url = config.download_url({"op": "dl", "nb": NB, "atype": "audio"})
    client = starlette_testclient.TestClient(app)
    resp = client.get(_path(url))
    assert resp.status_code == 500
