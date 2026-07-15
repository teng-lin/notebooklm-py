"""Tool-branch tests for the remote file-transfer behavior of ``source_add`` and
``studio_download``.

Three branches each: file-transfer configured → a signed-URL payload; http without
config → a clean "not configured" error; and (config absent) stdio → the existing
path behavior. The transport is detected via ``get_http_request`` (raises on stdio);
the http-without-config branch is exercised by patching it to a fake request.
"""

from __future__ import annotations

import base64
import contextlib
import os
import textwrap
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py.
pytest.importorskip("fastmcp")

from datetime import datetime, timezone  # noqa: E402 - after importorskip guard

from fastmcp import Client  # noqa: E402 - after importorskip guard
from fastmcp.exceptions import ToolError  # noqa: E402 - after importorskip guard

import notebooklm.mcp.tools._fileupload as fileupload_mod  # noqa: E402 - after importorskip guard
import notebooklm.mcp.tools._studio_download as art_mod  # noqa: E402 - after importorskip guard
import notebooklm.mcp.tools.sources as src_mod  # noqa: E402 - after importorskip guard
from notebooklm._types.artifacts import (  # noqa: E402 - after importorskip guard
    ArtifactStatus,
    ArtifactTypeCode,
)
from notebooklm._types.sources import SourceType  # noqa: E402 - after importorskip guard
from notebooklm.mcp._filelink import (  # noqa: E402 - after importorskip guard
    FileLinkSigner,
    FileTransferConfig,
)
from notebooklm.mcp.server import create_server  # noqa: E402 - after importorskip guard
from notebooklm.rpc.types import SourceStatus  # noqa: E402 - after importorskip guard
from notebooklm.types import Artifact, ArtifactType  # noqa: E402 - after importorskip guard

from .conftest import AsyncMock  # noqa: E402 - after importorskip guard

BASE = "https://files.test"
NB_ID = "11111111-1111-1111-1111-111111111111"

