"""Lifecycle and compatibility contract for the inert lazy Web sidecar."""

from __future__ import annotations

import asyncio
import json
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import notebooklm._android.auth as android_auth
import notebooklm._client_assembly as assembly
from notebooklm._auth.master_token_types import MasterToken
from notebooklm._web.transport.sidecar import LazyWebSidecar
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from notebooklm.rpc import RPCMethod


def _assert_republished_cancel_message(error: asyncio.CancelledError, expected: str) -> None:
    """Assert first-cancel precedence across supported asyncio versions.

    Python 3.10 drops the optional cancellation message when cancellation is
    caught and republished through shielded lifecycle cleanup. Python 3.11+
    preserves it; neither version may replace it with a later message.
    """
    assert error.args == ((expected,) if sys.version_info >= (3, 11) else ())


class _Participant:
    def __init__(self) -> None:
        self.bound: list[asyncio.AbstractEventLoop] = []
        self.resets = 0

    def set_bound_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.bound.append(loop)

    def reset_after_open(self) -> None:
        self.resets += 1


class _Transport:
    def __init__(self) -> None:
        self.opened: list[int] = []
        self.prepared = 0
        self.closed = 0

    async def open(self, loop: asyncio.AbstractEventLoop, epoch: int) -> None:
        del loop
        self.opened.append(epoch)

    async def prepare_close(self) -> None:
        self.prepared += 1

    async def close_resources(self) -> None:
        self.closed += 1


def _runtime(*, result: object = None) -> SimpleNamespace:
    return SimpleNamespace(
        reqid=_Participant(),
        auth_coord=_Participant(),
        web_transport=_Transport(),
        source_uploader=_Transport(),
        executor=SimpleNamespace(rpc_call=AsyncMock(return_value=result)),
        composed=SimpleNamespace(bind_runtime_collaborators=MagicMock()),
    )


def _blocking_partial_open_runtime() -> tuple[
    SimpleNamespace, asyncio.Event, asyncio.Event, asyncio.Event
]:
    runtime = _runtime()
    open_started = asyncio.Event()
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    async def partial_open(_loop: asyncio.AbstractEventLoop, epoch: int) -> None:
        runtime.web_transport.opened.append(epoch)
        open_started.set()
        await asyncio.Event().wait()

    async def blocking_close() -> None:
        close_started.set()
        await release_close.wait()
        runtime.web_transport.closed += 1

    runtime.web_transport.open = partial_open
    runtime.web_transport.close_resources = blocking_close
    return runtime, open_started, close_started, release_close


async def test_sidecar_is_inert_then_builds_once_and_reopens() -> None:
    built: list[SimpleNamespace] = []

    def build() -> SimpleNamespace:
        runtime = _runtime()
        built.append(runtime)
        return runtime

    sidecar = LazyWebSidecar(build)  # type: ignore[arg-type]
    loop = asyncio.get_running_loop()
    sidecar.set_bound_loop(loop)
    sidecar.reset_after_open()
    await sidecar.open(loop, 1)
    assert built == []

    first, second = await asyncio.gather(sidecar.materialize(1), sidecar.materialize(1))
    assert first is second is built[0]
    assert built[0].web_transport.opened == [1]
    assert built[0].source_uploader.opened == [1]

    await sidecar.prepare_close()
    await sidecar.close_resources()
    await sidecar.open(loop, 2)

    assert built[0].web_transport.opened == [1, 2]
    assert built[0].source_uploader.opened == [1, 2]
    assert built[0].web_transport.closed == 1
    assert built[0].source_uploader.closed == 1


async def test_sidecar_close_phases_stay_inert_before_materialization() -> None:
    sidecar = LazyWebSidecar(_runtime)  # type: ignore[arg-type]
    assert not sidecar.is_materialized

    # Lifecycle cleanup is also safe for a partially opened root.
    await sidecar.prepare_close()
    await sidecar.close_resources()

    loop = asyncio.get_running_loop()
    await sidecar.open(loop, 1)
    await sidecar.prepare_close()
    await sidecar.close_resources()
    assert sidecar.runtime is None


