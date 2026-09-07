"""Real Web assembly and file writers with only external HTTP I/O replaced."""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from notebooklm._artifact import _download_client, _publication, downloads
from notebooklm._curl_cffi_transport import CurlCffiAsyncClient
from notebooklm._runtime.call_supervisor import AdmissionState
from notebooklm._web.assets import WebAssetDownloadService
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from notebooklm.options import ClientConfig, WebBackendConfig

pytestmark = pytest.mark.filterwarnings(
    "ignore:Raw artifact download prefetch parameters are deprecated:DeprecationWarning"
)

URL = "https://notebooklm.google.com/test/audio.mp4"
PAYLOAD = b"audio-content" * 8192


def make_client(secret: str = "one") -> NotebookLMClient:
    return NotebookLMClient(
        AuthTokens(cookies={"SID": secret}, csrf_token="csrf", session_id="session"),
        config=ClientConfig(backend=WebBackendConfig()),
    )


def assets(client: NotebookLMClient) -> WebAssetDownloadService:
    owner = client.artifacts._asset_downloads
    assert isinstance(owner, WebAssetDownloadService)
    assert owner in client._lifecycle._transports
    assert client.artifacts._downloads._asset is owner
    return owner


async def audio(client: NotebookLMClient, output: Path) -> str:
    row = ["audio", "Audio", 1, None, 3, None, [None] * 5 + [[[URL, None, "audio/mp4"]]]]
    return await client.artifacts.download_audio("notebook", str(output), artifacts_data=[row])


