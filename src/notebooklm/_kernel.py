"""Concrete transport kernel for NotebookLM session operations."""

from __future__ import annotations

from collections.abc import Callable, Mapping

import httpx

from ._core_transport import _PostBody, _stream_post_with_size_cap
from .auth import AuthTokens, build_cookie_jar
from .types import ConnectionLimits


class Kernel:
    """Own the live HTTP transport and cookie jar.

    Session lifecycle code decides when to open and close. The kernel owns the
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

    @property
    def http_client(self) -> httpx.AsyncClient | None:
        """Return the live HTTP client, or ``None`` when closed."""
        return self._http_client

    @http_client.setter
    def http_client(self, value: httpx.AsyncClient | None) -> None:
        # Test-injection seam for tests that assign through Session._http_client.
        self._http_client = value

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
        capture_cookie_snapshot(self._http_client.cookies)

    async def post(
        self,
        url: str,
        headers: Mapping[str, str] | None,
        body: _PostBody,
    ) -> httpx.Response:
        """Issue a raw buffered POST through the live HTTP client."""
        return await _stream_post_with_size_cap(
            self.get_http_client(),
            url,
            body=body,
            headers=dict(headers) if headers is not None else None,
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


__all__ = ["Kernel"]