async def test_sidecar_serializes_forced_close_with_materialization() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    runtime = _runtime()

    async def blocking_open(_loop: asyncio.AbstractEventLoop, epoch: int) -> None:
        runtime.web_transport.opened.append(epoch)
        started.set()
        await release.wait()

    runtime.web_transport.open = blocking_open
    sidecar = LazyWebSidecar(lambda: runtime)  # type: ignore[arg-type]
    loop = asyncio.get_running_loop()
    await sidecar.open(loop, 1)

    materialize = asyncio.create_task(sidecar.materialize(1))
    await started.wait()
    prepare = asyncio.create_task(sidecar.prepare_close())
    await asyncio.sleep(0)
    assert not prepare.done()

    release.set()
    assert await materialize is runtime
    await prepare
    await sidecar.close_resources()

    assert runtime.web_transport.prepared == 1
    assert runtime.source_uploader.prepared == 1
    assert runtime.web_transport.closed == 1
    assert runtime.source_uploader.closed == 1
    with pytest.raises(RuntimeError, match="Client not initialized"):
        await sidecar.materialize(1)


async def test_sidecar_retires_a_candidate_that_fails_to_open() -> None:
    runtime = _runtime()

    async def fail_open(_loop: asyncio.AbstractEventLoop, _epoch: int) -> None:
        raise RuntimeError("open failed")

    runtime.web_transport.open = fail_open
    sidecar = LazyWebSidecar(lambda: runtime)  # type: ignore[arg-type]
    loop = asyncio.get_running_loop()
    await sidecar.open(loop, 1)

    with pytest.raises(RuntimeError, match="open failed"):
        await sidecar.materialize(1)

    assert sidecar.runtime is None
    assert runtime.web_transport.prepared == 1
    assert runtime.source_uploader.prepared == 1
    assert runtime.web_transport.closed == 1
    assert runtime.source_uploader.closed == 1


async def test_sidecar_first_open_cancellation_waits_for_candidate_retirement() -> None:
    runtime, open_started, close_started, release_close = _blocking_partial_open_runtime()
    sidecar = LazyWebSidecar(lambda: runtime)  # type: ignore[arg-type]
    loop = asyncio.get_running_loop()
    await sidecar.open(loop, 1)

    materialize = asyncio.create_task(sidecar.materialize(1))
    await open_started.wait()
    materialize.cancel("first cancellation")
    await close_started.wait()
    await asyncio.sleep(0)

    assert not materialize.done()
    assert sidecar._candidate_retirement is not None
    release_close.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await materialize

    _assert_republished_cancel_message(raised.value, "first cancellation")
    assert sidecar._candidate_retirement is None
    assert sidecar.runtime is None
    assert runtime.web_transport.closed == 1
    assert runtime.source_uploader.closed == 1


async def test_sidecar_recancellation_detaches_but_root_close_joins_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, open_started, close_started, release_close = _blocking_partial_open_runtime()
    monkeypatch.setattr(assembly, "build_web_runtime", MagicMock(return_value=runtime))
    monkeypatch.setattr(android_auth, "_require_gpsoauth", lambda: object())
    client = NotebookLMClient(
        AuthTokens(cookies={"SID": "sid"}, csrf_token="csrf", session_id="session"),
        backend="android",
    )
    assert client._android_runtime is not None
    client._android_runtime.bearer_provider._profile_store.read_master_token = MagicMock(
        return_value=MasterToken(email="test@example.com", android_id="1234", secret="secret")
    )
    client._android_runtime.session._grpc_loader = lambda: object()
    client._android_runtime.session._protobuf_loader = lambda: object()

    await client.__aenter__()
    try:
        with pytest.warns(DeprecationWarning, match="crosses from Android"):
            materialize = asyncio.create_task(client.rpc_call(RPCMethod.LIST_NOTEBOOKS, []))
            await open_started.wait()
            materialize.cancel("first cancellation")
            await close_started.wait()
            materialize.cancel("second cancellation")
            with pytest.raises(asyncio.CancelledError) as raised:
                await materialize

        _assert_republished_cancel_message(raised.value, "first cancellation")
        sidecar = client._web_sidecar
        assert sidecar is not None
        retirement = sidecar._candidate_retirement
        assert retirement is not None
        assert not retirement.done()
        assert sidecar.runtime is None

        root_close = asyncio.create_task(client.close(drain=False))
        await asyncio.sleep(0)
        assert not root_close.done()
        release_close.set()
        await root_close

        assert retirement.done()
        assert sidecar._candidate_retirement is None
        assert runtime.web_transport.closed == 1
        assert runtime.source_uploader.closed == 1
    finally:
        release_close.set()
        if client.is_connected:
            await client.close(drain=False)


async def test_sidecar_propagates_a_close_phase_failure() -> None:
    runtime = _runtime()
    sidecar = LazyWebSidecar(lambda: runtime)  # type: ignore[arg-type]
    loop = asyncio.get_running_loop()
    await sidecar.open(loop, 1)
    await sidecar.materialize(1)

    async def fail_prepare() -> None:
        raise RuntimeError("prepare failed")

    runtime.web_transport.prepare_close = fail_prepare
    with pytest.raises(RuntimeError, match="prepare failed"):
        await sidecar.prepare_close()