_AID_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _audio_artifact(art_id: str, title: str = "Podcast", *, completed: bool = True) -> Artifact:
    """A real audio ``Artifact`` for the remote download pre-validation tests."""
    return Artifact(
        id=art_id,
        title=title,
        _artifact_type=ArtifactTypeCode.AUDIO.value,
        status=int(ArtifactStatus.COMPLETED if completed else ArtifactStatus.PROCESSING),
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


@dataclass
class FakeSource:
    id: str
    title: str | None = None


@dataclass
class FakeReadyPdf:
    """A READY pdf ``Source`` for the ``source_add(bytes_base64=…)`` happy path — carries the
    ``kind`` / ``status`` / ``is_error`` properties ``_source_view`` reads (the plain
    ``FakeSource`` above lacks them, so it can't flow through ``_add_result_payload``)."""

    id: str
    title: str | None = None

    @property
    def is_ready(self) -> bool:
        return True

    @property
    def is_error(self) -> bool:
        return False

    @property
    def kind(self) -> SourceType:
        return SourceType.PDF

    @property
    def status(self) -> SourceStatus:
        return SourceStatus.READY


@pytest.fixture
def config() -> FileTransferConfig:
    return FileTransferConfig(signer=FileLinkSigner(b"k" * 32), base_url=BASE)


async def _call(
    mock_client: MagicMock,
    file_transfer: FileTransferConfig | None,
    tool: str,
    args: dict[str, Any],
) -> Any:
    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[MagicMock]:
        yield mock_client

    server = create_server(client_factory=factory, file_transfer=file_transfer)
    async with Client(server) as client:
        return await client.call_tool(tool, args)


# --------------------------------------------------------------------------- #
# source_add type=file
# --------------------------------------------------------------------------- #
async def test_source_add_file_with_config_returns_upload_url(mock_client, config) -> None:
    result = await _call(
        mock_client,
        config,
        "source_add",
        {
            "notebook": NB_ID,
            "source_type": "file",
            "title": "My Doc",
            "mime_type": "application/pdf",
        },
    )
    sc = result.structured_content
    assert sc["status"] == "upload_required"
    assert sc["url"].startswith(f"{BASE}/files/ul/")
    assert sc["notebook_id"] == NB_ID
    assert isinstance(sc["expires_at"], int)
    # Human-friendly expiry mirrors the unix timestamp (#1801).
    assert sc["expires_in_seconds"] == 15 * 60
    assert sc["expires_at_iso"].endswith("Z")
    # ISO string round-trips to the same instant as the unix expires_at.
    parsed = datetime.fromisoformat(sc["expires_at_iso"].replace("Z", "+00:00"))
    assert parsed == datetime.fromtimestamp(sc["expires_at"], tz=timezone.utc)
    # The signed token carries the title + mime (so the browser round-trip keeps them).
    token = sc["url"].rsplit("/", 1)[1]
    payload = config.signer.verify(token, op="ul")
    assert payload["title"] == "My Doc"
    assert payload["mime"] == "application/pdf"
    # Human/browser path is first-class (a named object, not prose) so an agent that
    # can't upload the bytes itself reliably surfaces it to the user (#1801). It is now the
    # SHORT tap-friendly /u/<shortid> link (a long opaque token gets mangled in mobile chat),
    # NOT the deprecated top-level /files/ul url — and it resolves to the same signed token.
    human = sc["human_upload"]
    assert human["url"].startswith(f"{BASE}/u/")
    assert human["url"] != sc["url"]
    assert config.short_links.get(human["url"].rsplit("/", 1)[1]) == token
    assert "browser" in human["instructions"]
    # The mobile case is what makes the human path first-class — lock it in (#1801).
    assert "mobile" in human["instructions"]
    # mime was supplied → the request Content-Type is ignored, so no Content-Type hint.
    assert sc["mime_locked"] is True
    # The response self-documents the agent-direct path so an agent doesn't fall
    # back to the human "open in a browser" flow it can't perform.
    agent = sc["agent_upload"]
    assert agent["method"] == "POST"
    assert agent["headers"]["Accept"] == "application/json"
    assert "Content-Type" not in agent["headers"]
    # Locked → the example must NOT carry Content-Type either (server ignores it).
    assert "Content-Type" not in agent["example"]
    assert agent["url"].startswith(sc["url"])
    assert sc["url"] in agent["example"]
    # One authoritative try-then-fallback rule, not a per-environment prediction.
    assert "human_upload.url" in sc["agent_instructions"]


async def test_source_add_file_default_title_from_path_basename(mock_client, config) -> None:
    # A `path` is ACCEPTED on remote (not opened) — its basename seeds the title.
    result = await _call(
        mock_client,
        config,
        "source_add",
        {"notebook": NB_ID, "source_type": "file", "path": "/home/me/report.pdf"},
    )
    sc = result.structured_content
    token = sc["url"].rsplit("/", 1)[1]
    assert config.signer.verify(token, op="ul")["title"] == "report.pdf"
    # No mime supplied → not locked, so the agent path exposes a Content-Type knob (#1801).
    assert sc["mime_locked"] is False
    assert "Content-Type" in sc["agent_upload"]["headers"]
    # Unlocked → the copy-paste example must set Content-Type too (no server sniffing).
    assert "Content-Type" in sc["agent_upload"]["example"]


async def test_source_add_file_empty_mime_is_not_locked(mock_client, config) -> None:
    # An empty mime_type is falsy, so it is NOT signed into the token — mime_locked
    # must mirror that (bool, not `is not None`) or it would lie to the agent (#1801).
    result = await _call(
        mock_client,
        config,
        "source_add",
        {"notebook": NB_ID, "source_type": "file", "title": "Doc", "mime_type": ""},
    )
    sc = result.structured_content
    assert "mime" not in config.signer.verify(sc["url"].rsplit("/", 1)[1], op="ul")
    assert sc["mime_locked"] is False
    assert "Content-Type" in sc["agent_upload"]["headers"]


async def test_source_add_file_http_without_config_is_not_configured_error(
    monkeypatch, mock_client
) -> None:
    # Force the http-transport branch while file transfer is unset.
    monkeypatch.setattr(src_mod, "get_http_request", lambda: MagicMock())
    with pytest.raises(ToolError) as excinfo:
        await _call(
            mock_client,
            None,
            "source_add",
            {"notebook": NB_ID, "source_type": "file", "path": "/x.pdf"},
        )
    assert "not configured" in str(excinfo.value)
    assert "NOTEBOOKLM_MCP_PUBLIC_URL" in str(excinfo.value)


async def test_source_add_file_stdio_keeps_path_behavior(mock_client) -> None:
    # No config + stdio (get_http_request raises) → the existing local-path add.
    mock_client.sources.add_text = AsyncMock(return_value=FakeSource(id="s1", title="T"))
    # A non-existent, non-path-shaped string falls back to text ingest in the core;
    # use a real-ish behavior by mocking add_file via an existing-file-free path is
    # awkward, so assert the path is REQUIRED instead (the clearest stdio contract).
    with pytest.raises(ToolError) as excinfo:
        await _call(mock_client, None, "source_add", {"notebook": NB_ID, "source_type": "file"})
    assert "requires 'path'" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# source_add(source_type="file", bytes_base64=…) — in-channel small-file byte
# upload (#1803), folded into source_add by #1890 (was source_upload_bytes).
# --------------------------------------------------------------------------- #
async def test_source_add_bytes_adds_and_echoes_source(mock_client) -> None:
    # The decoded bytes are spooled to a private 0600 temp file and handed to the
    # SAME add_file path source_add uses; the added source is echoed with labels.
    seen: dict[str, Any] = {}

    async def _capture(nb_id, path, mime, *, title=None):
        seen["nb"] = nb_id
        seen["path"] = path
        seen["mime"] = mime
        seen["title"] = title
        with open(path, "rb") as fh:
            seen["bytes"] = fh.read()
        seen["mode"] = oct(os.stat(path).st_mode & 0o777)
        return FakeReadyPdf(id="src-1", title="report.pdf")

    mock_client.sources.add_file = AsyncMock(side_effect=_capture)
    result = await _call(
        mock_client,
        None,  # transport-agnostic: needs no file-transfer config
        "source_add",
        {
            "notebook": NB_ID,
            "source_type": "file",
            "bytes_base64": base64.b64encode(b"%PDF-1.4 hello").decode(),
            "filename": "report.pdf",
            "mime_type": "application/pdf",
        },
    )
    sc = result.structured_content
    assert sc["status"] == "added"
    # Echoes the resolved canonical notebook_id, like source_add (#1808).
    assert sc["notebook_id"] == NB_ID
    assert sc["source"]["id"] == "src-1"
    # Same enriched echo as source_add (kind + status_label), not a bare id.
    assert sc["source"]["kind"] == "pdf"
    assert sc["source"]["status_label"] == "ready"
    # The exact decoded bytes reached disk, under the spooled basename.
    assert seen["bytes"] == b"%PDF-1.4 hello"
    # 0600 is a POSIX guarantee; Windows doesn't honor Unix mode bits (os.open there
    # yields 0o666), so only assert the spool file's perms off-Windows.
    if os.name != "nt":
        assert seen["mode"] == "0o600"
    assert seen["nb"] == NB_ID
    assert os.path.isabs(seen["path"])
    assert os.path.basename(seen["path"]) == "report.pdf"
    assert seen["mime"] == "application/pdf"
    assert seen["title"] is None
    # The temp tree is cleaned up after the add returns (nothing left on disk).
    assert not os.path.exists(seen["path"])


async def test_source_add_bytes_default_filename_and_no_mime(mock_client) -> None:
    seen: dict[str, Any] = {}

    async def _capture(nb_id, path, mime, *, title=None):
        seen["path"] = path
        seen["mime"] = mime
        return FakeReadyPdf(id="s")

    mock_client.sources.add_file = AsyncMock(side_effect=_capture)
    await _call(
        mock_client,
        None,
        "source_add",
        {
            "notebook": NB_ID,
            "source_type": "file",
            "bytes_base64": base64.b64encode(b"data").decode(),
        },
    )
    # No filename → the shared safe-name default; no mime → None passed through.
    assert os.path.basename(seen["path"]) == "upload.bin"
    assert seen["mime"] is None


async def test_source_add_bytes_sanitizes_traversal_filename(mock_client) -> None:
    seen: dict[str, Any] = {}

    async def _capture(nb_id, path, mime, *, title=None):
        seen["path"] = path
        return FakeReadyPdf(id="s")

    mock_client.sources.add_file = AsyncMock(side_effect=_capture)
    await _call(
        mock_client,
        None,
        "source_add",
        {
            "notebook": NB_ID,
            "source_type": "file",
            "bytes_base64": base64.b64encode(b"x").decode(),
            "filename": "../../etc/passwd",
        },
    )
    # safe_upload_name basenames the path — no escape from the temp dir.
    assert os.path.basename(seen["path"]) == "passwd"
    assert "/etc/passwd" not in seen["path"]


async def test_source_add_bytes_tolerates_wrapped_base64(mock_client) -> None:
    # 76-col-wrapped (MIME-style) base64 with newlines still decodes to the exact bytes.
    seen: dict[str, Any] = {}

    async def _capture(nb_id, path, mime, *, title=None):
        with open(path, "rb") as fh:
            seen["bytes"] = fh.read()
        return FakeReadyPdf(id="s")

    mock_client.sources.add_file = AsyncMock(side_effect=_capture)
    raw = bytes(range(120)) * 3
    wrapped = "\n".join(textwrap.wrap(base64.b64encode(raw).decode(), 76))
    await _call(
        mock_client,
        None,
        "source_add",
        {"notebook": NB_ID, "source_type": "file", "bytes_base64": wrapped, "filename": "blob.bin"},
    )
    assert seen["bytes"] == raw


async def test_source_add_bytes_accepts_wrapped_base64_near_cap(mock_client) -> None:
    # Regression: the cap is applied to the WHITESPACE-STRIPPED base64, not the raw
    # string. A valid wrapped payload whose raw length (incl. newlines) exceeds the
    # cap but whose cleaned length is within it must be ACCEPTED — an earlier draft
    # checked the raw length and wrongly rejected near-cap wrapped payloads.
    seen: dict[str, Any] = {}

    async def _capture(nb_id, path, mime, *, title=None):
        with open(path, "rb") as fh:
            seen["bytes"] = fh.read()
        return FakeReadyPdf(id="s")

    mock_client.sources.add_file = AsyncMock(side_effect=_capture)
    raw = os.urandom(7425)  # -> 9900 base64 chars (divisible by 3, no padding)
    wrapped = "\n".join(textwrap.wrap(base64.b64encode(raw).decode(), 76))
    cap = fileupload_mod._MAX_UPLOAD_B64_CHARS
    assert len(wrapped) > cap  # raw (with newlines) exceeds the cap ...
    assert len("".join(wrapped.split())) <= cap  # ... but the cleaned base64 is within it
    await _call(
        mock_client,
        None,
        "source_add",
        {"notebook": NB_ID, "source_type": "file", "bytes_base64": wrapped, "filename": "b.bin"},
    )
    assert seen["bytes"] == raw


async def test_source_add_bytes_rejects_oversized_before_add(mock_client) -> None:
    # A payload over the base64-char cap is rejected up front — no add_file call.
    mock_client.sources.add_file = AsyncMock()
    big = base64.b64encode(os.urandom(9000)).decode()  # ~12000 chars > 10000 cap
    assert len(big) > fileupload_mod._MAX_UPLOAD_B64_CHARS
    with pytest.raises(ToolError) as excinfo:
        await _call(
            mock_client,
            None,
            "source_add",
            {"notebook": NB_ID, "source_type": "file", "bytes_base64": big},
        )
    msg = str(excinfo.value)
    assert "cap" in msg
    # The error names the signed-URL fallback the agent should take instead.
    assert "source_add" in msg
    mock_client.sources.add_file.assert_not_awaited()


async def test_source_add_bytes_rejects_malformed_base64(mock_client) -> None:
    mock_client.sources.add_file = AsyncMock()
    with pytest.raises(ToolError) as excinfo:
        await _call(
            mock_client,
            None,
            "source_add",
            {"notebook": NB_ID, "source_type": "file", "bytes_base64": "!!! not base64 !!!"},
        )
    assert "not valid base64" in str(excinfo.value)
    mock_client.sources.add_file.assert_not_awaited()


@pytest.mark.parametrize("payload", ["", "   \n\t  "])
async def test_source_add_bytes_rejects_empty_or_whitespace_payload(mock_client, payload) -> None:
    # Both an empty AND an all-whitespace payload decode to zero bytes (whitespace is
    # stripped before decode) — rejected before any add.
    mock_client.sources.add_file = AsyncMock()
    with pytest.raises(ToolError) as excinfo:
        await _call(
            mock_client,
            None,
            "source_add",
            {"notebook": NB_ID, "source_type": "file", "bytes_base64": payload},
        )
    assert "no bytes" in str(excinfo.value)
    mock_client.sources.add_file.assert_not_awaited()


async def test_source_add_bytes_accepts_exact_cap_boundary(mock_client) -> None:
    # The cap is `> _MAX_UPLOAD_B64_CHARS` — a payload of EXACTLY that length is
    # accepted (7500 bytes → 10000 base64 chars, no padding). Guards the >/>= boundary.
    seen: dict[str, Any] = {}

    async def _capture(nb_id, path, mime, *, title=None):
        seen["ok"] = True
        return FakeReadyPdf(id="s")

    mock_client.sources.add_file = AsyncMock(side_effect=_capture)
    payload = base64.b64encode(os.urandom(7500)).decode()
    assert len(payload) == fileupload_mod._MAX_UPLOAD_B64_CHARS
    await _call(
        mock_client,
        None,
        "source_add",
        {"notebook": NB_ID, "source_type": "file", "bytes_base64": payload, "filename": "b.bin"},
    )
    assert seen.get("ok")


async def test_source_add_bytes_passes_title_through(mock_client) -> None:
    # An explicit title reaches the add path (vs the filename-derived default).
    seen: dict[str, Any] = {}

    async def _capture(nb_id, path, mime, *, title=None):
        seen["title"] = title
        return FakeReadyPdf(id="s")

    mock_client.sources.add_file = AsyncMock(side_effect=_capture)
    await _call(
        mock_client,
        None,
        "source_add",
        {
            "notebook": NB_ID,
            "source_type": "file",
            "bytes_base64": base64.b64encode(b"x").decode(),
            "filename": "x.bin",
            "title": "My Title",
        },
    )
    assert seen["title"] == "My Title"


async def test_source_add_bytes_cleans_up_temp_on_add_error(mock_client) -> None:
    # The finally rmtree is this tool's core safety guarantee — the docstring promises
    # removal "on success, a rejected add, or an error". Capture the spool path, then
    # make the add itself raise, and assert both the file and its mkdtemp parent are gone.
    captured: dict[str, Any] = {}

    async def _boom(nb_id, path, mime, *, title=None):
        captured["path"] = path
        assert os.path.exists(path)  # the spooled file is present DURING the add
        raise RuntimeError("upstream add blew up")

    mock_client.sources.add_file = AsyncMock(side_effect=_boom)
    with pytest.raises(ToolError):
        await _call(
            mock_client,
            None,
            "source_add",
            {
                "notebook": NB_ID,
                "source_type": "file",
                "bytes_base64": base64.b64encode(b"data").decode(),
                "filename": "x.bin",
            },
        )
    assert "path" in captured  # the add was actually reached
    assert not os.path.exists(captured["path"])
    assert not os.path.exists(os.path.dirname(captured["path"]))


async def test_source_add_bytes_rejects_non_file_source_type(mock_client) -> None:
    # bytes_base64 is a `file` input mode only; pairing it with another source_type
    # is a VALIDATION error raised before any notebook I/O (#1890).
    mock_client.sources.add_url = AsyncMock()
    with pytest.raises(ToolError) as excinfo:
        await _call(
            mock_client,
            None,
            "source_add",
            {
                "notebook": NB_ID,
                "source_type": "url",
                "bytes_base64": base64.b64encode(b"x").decode(),
            },
        )
    msg = str(excinfo.value)
    assert "VALIDATION" in msg
    assert "bytes_base64" in msg
    mock_client.sources.add_url.assert_not_called()


async def test_source_add_bytes_and_path_are_mutually_exclusive(mock_client) -> None:
    # path and bytes_base64 are two ways to supply the SAME file input — reject both.
    mock_client.sources.add_file = AsyncMock()
    with pytest.raises(ToolError) as excinfo:
        await _call(
            mock_client,
            None,
            "source_add",
            {
                "notebook": NB_ID,
                "source_type": "file",
                "path": "/tmp/doc.pdf",
                "bytes_base64": base64.b64encode(b"x").decode(),
            },
        )
    assert "VALIDATION" in str(excinfo.value)
    mock_client.sources.add_file.assert_not_called()


async def test_source_add_filename_without_bytes_is_rejected(mock_client) -> None:
    # filename only seeds the byte spool's title/extension — meaningless without
    # bytes_base64 (a stdio path's basename comes from `path`).
    mock_client.sources.add_file = AsyncMock()
    with pytest.raises(ToolError) as excinfo:
        await _call(
            mock_client,
            None,
            "source_add",
            {"notebook": NB_ID, "source_type": "file", "filename": "x.bin"},
        )
    msg = str(excinfo.value)
    assert "VALIDATION" in msg
    assert "filename" in msg
    mock_client.sources.add_file.assert_not_called()


# --------------------------------------------------------------------------- #
# studio_download
# --------------------------------------------------------------------------- #
async def test_artifact_download_with_config_returns_resource_link(mock_client, config) -> None:
    result = await _call(
        mock_client,
        config,
        "studio_download",
        {"notebook": NB_ID, "artifact_type": "audio"},
    )
    sc = result.structured_content
    assert sc["status"] == "download_ready"
    assert sc["url"].startswith(f"{BASE}/files/dl/")
    assert sc["artifact_type"] == "audio"
    # Presentation metadata enriches the payload (#1826): a latest-by-type download
    # has no known title, so the filename falls back to the type name + extension,
    # the mime comes from the central table, and size is unknown up front.
    assert sc["filename"] == "audio.mp3"
    assert sc["mime_type"] == "audio/mpeg"
    assert sc["size_bytes"] is None
    # A clickable resource_link content item is included for claude.ai.
    assert any(getattr(block, "type", None) == "resource_link" for block in result.content)
    token = sc["url"].rsplit("/", 1)[1]
    payload = config.signer.verify(token, op="dl")
    assert payload["atype"] == "audio"


async def test_artifact_download_with_config_carries_format(mock_client, config) -> None:
    result = await _call(
        mock_client,
        config,
        "studio_download",
        {"notebook": NB_ID, "artifact_type": "quiz", "output_format": "markdown"},
    )
    token = result.structured_content["url"].rsplit("/", 1)[1]
    assert config.signer.verify(token, op="dl")["fmt"] == "markdown"


async def test_artifact_download_slide_deck_pptx_metadata(mock_client, config) -> None:
    # A slide-deck pptx download advertises the pptx extension + Office MIME (#1826).
    # Latest-by-type (no id) → the type-name filename fallback, no list issued.
    mock_client.artifacts.list = AsyncMock(return_value=[])
    result = await _call(
        mock_client,
        config,
        "studio_download",
        {"notebook": NB_ID, "artifact_type": "slide-deck", "output_format": "pptx"},
    )
    sc = result.structured_content
    assert sc["filename"] == "slide-deck.pptx"
    assert sc["mime_type"] == (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    assert sc["size_bytes"] is None
    mock_client.artifacts.list.assert_not_awaited()


async def test_artifact_download_quiz_markdown_metadata(mock_client, config) -> None:
    # A quiz markdown download advertises the .md extension + text/markdown MIME (#1826).
    result = await _call(
        mock_client,
        config,
        "studio_download",
        {"notebook": NB_ID, "artifact_type": "quiz", "output_format": "markdown"},
    )
    sc = result.structured_content
    assert sc["filename"] == "quiz.md"
    assert sc["mime_type"] == "text/markdown"


async def test_artifact_download_explicit_id_pptx_uses_title(mock_client, config) -> None:
    # Explicit id + a format override: the filename combines the resolved title with
    # the format-selected extension, and the MIME matches (#1826).
    deck = Artifact(
        id=_AID_A,
        title="Q3 Deck",
        _artifact_type=ArtifactTypeCode.SLIDE_DECK.value,
        status=int(ArtifactStatus.COMPLETED),
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    mock_client.artifacts.list = AsyncMock(return_value=[deck])
    result = await _call(
        mock_client,
        config,
        "studio_download",
        {
            "notebook": NB_ID,
            "artifact_type": "slide-deck",
            "artifact_id": _AID_A,
            "output_format": "pptx",
        },
    )
    sc = result.structured_content
    assert sc["filename"] == "Q3 Deck.pptx"
    assert sc["mime_type"] == (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )


async def test_artifact_download_config_rejects_bad_format(mock_client, config) -> None:
    with pytest.raises(ToolError) as excinfo:
        await _call(
            mock_client,
            config,
            "studio_download",
            {"notebook": NB_ID, "artifact_type": "audio", "output_format": "pdf"},
        )
    msg = str(excinfo.value)
    assert "VALIDATION" in msg
    # The remote signed-URL path shares studio.py's validation, so it emits the same
    # self-documenting text as the stdio path (see the sibling test in test_studio.py).
    assert "supported formats: default only" in msg
    assert "omit output_format" in msg


async def test_artifact_download_config_rejects_invalid_format_value(mock_client, config) -> None:
    # A bad VALUE for a type that DOES have a format axis must fail at mint time
    # (both transports), not mint a token whose link only 500s when opened.
    with pytest.raises(ToolError) as excinfo:
        await _call(
            mock_client,
            config,
            "studio_download",
            {"notebook": NB_ID, "artifact_type": "slide-deck", "output_format": "docx"},
        )
    assert "validation error" in str(excinfo.value)


async def test_artifact_download_http_without_config_is_not_configured_error(
    monkeypatch, mock_client
) -> None:
    monkeypatch.setattr(art_mod, "get_http_request", lambda: MagicMock())
    with pytest.raises(ToolError) as excinfo:
        await _call(
            mock_client,
            None,
            "studio_download",
            {"notebook": NB_ID, "artifact_type": "audio"},
        )
    assert "not configured" in str(excinfo.value)


async def test_artifact_download_http_without_config_with_path_still_not_configured(
    monkeypatch, mock_client
) -> None:
    # Regression: a supplied `path` on remote-without-config must NOT fall through
    # to a server-side download (writing to an unreachable server path) — it must
    # report "not configured", mirroring source_add type=file.
    monkeypatch.setattr(art_mod, "get_http_request", lambda: MagicMock())
    with pytest.raises(ToolError) as excinfo:
        await _call(
            mock_client,
            None,
            "studio_download",
            {"notebook": NB_ID, "artifact_type": "audio", "path": "/tmp/out.mp3"},
        )
    assert "not configured" in str(excinfo.value)


async def test_artifact_download_stdio_missing_path_is_clear_error(mock_client) -> None:
    # stdio (no config, get_http_request raises) without a path → a clear error.
    with pytest.raises(ToolError) as excinfo:
        await _call(
            mock_client,
            None,
            "studio_download",
            {"notebook": NB_ID, "artifact_type": "audio"},
        )
    assert "requires 'path'" in str(excinfo.value)
    assert "stdio" in str(excinfo.value)


# The stdio path-download happy path (file_transfer absent) is already covered by
# ``test_studio.py::test_artifact_download_audio`` (its server has no file
# transfer), so it is not duplicated here.


async def test_artifact_download_remote_tool_encodes_aid(mock_client, config) -> None:
    # under http transport (with config), an EXPLICIT artifact_id is pre-validated
    # against the type-scoped list BEFORE the signed URL is minted, then the canonical
    # id is encoded in the token.
    mock_client.artifacts.list = AsyncMock(return_value=[_audio_artifact(_AID_A)])
    result = await _call(
        mock_client,
        config,
        "studio_download",
        {"notebook": NB_ID, "artifact_type": "audio", "artifact_id": _AID_A},
    )
    sc = result.structured_content
    assert sc["status"] == "download_ready"
    # The structured payload echoes the targeted id (self-describing), not only the
    # token.
    assert sc["artifact_id"] == _AID_A
    # An explicit id resolves the artifact's title cheaply from the same list, so the
    # advertised filename is derived from it (not the type-name fallback) (#1826).
    assert sc["filename"] == "Podcast.mp3"
    assert sc["mime_type"] == "audio/mpeg"
    url = sc["url"]
    token = url.split("/")[-1]
    payload = config.signer.verify(token, op="dl")
    assert payload["aid"] == _AID_A
    # Pre-validation used the SAME type-scoped fetch the remote route resolves over
    # (skips the mind-map sub-fetch for a non-mind-map kind).
    mock_client.artifacts.list.assert_awaited_once_with(NB_ID, ArtifactType.AUDIO)


async def test_artifact_download_remote_mind_map_uses_type_scoped_list(mock_client, config) -> None:
    # A note-backed mind-map id under artifact_type="mind-map" pre-validates over the
    # SAME type-scoped fetch the route uses: list(nb_id, MIND_MAP) (which merges the
    # note-backed rows), locking the faithfulness the code comment claims.
    mm = Artifact(
        id=_AID_A,
        title="My Map",
        _artifact_type=ArtifactTypeCode.MIND_MAP.value,
        status=int(ArtifactStatus.COMPLETED),
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    mock_client.artifacts.list = AsyncMock(return_value=[mm])
    result = await _call(
        mock_client,
        config,
        "studio_download",
        {"notebook": NB_ID, "artifact_type": "mind-map", "artifact_id": _AID_A},
    )
    assert result.structured_content["status"] == "download_ready"
    assert result.structured_content["artifact_id"] == _AID_A
    mock_client.artifacts.list.assert_awaited_once_with(NB_ID, ArtifactType.MIND_MAP)


async def test_artifact_download_remote_ref_to_incomplete_does_not_mint(
    mock_client, config
) -> None:
    # A `artifact` ref that resolves to a real-but-still-generating artifact must NOT
    # mint a URL on remote (it would 400 when opened, since the route serves only
    # completed artifacts) — it fails up front with a clear structured error.
    mock_client.artifacts.list = AsyncMock(
        return_value=[_audio_artifact(_AID_A, "Podcast", completed=False)]
    )
    with pytest.raises(ToolError) as excinfo:
        await _call(
            mock_client,
            config,
            "studio_download",
            {"notebook": NB_ID, "artifact": "Podcast"},
        )
    assert "not finished generating" in str(excinfo.value)


async def test_artifact_download_remote_uppercase_aid_canonicalized(mock_client, config) -> None:
    # An UPPERCASE full-UUID artifact_id is canonicalized to the list's lowercase id
    # before minting (the token must carry the canonical id, not the caller's casing).
    mock_client.artifacts.list = AsyncMock(return_value=[_audio_artifact(_AID_A)])
    result = await _call(
        mock_client,
        config,
        "studio_download",
        {"notebook": NB_ID, "artifact_type": "audio", "artifact_id": _AID_A.upper()},
    )
    sc = result.structured_content
    assert sc["status"] == "download_ready"
    assert sc["artifact_id"] == _AID_A  # canonical lowercase, not the uppercase input
    token = sc["url"].split("/")[-1]
    assert config.signer.verify(token, op="dl")["aid"] == _AID_A


async def test_artifact_download_remote_unknown_aid_fails_before_mint(mock_client, config) -> None:
    # A full-UUID artifact_id absent from the list fails at tool-call time (structured
    # error) — no download_ready URL is handed out.
    mock_client.artifacts.list = AsyncMock(return_value=[_audio_artifact(_AID_A)])
    with pytest.raises(ToolError) as excinfo:
        await _call(
            mock_client,
            config,
            "studio_download",
            {
                "notebook": NB_ID,
                "artifact_type": "audio",
                "artifact_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
            },
        )
    assert "not found" in str(excinfo.value)


async def test_artifact_download_remote_incomplete_aid_excluded(mock_client, config) -> None:
    # A full id that exists but is NOT completed is dropped by the is_completed filter,
    # yet surfaces the SAME actionable "not finished generating" message the ref path
    # gives (detected from the already-fetched list, no browser 400, no extra RPC).
    mock_client.artifacts.list = AsyncMock(return_value=[_audio_artifact(_AID_A, completed=False)])
    with pytest.raises(ToolError) as excinfo:
        await _call(
            mock_client,
            config,
            "studio_download",
            {"notebook": NB_ID, "artifact_type": "audio", "artifact_id": _AID_A},
        )
    assert "not finished generating" in str(excinfo.value)


async def test_artifact_download_remote_ambiguous_aid_prefix_fails_before_mint(
    mock_client, config
) -> None:
    # Two audio artifacts sharing a prefix + that prefix as artifact_id → ambiguous,
    # fails at mint time (no URL).
    mock_client.artifacts.list = AsyncMock(
        return_value=[
            _audio_artifact("cccccccc-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "A"),
            _audio_artifact("cccccccc-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "B"),
        ]
    )
    with pytest.raises(ToolError) as excinfo:
        await _call(
            mock_client,
            config,
            "studio_download",
            {"notebook": NB_ID, "artifact_type": "audio", "artifact_id": "cccccccc"},
        )
    assert "Ambiguous ID" in str(excinfo.value)


async def test_artifact_download_remote_latest_skips_prevalidation(mock_client, config) -> None:
    # The "latest" path (no artifact_id) must NOT pre-validate — it mints even with an
    # empty list (the route resolves "latest" when the link is opened).
    mock_client.artifacts.list = AsyncMock(return_value=[])
    result = await _call(
        mock_client,
        config,
        "studio_download",
        {"notebook": NB_ID, "artifact_type": "audio"},
    )
    assert result.structured_content["status"] == "download_ready"
    # No explicit id → the pre-validation branch never ran.
    mock_client.artifacts.list.assert_not_awaited()


async def test_artifact_download_remote_ref_path_not_double_validated(mock_client, config) -> None:
    # The `artifact` name/id ref path resolves + derives the type via a single
    # list(nb_id) (no type argument); the explicit-id pre-validation branch must NOT
    # fire (it would be a second list carrying spec.kind). Assert on CALL ARGS, not a
    # bare await count: no artifacts.list call passed a second (artifact_type) positional.
    mock_client.artifacts.list = AsyncMock(return_value=[_audio_artifact(_AID_A, "Podcast")])
    result = await _call(
        mock_client,
        config,
        "studio_download",
        {"notebook": NB_ID, "artifact": "Podcast"},
    )
    assert result.structured_content["status"] == "download_ready"
    assert result.structured_content["artifact_id"] == _AID_A
    # No list call carried the pre-validation's distinctive type-scoped positional.
    assert all(len(call.args) < 2 for call in mock_client.artifacts.list.await_args_list)
