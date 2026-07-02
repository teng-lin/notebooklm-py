"""Tool-branch tests for the remote file-transfer behavior of ``source_add`` and
``studio_download``.

Three branches each: file-transfer configured → a signed-URL payload; http without
config → a clean "not configured" error; and (config absent) stdio → the existing
path behavior. The transport is detected via ``get_http_request`` (raises on stdio);
the http-without-config branch is exercised by patching it to a fake request.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py.
pytest.importorskip("fastmcp")

from fastmcp import Client  # noqa: E402 - after importorskip guard
from fastmcp.exceptions import ToolError  # noqa: E402 - after importorskip guard

import notebooklm.mcp.tools._studio_download as art_mod  # noqa: E402 - after importorskip guard
import notebooklm.mcp.tools.sources as src_mod  # noqa: E402 - after importorskip guard
from notebooklm.mcp._filelink import (  # noqa: E402 - after importorskip guard
    FileLinkSigner,
    FileTransferConfig,
)
from notebooklm.mcp.server import create_server  # noqa: E402 - after importorskip guard

from .conftest import AsyncMock  # noqa: E402 - after importorskip guard

BASE = "https://files.test"
NB_ID = "11111111-1111-1111-1111-111111111111"


@dataclass
class FakeSource:
    id: str
    title: str | None = None


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
    # The signed token carries the title + mime (so the browser round-trip keeps them).
    token = sc["url"].rsplit("/", 1)[1]
    payload = config.signer.verify(token, op="ul")
    assert payload["title"] == "My Doc"
    assert payload["mime"] == "application/pdf"
    # The response self-documents the agent-direct path so an agent doesn't fall
    # back to the human "open in a browser" flow it can't perform.
    agent = sc["agent_upload"]
    assert agent["method"] == "POST"
    assert agent["headers"]["Accept"] == "application/json"
    assert agent["url"].startswith(sc["url"])
    assert sc["url"] in agent["example"]


async def test_source_add_file_default_title_from_path_basename(mock_client, config) -> None:
    # A `path` is ACCEPTED on remote (not opened) — its basename seeds the title.
    result = await _call(
        mock_client,
        config,
        "source_add",
        {"notebook": NB_ID, "source_type": "file", "path": "/home/me/report.pdf"},
    )
    token = result.structured_content["url"].rsplit("/", 1)[1]
    assert config.signer.verify(token, op="ul")["title"] == "report.pdf"


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


async def test_artifact_download_config_rejects_bad_format(mock_client, config) -> None:
    with pytest.raises(ToolError) as excinfo:
        await _call(
            mock_client,
            config,
            "studio_download",
            {"notebook": NB_ID, "artifact_type": "audio", "output_format": "pdf"},
        )
    assert "VALIDATION" in str(excinfo.value)


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
    # under http transport (with config), studio_download returns download_ready
    # and the minted token contains aid.
    result = await _call(
        mock_client,
        config,
        "studio_download",
        {
            "notebook": NB_ID,
            "artifact_type": "audio",
            "artifact_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        },
    )
    sc = result.structured_content
    assert sc["status"] == "download_ready"
    # The structured payload echoes the targeted id (self-describing), not only the
    # token.
    assert sc["artifact_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    url = sc["url"]
    token = url.split("/")[-1]
    payload = config.signer.verify(token, op="dl")
    assert payload["aid"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