async def test_android_sidecar_uses_own_refresh_ladder_and_persists_cookies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = tmp_path / "storage_state.json"
    storage.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "SID",
                        "value": "old",
                        "domain": ".google.com",
                        "path": "/",
                    },
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "ts",
                        "domain": ".google.com",
                        "path": "/",
                    },
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    jar = httpx.Cookies()
    jar.set("SID", "old", domain=".google.com", path="/")
    jar.set("__Secure-1PSIDTS", "ts", domain=".google.com", path="/")
    auth = AuthTokens(
        cookies={"SID": "old", "__Secure-1PSIDTS": "ts"},
        csrf_token="csrf",
        session_id="session",
        storage_path=storage,
        cookie_jar=jar,
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(401, request=request)
        return httpx.Response(
            200,
            text="ignored by the injected decoder",
            headers={"Set-Cookie": "SID=fresh; Domain=.google.com; Path=/"},
            request=request,
        )

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(assembly, "_resolve_async_client_factory", lambda _factory: client_factory)
    monkeypatch.setattr(android_auth, "_require_gpsoauth", lambda: object())
    client = NotebookLMClient(auth, backend="android")
    client._seams.decode_response = lambda *_args, **_kwargs: ["ok"]
    client._seams.sleep = AsyncMock()
    assert client._android_runtime is not None
    client._android_runtime.bearer_provider._profile_store.read_master_token = MagicMock(
        return_value=MasterToken(email="test@example.com", android_id="1234", secret="secret")
    )
    client._android_runtime.session._grpc_loader = lambda: object()
    client._android_runtime.session._protobuf_loader = lambda: object()
    refresh = AsyncMock(return_value=auth)
    monkeypatch.setattr(client, "_refresh_sidecar_auth_for_epoch", refresh)

    await client.__aenter__()
    try:
        with pytest.warns(DeprecationWarning, match="crosses from Android"):
            assert await client.rpc_call(RPCMethod.LIST_NOTEBOOKS, []) == ["ok"]
        assert client._web_sidecar is not None
        runtime = client._web_sidecar.runtime
        assert runtime is not None
        assert runtime.web_transport._keepalive_task is None
        assert calls == 2
        refresh.assert_awaited_once()
    finally:
        await client.close()

    saved = json.loads(storage.read_text(encoding="utf-8"))
    sid = next(row for row in saved["cookies"] if row["name"] == "SID")
    assert sid["value"] == "fresh"


async def test_android_deprecated_rpc_call_builds_once_warns_once_and_refuses_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(result=["ok"])
    build = MagicMock(return_value=runtime)
    monkeypatch.setattr(assembly, "build_web_runtime", build)
    monkeypatch.setattr(android_auth, "_require_gpsoauth", lambda: object())
    client = NotebookLMClient(
        AuthTokens(cookies={"SID": "sid"}, csrf_token="csrf", session_id="session"),
        backend="android",
    )
    assert client._android_runtime is not None
    client._android_runtime.bearer_provider._profile_store.read_master_token = MagicMock(
        return_value=MasterToken(email="test@example.com", android_id="1234", secret="secret")
    )
    client._android_runtime.session._grpc_loader = lambda: object()
    client._android_runtime.session._protobuf_loader = lambda: object()

    await client.__aenter__()
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            assert await client.rpc_call(RPCMethod.LIST_NOTEBOOKS, []) == ["ok"]
            assert await client.rpc_call(RPCMethod.LIST_NOTEBOOKS, []) == ["ok"]
        assert len(caught) == 1
        assert "crosses from Android" in str(caught[0].message)
        assert build.call_count == 1
        assert runtime.executor.rpc_call.await_count == 2

        await client.drain()
        with pytest.raises(RuntimeError, match="not accepting new operations"):
            await client.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])
    finally:
        await client.close(drain=False)


async def test_web_deprecated_rpc_call_warns_once(monkeypatch: pytest.MonkeyPatch) -> None:
    client = NotebookLMClient(
        AuthTokens(cookies={"SID": "sid"}, csrf_token="csrf", session_id="session")
    )
    assert client._web_runtime is not None
    call = AsyncMock(return_value=[])
    monkeypatch.setattr(client._web_runtime.executor, "rpc_call", call)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await client.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])
        await client.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

    assert len(caught) == 1
    assert "client.raw.call" in str(caught[0].message)
    assert call.await_count == 2
