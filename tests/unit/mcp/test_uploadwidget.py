"""Tests for the experimental in-app MCP-App upload widget (``_uploadwidget``).

The widget is opt-in (``NOTEBOOKLM_MCP_UPLOAD_WIDGET=1``): it must stay OUT of the default
tool surface, and when enabled it must emit the host-specific render gates (``_meta.ui.domain``,
the flat ``ui/resourceUri`` key, the ``text/html;profile=mcp-app`` mime) that claude.ai requires.
"""

from __future__ import annotations

import contextlib
import hashlib
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("fastmcp")

from notebooklm.mcp import _uploadwidget  # noqa: E402
from notebooklm.mcp._clientprovider import ClientProvider  # noqa: E402
from notebooklm.mcp._filelink import (  # noqa: E402
    UPLOAD_TTL,
    WIDGET_UPLOAD_TTL,
    FileLinkSigner,
    FileTransferConfig,
)
from notebooklm.mcp._uploadwidget import (  # noqa: E402
    _MAX_WIDGET_FILES,
    _WIDGET_HTML,
    _widget_domain,
)
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
        "setInterval",  # persistent toolOutput poll — survives ChatGPT's late first-call template fetch
        "p.toolResult",  # unwrap the ui/notifications/tool-result envelope
        'addEventListener("message"',  # claude.ai/Grok tool-result path
        'type="file" multiple',  # universal picker, multi-select
        "upload_urls",  # reads the token pool (one single-use token per file)
        "?filename=",  # direct-PUT to /files/ul
        "confirmUpload",  # auto-confirm invoker (#1891)
        "callTool",  # ChatGPT auto-confirm path (window.openai.callTool)
        '"tools/call"',  # claude.ai auto-confirm path (postMessage tools/call)
    ):
        assert marker in _WIDGET_HTML, marker


def test_widget_html_auto_confirms_only_on_success() -> None:
    # #1891: the auto-confirm fires from inside the res.ok branch (a committed upload), never on a
    # failed POST — a corrupted/failed upload must not tell the model a source was added.
    assert "uploadUrls[i]=null;confirmUpload(tok)" in _WIDGET_HTML
    # It reads the confirm contract the tool returns for the arg/link...
    assert "confirmSpec=d.confirm" in _WIDGET_HTML


def test_widget_html_hard_allowlists_the_confirm_tool() -> None:
    # SECURITY: confirmSpec arrives via the un-origin-checked postMessage handler, so the tool name
    # must be hard-allowlisted — a spoofed message must not be able to redirect which tool runs.
    assert 'CONFIRM_TOOL="await_upload"' in _WIDGET_HTML
    assert "confirmSpec.tool!==CONFIRM_TOOL" in _WIDGET_HTML  # gate before invoking
    # The invocation uses the constant, never the message-supplied name.
    assert "oai.callTool(CONFIRM_TOOL,args)" in _WIDGET_HTML
    assert "name:CONFIRM_TOOL" in _WIDGET_HTML
    # callTool rejection is swallowed (no unhandled promise rejection in the host console).
    assert "oai.callTool(CONFIRM_TOOL,args).catch(" in _WIDGET_HTML


def test_widget_confirm_uses_unique_monotonic_rpc_id() -> None:
    # A multi-file batch fires confirmUpload once per file; two completions in the same millisecond
    # must NOT collide on the JSON-RPC id, so the id is a strictly-monotonic counter, not Date.now().
    assert 'id:"cf"+(++cfSeq)' in _WIDGET_HTML
    assert "let cfSeq=0" in _WIDGET_HTML
    assert 'id:"cf"+Date.now()' not in _WIDGET_HTML  # the collision-prone form must be gone


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


async def test_widget_tool_returns_single_use_token_pool(monkeypatch) -> None:
    """source_add_widget mints a POOL of distinct single-use tokens (one per file) so the widget
    can add multiple files, with upload_url kept as the first for await_upload back-compat."""
    monkeypatch.setenv("NOTEBOOKLM_MCP_UPLOAD_WIDGET", "1")
    cfg = _cfg()
    mcp = _server(cfg)
    tool = {t.name: t for t in await mcp._list_tools()}["source_add_widget"]
    monkeypatch.setattr(_uploadwidget, "resolve_notebook", AsyncMock(return_value="nb-123"))
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=SimpleNamespace(
                client_provider=ClientProvider.of(MagicMock()), file_transfer=cfg
            )
        )
    )

    result = await tool.fn(ctx, notebook="My Notebook")

    urls = result["upload_urls"]
    assert len(urls) == _MAX_WIDGET_FILES
    assert len(set(urls)) == _MAX_WIDGET_FILES  # distinct tokens → independent single-use jtis
    assert all("/files/ul/" in u for u in urls)
    assert result["upload_url"] == urls[0]  # await_upload back-compat
    assert result["notebook_id"] == "nb-123"


async def test_widget_tool_returns_auto_confirm_contract(monkeypatch) -> None:
    """#1891: source_add_widget returns a machine-readable ``confirm`` contract the widget fires
    after a successful upload — await_upload on the link just used, so the model confirms the add
    with no second prompt. ``values`` mirrors the token pool; back-compat fields are unchanged."""
    monkeypatch.setenv("NOTEBOOKLM_MCP_UPLOAD_WIDGET", "1")
    cfg = _cfg()
    mcp = _server(cfg)
    tool = {t.name: t for t in await mcp._list_tools()}["source_add_widget"]
    monkeypatch.setattr(_uploadwidget, "resolve_notebook", AsyncMock(return_value="nb-123"))
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=SimpleNamespace(
                client_provider=ClientProvider.of(MagicMock()), file_transfer=cfg
            )
        )
    )

    result = await tool.fn(ctx, notebook="My Notebook")

    assert result["confirm"] == {
        "tool": "await_upload",
        "arg": "upload_link",
        "values": result["upload_urls"],
    }
    # Additive only — the pool/back-compat fields the widget already relies on are untouched.
    assert result["upload_url"] == result["upload_urls"][0]


async def test_widget_pool_tokens_carry_the_longer_widget_ttl(monkeypatch) -> None:
    """The whole pool is minted at one instant but uploaded sequentially, so every token must
    carry the longer WIDGET_UPLOAD_TTL — otherwise a later file's token expires mid-batch and
    its upload silently 403s (#1894)."""
    monkeypatch.setenv("NOTEBOOKLM_MCP_UPLOAD_WIDGET", "1")
    cfg = _cfg()
    mcp = _server(cfg)
    tool = {t.name: t for t in await mcp._list_tools()}["source_add_widget"]
    monkeypatch.setattr(_uploadwidget, "resolve_notebook", AsyncMock(return_value="nb-123"))
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=SimpleNamespace(
                client_provider=ClientProvider.of(MagicMock()), file_transfer=cfg
            )
        )
    )

    before = int(time.time())
    result = await tool.fn(ctx, notebook="My Notebook")

    assert WIDGET_UPLOAD_TTL > UPLOAD_TTL  # the whole point: pool outlives the single-link window
    for url in result["upload_urls"]:
        payload = cfg.signer.verify(url.rsplit("/", 1)[1], op="ul")
        # Every pool token gets the longer widget TTL, and stays a proper single-use ul token.
        assert (
            before + WIDGET_UPLOAD_TTL <= payload["exp"] <= int(time.time()) + WIDGET_UPLOAD_TTL + 1
        )
        assert payload["op"] == "ul"
        assert isinstance(payload["jti"], str) and payload["jti"]
