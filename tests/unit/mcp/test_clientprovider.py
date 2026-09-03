"""Lazy client open + the #2330 handshake regression.

Before #2330 the FastMCP lifespan *awaited* the client open, so the MCP
``initialize`` handshake could not be answered until Google's auth round-trip
finished. That round-trip's own budget (a 15 s ``RotateCookies`` poke plus a
30 s CSRF fetch on the happy rung, more on the cold-recovery ladder) exceeds the
30 s deadline Claude Code gives the handshake, so a slow or rate-limited Google
surfaced as an opaque ``CONNECT_TIMEOUT`` instead of a real error.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from types import TracebackType
from typing import cast
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastmcp")

from fastmcp import Client  # noqa: E402 - after importorskip guard

from notebooklm.client import NotebookLMClient  # noqa: E402
from notebooklm.mcp._clientprovider import ClientProvider  # noqa: E402
from notebooklm.mcp._context import AppState  # noqa: E402
from notebooklm.mcp.server import create_server  # noqa: E402


class _SlowFactory:
    """Client factory whose open blocks until released (stands in for slow auth)."""

    def __init__(self, client: NotebookLMClient | None = None) -> None:
        self.client = client if client is not None else cast("NotebookLMClient", MagicMock())
        self.gate = asyncio.Event()
        self.opens = 0
        self.closes = 0
        self.error: Exception | None = None
        self.entered = asyncio.Event()

    def __call__(self) -> contextlib.AbstractAsyncContextManager[NotebookLMClient]:
        return self._cm()

    @contextlib.asynccontextmanager
    async def _cm(self) -> AsyncIterator[NotebookLMClient]:
        self.opens += 1
        self.entered.set()
        await self.gate.wait()
        if self.error is not None:
            raise self.error
        try:
            yield self.client
        finally:
            self.closes += 1


async def test_handshake_is_not_gated_on_the_client_open() -> None:
    """#2330: initialize + tool discovery complete while the open is still blocked."""
    factory = _SlowFactory()
    server = create_server(client_factory=factory)

    async with Client(server) as client:  # performs initialize
        # The open has been *started* (warm-up) but has not completed.
        await asyncio.wait_for(factory.entered.wait(), timeout=5)
        tools = await asyncio.wait_for(client.list_tools(), timeout=5)
        assert tools, "tool discovery must not wait on the client open"
        factory.gate.set()

    assert factory.opens == 1


async def test_lifespan_warms_the_client_in_the_background() -> None:
    """The lifespan starts the open itself, so the first tool call is usually warm."""
    factory = _SlowFactory()
    factory.gate.set()
    server = create_server(client_factory=factory)

    async with Client(server):
        # Nothing was called yet, but the warm-up already opened the client.
        for _ in range(50):
            if factory.opens:
                break
            await asyncio.sleep(0.01)
        assert factory.opens == 1


async def test_lifespan_closes_the_client_it_opened() -> None:
    factory = _SlowFactory()
    factory.gate.set()
    server = create_server(client_factory=factory)

    async with Client(server):
        pass

    assert factory.closes == 1


async def test_lifespan_shutdown_while_the_open_is_still_in_flight() -> None:
    """A server torn down mid-open cancels the warm-up instead of hanging."""
    factory = _SlowFactory()
    server = create_server(client_factory=factory)

    async with Client(server):
        await asyncio.wait_for(factory.entered.wait(), timeout=5)
    # Never released; shutdown must not wait on it.
    assert factory.opens == 1


async def test_get_opens_once_for_concurrent_callers() -> None:
    """Concurrent tool calls join a single in-flight open, not one open each."""
    factory = _SlowFactory()
    provider = ClientProvider(factory)

    waiters = [asyncio.create_task(provider.get()) for _ in range(5)]
    await asyncio.wait_for(factory.entered.wait(), timeout=5)
    factory.gate.set()

    assert await asyncio.gather(*waiters) == [factory.client] * 5
    assert factory.opens == 1
    await provider.aclose()


async def test_a_cancelled_waiter_does_not_abort_the_shared_open() -> None:
    """One caller's timeout must not cancel the open every other caller joined."""
    factory = _SlowFactory()
    provider = ClientProvider(factory)

    doomed = asyncio.create_task(provider.get())
    survivor = asyncio.create_task(provider.get())
    await asyncio.wait_for(factory.entered.wait(), timeout=5)

    doomed.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await doomed
    factory.gate.set()

    assert await asyncio.wait_for(survivor, timeout=5) is factory.client
    assert factory.opens == 1
    await provider.aclose()


async def test_a_failed_open_is_retried_by_the_next_call() -> None:
    """A failed open is not cached: a mid-session re-login recovers the server."""
    factory = _SlowFactory()
    factory.error = RuntimeError("expired cookies")
    factory.gate.set()
    provider = ClientProvider(factory)

    with pytest.raises(RuntimeError, match="expired cookies"):
        await provider.get()
    assert not provider.is_open

    factory.error = None
    assert await provider.get() is factory.client
    assert factory.opens == 2
    await provider.aclose()


