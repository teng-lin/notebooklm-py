"""Unit tests for the concrete transport Kernel."""

from __future__ import annotations

import httpx
import pytest

import notebooklm._kernel as kernel_module
from notebooklm._kernel import Kernel
from notebooklm.auth import AuthTokens
from notebooklm.types import ConnectionLimits


def _auth_tokens() -> AuthTokens:
    return AuthTokens(
        csrf_token="csrf",
        session_id="sid",
        cookies={"SID": "cookie-value"},
        storage_path=None,
    )


@pytest.mark.asyncio
async def test_open_builds_http_client_and_captures_live_cookie_snapshot() -> None:
    kernel = Kernel()
    captured: list[httpx.Cookies] = []

    await kernel.open(
        auth=_auth_tokens(),
        timeout=30.0,
        connect_timeout=10.0,
        limits=ConnectionLimits(),
        capture_cookie_snapshot=captured.append,
    )
    try:
        assert kernel.http_client is not None
        assert len(captured) == 1
        assert captured[0] is kernel.cookies
    finally:
        await kernel.aclose()


@pytest.mark.asyncio
async def test_open_is_idempotent() -> None:
    kernel = Kernel()
    captured: list[httpx.Cookies] = []

    await kernel.open(
        auth=_auth_tokens(),
        timeout=30.0,
        connect_timeout=10.0,
        limits=ConnectionLimits(),
        capture_cookie_snapshot=captured.append,
    )
    first_client = kernel.http_client
    await kernel.open(
        auth=_auth_tokens(),
        timeout=30.0,
        connect_timeout=10.0,
        limits=ConnectionLimits(),
        capture_cookie_snapshot=captured.append,
    )
    try:
        assert kernel.http_client is first_client
        assert len(captured) == 1
    finally:
        await kernel.aclose()


@pytest.mark.asyncio
async def test_open_preserves_explicit_empty_cookie_jar(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_build_cookie_jar(**_: object) -> httpx.Cookies:
        raise AssertionError("explicit cookie_jar should not be rebuilt")

    monkeypatch.setattr(kernel_module, "build_cookie_jar", fail_build_cookie_jar)
    kernel = Kernel()
    await kernel.open(
        auth=AuthTokens(
            csrf_token="csrf",
            session_id="sid",
            cookies={},
            cookie_jar=httpx.Cookies(),
            storage_path=None,
        ),
        timeout=30.0,
        connect_timeout=10.0,
        limits=ConnectionLimits(),
        capture_cookie_snapshot=lambda _: None,
    )
    try:
        assert kernel.http_client is not None
    finally:
        await kernel.aclose()


@pytest.mark.asyncio
async def test_open_closes_client_when_cookie_snapshot_raises() -> None:
    closed: list[httpx.AsyncClient] = []

    class _TrackingClient(httpx.AsyncClient):
        async def aclose(self) -> None:
            closed.append(self)
            await super().aclose()

    def async_client_factory(**kwargs: object) -> httpx.AsyncClient:
        return _TrackingClient(**kwargs)  # type: ignore[arg-type]

    kernel = Kernel(async_client_factory=async_client_factory)

    def boom(_: httpx.Cookies) -> None:
        raise RuntimeError("snapshot failed")

    with pytest.raises(RuntimeError, match="snapshot failed"):
        await kernel.open(
            auth=_auth_tokens(),
            timeout=30.0,
            connect_timeout=10.0,
            limits=ConnectionLimits(),
            capture_cookie_snapshot=boom,
        )

    # The partially opened client must have been closed and the kernel reset so
    # it does not leak a live connection pool (issue #1163).
    assert kernel.http_client is None
    assert len(closed) == 1
    assert closed[0].is_closed


@pytest.mark.asyncio
async def test_post_uses_live_http_client_streaming_post() -> None:
    seen_requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, content=b"ok")

    transport = httpx.MockTransport(handler)

    def async_client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)  # type: ignore[arg-type]

    kernel = Kernel(async_client_factory=async_client_factory)
    await kernel.open(
        auth=_auth_tokens(),
        timeout=30.0,
        connect_timeout=10.0,
        limits=ConnectionLimits(),
        capture_cookie_snapshot=lambda _: None,
    )
    try:
        response = await kernel.post(
            "https://example.com/batchexecute",
            headers={"X-Test": "yes"},
            body=b"payload",
        )
    finally:
        await kernel.aclose()

    assert response.text == "ok"
    assert len(seen_requests) == 1
    request = seen_requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://example.com/batchexecute"
    assert request.headers["X-Test"] == "yes"
    assert request.content == b"payload"


@pytest.mark.asyncio
async def test_post_read_timeout_override_preserves_other_timeout_slots() -> None:
    seen_requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, content=b"ok")

    transport = httpx.MockTransport(handler)

    def async_client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)  # type: ignore[arg-type]

    kernel = Kernel(async_client_factory=async_client_factory)
    await kernel.open(
        auth=_auth_tokens(),
        timeout=30.0,
        connect_timeout=10.0,
        limits=ConnectionLimits(),
        capture_cookie_snapshot=lambda _: None,
    )
    try:
        await kernel.post(
            "https://example.com/chat",
            headers={},
            body=b"payload",
            read_timeout=300.0,
        )
    finally:
        await kernel.aclose()

    timeout = seen_requests[0].extensions["timeout"]
    assert timeout["connect"] == 10.0
    assert timeout["read"] == 300.0
    assert timeout["write"] == 30.0
    assert timeout["pool"] == 30.0


@pytest.mark.asyncio
async def test_aclose_marks_kernel_closed_and_is_idempotent() -> None:
    kernel = Kernel()
    await kernel.open(
        auth=_auth_tokens(),
        timeout=30.0,
        connect_timeout=10.0,
        limits=ConnectionLimits(),
        capture_cookie_snapshot=lambda _: None,
    )

    await kernel.aclose()
    await kernel.aclose()

    assert kernel.http_client is None
    with pytest.raises(RuntimeError, match="Client not initialized"):
        kernel.get_http_client()
