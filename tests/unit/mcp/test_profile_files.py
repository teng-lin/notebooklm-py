"""Profile isolation across signed HTTP file links and widget confirmations."""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
from collections.abc import AsyncIterator
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit

import pytest

pytest.importorskip("fastmcp")

from fastmcp import Client  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from notebooklm.mcp import _filelink, _fileroutes  # noqa: E402
from notebooklm.mcp._filelink import FileLinkSigner, FileTransferConfig  # noqa: E402
from notebooklm.mcp._uploadwidget import _WIDGET_HTML  # noqa: E402
from notebooklm.mcp.server import create_server  # noqa: E402

_NOTEBOOK = "11111111-1111-1111-1111-111111111111"


def _server(config: FileTransferConfig, clients: dict[str, MagicMock] | None = None):
    clients = clients or {name: MagicMock() for name in ("work", "personal")}

    @contextlib.asynccontextmanager
    async def factory(profile: str) -> AsyncIterator[MagicMock]:
        yield clients[profile]

    return create_server(
        profiles=list(clients),
        backend="android",
        profile_client_factory=factory,
        file_transfer=config,
    )


@pytest.fixture
def file_config() -> FileTransferConfig:
    return FileTransferConfig(FileLinkSigner(b"k" * 32), "https://files.test")


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("unconfirmed", [False, True])
async def test_upload_receipts_require_the_signed_profile(
    file_config: FileTransferConfig,
    monkeypatch: pytest.MonkeyPatch,
    expired: bool,
    unconfirmed: bool,
) -> None:
    """Both receipt recovery paths reject another profile before exposing a result."""
    work_config = replace(file_config, profile="work")
    url = work_config.upload_url({"nb": _NOTEBOOK})
    claims = file_config.signer.verify(url.rsplit("/", 1)[1], op="ul")
    receipt = {"source_id": "work-source", "name": "private-work-file.txt"}
    if unconfirmed:
        receipt["status"] = "unconfirmed"
    file_config.jti_store.commit(claims["jti"], claims["exp"], result=receipt)
    if expired:
        monkeypatch.setattr(_filelink.time, "time", lambda: claims["exp"] + 5)

    async with Client(_server(file_config)) as client:
        wrong = await client.call_tool(
            "await_upload", {"profile": "personal", "upload_link": url, "timeout": 0}
        )
        assert wrong.data["status"] == "expired_or_invalid"
        assert "work-source" not in json.dumps(wrong.data)
        assert "private-work-file" not in json.dumps(wrong.data)
        correct = await client.call_tool(
            "await_upload", {"profile": "work", "upload_link": url, "timeout": 0}
        )
        assert correct.data["status"] == ("unconfirmed" if unconfirmed else "received")
        assert correct.data["source_id"] == "work-source"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for widget execution")
@pytest.mark.parametrize("host", ["chatgpt", "mcp-app"])
async def test_widget_confirmation_keeps_profile_through_both_host_bridges(
    file_config: FileTransferConfig, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """Execute the widget bridge with a real tool result, then replay its MCP call."""
    monkeypatch.setenv("NOTEBOOKLM_MCP_UPLOAD_WIDGET", "1")
    script = _WIDGET_HTML.split('<script type="module">', 1)[1].split("</script>", 1)[0]
    async with Client(_server(file_config)) as client:
        widget = await client.call_tool(
            "source_add_widget", {"profile": "work", "notebook": _NOTEBOOK}
        )
        data = widget.data
        assert data["confirm"]["profile"] == "work"
        for url in data["upload_urls"]:
            claims = file_config.signer.verify(url.rsplit("/", 1)[1], op="ul")
            assert claims["profile"] == "work"
        url = data["upload_url"]
        claims = file_config.signer.verify(url.rsplit("/", 1)[1], op="ul")
        file_config.jti_store.commit(
            claims["jti"], claims["exp"], result={"source_id": "widget-source"}
        )
        harness = r"""
const vm = require('node:vm');
const calls = [];
const elements = Object.fromEntries(['sub', 'out', 'f', 'up'].map(id => [id, {
  textContent: '', disabled: true, addEventListener() {}
}]));
const context = {
  document: {getElementById: id => elements[id], documentElement: {}},
  window: {parent: {postMessage(message) {
    if (message.method === 'tools/call') calls.push(message.params);
  }}, addEventListener() {}},
  setTimeout() {}, setInterval() {}, clearInterval() {}
};
if (__HOST__ === 'chatgpt') context.window.openai = {
  callTool: async (name, args) => calls.push({name, arguments: args})
};
vm.createContext(context);
vm.runInContext(__SCRIPT__, context);
vm.runInContext(__CONFIRM__, context);
process.stdout.write(JSON.stringify(calls));
"""
        harness = (
            harness.replace("__HOST__", json.dumps(host))
            .replace("__SCRIPT__", json.dumps(script))
            .replace(
                "__CONFIRM__",
                json.dumps(
                    f"confirmSpec={json.dumps(data['confirm'])};confirmUpload({json.dumps(url)});"
                ),
            )
        )
        execution = subprocess.run(
            ["node", "-e", harness], check=True, capture_output=True, text=True
        )
        calls = json.loads(execution.stdout)
        assert calls == [
            {"name": "await_upload", "arguments": {"profile": "work", "upload_link": url}}
        ]
        confirmed = await client.call_tool(calls[0]["name"], calls[0]["arguments"])
        assert confirmed.data["source_id"] == "widget-source"


@pytest.mark.parametrize("profile", ["work", "personal"])
def test_http_upload_uses_signed_profile(
    file_config: FileTransferConfig, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    """The standalone signed route selects the client without an MCP request Context."""
    clients = {name: MagicMock(name=name) for name in ("work", "personal")}
    execute = AsyncMock(return_value=SimpleNamespace(source=SimpleNamespace(id="uploaded-source")))
    monkeypatch.setattr(_fileroutes.add_core, "execute_source_add", execute)
    url = replace(file_config, profile=profile).upload_url({"nb": _NOTEBOOK})
    with TestClient(_server(file_config, clients).http_app()) as client:
        response = client.post(
            urlsplit(url).path + "?filename=note.txt",
            content=b"uploaded text",
            headers={"Content-Type": "text/plain", "Accept": "application/json"},
        )
    assert response.status_code == 200
    execute.assert_awaited_once()
    assert execute.await_args.args[0] is clients[profile]
    assert execute.await_args.args[1].notebook_id == _NOTEBOOK
    claims = file_config.signer.verify(url.rsplit("/", 1)[1], op="ul")
    assert file_config.jti_store.completed(claims["jti"])["source_id"] == "uploaded-source"