async def test_a_failed_warm_up_is_logged_and_retried(caplog: pytest.LogCaptureFixture) -> None:
    """The unawaited warm-up retrieves its own exception and leaves a breadcrumb."""
    factory = _SlowFactory()
    factory.error = RuntimeError("expired cookies")
    factory.gate.set()
    provider = ClientProvider(factory)

    with caplog.at_level("WARNING", logger="notebooklm.mcp._clientprovider"):
        provider.start()
        for _ in range(100):
            if "expired cookies" in caplog.text:
                break
            await asyncio.sleep(0.01)

    assert "expired cookies" in caplog.text
    factory.error = None
    assert await provider.get() is factory.client
    await provider.aclose()


async def test_start_is_a_no_op_once_open_or_closed() -> None:
    factory = _SlowFactory()
    factory.gate.set()
    provider = ClientProvider(factory)

    await provider.get()
    provider.start()
    assert factory.opens == 1

    await provider.aclose()
    provider.start()
    assert factory.opens == 1


async def test_get_after_close_refuses_instead_of_reopening() -> None:
    factory = _SlowFactory()
    factory.gate.set()
    provider = ClientProvider(factory)
    await provider.get()
    await provider.aclose()

    with pytest.raises(RuntimeError, match="shutting down"):
        await provider.get()
    assert factory.opens == 1


async def test_close_racing_an_in_flight_open_does_not_leak_the_client() -> None:
    """An open that lands after aclose() closes what it opened and hands out nothing."""
    factory = _SlowFactory()
    provider = ClientProvider(factory)

    pending = asyncio.create_task(provider.get())
    await asyncio.wait_for(factory.entered.wait(), timeout=5)
    # Reaching into ``_closed`` is deliberate: this pins the narrow window where
    # ``aclose`` flipped the flag but the open had already passed its last await
    # point, so the task's cancel arrives too late. ``aclose()`` itself cancels
    # the in-flight open (covered above); only the flag reproduces THIS race.
    provider._closed = True
    factory.gate.set()

    with pytest.raises(RuntimeError, match="shutting down"):
        await asyncio.wait_for(pending, timeout=5)
    assert factory.closes == 1
    assert not provider.is_open


async def test_aclose_is_idempotent() -> None:
    factory = _SlowFactory()
    factory.gate.set()
    provider = ClientProvider(factory)
    await provider.get()

    await provider.aclose()
    await provider.aclose()
    assert factory.closes == 1


async def test_of_holds_a_client_it_does_not_own() -> None:
    """The test seam hands back the client without ever opening or closing one."""
    sentinel = cast("NotebookLMClient", MagicMock())
    provider = ClientProvider.of(sentinel)

    assert provider.is_open
    assert await provider.get() is sentinel
    await provider.aclose()  # does not close a client it never opened


async def test_a_stale_done_callback_does_not_clobber_a_newer_open() -> None:
    """A failed open's callback fires a tick later — by then a retry may already
    own the slot, and clearing it would strand that retry's waiters."""
    factory = _SlowFactory()
    provider = ClientProvider(factory)

    live = provider._ensure_open_task()

    async def _boom() -> NotebookLMClient:
        raise RuntimeError("an earlier, already-replaced open")

    stale = asyncio.ensure_future(_boom())
    with contextlib.suppress(RuntimeError):
        await stale
    provider._on_open_done(stale)

    assert provider._open_task is live
    factory.gate.set()
    assert await asyncio.wait_for(live, timeout=5) is factory.client
    await provider.aclose()


async def test_a_failed_open_reaches_the_agent_as_a_categorized_auth_error() -> None:
    """The whole point of #2330: an auth failure must arrive as a real, actionable
    tool error, not an opaque transport timeout — and not a generic UNEXPECTED,
    which is what happens if the lazy open resolves outside ``mcp_errors()``."""
    from fastmcp.exceptions import ToolError

    from notebooklm.exceptions import AuthError

    factory = _SlowFactory()
    factory.error = AuthError("session expired: cookie SID=AAAA1111secret")
    factory.gate.set()

    async with Client(create_server(client_factory=factory)) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool("notebook_list", {})

    message = str(excinfo.value)
    assert message.startswith("AUTH:"), message
    assert "retriable" in message
    assert "AAAA1111secret" not in message  # scrubbed on the way out