class PausedBody(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        yield PAYLOAD[:65536]
        self.entered.set()
        await self.release.wait()
        yield PAYLOAD[65536:]

    async def aclose(self) -> None:
        self.closed = True


def mock_http(monkeypatch, handler):
    original = httpx.AsyncClient
    clients = []

    class DownloadClient(original):
        def __init__(self, **kwargs):
            super().__init__(**kwargs, transport=httpx.MockTransport(handler))
            clients.append(self)

    monkeypatch.setattr(httpx, "AsyncClient", DownloadClient)
    monkeypatch.setattr(downloads, "resolve_transport_factory", lambda: DownloadClient)
    monkeypatch.setattr(_download_client, "resolve_transport_factory", lambda: DownloadClient)
    return clients


def mock_transport(monkeypatch, transport, handler):
    if transport == "httpx":
        return mock_http(monkeypatch, handler)
    clients = []

    class TerminalSession:
        def __init__(self, **kwargs):
            self.cookies = httpx.Cookies()
            self.headers = {}
            self.is_closed = False
            clients.append(self)

        async def get(self, url, **kwargs):
            request = httpx.Request("GET", url)
            httpx.Cookies(kwargs.get("cookies")).set_cookie_header(request)
            response = handler(request)
            if hasattr(response, "__await__"):
                response = await response
            return SimpleNamespace(
                status_code=response.status_code,
                headers=response.headers,
                content=response.content,
                url=url,
            )

        async def close(self):
            self.is_closed = True

    # Only curl's external HTTP terminal is fake. The production constructor,
    # get_guarded redirect loop, credential policy, and close methods all run.
    monkeypatch.setitem(
        sys.modules,
        "curl_cffi.requests",
        SimpleNamespace(
            AsyncSession=TerminalSession, RequestsError=type("RequestsError", (Exception,), {})
        ),
    )
    monkeypatch.setattr(downloads, "resolve_transport_factory", lambda: CurlCffiAsyncClient)
    monkeypatch.setattr(_download_client, "resolve_transport_factory", lambda: CurlCffiAsyncClient)
    return clients


async def wait_draining(client):
    for _ in range(100):
        if client._collaborators.call_supervisor._current.state is AdmissionState.DRAINING:
            return
        await asyncio.sleep(0)
    pytest.fail("close did not reach drain")


def assert_settled(client, http_clients):
    owner = assets(client)
    assert not owner._tasks
    assert not owner._clients
    assert not client._collaborators.call_supervisor._retired
    assert all(item.is_closed for item in http_clients)
    assert not any(t.name.startswith("artifact-dl-writer-") for t in threading.enumerate())


@pytest.mark.asyncio
@pytest.mark.parametrize("close_mode", ["force", "expiry", "graceful"])
async def test_real_stream_close_settles_before_reopen(monkeypatch, tmp_path, close_mode):
    body = PausedBody()
    clients = mock_http(monkeypatch, lambda request: httpx.Response(200, stream=body))
    client = make_client()
    await client.__aenter__()
    output = tmp_path / "audio.mp4"
    output.write_bytes(b"original")
    task = asyncio.create_task(audio(client, output))
    await asyncio.wait_for(body.entered.wait(), 2)
    if close_mode == "graceful":
        closing = asyncio.create_task(client.close())
        await wait_draining(client)
        assert not closing.done()
        with pytest.raises(RuntimeError, match="drain|closing"):
            await audio(client, tmp_path / "unrelated")
        body.release.set()
        assert await task == str(output)
        await closing
        assert output.read_bytes() == PAYLOAD
    else:
        if close_mode == "expiry":
            with pytest.raises(TimeoutError):
                await client.close(drain_timeout=0)
        else:
            await client.close(drain=False)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert output.read_bytes() == b"original"
    assert body.closed
    assert_settled(client, clients)
    assert list(tmp_path.glob("*.tmp")) == []
    await client.__aenter__()
    body.release.set()
    await asyncio.sleep(0)
    assert output.read_bytes() == (PAYLOAD if close_mode == "graceful" else b"original")
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["batch", "curl", "local"])
@pytest.mark.parametrize("close_mode", ["force", "expiry", "cancel", "graceful"])
async def test_buffered_publication_settles_writer_before_close(
    monkeypatch, tmp_path, kind, close_mode
):
    clients = mock_transport(
        monkeypatch,
        "curl" if kind == "curl" else "httpx",
        lambda request: httpx.Response(200, content=PAYLOAD),
    )
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_write = Path.write_bytes

    def paused_write(path, data):
        result = original_write(path, data)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "writer was never released"
        return result

    monkeypatch.setattr(Path, "write_bytes", paused_write)
    client = make_client()
    await client.__aenter__()
    output = tmp_path / "output"
    original_write(output, b"original")
    owner = assets(client)
    if kind == "batch":
        work = owner.download_urls_batch([(URL, str(output))])
    elif kind == "local":
        work = owner.write_file(str(output), lambda path: path.write_bytes(PAYLOAD))
    else:
        work = audio(client, output)
    task = asyncio.create_task(work)
    await asyncio.wait_for(entered.wait(), 2)
    if close_mode == "cancel":
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        closing = asyncio.create_task(client.close(drain=False))
    else:
        closing = asyncio.create_task(
            client.close(
                drain=close_mode != "force", drain_timeout=0 if close_mode == "expiry" else None
            )
        )
    for _ in range(20):
        await asyncio.sleep(0)
    assert not closing.done()
    assert not task.done()
    assert output.read_bytes() == b"original"
    release.set()
    if close_mode == "graceful":
        await task
        await closing
        assert output.read_bytes() == PAYLOAD
    else:
        if close_mode == "expiry":
            with pytest.raises(TimeoutError):
                await closing
        else:
            await closing
        with pytest.raises(asyncio.CancelledError):
            await task
        assert output.read_bytes() == b"original"
    assert_settled(client, clients)
    assert list(tmp_path.glob("*.tmp")) == []
    await client.__aenter__()
    assert output.read_bytes() == (PAYLOAD if close_mode == "graceful" else b"original")
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["httpx", "curl"])
@pytest.mark.parametrize("batch", [False, True])
async def test_live_cookie_isolation_and_redirect_rotation(monkeypatch, tmp_path, transport, batch):
    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers.get("cookie", "")))
        if request.url.path.endswith("audio.mp4"):
            return httpx.Response(
                302,
                headers={"location": "/redirected", "set-cookie": "ROTATED=yes; Path=/; Secure"},
            )
        return httpx.Response(200, content=PAYLOAD)

    clients = mock_transport(monkeypatch, transport, handler)
    monkeypatch.setenv("NOTEBOOKLM_PROFILE", "must-not-be-read")
    monkeypatch.setattr(downloads, "load_httpx_cookies", lambda **kw: pytest.fail("disk cookies"))
    first, second = make_client("one"), make_client("two")
    await first.__aenter__()
    await second.__aenter__()
    # Change the live owner after open; compatibility shadows are deliberately stale.
    assert first._web_runtime is not None
    first._web_runtime.kernel.cookies.set("LIVE", "first", domain="notebooklm.google.com")
    first.auth.cookies["SID"] = "shadow"
    try:
        for index, client in enumerate((first, second, first)):
            output = tmp_path / str(index)
            if batch:
                result = await assets(client).download_urls_batch([(URL, str(output))])
                assert result.all_succeeded
            else:
                await audio(client, output)
        assert len(seen) == 6
        assert all("SID=one" in value and "LIVE=first" in value for _, value in seen[:2])
        assert all("SID=two" in value and "LIVE=" not in value for _, value in seen[2:4])
        assert all("SID=one" in value and "SID=two" not in value for _, value in seen[4:])
        assert all("shadow" not in value for _, value in seen)
        assert "ROTATED=yes" in seen[1][1]
        assert "ROTATED=yes" in seen[4][1]
    finally:
        await first.close()
        await second.close()
    assert_settled(first, clients)
    assert_settled(second, clients)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["httpx", "curl"])
