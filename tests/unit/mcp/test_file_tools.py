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

from datetime import datetime, timezone  # noqa: E402 - after importorskip guard

from fastmcp import Client  # noqa: E402 - after importorskip guard
from fastmcp.exceptions import ToolError  # noqa: E402 - after importorskip guard

import notebooklm.mcp.tools._studio_download as art_mod  # noqa: E402 - after importorskip guard
import notebooklm.mcp.tools.sources as src_mod  # noqa: E402 - after importorskip guard
from notebooklm._types.artifacts import (  # noqa: E402 - after importorskip guard
    ArtifactStatus,
    ArtifactTypeCode,
)
from notebooklm.mcp._filelink import (  # noqa: E402 - after importorskip guard
    FileLinkSigner,
    FileTransferConfig,
)
from notebooklm.mcp.server import create_server  # noqa: E402 - after importorskip guard
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
