"""Concrete transport kernel for NotebookLM session operations."""

from __future__ import annotations

from collections.abc import Callable, Mapping

import httpx

from ._request_types import PostBody
from ._streaming_post import stream_post_with_size_cap
from .auth import AuthTokens, build_cookie_jar
from .types import ConnectionLimits


class Kernel:
    """Own the live HTTP transport and cookie jar.

    Client lifecycle code decides when to open and close. The kernel owns the
    concrete ``httpx.AsyncClient`` instance, its cookie jar, raw POST execution,
    and shielded teardown target.
    """

    def __init__(
        self,
        *,
        async_client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        self._async_client_factory = async_client_factory
        self._http_client: httpx.AsyncClient | None = None
        self._timeout: float | None = None
        self._connect_timeout: float | None = None

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
        """Return the live HTTP client's cookie jar.

        Raises ``RuntimeError`` if called before :meth:`open`.
        """
        return self.get_http_client().cookies

    def get_http_client(self) -> httpx.AsyncClient:
        """Return the live HTTP client or raise the legacy not-open error."""
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
    ) -> None:
        """Build the HTTP client and capture its normalized cookie baseline."""
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
        cookies = (
            auth.cookie_jar
            if auth.cookie_jar is not None
            else build_cookie_jar(
                cookies=auth.cookies,
                storage_path=auth.storage_path,
            )
        )

        self._http_client = self._async_client_factory(
            headers={
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            },
            cookies=cookies,
            timeout=http_timeout,
            follow_redirects=True,
            limits=limits.to_httpx_limits(),
        )
        # If the snapshot raises, __aenter__ has effectively failed, so Python
        # never calls __aexit__ and the freshly built client would leak its
        # connection pool. Close it and reset state before re-raising so a
        # partial open cannot orphan a live client.
        try:
            capture_cookie_snapshot(self._http_client.cookies)
        except BaseException:
            await self.aclose()
            raise

    async def post(
        self,
        url: str,
        headers: Mapping[str, str] | None,
        body: PostBody,
        *,
        read_timeout: float | None = None,
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
        return await stream_post_with_size_cap(
            self.get_http_client(),
            url,
            body=body,
            headers=dict(headers) if headers is not None else None,
            timeout=timeout_override,
        )

    async def aclose(self) -> None:
        """Close the live HTTP client and mark the kernel closed."""
        client = self._http_client
        if client is None:
            return
        try:
            await client.aclose()
        finally:
            self._http_client = None
            self._timeout = None
            self._connect_timeout = None


__all__ = ["Kernel"]
