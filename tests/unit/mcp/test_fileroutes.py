"""Unit tests for the ``/files/*`` side-channel routes (``mcp/_fileroutes.py``).

Driven through a Starlette ``TestClient`` over the real FastMCP ``http_app()``,
with the client bound by the server lifespan (a mocked ``NotebookLMClient`` via the
``client_factory`` seam) — and crucially **no bearer header**: the signed token is
the sole auth for these routes (a regression tripwire if a FastMCP upgrade starts
gating custom routes). Covers download/upload happy paths, token rejection, the
running byte cap, ``?filename`` handling, temp cleanup, and the lifespan-unset 500.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py.
pytest.importorskip("fastmcp")
starlette_testclient = pytest.importorskip("starlette.testclient")

from notebooklm._types.artifacts import (  # noqa: E402 - after importorskip guard
    ArtifactStatus,
    ArtifactTypeCode,
)
from notebooklm.mcp import _fileroutes  # noqa: E402 - after importorskip guard
from notebooklm.mcp._auth import build_auth_provider  # noqa: E402 - after importorskip guard
from notebooklm.mcp._filelink import (  # noqa: E402 - after importorskip guard
    FileLinkSigner,
    FileTransferConfig,
)
from notebooklm.mcp.server import create_server  # noqa: E402 - after importorskip guard
from notebooklm.types import Artifact  # noqa: E402 - after importorskip guard

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


def test_download_concurrency_cap_returns_429(monkeypatch, mock_client, config) -> None:
    # Security: a leaked/replayable dl token must not drive unbounded parallel temp
    # spools + Google fetches. At the in-flight cap, the next download is a fast 429
    # (no temp dir, no fetch).
    fetch = AsyncMock()
    monkeypatch.setattr(_fileroutes.download_core, "execute_download", fetch)
    monkeypatch.setattr(_fileroutes, "_inflight_downloads", _fileroutes._MAX_CONCURRENT_DOWNLOADS)
    app = _build(mock_client, config)
    url = config.download_url({"op": "dl", "nb": NB, "atype": "audio"})
    with starlette_testclient.TestClient(app) as client:
        resp = client.get(_path(url))
    assert resp.status_code == 429
    fetch.assert_not_awaited()  # rejected before any temp dir / upstream fetch


def test_download_success_releases_slot(monkeypatch, mock_client, config) -> None:
    # The in-flight slot is released on handler return (a ``finally``), so a
    # completed download leaves the counter where it started — no slow leak.
    monkeypatch.setattr(
        _fileroutes.download_core, "execute_download", _fake_download_writing(b"AUDIO")
    )
    before = _fileroutes._inflight_downloads
    app = _build(mock_client, config)
    url = config.download_url({"op": "dl", "nb": NB, "atype": "audio"})
    with starlette_testclient.TestClient(app) as client:
        resp = client.get(_path(url))
    assert resp.status_code == 200
    assert _fileroutes._inflight_downloads == before


def test_download_error_releases_slot(monkeypatch, mock_client, config) -> None:
    # An error outcome (409) must also release the slot via the ``finally``.
    async def fake(plan, client, *, notebook_resolver, artifact_resolver, progress=None):
        return _fileroutes.download_core.DownloadResult(
            outcome=_fileroutes.download_core.DownloadOutcome.NO_ARTIFACTS,
            error="none yet",
        )

    monkeypatch.setattr(_fileroutes.download_core, "execute_download", fake)
    before = _fileroutes._inflight_downloads
    app = _build(mock_client, config)
    url = config.download_url({"op": "dl", "nb": NB, "atype": "audio"})
    with starlette_testclient.TestClient(app) as client:
        resp = client.get(_path(url))
    assert resp.status_code == 409
    assert _fileroutes._inflight_downloads == before


def test_download_mkdtemp_failure_is_clean_500_releases_slot(
    monkeypatch, mock_client, config
) -> None:
    # ``mkdtemp`` raising (ENOSPC — the temp-disk exhaustion this cap guards against)
    # must be a CLEAN caught 500 (not a raw Starlette 500) AND must not leak the slot.
    # Uses the DEFAULT TestClient (``raise_server_exceptions=True``): an uncaught OSError
    # would be re-raised here, so a returned 500 proves the exception is now caught.
    def boom(*_a, **_k):
        raise OSError("No space left on device")

    monkeypatch.setattr(_fileroutes.tempfile, "mkdtemp", boom)
    before = _fileroutes._inflight_downloads
    app = _build(mock_client, config)
    url = config.download_url({"op": "dl", "nb": NB, "atype": "audio"})
    with starlette_testclient.TestClient(app) as client:
        resp = client.get(_path(url))
    assert resp.status_code == 500
    assert "server storage error" in resp.text  # clean body, not raw "Internal Server Error"
    assert _fileroutes._inflight_downloads == before  # slot released


def test_download_post_fetch_exception_cleans_temp_and_releases_slot(
    monkeypatch, mock_client, config
) -> None:
    # An unexpected error AFTER a successful fetch (here: building the download name)
    # must still clean the spooled temp dir (not just the error/early-return paths)
    # and release the slot — the outer finally guarantees both.
    monkeypatch.setattr(
        _fileroutes.download_core, "execute_download", _fake_download_writing(b"AUDIO")
    )

    def boom(*_a, **_k):
        raise RuntimeError("cannot build filename")

    monkeypatch.setattr(_fileroutes.download_core, "artifact_title_to_filename", boom)
    cleaned: list[str] = []
    real_cleanup = _fileroutes._cleanup
    monkeypatch.setattr(_fileroutes, "_cleanup", lambda d: (cleaned.append(d), real_cleanup(d))[1])
    before = _fileroutes._inflight_downloads
    app = _build(mock_client, config)
    url = config.download_url({"op": "dl", "nb": NB, "atype": "audio"})
    with starlette_testclient.TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(_path(url))
    assert resp.status_code == 500
    assert cleaned, "temp dir must be cleaned on a post-fetch exception"
    assert _fileroutes._inflight_downloads == before


async def test_slot_held_response_releases_on_stream_abort(monkeypatch, tmp_path) -> None:
    # The slot is held for the whole stream (so slow/held downloads still count),
    # but released from the response's ``finally`` even when the stream aborts
    # mid-flight (client disconnect / bad Range) — so a held slot can never leak.
    temp_dir = str(tmp_path / "dl")
    os.mkdir(temp_dir)
    served = os.path.join(temp_dir, "artifact.mp3")
    Path(served).write_bytes(b"AUDIO")

    async def boom_call(self, *_a, **_k):
        raise RuntimeError("client disconnected mid-stream")

    monkeypatch.setattr(_fileroutes.FileResponse, "__call__", boom_call)
    monkeypatch.setattr(_fileroutes, "_inflight_downloads", 1)
    resp = _fileroutes._SlotHeldFileResponse(served, temp_dir=temp_dir)
    with pytest.raises(RuntimeError):
        await resp({"type": "http", "method": "GET", "headers": []}, None, None)
    assert _fileroutes._inflight_downloads == 0  # slot released despite the abort
    assert not os.path.exists(temp_dir)  # temp dir cleaned despite the abort


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


def test_download_route_forwards_aid_from_token(monkeypatch, mock_client, config) -> None:
    captured: dict[str, object] = {}

    async def fake(plan, client, *, notebook_resolver, artifact_resolver, progress=None):
        captured["artifact_id"] = plan.artifact_id
        captured["latest"] = plan.latest
        # Exercise the resolver to prove it's called
        artifact_resolver([{"id": "a1", "title": "My Podcast"}], "a1")
        await notebook_resolver(plan.notebook_id)
        Path(plan.output_path).write_bytes(b"AUDIO")
        return _fileroutes.download_core.DownloadResult(
            outcome=_fileroutes.download_core.DownloadOutcome.SINGLE_DOWNLOADED,
            artifact={"id": "a1", "title": "My Podcast", "selection_reason": "by id"},
            output_path=plan.output_path,
        )

    monkeypatch.setattr(_fileroutes.download_core, "execute_download", fake)
    app = _build(mock_client, config)
    url = config.download_url({"nb": NB, "atype": "audio", "aid": "a1"})
    with starlette_testclient.TestClient(app) as client:
        resp = client.get(_path(url))
    assert resp.status_code == 200
    assert captured["artifact_id"] == "a1"
    assert captured["latest"] is False


def test_download_route_latest_unchanged(monkeypatch, mock_client, config) -> None:
    captured: dict[str, object] = {}

    async def fake(plan, client, *, notebook_resolver, artifact_resolver, progress=None):
        captured["artifact_id"] = plan.artifact_id
        captured["latest"] = plan.latest
        await notebook_resolver(plan.notebook_id)
        Path(plan.output_path).write_bytes(b"AUDIO")
        return _fileroutes.download_core.DownloadResult(
            outcome=_fileroutes.download_core.DownloadOutcome.SINGLE_DOWNLOADED,
            artifact={"id": "a1", "title": "My Podcast", "selection_reason": "latest"},
            output_path=plan.output_path,
        )

    monkeypatch.setattr(_fileroutes.download_core, "execute_download", fake)
    app = _build(mock_client, config)
    url = config.download_url({"nb": NB, "atype": "audio"})
    with starlette_testclient.TestClient(app) as client:
        resp = client.get(_path(url))
    assert resp.status_code == 200
    assert captured["artifact_id"] is None
    assert captured["latest"] is True


def test_download_route_ambiguous_aid_is_400_not_500(mock_client, config) -> None:
    # An ambiguous ``aid`` prefix in the token must surface as a clean 400 (the real
    # ``_resolve_artifact_id`` raises AmbiguousIdError → ValidationError), NOT bubble
    # up as a Starlette 500. Runs the REAL execute_download (no monkeypatch) so the
    # resolver actually fires against the mocked artifact list.
    art_1 = Artifact(
        id="cccccccc-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        title="Podcast A",
        _artifact_type=ArtifactTypeCode.AUDIO.value,
        status=int(ArtifactStatus.COMPLETED),
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    art_2 = Artifact(
        id="cccccccc-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        title="Podcast B",
        _artifact_type=ArtifactTypeCode.AUDIO.value,
        status=int(ArtifactStatus.COMPLETED),
        created_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
    )
    mock_client.artifacts.list = AsyncMock(return_value=[art_1, art_2])
    app = _build(mock_client, config)
    url = config.download_url({"nb": NB, "atype": "audio", "aid": "cccccccc"})
    with starlette_testclient.TestClient(app) as client:
        resp = client.get(_path(url))
    assert resp.status_code == 400
    assert "Ambiguous ID" in resp.text


def _one_audio() -> Artifact:
    return Artifact(
        id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        title="Podcast A",
        _artifact_type=ArtifactTypeCode.AUDIO.value,
        status=int(ArtifactStatus.COMPLETED),
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def test_download_route_full_uuid_miss_is_400(mock_client, config) -> None:
    # A not-found full UUID ``aid`` resolves hard (uniform with a missing prefix) →
    # 400 with the real "not found" message, not the generic 409 not-ready text.
    mock_client.artifacts.list = AsyncMock(return_value=[_one_audio()])
    app = _build(mock_client, config)
    url = config.download_url(
        {"nb": NB, "atype": "audio", "aid": "dddddddd-dddd-dddd-dddd-dddddddddddd"}
    )
    with starlette_testclient.TestClient(app) as client:
        resp = client.get(_path(url))
    assert resp.status_code == 400
    assert "not found" in resp.text


def test_download_route_no_match_prefix_is_400(mock_client, config) -> None:
    mock_client.artifacts.list = AsyncMock(return_value=[_one_audio()])
    app = _build(mock_client, config)
    url = config.download_url({"nb": NB, "atype": "audio", "aid": "ffff"})
    with starlette_testclient.TestClient(app) as client:
        resp = client.get(_path(url))
    assert resp.status_code == 400


def test_download_route_non_string_aid_is_400_not_500(mock_client, config) -> None:
    # A signed token whose ``aid`` is not a string must fail as a clean 400, not a
    # 500 from ``_resolve_artifact_id`` calling ``.strip()`` on a non-str.
    app = _build(mock_client, config)
    url = config.download_url({"nb": NB, "atype": "audio", "aid": 123})
    with starlette_testclient.TestClient(app) as client:
        resp = client.get(_path(url))
    assert resp.status_code == 400


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


def test_short_link_redirects_to_canonical_upload_page(mock_client, config) -> None:
    # /u/<shortid> is the tap-friendly link handed to mobile users; it 302-redirects to the
    # real /files/ul/<token> page (single source of truth for the picker + POST).
    app = _build(mock_client, config)
    short = config.short_upload_url({"nb": NB})  # https://files.test/u/<shortid>
    shortid = short.rsplit("/", 1)[1]
    with starlette_testclient.TestClient(app) as client:
        resp = client.get(f"/u/{shortid}", follow_redirects=False)
        assert resp.status_code in (302, 307)
        loc = resp.headers["location"]
        assert "/files/ul/" in loc
        # following it renders the real upload page
        page = client.get(loc)
    assert page.status_code == 200
    assert "Upload a source to NotebookLM" in page.text


def test_short_link_unknown_id_404(mock_client, config) -> None:
    app = _build(mock_client, config)
    with starlette_testclient.TestClient(app) as client:
        resp = client.get("/u/doesnotexist", follow_redirects=False)
    assert resp.status_code == 404


def test_upload_cors_preflight_and_allow_origin(mock_client, config) -> None:
    # The in-app widget (Phase 3) POSTs cross-origin from its sandboxed iframe, so /files/ul
    # must answer the CORS preflight and allow-origin the success response.
    add_file = AsyncMock(return_value=MagicMock(id="src-cors"))
    mock_client.sources.add_file = add_file
    app = _build(mock_client, config)
    url = config.upload_url({"op": "ul", "nb": NB})
    with starlette_testclient.TestClient(app) as client:
        pre = client.options(_path(url))
        assert pre.status_code == 204
        assert pre.headers["access-control-allow-origin"] == "*"
        assert "POST" in pre.headers["access-control-allow-methods"]
        resp = client.post(
            _path(url) + "?filename=x.pdf", content=b"DATA", headers={"Accept": "application/json"}
        )
        # A rejected (bad/expired token) POST must ALSO carry ACAO, so the cross-origin widget
        # can read the real error instead of an opaque "Failed to fetch".
        rej = client.post("/files/ul/bogus.token", content=b"DATA")
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"
    assert rej.status_code == 403
    assert rej.headers["access-control-allow-origin"] == "*"


def test_upload_post_records_completion_result_for_await_upload(mock_client, config) -> None:
    # Phase 1: a successful POST records {source_id, name, size, mime} in the in-process
    # completion map keyed by jti, so a same-process await_upload poll surfaces the source.
    add_file = AsyncMock(return_value=MagicMock(id="src-77"))
    mock_client.sources.add_file = add_file
    app = _build(mock_client, config)
    url = config.upload_url({"op": "ul", "nb": NB, "mime": "application/pdf"})
    jti = config.signer.verify(url.rsplit("/", 1)[1], op="ul")["jti"]
    assert config.jti_store.completed(jti) is None  # nothing before the upload
    with starlette_testclient.TestClient(app) as client:
        resp = client.post(_path(url) + "?filename=paper.pdf", content=b"PDFDATA")
    assert resp.status_code == 200
    record = config.jti_store.completed(jti)
    assert record == {
        "source_id": "src-77",
        "name": "paper.pdf",
        "size": len(b"PDFDATA"),
        "mime": "application/pdf",
    }


def test_upload_failed_add_records_no_completion_result(monkeypatch, mock_client, config) -> None:
    # Success-only by design: a failed add rolls the jti back (retryable) and writes NO
    # completion record, so await_upload stays "pending" rather than reporting a phantom add.
    from notebooklm.exceptions import NotebookLMError

    mock_client.sources.add_file = AsyncMock(side_effect=NotebookLMError("boom"))
    app = _build(mock_client, config)
    url = config.upload_url({"op": "ul", "nb": NB})
    jti = config.signer.verify(url.rsplit("/", 1)[1], op="ul")["jti"]
    with starlette_testclient.TestClient(app) as client:
        resp = client.post(_path(url) + "?filename=x.pdf", content=b"DATA")
    assert resp.status_code >= 400
    assert config.jti_store.completed(jti) is None


def test_upload_upstream_rejection_surfaces_clean_redacted_4xx(mock_client, config) -> None:
    # Regression (#1892): an unsupported file type (.pub) is rejected upstream with an
    # HTTP 400 inside add_file → start_resumable_upload. The client layer now classifies
    # that as a ValidationError (not a raw httpx.HTTPStatusError), so the route returns a
    # clean, redacted 4xx — NOT an opaque 500. The message is scrubbed and the response
    # still carries the ACAO header so the cross-origin widget can read the real error.
    from notebooklm.exceptions import ValidationError

    mock_client.sources.add_file = AsyncMock(
        side_effect=ValidationError(
            "NotebookLM rejected the upload of 'x.pub' (HTTP 400: Bad Request). "
            "The file type or content may be unsupported. f.sid=SUPERSECRETVALUE12345"
        )
    )
    app = _build(mock_client, config)
    url = config.upload_url({"op": "ul", "nb": NB})
    with starlette_testclient.TestClient(app) as client:
        resp = client.post(
            _path(url) + "?filename=x.pub",
            content=b"PUBDATA",
            headers={"Origin": "https://example.test"},
        )
    assert resp.status_code == 400  # a clean 4xx, not a 500
    assert "SUPERSECRETVALUE12345" not in resp.text  # secret scrubbed by redact()
    # The load-bearing assertion: without ACAO the browser blocks the cross-origin
    # response as a CORS failure and the widget shows only "TypeError: Failed to
    # fetch". With it, the user sees the real, readable reason in the body.
    assert resp.headers["access-control-allow-origin"] == "*"
    assert "unsupported" in resp.text.lower()
    assert resp.headers["cache-control"] == "no-store"


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
        ("a\x7fb.pdf", "ab.pdf"),  # DEL stripped
        ("a\x85b.pdf", "ab.pdf"),  # C1 control (NEL) stripped
        ("a\x80\x9fb.pdf", "ab.pdf"),  # C1 range stripped
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


def test_safe_upload_name_truncates_by_bytes_preserving_extension() -> None:
    # A multibyte name (emoji = 4 UTF-8 bytes each) must be clipped to the 255-BYTE
    # filesystem basename limit, not merely 255 characters, and keep its extension.
    name = _fileroutes._safe_upload_name("😀" * 300 + ".pdf")
    assert len(name.encode("utf-8")) <= 255
    assert name.endswith(".pdf")
    # No partial multibyte char survives the byte clip.
    assert name.encode("utf-8").decode("utf-8") == name


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
    # The cross-origin widget must be able to READ the failure status (not a generic
    # "Failed to fetch") to show a useful message — so error responses carry ACAO too.
    assert resp.headers["access-control-allow-origin"] == "*"
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
# Upload single-use (jti) enforcement — #1746
# --------------------------------------------------------------------------- #
def test_upload_single_use_replay_rejected(mock_client, config) -> None:
    # Security (#1746): a leaked ul token is a content-agnostic write primitive. After
    # ONE successful add it is burned — a second POST with the same token 403s, and only
    # the first request actually added a source.
    add_file = AsyncMock(return_value=MagicMock(id="src-1"))
    mock_client.sources.add_file = add_file
    app = _build(mock_client, config)
    url = config.upload_url({"op": "ul", "nb": NB})
    with starlette_testclient.TestClient(app) as client:
        first = client.post(_path(url) + "?filename=a.pdf", content=b"DATA")
        second = client.post(_path(url) + "?filename=a.pdf", content=b"DATA")
    assert first.status_code == 200
    assert second.status_code == 403  # jti consumed → replay rejected
    add_file.assert_awaited_once()  # only the first request added a source


def test_upload_page_get_does_not_consume_jti(mock_client, config) -> None:
    # Loading the upload PAGE (GET) only calls verify(), it must NOT claim the jti — else
    # the user's subsequent POST would 403 before they ever upload.
    add_file = AsyncMock(return_value=MagicMock(id="src-1"))
    mock_client.sources.add_file = add_file
    app = _build(mock_client, config)
    url = config.upload_url({"op": "ul", "nb": NB})
    with starlette_testclient.TestClient(app) as client:
        page = client.get(_path(url))
        posted = client.post(_path(url) + "?filename=a.pdf", content=b"DATA")
    assert page.status_code == 200  # page rendered
    assert posted.status_code == 200  # POST still works — GET never burned the token


def test_upload_failed_add_frees_jti_for_retry(monkeypatch, mock_client, config) -> None:
    # record-on-success: a failed add rolls the jti back (via the route's finally), so the
    # SAME link is retryable — honors ADR-0024's large-file retry window.
    from notebooklm.exceptions import ServerError

    calls = {"n": 0}

    async def fake(client, exec_plan):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ServerError("upstream 503")
        return MagicMock(source=MagicMock(id="src-ok"))

    monkeypatch.setattr(_fileroutes.add_core, "execute_source_add", fake)
    app = _build(mock_client, config)
    url = config.upload_url({"op": "ul", "nb": NB})
    with starlette_testclient.TestClient(app) as client:
        first = client.post(_path(url) + "?filename=a.pdf", content=b"DATA")
        second = client.post(_path(url) + "?filename=a.pdf", content=b"DATA")
    assert first.status_code == 502  # add failed → jti rolled back
    assert second.status_code == 200  # retry with the same link succeeds
    assert "src-ok" in second.text


def test_upload_429_does_not_burn_jti(monkeypatch, mock_client, config) -> None:
    # A 429 (concurrency cap) is not a use of the token: the claim is rolled back in the
    # outer finally, so the same link works once a slot frees up.
    add_file = AsyncMock(return_value=MagicMock(id="src-1"))
    mock_client.sources.add_file = add_file
    app = _build(mock_client, config)
    url = config.upload_url({"op": "ul", "nb": NB})
    with starlette_testclient.TestClient(app) as client:
        monkeypatch.setattr(_fileroutes, "_inflight_uploads", _fileroutes._MAX_CONCURRENT_UPLOADS)
        capped = client.post(_path(url) + "?filename=a.pdf", content=b"DATA")
        monkeypatch.setattr(_fileroutes, "_inflight_uploads", 0)
        retried = client.post(_path(url) + "?filename=a.pdf", content=b"DATA")
    assert capped.status_code == 429
    assert retried.status_code == 200  # the 429 rolled the claim back → link still usable


def test_upload_midstream_413_does_not_burn_jti(monkeypatch, mock_client, config) -> None:
    # The over-cap early-return INSIDE the streaming loop is a distinct rollback path
    # from the pre-slot 429 and the post-stream error paths: it must also release the
    # claim (via the outer finally) so a corrected retry on the same link works.
    monkeypatch.setattr(_fileroutes, "MAX_UPLOAD_BYTES", 5)
    mock_client.sources.add_file = AsyncMock(return_value=MagicMock(id="src-1"))
    app = _build(mock_client, config)
    url = config.upload_url({"op": "ul", "nb": NB})

    def big_body() -> Iterator[bytes]:
        yield b"abcd"
        yield b"efgh"  # streams past the 5-byte cap → mid-stream 413

    with starlette_testclient.TestClient(app) as client:
        capped = client.post(_path(url) + "?filename=a.pdf", content=big_body())
        retried = client.post(_path(url) + "?filename=a.pdf", content=b"ok")
    assert capped.status_code == 413
    assert retried.status_code == 200  # mid-stream 413 rolled the claim back


def test_upload_mkdtemp_failure_is_clean_500_releases_slot_and_frees_jti(
    monkeypatch, mock_client, config
) -> None:
    # ``mkdtemp`` raising (ENOSPC — the temp-disk exhaustion the cap guards against)
    # sits OUTSIDE the spool ``try`` whose ``except OSError`` maps a bad filename to a
    # 400, so without its own catch it escaped as a RAW Starlette 500. This uses the
    # DEFAULT TestClient (``raise_server_exceptions=True``): an uncaught OSError would
    # be re-raised here (failing the test), so a returned 500 proves it is now caught.
    # It must also release the slot and — being a non-use of the token — roll the jti
    # back so the same link is retryable.
    add_file = AsyncMock(return_value=MagicMock(id="src-1"))
    mock_client.sources.add_file = add_file
    real_mkdtemp = _fileroutes.tempfile.mkdtemp

    def boom(*_a, **_k):
        raise OSError("No space left on device")

    before = _fileroutes._inflight_uploads
    app = _build(mock_client, config)
    url = config.upload_url({"op": "ul", "nb": NB})
    with starlette_testclient.TestClient(app) as client:
        monkeypatch.setattr(_fileroutes.tempfile, "mkdtemp", boom)
        failed = client.post(_path(url) + "?filename=a.pdf", content=b"DATA")
        monkeypatch.setattr(_fileroutes.tempfile, "mkdtemp", real_mkdtemp)
        retried = client.post(_path(url) + "?filename=a.pdf", content=b"DATA")
    assert failed.status_code == 500  # clean caught 500, not a raised uncaught exception
    assert "server storage error" in failed.text  # our message, not raw "Internal Server Error"
    assert _fileroutes._inflight_uploads == before  # slot released
    assert retried.status_code == 200  # jti rolled back → same link retryable
    add_file.assert_awaited_once()  # failed attempt short-circuited before the add; only retry added


def test_download_multi_use_range_resume_preserved(monkeypatch, mock_client, config) -> None:
    # RANGE/RESUME GUARDRAIL: dl tokens stay multi-use so a resumed download (a second
    # GET, e.g. a Range reconnect) is not 403'd. Do NOT regress this — any future dl
    # single-use must first solve resume.
    monkeypatch.setattr(
        _fileroutes.download_core, "execute_download", _fake_download_writing(b"AUDIO")
    )
    app = _build(mock_client, config)
    url = config.download_url({"op": "dl", "nb": NB, "atype": "audio"})
    with starlette_testclient.TestClient(app) as client:
        first = client.get(_path(url))
        second = client.get(_path(url))  # a Range/resume re-GET must still stream
    assert first.status_code == 200 and second.status_code == 200
    assert first.content == b"AUDIO" and second.content == b"AUDIO"


def test_upload_missing_jti_token_403(mock_client, config) -> None:
    # A validly-signed ul token whose payload carries NO jti (older / hand-built) is
    # rejected by the defensive guard. Built directly with the fixture key (b"k"*32),
    # mirroring test_filelink's tamper construction, so the MAC is valid but jti absent.
    key = b"k" * 32
    body = {"exp": int(time.time()) + 60, "nb": NB, "op": "ul"}  # no jti
    encoded = (
        base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
        .rstrip(b"=")
        .decode()
    )
    mac = hmac.new(key, encoded.encode("ascii"), hashlib.sha256).digest()
    token = f"{encoded}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"
    app = _build(mock_client, config)
    with starlette_testclient.TestClient(app) as client:
        resp = client.post(f"/files/ul/{token}?filename=a.pdf", content=b"DATA")
    assert resp.status_code == 403


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


# --------------------------------------------------------------------------- #
# Upstream-error classification + redaction (#1682)
# --------------------------------------------------------------------------- #
def _raising_download(exc: BaseException):
    async def fake(plan, client, *, notebook_resolver, artifact_resolver, progress=None):
        raise exc

    return fake


def test_download_raised_auth_error_is_502_not_raw_500(monkeypatch, mock_client, config) -> None:
    # A NotebookLMError raised out of execute_download (e.g. the unwrapped artifact
    # list RPC) must classify to a clean status, not bubble up as a raw Starlette 500.
    from notebooklm.exceptions import AuthError

    monkeypatch.setattr(
        _fileroutes.download_core,
        "execute_download",
        _raising_download(AuthError("session expired: cookie SID=AAAA1111secret")),
    )
    app = _build(mock_client, config)
    url = config.download_url({"op": "dl", "nb": NB, "atype": "audio"})
    with starlette_testclient.TestClient(app) as client:
        resp = client.get(_path(url))
    assert resp.status_code == 502  # AUTH → 502 (token-authed route; upstream failure)
    assert "AAAA1111secret" not in resp.text  # the cookie value is redacted


def test_download_raised_not_found_is_404(monkeypatch, mock_client, config) -> None:
    from notebooklm.exceptions import NotFoundError

    monkeypatch.setattr(
        _fileroutes.download_core,
        "execute_download",
        _raising_download(NotFoundError("notebook gone")),
    )
    app = _build(mock_client, config)
    url = config.download_url({"op": "dl", "nb": NB, "atype": "audio"})
    with starlette_testclient.TestClient(app) as client:
        resp = client.get(_path(url))
    assert resp.status_code == 404


def test_download_returned_error_outcome_stays_409_and_hides_detail(
    monkeypatch, mock_client, config
) -> None:
    # A FAILURE the core *returns* (DownloadOutcome.ERROR, e.g. a full-id not-found)
    # stays a generic 409 with the error detail discarded — deliberately NOT
    # re-statused to 502 (that would regress not-found) and never leaks result.error.
    async def fake(plan, client, *, notebook_resolver, artifact_resolver, progress=None):
        return _fileroutes.download_core.DownloadResult(
            outcome=_fileroutes.download_core.DownloadOutcome.ERROR,
            error="boom /home/secretuser/leak.json",
        )

    monkeypatch.setattr(_fileroutes.download_core, "execute_download", fake)
    app = _build(mock_client, config)
    url = config.download_url({"op": "dl", "nb": NB, "atype": "audio"})
    with starlette_testclient.TestClient(app) as client:
        resp = client.get(_path(url))
    assert resp.status_code == 409
    assert "secretuser" not in resp.text
    assert "leak.json" not in resp.text


def test_upload_raised_server_error_is_502(monkeypatch, mock_client, config) -> None:
    from notebooklm.exceptions import ServerError

    async def fake(client, exec_plan):
        raise ServerError("upstream 503")

    monkeypatch.setattr(_fileroutes.add_core, "execute_source_add", fake)
    app = _build(mock_client, config)
    url = config.upload_url({"op": "ul", "nb": NB})
    with starlette_testclient.TestClient(app) as client:
        resp = client.post(_path(url) + "?filename=a.pdf", content=b"DATA")
    assert resp.status_code == 502
    # The bytes already uploaded by the time the source-add RPC fails, so the body
    # tells the user a retry re-sends the whole file (vs a mid-stream failure).
    assert "Your file uploaded, but adding it as a source failed" in resp.text
    assert "a retry re-uploads it" in resp.text  # the actionable half must not regress
    assert "upstream 503" in resp.text  # the redacted upstream exc text still surfaces


def test_upload_validation_error_redacts_local_path(monkeypatch, mock_client, config) -> None:
    # A validate-path rejection can embed the local file path; the 400 body must be
    # redacted (the home-dir username masked) while keeping the friendly prefix.
    from notebooklm.exceptions import ValidationError

    async def fake(client, exec_plan):
        raise ValidationError("path /home/secretuser/private/x.pdf is not allowed")

    monkeypatch.setattr(_fileroutes.add_core, "execute_source_add", fake)
    app = _build(mock_client, config)
    url = config.upload_url({"op": "ul", "nb": NB})
    with starlette_testclient.TestClient(app) as client:
        resp = client.post(_path(url) + "?filename=a.pdf", content=b"DATA")
    assert resp.status_code == 400
    assert "Upload rejected:" in resp.text
    assert "secretuser" not in resp.text  # home-dir username redacted


def test_file_route_status_table_covers_every_error_category() -> None:
    """Every ``ErrorCategory`` has a file-route status (no silent fallback)."""
    from notebooklm._app.errors import ErrorCategory

    assert set(_fileroutes._FILE_ROUTE_STATUS) == set(ErrorCategory)
