"""Layer-B e2e: the MCP **HTTP transport + bearer gate + signed-URL file routes**
against the real NotebookLM API, driven entirely in-process over
``httpx.ASGITransport`` (no socket, no subprocess).

Follows the proven repo pattern at ``tests/unit/mcp/test_remote_auth.py`` — the
only difference is that the ``client_factory`` yields the LIVE e2e ``client``
fixture instead of a stub, so tool calls hit real Google while still exercising:

* the bearer auth gate on ``/mcp`` (unauth → 401, bearer → tools),
* the ``.well-known`` discovery metadata,
* the ``/files/ul`` + ``/files/dl`` signed-URL routes (the claude.ai connector's
  out-of-band upload/download channel), including the
  custom-route-bypasses-bearer tripwire (``/files/ul/<bad>`` → **403**, not 401).

Requires auth and the ``mcp`` extra (``importorskip``); auto-marked ``e2e`` by
``conftest.pytest_itemcollected``.
"""

from __future__ import annotations

import os

import pytest

# Require the `mcp` extra; skip the whole module cleanly when fastmcp is absent.
pytest.importorskip("fastmcp")

from notebooklm.mcp._filelink import (  # noqa: E402 - after importorskip guard
    FileLinkSigner,
    FileTransferConfig,
)

from ._mcp_live_helpers import (  # noqa: E402 - after importorskip guard
    pick_downloadable_artifact,
)
from .conftest import (  # noqa: E402 - after importorskip guard
    asgi_path,
    inprocess_mcp_server,
    requires_auth,
)

pytestmark = pytest.mark.e2e

# A bare-https origin satisfies ``FileTransferConfig`` without going through
# ``__main__._build_file_transfer`` (which would re-validate); the in-process
# ASGI client routes by path, so the host never has to be reachable.
_PUBLIC_BASE_URL = "https://127.0.0.1"


def _file_transfer() -> FileTransferConfig:
    return FileTransferConfig(FileLinkSigner(os.urandom(32)), _PUBLIC_BASE_URL)


@requires_auth
class TestMcpHttpAuthGate:
    """The bearer gate on the in-process HTTP transport."""

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_unauthenticated_mcp_is_rejected(self, client):
        """A ``/mcp`` request with no bearer is rejected (401) before any tool."""
        async with inprocess_mcp_server(client) as im, im.raw_client() as raw:
            resp = await raw.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Accept": "application/json, text/event-stream"},
            )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_bearer_lists_tools_live(self, client):
        """With the correct bearer, the MCP client lists tools over the HTTP transport."""
        async with inprocess_mcp_server(client) as im, im.mcp_client() as mcp:
            tools = await mcp.list_tools()
        assert any(t.name == "notebook_list" for t in tools)


@requires_auth
class TestMcpHttpWellKnown:
    """OAuth discovery metadata is auth-type-specific."""

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_bearer_only_serves_no_oauth_metadata(self, client):
        """Bearer-only auth mounts NO OAuth discovery routes (empirically verified
        against fastmcp 3.2.0: a plain ``TokenVerifier`` serves neither the
        protected-resource nor the authorization-server metadata). Bearer clients
        (Claude Code/Desktop) carry the token directly and never run OAuth
        discovery; only the self-hosted OAuth provider (the claude.ai path, tested
        below) mounts ``.well-known`` metadata."""
        async with inprocess_mcp_server(client) as im, im.raw_client() as raw:
            for path in (
                "/.well-known/oauth-protected-resource",
                "/.well-known/oauth-authorization-server",
            ):
                resp = await raw.get(path)
                assert resp.status_code == 404, (
                    f"{path} unexpectedly served (fastmcp metadata behavior changed?): "
                    f"{resp.status_code}"
                )

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_authorization_server_metadata_with_oauth_provider(self, client):
        """An OAuth provider mounts the *authorization-server* metadata route.

        Bearer-only auth has NO authorization-server route, so this uses a real
        self-hosted OAuth provider (the claude.ai login path).
        """
        from notebooklm.mcp._oauth import OAuthConfig, build_oauth_provider

        oauth = build_oauth_provider(
            OAuthConfig(password="a-strong-random-password-1234567890", base_url=_PUBLIC_BASE_URL)
        )
        async with inprocess_mcp_server(client, oauth=oauth) as im, im.raw_client() as raw:
            resp = await raw.get("/.well-known/oauth-authorization-server")
        assert resp.status_code == 200
        assert resp.json().get("registration_endpoint")


