"""Concrete transport kernel for NotebookLM session operations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import httpx

from ..._loop_bound import EpochFenced
from ...auth import AuthTokens, build_cookie_jar
from ...types import ConnectionLimits
from .request_types import PostBody
from .streaming_post import stream_post_with_size_cap


class Kernel(EpochFenced):
    """Own the live HTTP transport and cookie jar.

    Client lifecycle code decides when to open and close. The kernel owns the
    concrete ``httpx.AsyncClient`` instance, its cookie jar, raw POST execution,
    and shielded teardown target.
    """

    def __init__(
        self,
        *,
        auth: AuthTokens | None = None,
        async_client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        super().__init__("NotebookLMClient resource generation is retired")
        self._async_client_factory = async_client_factory
        self._http_client: httpx.AsyncClient | None = None
        # The kernel owns the one mutable cookie jar for the whole client
        # lifetime, including the network-free pre-open and post-close states.
        # A composed client seeds this once from AuthTokens' bootstrap shadow;
        # after open, ``get_cookies`` resolves directly to the transport jar.
        self._cookies = self._bootstrap_cookies(auth) if auth is not None else None
        self._timeout: float | None = None
        self._connect_timeout: float | None = None

    def activate(self, epoch: int) -> None:
        """Activate ``epoch`` before a live client can be published."""
        if self._http_client is not None and self._active_epoch != epoch:
            raise RuntimeError("Cannot replace a live web transport generation.")
        super().activate(epoch)

    @staticmethod
    def _bootstrap_cookies(auth: AuthTokens) -> httpx.Cookies:
        """Copy the compatibility bootstrap projection into kernel ownership."""
        cookie_jar = auth.cookie_jar
        seed = (
            cookie_jar
            if cookie_jar is not None
            else build_cookie_jar(
                cookies=auth.cookies,
                storage_path=auth.storage_path,
            )
        )
        return httpx.Cookies(seed)

    @property
    def http_client(self) -> httpx.AsyncClient | None:
        """Return the live HTTP client, or ``None`` when closed.

        The property is read-only by design. Production code mutates the
        underlying client only through :meth:`open` (which constructs the
        live client via the injected ``async_client_factory``) and
        :meth:`aclose` (which nulls it on teardown). Tests that need to
        substitute the live transport at construction time should inject
        an ``async_client_factory`` into the client composition helper
        (the factory is forwarded into this kernel's ``__init__``); tests
        that need to swap the live client AFTER ``open()`` should use the
        dedicated test helper at
        ``tests/_fixtures/kernel_test_helpers.py``.
        """
        return self._http_client

    @property
    def cookies(self) -> httpx.Cookies:
        """Return the kernel-owned cookie jar.

        A composed client seeds the jar during construction, so network-free
        identity reads work before :meth:`open` and after :meth:`aclose`.
        Direct ``Kernel()`` callers that supplied neither ``auth=`` nor called
        :meth:`open` still receive ``RuntimeError``.
        """
        return self.get_cookies()

    def get_cookies(self, *, expected_epoch: int | None = None) -> httpx.Cookies:
        """Return the current transport jar or its retained closed-state owner.

        Network-free identity reads deliberately omit ``expected_epoch`` so
        they remain available before open and after close.  A live workflow
        supplies its admitted epoch and is fenced before it can observe a
        reopened generation's cookie jar.
        """
        if expected_epoch is not None:
            self.assert_epoch(expected_epoch)
        if self._http_client is not None:
            return self._http_client.cookies
        if self._cookies is None:
            raise RuntimeError("Cookie jar not initialized. Open the client first.")
        return self._cookies

    def get_http_client(self, *, expected_epoch: int | None = None) -> httpx.AsyncClient:
        """Return the live HTTP client or raise the legacy not-open error."""
        if expected_epoch is not None:
            self.assert_epoch(expected_epoch)
        if self._http_client is None:
            raise RuntimeError("Client not initialized. Use 'async with' context.")
        return self._http_client

    async def open(
        self,
        *,
        auth: AuthTokens,
        timeout: float,
        connect_timeout: float,
        limits: ConnectionLimits,
        capture_cookie_snapshot: Callable[[httpx.Cookies], object],
        expected_epoch: int | None = None,
    ) -> None:
        """Build the HTTP client and capture its normalized cookie baseline."""
        if expected_epoch is not None:
            self.assert_epoch(expected_epoch)
        # ClientLifecycle owns the primary idempotency guard. Keep this
        # secondary guard so direct Kernel callers also preserve the live client.
        if self._http_client is not None:
            return

        http_timeout = httpx.Timeout(
            connect=connect_timeout,
            read=timeout,
            write=timeout,
            pool=timeout,
        )
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        # Direct Kernel callers seed here; composed clients already seeded at
        # construction so account identity can be resolved before open. This
        # helper is the ONE legitimate internal read of AuthTokens' cookie
        # shadows. It becomes ``auth.initial_cookies`` in ADR-0032 Phase B.
        if self._cookies is None:
            self._cookies = self._bootstrap_cookies(auth)

        self._http_client = self._async_client_factory(
            headers={
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            },
            cookies=self._cookies,
            timeout=http_timeout,
            follow_redirects=True,
            limits=limits.to_httpx_limits(),
        )
        # ``httpx.AsyncClient`` copies the seed. Retire the pre-open jar and
        # retain the transport-owned object as the kernel's sole authority.
        self._cookies = self._http_client.cookies
        # If the snapshot raises, __aenter__ has effectively failed, so Python
        # never calls __aexit__ and the freshly built client would leak its
        # connection pool. Close it and reset state before re-raising so a
        # partial open cannot orphan a live client.
        try:
            capture_cookie_snapshot(self._http_client.cookies)
        except BaseException as open_error:
            cleanup_error: BaseException | None = None
            try:
                await self.aclose()
            except BaseException as exc:
                cleanup_error = exc

            # The original open failure beats ordinary cleanup failure.  A
            # process-exit signal from either phase still wins, with the
            # earlier open-phase signal taking precedence when both exist.
            if isinstance(open_error, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(cleanup_error, (KeyboardInterrupt, SystemExit)):
                raise cleanup_error from open_error
            raise

    async def post(
        self,
        url: str,
        headers: Mapping[str, str] | None,
        body: PostBody,
        *,
        read_timeout: float | None = None,
        max_response_bytes: int | None = None,
        expected_epoch: int | None = None,
    ) -> httpx.Response:
        """Issue a raw buffered POST through the live HTTP client."""
        timeout_override: httpx.Timeout | None = None
        if read_timeout is not None:
            # Widen ONLY the read (inactivity) slot; keep connect/write/pool at
            # the stored base values verbatim — including ``None``, which httpx
            # treats as "no timeout", so an explicit infinite-timeout client
            # keeps that intent on its other slots instead of inheriting the
            # chat read window.
            timeout_override = httpx.Timeout(
                connect=self._connect_timeout,
                read=read_timeout,
                write=self._timeout,
                pool=self._timeout,
            )
        headers_arg = dict(headers) if headers is not None else None
        stream_kwargs: dict[str, Any] = {}
        if max_response_bytes is not None:
            stream_kwargs["max_bytes"] = max_response_bytes
        return await stream_post_with_size_cap(
            self.get_http_client(expected_epoch=expected_epoch),
            url,
            body=body,
            headers=headers_arg,
            timeout=timeout_override,
            **stream_kwargs,
        )

    async def aclose(self) -> None:
        """Close the live HTTP client and mark the kernel closed."""
        client = self._http_client
        if client is None:
            return
        try:
            await client.aclose()
        finally:
            # Retain the exact transport jar (not a detached snapshot) for
            # network-free identity reads while the transport is closed and as
            # the seed for a later reopen of this same client.
            self._cookies = client.cookies
            self._http_client = None
            self._timeout = None
            self._connect_timeout = None
            self.fence()


__all__ = ["Kernel"]
