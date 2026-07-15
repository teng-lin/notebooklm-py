"""Tests for the experimental in-app MCP-App upload widget (``_uploadwidget``).

The widget is opt-in (``NOTEBOOKLM_MCP_UPLOAD_WIDGET=1``): it must stay OUT of the default
tool surface, and when enabled it must emit the host-specific render gates (``_meta.ui.domain``,
the flat ``ui/resourceUri`` key, the ``text/html;profile=mcp-app`` mime) that claude.ai requires.
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastmcp")

from notebooklm.mcp._filelink import FileLinkSigner, FileTransferConfig  # noqa: E402
from notebooklm.mcp._uploadwidget import _WIDGET_HTML, _widget_domain  # noqa: E402
from notebooklm.mcp.server import create_server  # noqa: E402

_BASE = "https://notebooklm-test.example"
_WIDGET_URI = "ui://notebooklm/upload-v1"


def _server(config: FileTransferConfig | None):
    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[MagicMock]:
        yield MagicMock()

    return create_server(client_factory=factory, file_transfer=config)


def _cfg() -> FileTransferConfig:
    return FileTransferConfig(signer=FileLinkSigner(b"k" * 32), base_url=_BASE)


def test_widget_html_is_cross_host() -> None:
    # Renders + acquires the tool result on both claude.ai/Grok (postMessage) and ChatGPT
    # (window.openai.toolOutput), with the unconditional initialized handshake and a universal
    # <input type=file> + direct POST to the upload_url.
    for marker in (
        'method:"ui/notifications/initialized"',  # claude.ai render gate
        "window.openai",  # ChatGPT bridge
        "oai.toolOutput",  # ChatGPT tool-result path
        'addEventListener("message"',  # claude.ai/Grok tool-result path
        'type="file"',  # universal picker
        "?filename=",  # direct-PUT to /files/ul
    ):
        assert marker in _WIDGET_HTML, marker


def test_widget_domain_is_sha256_of_endpoint() -> None:
    expected = hashlib.sha256(f"{_BASE}/mcp".encode()).hexdigest()[:32] + ".claudemcpcontent.com"
    assert _widget_domain(_BASE) == expected
    assert _widget_domain(_BASE + "/") == expected  # trailing slash normalized


async def test_widget_absent_by_default(monkeypatch) -> None:
    monkeypatch.delenv("NOTEBOOKLM_MCP_UPLOAD_WIDGET", raising=False)
    mcp = _server(_cfg())
    names = {t.name for t in await mcp._list_tools()}
    assert "source_add_widget" not in names  # opt-in: never in the default surface


async def test_widget_absent_without_file_transfer(monkeypatch) -> None:
    monkeypatch.setenv("NOTEBOOKLM_MCP_UPLOAD_WIDGET", "1")
    mcp = _server(None)  # no public URL → no widget even with the flag
    names = {t.name for t in await mcp._list_tools()}
    assert "source_add_widget" not in names


async def test_widget_registers_with_claudeai_render_gates(monkeypatch) -> None:
    monkeypatch.setenv("NOTEBOOKLM_MCP_UPLOAD_WIDGET", "1")
    cfg = _cfg()
    mcp = _server(cfg)

    tools = {t.name: t for t in await mcp._list_tools()}
    assert "source_add_widget" in tools
    # NOT read-only: it mints an upload_url that adds a source (mutation). A readOnlyHint would let
    # hosts auto-invoke it without the consent capability-creation warrants.
    ann = tools["source_add_widget"].annotations
    assert ann is None or not getattr(ann, "readOnlyHint", False)
    meta = tools["source_add_widget"].meta or {}
    # BOTH the flat key (what claude.ai reads) and the spec-nested form.
    assert meta.get("ui/resourceUri") == _WIDGET_URI
    assert meta.get("ui", {}).get("resourceUri") == _WIDGET_URI
    assert meta.get("ui", {}).get("visibility") == ["model"]

    # ChatGPT (Apps SDK) reads openai/outputTemplate — pointed at the SAME single resource,
    # because claude.ai follows this key too and can't render a separate skybridge mime.
    assert meta.get("openai/outputTemplate") == _WIDGET_URI

    resources = {str(r.uri): r for r in await mcp._list_resources()}
    assert "ui://notebooklm/upload-openai-v1" not in resources  # collapsed to one resource
    res = resources[_WIDGET_URI]
    assert res.mime_type == "text/html;profile=mcp-app"  # the standard both hosts accept
    ui = (res.meta or {}).get("ui", {})
    assert ui.get("domain") == _widget_domain(_BASE)  # the claude.ai render gate
    assert ui.get("csp", {}).get("connectDomains") == [_BASE]  # widget → /files/ul allowed
    # ChatGPT reads openai/widgetCSP off the same resource.
    assert (res.meta or {}).get("openai/widgetCSP", {}).get("connect_domains") == [_BASE]