@requires_auth
class TestMcpHttpFileRoutes:
    """Signed-URL upload/download routes against the live API."""

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_bad_upload_token_is_403_not_401(self, client):
        """``/files/ul/<bad>`` → 403 (custom routes are NOT behind the bearer gate).

        Pins the security-critical fact that the signed token — not the MCP
        bearer — is the sole auth for the file routes: an unauthenticated browser
        opening a bad link gets a 403 from the signer, never a 401.
        """
        async with (
            inprocess_mcp_server(client, file_transfer=_file_transfer()) as im,
            im.raw_client() as raw,
        ):
            resp = await raw.get("/files/ul/not-a-valid-token")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_upload_roundtrip(self, client, temp_notebook):
        """source_add(file) mints an upload URL; a raw POST adds the source live."""
        nb = temp_notebook.id
        body = b"Live MCP upload roundtrip content for the e2e suite.\n"
        async with inprocess_mcp_server(client, file_transfer=_file_transfer()) as im:
            # Mint the signed upload URL through the MCP tool over HTTP.
            async with im.mcp_client() as mcp:
                result = await mcp.call_tool(
                    "source_add",
                    {
                        "notebook": nb,
                        "source_type": "file",
                        "title": "MCP HTTP Upload",
                        "mime_type": "text/plain",
                    },
                )
            structured = result.structured_content
            assert structured["status"] == "upload_required"
            upload_path = asgi_path(structured["url"])

            # Upload the raw bytes (the browser fetch passes the filename via query).
            async with im.raw_client() as raw:
                up = await raw.post(
                    f"{upload_path}?filename=mcp-upload.txt",
                    content=body,
                    headers={"Accept": "application/json", "Content-Type": "text/plain"},
                )
            assert up.status_code == 200, up.text
            payload = up.json()
            assert payload["status"] == "added"
            source_id = payload["source_id"]
            assert source_id

            # Confirm the source landed, live, through the MCP listing.
            async with im.mcp_client() as mcp:
                listing = await mcp.call_tool("source_list", {"notebook": nb})
            sc = listing.structured_content
            assert sc is not None, "source_list returned no structured content"
            ids = [s["id"] for s in sc["sources"]]
            assert source_id in ids

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_download_roundtrip(self, client, generation_notebook_id):
        """artifact_download mints a download URL; a raw GET streams the bytes.

        Reuses an EXISTING artifact on the generation notebook (no fresh
        generation); skips cleanly when none is present.
        """
        async with inprocess_mcp_server(client, file_transfer=_file_transfer()) as im:
            async with im.mcp_client() as mcp:
                listing = await mcp.call_tool("studio_list", {"notebook": generation_notebook_id})
                sc = listing.structured_content
                assert sc is not None, "studio_list returned no structured content"
                candidate = pick_downloadable_artifact(sc["items"])
                if candidate is None:
                    pytest.skip("no existing downloadable artifact on the generation notebook")

                # A merged item's hyphenated ``type`` IS the studio_download key.
                dl_type = candidate["type"]
                result = await mcp.call_tool(
                    "studio_download",
                    {"notebook": generation_notebook_id, "artifact_type": dl_type},
                )
            structured = result.structured_content
            assert structured["status"] == "download_ready"
            download_path = asgi_path(structured["url"])

            async with im.raw_client() as raw:
                resp = await raw.get(download_path)
            assert resp.status_code == 200, resp.text
            assert len(resp.content) > 0
            assert "content-disposition" in {k.lower() for k in resp.headers}
