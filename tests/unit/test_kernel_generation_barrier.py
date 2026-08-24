"""Generation-attempt fencing for the backend-private cookie jar."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

import notebooklm._runtime.init as runtime_init
from notebooklm._auth.cookie_types import CookieJar
from notebooklm._kernel import Kernel
from notebooklm._runtime.transport import RuntimeTransport
from notebooklm._web_cookie_provider import WebCookieGeneration
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient


def _generation(epoch: int, sid: str) -> WebCookieGeneration:
    cookies = httpx.Cookies()
    cookies.set("SID", sid, domain=".google.com", path="/")
    return WebCookieGeneration(
        csrf_token=f"csrf-{epoch}",
        session_id=f"session-{epoch}",
        authuser=epoch,
        account_email=None,
        cookies=CookieJar.from_httpx(cookies),
        generation=epoch,
    )


def _sid(cookies: httpx.Cookies | CookieJar) -> str | None:
    jar = cookies.to_httpx() if isinstance(cookies, CookieJar) else cookies
    return jar.get("SID")


def _auth() -> AuthTokens:
    return AuthTokens(cookies={"SID": "old"}, csrf_token="csrf-0", session_id="session-0")


@pytest.mark.asyncio
async def test_late_old_httpx_response_cannot_mutate_the_new_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real overlapping response settles before the newer jar is installed."""
    old_entered = asyncio.Event()
    release_old = asyncio.Event()
    new_entered = asyncio.Event()
    observed: list[tuple[str, str | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        marker = request.headers["x-generation"]
        observed.append((marker, request.headers.get("cookie")))
        if marker == "0":
            old_entered.set()
            await release_old.wait()
            return httpx.Response(
                200,
                headers={"set-cookie": "SID=late-old; Path=/; Domain=.google.com"},
                request=request,
            )
        new_entered.set()
        return httpx.Response(200, request=request)

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    auth = AuthTokens(
        cookies={("SID", ".google.com", "/"): "old"},
        csrf_token="csrf-0",
        session_id="session-0",
    )
    monkeypatch.setattr(runtime_init, "_resolve_async_client_factory", lambda _value: factory)
    client = NotebookLMClient(auth)
    await client.__aenter__()
    try:
        provider = client._provider
        transport = client._backend._runtime._transport
        assert isinstance(transport, RuntimeTransport)

        def build(snapshot: WebCookieGeneration) -> tuple[str, bytes, dict[str, str]]:
            return (
                "https://notebooklm.google.com/_/rpc",
                b"body",
                {"x-generation": str(snapshot.generation)},
            )

        old = asyncio.create_task(
            transport.perform_authed_post(build_request=build, log_label="old")
        )
        await old_entered.wait()

        async def refresh_work() -> AuthTokens:
            provider._kernel.cookies.set("SID", "refresh-new", domain=".google.com", path="/")
            provider.auth.csrf_token = "csrf-1"
            provider.auth.session_id = "session-1"
            return provider.auth

        await provider.run_refresh_transaction(refresh_work)
        assert (await provider.generation()).generation == 1

        direct_snapshot = asyncio.create_task(provider.reconciled_generation())
        new = asyncio.create_task(
            transport.perform_authed_post(build_request=build, log_label="new")
        )
        await asyncio.sleep(0)
        assert not direct_snapshot.done()
        assert not new_entered.is_set()

        release_old.set()
        await old
        await new
        reconciled = await direct_snapshot

        assert [marker for marker, _cookie in observed] == ["0", "1"]
        assert "SID=old" in (observed[0][1] or "")
        assert "SID=refresh-new" in (observed[1][1] or "")
        assert _sid(client._backend._backend_session.kernel.cookies) == "refresh-new"
        assert reconciled.generation == 1
        assert _sid(reconciled.cookies) == "refresh-new"
    finally:
        release_old.set()
        await client.close(drain=False)


@pytest.mark.asyncio
async def test_drain_false_close_does_not_wait_for_a_hung_backend_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Immediate close snapshots best-effort state without joining the barrier."""
    request_entered = asyncio.Event()
    release_request = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        request_entered.set()
        await release_request.wait()
        return httpx.Response(200, request=request)

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(runtime_init, "_resolve_async_client_factory", lambda _value: factory)
    client = NotebookLMClient(_auth())
    await client.__aenter__()
    transport = client._backend._runtime._transport
    assert isinstance(transport, RuntimeTransport)

    def build(snapshot: WebCookieGeneration) -> tuple[str, bytes, dict[str, str]]:
        return (
            "https://notebooklm.google.com/_/rpc",
            b"body",
            {"x-generation": str(snapshot.generation)},
        )

    post = asyncio.create_task(
        transport.perform_authed_post(build_request=build, log_label="hung-close")
    )
    await request_entered.wait()
    close = asyncio.create_task(client.close(drain=False))
    try:
        await asyncio.wait_for(asyncio.shield(close), timeout=0.5)
        assert not client.is_connected
        assert not post.done()
    finally:
        release_request.set()
        await asyncio.gather(post, return_exceptions=True)
        await asyncio.gather(close, return_exceptions=True)


@pytest.mark.asyncio
async def test_drain_false_close_bypasses_a_reconciler_waiting_on_a_hung_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct-leg reconciler cannot hold immediate close behind its lock."""
    request_entered = asyncio.Event()
    release_request = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        request_entered.set()
        await release_request.wait()
        return httpx.Response(200, request=request)

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(runtime_init, "_resolve_async_client_factory", lambda _value: factory)
    client = NotebookLMClient(_auth())
    await client.__aenter__()
    transport = client._backend._runtime._transport
    assert isinstance(transport, RuntimeTransport)

    def build(snapshot: WebCookieGeneration) -> tuple[str, bytes, dict[str, str]]:
        return (
            "https://notebooklm.google.com/_/rpc",
            b"body",
            {"x-generation": str(snapshot.generation)},
        )

    post = asyncio.create_task(
        transport.perform_authed_post(build_request=build, log_label="hung-reconcile-close")
    )
    await request_entered.wait()
    reconcile = asyncio.create_task(client._provider.reconciled_generation())
    while not client._backend._backend_session.kernel._generation_transition_waiters:
        await asyncio.sleep(0)

    close = asyncio.create_task(client.close(drain=False))
    try:
        await asyncio.wait_for(asyncio.shield(close), timeout=0.5)
        assert not client.is_connected
        assert not reconcile.done()
    finally:
        release_request.set()
        await asyncio.gather(post, return_exceptions=True)
        result = (await asyncio.gather(reconcile, return_exceptions=True))[0]
        await asyncio.gather(close, return_exceptions=True)

    assert isinstance(result, RuntimeError)
    assert "provider is closing" in str(result)


@pytest.mark.asyncio
async def test_drain_timeout_closes_without_rejoining_the_hung_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timed-out drain tears down immediately before re-raising its timeout."""
    request_entered = asyncio.Event()
    release_request = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        request_entered.set()
        await release_request.wait()
        return httpx.Response(200, request=request)

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(runtime_init, "_resolve_async_client_factory", lambda _value: factory)
    client = NotebookLMClient(_auth())
    await client.__aenter__()
    transport = client._backend._runtime._transport
    assert isinstance(transport, RuntimeTransport)

    def build(snapshot: WebCookieGeneration) -> tuple[str, bytes, dict[str, str]]:
        return (
            "https://notebooklm.google.com/_/rpc",
            b"body",
            {"x-generation": str(snapshot.generation)},
        )

    post = asyncio.create_task(
        transport.perform_authed_post(build_request=build, log_label="hung-drain-timeout")
    )
    await request_entered.wait()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                client.close(drain=True, drain_timeout=0.01),
                timeout=0.5,
            )
        assert not client.is_connected
        assert not post.done()
    finally:
        release_request.set()
        await asyncio.gather(post, return_exceptions=True)


@pytest.mark.asyncio
async def test_new_epoch_waiter_closes_old_epoch_admission_without_serializing_peers() -> None:
    """Same-epoch attempts overlap, while a queued transition cannot starve."""
    kernel = Kernel(auth=_auth())
    old_generation = _generation(0, "old")
    new_generation = _generation(1, "new")
    assert kernel.install_generation(old_generation)

    first = await kernel.begin_generation_attempt(old_generation)
    peer = await kernel.begin_generation_attempt(old_generation)
    assert first is not None and peer is not None

    transition = asyncio.create_task(kernel.begin_generation_attempt(new_generation))
    await asyncio.sleep(0)
    late_old = asyncio.create_task(kernel.begin_generation_attempt(old_generation))
    await asyncio.sleep(0)
    assert not transition.done()
    assert not late_old.done()

    await first.release()
    await peer.release()
    admitted_new = await transition
    assert admitted_new is not None
    assert await late_old is None
    await admitted_new.release()


@pytest.mark.asyncio
async def test_synchronous_install_cannot_silently_drop_a_new_generation() -> None:
    """Busy compatibility installation fails instead of looking already current."""
    kernel = Kernel(auth=_auth())
    old_generation = _generation(0, "old")
    assert kernel.install_generation(old_generation)
    attempt = await kernel.begin_generation_attempt(old_generation)
    assert attempt is not None

    with pytest.raises(RuntimeError, match="barrier is active"):
        kernel.install_generation(_generation(1, "new"))

    await attempt.release()


@pytest.mark.asyncio
async def test_cancelled_transition_waiter_reopens_same_epoch_admission() -> None:
    """Cancellation removes the fairness gate instead of stranding peers."""
    kernel = Kernel(auth=_auth())
    old_generation = _generation(0, "old")
    assert kernel.install_generation(old_generation)
    first = await kernel.begin_generation_attempt(old_generation)
    assert first is not None

    transition = asyncio.create_task(kernel.begin_generation_attempt(_generation(1, "new")))
    await asyncio.sleep(0)
    late_peer = asyncio.create_task(kernel.begin_generation_attempt(old_generation))
    await asyncio.sleep(0)
    assert not late_peer.done()

    transition.cancel()
    with pytest.raises(asyncio.CancelledError):
        await transition
    admitted_peer = await asyncio.wait_for(late_peer, timeout=1.0)
    assert admitted_peer is not None

    await admitted_peer.release()
    await first.release()


@pytest.mark.asyncio
async def test_cancelled_attempt_releases_the_generation_barrier() -> None:
    """A cancelled response cannot leave every newer request parked forever."""
    kernel = Kernel(auth=_auth())
    old_generation = _generation(0, "old")
    assert kernel.install_generation(old_generation)
    attempt = await kernel.begin_generation_attempt(old_generation)
    assert attempt is not None
    started = asyncio.Event()

    async def cancelled_owner() -> None:
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            await attempt.release()

    owner = asyncio.create_task(cancelled_owner())
    await started.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner

    newer = await asyncio.wait_for(
        kernel.begin_generation_attempt(_generation(1, "new")), timeout=1.0
    )
    assert newer is not None
    await newer.release()


def test_generation_barrier_rebuilds_after_cross_loop_reopen() -> None:
    """A reopened kernel never reuses a Condition bound to the prior loop."""
    kernel = Kernel(auth=_auth())
    old_condition: asyncio.Condition | None = None

    async def use_on_first_loop() -> None:
        nonlocal old_condition
        kernel.set_bound_loop(asyncio.get_running_loop())
        attempt = await kernel.begin_generation_attempt(_generation(0, "old"))
        assert attempt is not None
        old_condition = kernel._generation_condition
        await attempt.release()

    async def use_on_second_loop() -> None:
        kernel.set_bound_loop(asyncio.get_running_loop())
        kernel.reset_after_open()
        attempt = await kernel.begin_generation_attempt(_generation(1, "new"))
        assert attempt is not None
        assert kernel._generation_condition is not old_condition
        await attempt.release()

    asyncio.run(use_on_first_loop())
    asyncio.run(use_on_second_loop())
