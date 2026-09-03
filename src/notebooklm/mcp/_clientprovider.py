"""Lazy, single-flight ownership of the process-wide NotebookLM client.

Issue #2330: the FastMCP lifespan used to *open* the client before yielding, so
the MCP ``initialize`` handshake could not be answered until Google's auth
round-trip finished. That handshake is on a client-side deadline (Claude Code
gives it 30 s), while the open path's own budget is larger — a 15 s
``RotateCookies`` poke plus a 30 s CSRF fetch on the happy rung, and more when
the cold-recovery ladder (refresh command / headless re-auth / master-token
re-mint) runs. A slow or rate-limited Google therefore surfaced to the user as
an opaque ``CONNECT_TIMEOUT``, and each retry spawned a fresh process that
redid the same work.

This provider decouples the two. The lifespan constructs one and *warms* it in
the background, then yields immediately, so ``initialize`` answers at once. The
open is awaited at the first tool call instead, where a failure is reported as a
real, categorized tool error rather than a dead transport.

Contract:

* **One client per process, one open at a time.** Concurrent callers join a
  single in-flight open (``asyncio.Task``) rather than racing their own.
* **A caller's cancellation never aborts the shared open.** Waiters
  :func:`asyncio.shield` the task, so a client-side timeout on one tool call
  leaves the warm-up running for everyone else.
* **Failures are retried, not cached.** A failed open clears the slot so the
  next tool call tries again — that is how a mid-session ``notebooklm login``
  recovers a server that started with expired cookies, without a restart.
* **Loop affinity (ADR-0004).** The provider is created and awaited entirely on
  the server's event loop, so the client stays bound to the loop that opened it.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import TYPE_CHECKING

from .._redact import redact

if TYPE_CHECKING:
    from ..client import NotebookLMClient

__all__ = ["ClientProvider"]

logger = logging.getLogger(__name__)

#: A factory returns an async-context-manager that yields the client. Matches
#: ``notebooklm.mcp.server.ClientFactory``; duplicated as a local alias so this
#: module does not import ``server`` (which imports this one).
ClientFactory = Callable[[], AbstractAsyncContextManager["NotebookLMClient"]]


class ClientProvider:
    """Own the lazily-opened, process-wide client behind a single-flight open."""

    def __init__(self, factory: ClientFactory) -> None:
        self._factory = factory
        self._cm: AbstractAsyncContextManager[NotebookLMClient] | None = None
        self._client: NotebookLMClient | None = None
        self._open_task: asyncio.Task[NotebookLMClient] | None = None
        self._closed = False

    @classmethod
    def of(cls, client: NotebookLMClient) -> ClientProvider:
        """Return a provider already holding ``client`` (test seam).

        The provider does not own ``client``: :meth:`aclose` leaves it open,
        because whoever built it keeps the close responsibility.
        """

        def _factory() -> AbstractAsyncContextManager[NotebookLMClient]:  # pragma: no cover
            raise AssertionError("ClientProvider.of() never opens a client")

        provider = cls(_factory)
        provider._client = client
        return provider

    @property
    def is_open(self) -> bool:
        """Whether the client is already open (no round-trip needed to use it)."""
        return self._client is not None

    def start(self) -> None:
        """Kick off the background warm-up open, if one is not already running.

        Fire-and-forget: the caller does not await it, so the handshake is not
        gated on Google. A warm-up failure is logged (and the slot cleared) so
        the first real tool call retries and reports the error to the agent.
        """
        if self._client is not None or self._closed:
            return
        self._ensure_open_task()

    async def get(self) -> NotebookLMClient:
        """Return the open client, opening it (or joining an in-flight open) first.

        Raises:
            RuntimeError: the provider has been closed (server shutting down).
            Exception: whatever the open path raised — an auth/network failure is
                surfaced to the tool caller, which projects it onto an MCP error.
        """
        client = self._client
        if client is not None:
            return client
        if self._closed:
            raise RuntimeError("The MCP server is shutting down; its NotebookLM client is closed.")
        # shield: a caller that gets cancelled (client-side timeout, disconnect)
        # must not cancel the open that every other waiter is also joined to.
        return await asyncio.shield(self._ensure_open_task())

    def _ensure_open_task(self) -> asyncio.Task[NotebookLMClient]:
        task = self._open_task
        if task is None or task.done():
            task = asyncio.ensure_future(self._open())
            self._open_task = task
            task.add_done_callback(self._on_open_done)
        return task

    def _on_open_done(self, task: asyncio.Task[NotebookLMClient]) -> None:
        """Clear the slot after a failed/cancelled open so the next call retries.

        The failure is retrieved FIRST and the identity check only guards the slot
        mutation. A callback runs a tick after its task settles, by which time a
        retry (or ``aclose``) may already own the slot — and returning early there
        would leave the exception unretrieved, so asyncio would later log it through
        its default handler with the raw ``repr``, routing around the ``redact``
        below. The identity check still has to exist: without it this callback would
        clear a *newer* task's slot and strand its waiters.
        """
        if task.cancelled():
            if self._open_task is task:
                self._open_task = None
            return
        error = task.exception()
        if error is not None:
            if self._open_task is task:
                self._open_task = None
            # Warm-up has no awaiter, so log here to both retrieve the exception
            # (no "never retrieved" warning) and leave a breadcrumb on stderr. A
            # tool call that joined this task still receives the real exception.
            #
            # Scrubbed at the SOURCE, not left to the logging pipeline: an open
            # failure is an auth error more often than not, so its text can carry
            # cookie values or the on-disk storage path — and ``mcp.__main__``
            # configures stderr with a bare ``logging.basicConfig``, whose root
            # handler carries no ``RedactingFilter``.
            logger.warning(
                "NotebookLM client open failed (will retry on next use): %s", redact(error)
            )

    async def _open(self) -> NotebookLMClient:
        cm = self._factory()
        client = await cm.__aenter__()
        if self._closed:
            # Shut down while we were opening — hand nothing out; close what we
            # just opened rather than leaking its connection pool.
            await cm.__aexit__(None, None, None)
            raise RuntimeError("The MCP server is shutting down; its NotebookLM client is closed.")
        self._cm = cm
        self._client = client
        return client

    async def aclose(
        self,
        exc_type: type[BaseException] | None = None,
        exc: BaseException | None = None,
        tb: TracebackType | None = None,
    ) -> bool | None:
        """Close the client if it was opened, cancelling any in-flight open.

        Idempotent. After this the provider refuses to hand out a client.

        The exception triple is forwarded verbatim to the factory context manager,
        exactly as the ``async with factory()`` this replaced used to do.
        ``NotebookLMClient.__aexit__`` arbitrates on it: when the lifespan body
        raised, a failure to close is demoted to a WARNING so it cannot mask the
        original cause. Passing ``(None, None, None)`` unconditionally would claim
        the body succeeded and let a close error bury a real lifespan failure. The
        context manager's suppression result is returned to the lifespan unchanged.
        """
        self._closed = True
        # Cancel while the task is still registered so the done-callback clears
        # the slot through its normal path; the reset below is belt-and-braces.
        task = self._open_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        self._open_task = None
        cm, self._cm = self._cm, None
        self._client = None
        if cm is not None:
            return await cm.__aexit__(exc_type, exc, tb)
        return None