async def test_the_warm_up_log_is_scrubbed_at_the_source(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``mcp.__main__`` configures stderr with a bare ``logging.basicConfig``, whose
    root handler carries no ``RedactingFilter`` — so this line scrubs itself."""
    from notebooklm.exceptions import AuthError

    factory = _SlowFactory()
    factory.error = AuthError("session expired: cookie SID=AAAA1111secret")
    factory.gate.set()
    provider = ClientProvider(factory)

    with caplog.at_level("WARNING", logger="notebooklm.mcp._clientprovider"):
        provider.start()
        for _ in range(100):
            if "client open failed" in caplog.text:
                break
            await asyncio.sleep(0.01)

    assert "client open failed" in caplog.text
    assert "AAAA1111secret" not in caplog.text
    await provider.aclose()


async def test_a_stale_failure_is_retrieved_even_when_the_slot_moved_on() -> None:
    """A done-callback runs a tick late — by then a retry may own the slot. The
    failure must still be retrieved, or asyncio logs it through its own handler
    with the raw ``repr``, routing around ``redact`` (#2330 review)."""
    factory = _SlowFactory()
    factory.error = RuntimeError("expired cookies")
    factory.gate.set()
    provider = ClientProvider(factory)

    stale = provider._ensure_open_task()
    # Keep the exception genuinely unretrieved until the callback under test runs.
    # Awaiting the task directly (even under suppress) would consume it and make
    # this regression pass before _on_open_done did any work.
    assert stale.remove_done_callback(provider._on_open_done) == 1
    done, pending = await asyncio.wait({stale})
    assert done == {stale}
    assert not pending
    assert getattr(stale, "_log_traceback", False)
    # Simulate the race the callback guards: something else claimed the slot.
    provider._open_task = None
    provider._on_open_done(stale)

    # The real assertion: the exception was consumed, so asyncio will not later
    # report it as "never retrieved" (which is what bypasses redaction).
    assert not getattr(stale, "_log_traceback", False)
    await provider.aclose()


async def test_aclose_forwards_the_lifespan_exception_to_the_client() -> None:
    """``NotebookLMClient.__aexit__`` demotes a close failure to a WARNING only when
    the body raised. Passing (None, None, None) would claim success and let a close
    error bury the real cause, which the `async with factory()` this replaced never
    did (#2330 review)."""
    seen: list[tuple[object, object]] = []

    @contextlib.asynccontextmanager
    async def factory():
        try:
            yield MagicMock()
        except BaseException as exc:  # what `async with` hands the CM
            seen.append((type(exc), exc))
            raise

    provider = ClientProvider(factory)
    await provider.get()

    boom = ValueError("lifespan blew up")
    await provider.aclose(type(boom), boom, boom.__traceback__)

    assert seen == [(ValueError, boom)]


async def test_shutdown_forwards_the_in_flight_exception() -> None:
    """The lifespan reads ``sys.exc_info()`` in its ``finally`` and hands the triple
    to ``_shutdown``; this pins that it reaches the client context manager."""
    from notebooklm.mcp.server import _shutdown

    seen: list[BaseException] = []

    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[NotebookLMClient]:
        try:
            yield cast("NotebookLMClient", MagicMock())
        except BaseException as exc:
            seen.append(exc)
            raise

    provider = ClientProvider(factory)
    await provider.get()
    state = AppState(client_provider=provider)

    boom = RuntimeError("host went away")
    # ``_shutdown`` does not re-raise: contextlib's ``__aexit__`` returns False when
    # the generator re-raises the exception it was handed, leaving propagation to the
    # ``finally`` that is already unwinding. What matters is that the client SAW it.
    await _shutdown(state, provider, type(boom), boom, boom.__traceback__)

    assert seen == [boom]


async def test_shutdown_reports_a_clean_exit_as_clean() -> None:
    """The same path on the happy exit must NOT claim the body raised."""
    seen: list[BaseException | None] = []

    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[NotebookLMClient]:
        try:
            yield cast("NotebookLMClient", MagicMock())
        except BaseException as exc:  # pragma: no cover - clean path takes the else
            seen.append(exc)
            raise
        else:
            seen.append(None)

    provider = ClientProvider(factory)
    await provider.get()
    state = AppState(client_provider=provider)

    await _shutdown_clean(state, provider)
    assert seen == [None]


async def test_lifespan_honors_client_context_manager_suppression() -> None:
    """The lazy provider preserves the factory CM's normal suppression contract."""

    class SuppressingManager:
        def __init__(self) -> None:
            self.client = cast("NotebookLMClient", MagicMock())
            self.seen: list[tuple[type[BaseException] | None, BaseException | None]] = []

        async def __aenter__(self) -> NotebookLMClient:
            return self.client

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> bool:
            self.seen.append((exc_type, exc))
            return True

    manager = SuppressingManager()
    server = create_server(client_factory=lambda: manager)
    lifespan = server._lifespan(server)
    state = await lifespan.__aenter__()
    await state.client_provider.get()  # ensure the background open has landed

    boom = RuntimeError("lifespan body failed")
    assert await lifespan.__aexit__(type(boom), boom, boom.__traceback__) is True
    assert manager.seen == [(RuntimeError, boom)]


async def _shutdown_clean(state: AppState, provider: ClientProvider) -> None:
    from notebooklm.mcp.server import _shutdown

    await _shutdown(state, provider, None, None, None)