@pytest.mark.parametrize("stage", ["credentials", "redirect"])
async def test_cancelled_credential_or_redirect_cannot_dispatch(
    monkeypatch, tmp_path, transport, stage
):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "/redirected"})

    clients = mock_transport(monkeypatch, transport, handler)
    client = make_client()
    await client.__aenter__()
    owner = assets(client)
    opened_clients = len(clients)
    entered = asyncio.Event()
    original = owner._load_cookies
    calls = 0

    async def paused_credentials():
        nonlocal calls
        calls += 1
        captured = await original()
        # The initial load prepares the transfer; then credentials are acquired
        # again for the first request and each redirected request.
        if calls == (1 if stage == "credentials" else 3):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # An uncooperative provider still cannot return new-generation
                # credentials or authorize a continuation after close fences it.
                pass
        return captured

    monkeypatch.setattr(owner, "_load_cookies", paused_credentials)
    task = asyncio.create_task(audio(client, tmp_path / "absent"))
    await asyncio.wait_for(entered.wait(), 2)
    await client.close(drain=False)
    with pytest.raises((RuntimeError, asyncio.CancelledError)):
        await task
    assert len(seen) == (0 if stage == "credentials" else 1)
    if stage == "credentials":
        assert len(clients) == opened_clients
    assert not (tmp_path / "absent").exists()
    assert list(tmp_path.glob("*.tmp")) == []
    assert_settled(client, clients)
    await client.__aenter__()
    assert len(seen) == (0 if stage == "credentials" else 1)
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["httpx", "curl", "batch", "local"])
async def test_epoch_is_checked_immediately_before_atomic_publication(monkeypatch, tmp_path, kind):
    clients = mock_transport(
        monkeypatch,
        "curl" if kind == "curl" else "httpx",
        lambda request: httpx.Response(200, content=PAYLOAD),
    )
    entered = asyncio.Event()
    if kind == "httpx":
        original = downloads._await_writer_exit

        async def paused_exit(thread, **kwargs):
            await original(thread, **kwargs)
            if kwargs.get("re_raise_cancel"):
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    pass

        monkeypatch.setattr(downloads, "_await_writer_exit", paused_exit)
    else:
        original = _publication.write_staging

        async def paused_staging(*args):
            await original(*args)
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass

        monkeypatch.setattr(downloads, "write_staging", paused_staging)
        monkeypatch.setattr(_publication, "write_staging", paused_staging)
    client = make_client()
    await client.__aenter__()
    output = tmp_path / "output"
    output.write_bytes(b"original")
    owner = assets(client)
    if kind == "batch":
        work = owner.download_urls_batch([(URL, str(output))])
    elif kind == "local":
        work = owner.write_file(str(output), lambda path: path.write_bytes(PAYLOAD))
    else:
        work = audio(client, output)
    task = asyncio.create_task(work)
    await asyncio.wait_for(entered.wait(), 2)
    await client.close(drain=False)
    with pytest.raises((RuntimeError, asyncio.CancelledError)):
        await task
    assert output.read_bytes() == b"original"
    assert list(tmp_path.glob("*.tmp")) == []
    assert_settled(client, clients)
    await client.__aenter__()
    assert output.read_bytes() == b"original"
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["report", "mind_map", "data_table"])
async def test_local_public_download_uses_owned_staged_writer(monkeypatch, tmp_path, kind):
    client = make_client()
    await client.__aenter__()
    output = tmp_path / "output"
    output.write_bytes(b"original")
    entered = asyncio.Event()
    original = _publication.write_staging

    async def paused_staging(*args):
        await original(*args)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(_publication, "write_staging", paused_staging)
    if kind == "report":
        kwargs = {"artifacts_data": [["report", "Report", 2, None, 3, None, None, ["# Report"]]]}
    elif kind == "mind_map":
        kwargs = {"mind_maps": [["map", [None, '{"name":"Root"}'], None, None, "Map"]]}
    else:
        rows = [[0, 10, [[0, 5, [[0, 5, [[0, 5, [["Col"]]]]]]]]]]
        row = ["table", "Table", 9, None, 3] + [None] * 13
        row.append([[[[[0, 100, None, None, [6, 7, rows]]]]]])
        kwargs = {"artifacts_data": [row]}
    task = asyncio.create_task(
        getattr(client.artifacts, "download_" + kind)("notebook", str(output), **kwargs)
    )
    await asyncio.wait_for(entered.wait(), 2)
    assert task in assets(client)._tasks
    await client.close(drain=False)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert output.read_bytes() == b"original"
    assert list(tmp_path.glob("*.tmp")) == []
    assert_settled(client, [])
