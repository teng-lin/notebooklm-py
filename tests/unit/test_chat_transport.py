"""Unit tests for :func:`notebooklm._chat_transport.chat_aware_authed_post`.

Exercises the chat-domain error-mapping seam over the generic transport
primitives. Each test injects a stub ``core`` whose ``_perform_authed_post``
raises one of the transport-layer exceptions (or the raw ``httpx``
status error) and asserts the function maps the failure to the expected
``ChatError`` / ``NetworkError`` shape, message, and exception chain.
The drain-tracking bookkeeping (``_begin_transport_post`` /
``_finish_transport_post``) is verified by checking the begin/finish
call counts and the operation-token round-trip.

The stub ``core`` is a lightweight ``SimpleNamespace`` rather than a
``MagicMock(spec=_ChatCore)`` so the tests stay independent of the
protocol's exact member set — they only need the three transport
primitives the function actually calls. After D2 cutover, the function
will run against the real ``ClientCore``; this unit-test layer keeps
the chat-side error mapping isolated from transport implementation
details.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from notebooklm._chat_transport import chat_aware_authed_post
from notebooklm._core_transport import (
    _TransportAuthExpired,
    _TransportRateLimited,
    _TransportServerError,
)
from notebooklm.exceptions import ChatError, NetworkError

# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


_SENTINEL_TOKEN = object()


def _make_request() -> httpx.Request:
    return httpx.Request("POST", "https://example.test/x")


def _make_status_error(code: int, *, retry_after: str | None = None) -> httpx.HTTPStatusError:
    headers = {"retry-after": retry_after} if retry_after else {}
    request = _make_request()
    response = httpx.Response(code, request=request, headers=headers)
    return httpx.HTTPStatusError(f"HTTP {code}", request=request, response=response)


def _make_stub_core(
    *,
    perform_side_effect: Any = None,
    perform_return_value: Any = None,
) -> SimpleNamespace:
    """Build a stub ``core`` matching the slice of ``_ChatCore`` we exercise.

    Pass ``perform_side_effect`` to make ``_perform_authed_post`` raise
    (exception instance) or invoke a callable; pass ``perform_return_value``
    to make it return that response unchanged. Exactly one of the two
    should be supplied per test — they are mutually exclusive.
    """
    return SimpleNamespace(
        _begin_transport_post=AsyncMock(return_value=_SENTINEL_TOKEN),
        _finish_transport_post=AsyncMock(return_value=None),
        _perform_authed_post=AsyncMock(
            side_effect=perform_side_effect,
            return_value=perform_return_value,
        ),
    )


def _noop_build_request(_snapshot: Any) -> tuple[str, str, dict[str, str]]:
    """Build-request stub: the real transport invokes this, our stub does not."""
    return "https://example.test/x", "payload", {}


# ---------------------------------------------------------------------------
# Happy path — bookkeeping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_aware_authed_post_returns_response_and_balances_bookkeeping():
    """Success path: response forwarded; begin/finish tokens balanced."""
    expected_response = httpx.Response(200, request=_make_request())
    core = _make_stub_core(perform_return_value=expected_response)

    result = await chat_aware_authed_post(
        core,  # type: ignore[arg-type]
        build_request=_noop_build_request,
        parse_label="chat.ask",
    )

    assert result is expected_response
    core._begin_transport_post.assert_awaited_once_with("chat.ask")
    core._finish_transport_post.assert_awaited_once_with(_SENTINEL_TOKEN)
    core._perform_authed_post.assert_awaited_once_with(
        build_request=_noop_build_request,
        log_label="chat.ask",
    )


# ---------------------------------------------------------------------------
# _TransportAuthExpired → ChatError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_auth_expired_maps_to_chat_error():
    original = _make_status_error(401)
    transport_exc = _TransportAuthExpired("auth refresh failed", original=original)
    core = _make_stub_core(perform_side_effect=transport_exc)

    with pytest.raises(ChatError) as excinfo:
        await chat_aware_authed_post(
            core,  # type: ignore[arg-type]
            build_request=_noop_build_request,
            parse_label="chat.ask",
        )

    assert "authentication expired" in str(excinfo.value).lower()
    assert "chat.ask" in str(excinfo.value)
    assert excinfo.value.__cause__ is transport_exc
    core._finish_transport_post.assert_awaited_once_with(_SENTINEL_TOKEN)


# ---------------------------------------------------------------------------
# _TransportRateLimited → ChatError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_rate_limited_with_retry_after_includes_retry_seconds():
    original = _make_status_error(429, retry_after="42")
    response = original.response
    transport_exc = _TransportRateLimited(
        "rate-limited",
        retry_after=42,
        response=response,
        original=original,
    )
    core = _make_stub_core(perform_side_effect=transport_exc)

    with pytest.raises(ChatError) as excinfo:
        await chat_aware_authed_post(
            core,  # type: ignore[arg-type]
            build_request=_noop_build_request,
            parse_label="chat.ask",
        )

    message = str(excinfo.value)
    assert "rate-limited" in message
    assert "HTTP 429" in message
    assert "Retry after 42 seconds" in message
    assert excinfo.value.__cause__ is transport_exc
    core._finish_transport_post.assert_awaited_once_with(_SENTINEL_TOKEN)


@pytest.mark.asyncio
async def test_transport_rate_limited_without_retry_after_omits_retry_clause():
    original = _make_status_error(429)
    response = original.response
    transport_exc = _TransportRateLimited(
        "rate-limited",
        retry_after=None,
        response=response,
        original=original,
    )
    core = _make_stub_core(perform_side_effect=transport_exc)

    with pytest.raises(ChatError) as excinfo:
        await chat_aware_authed_post(
            core,  # type: ignore[arg-type]
            build_request=_noop_build_request,
            parse_label="chat.ask",
        )

    message = str(excinfo.value)
    assert "rate-limited" in message
    assert "HTTP 429" in message
    assert "Retry after" not in message  # No "Retry after N seconds" clause.
    assert excinfo.value.__cause__ is transport_exc
    core._finish_transport_post.assert_awaited_once_with(_SENTINEL_TOKEN)


# ---------------------------------------------------------------------------
# _TransportServerError variants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_server_error_with_http_status_error_maps_to_chat_error():
    original = _make_status_error(503)
    transport_exc = _TransportServerError(
        "5xx after retries",
        original=original,
        response=original.response,
        status_code=503,
    )
    core = _make_stub_core(perform_side_effect=transport_exc)

    with pytest.raises(ChatError) as excinfo:
        await chat_aware_authed_post(
            core,  # type: ignore[arg-type]
            build_request=_noop_build_request,
            parse_label="chat.ask",
        )

    message = str(excinfo.value)
    assert "HTTP 503" in message
    assert "after retries" in message
    assert excinfo.value.__cause__ is transport_exc
    core._finish_transport_post.assert_awaited_once_with(_SENTINEL_TOKEN)


@pytest.mark.asyncio
async def test_transport_server_error_with_request_error_maps_to_network_error():
    original = httpx.RequestError("connect failed", request=_make_request())
    transport_exc = _TransportServerError("network failure", original=original)
    core = _make_stub_core(perform_side_effect=transport_exc)

    with pytest.raises(NetworkError) as excinfo:
        await chat_aware_authed_post(
            core,  # type: ignore[arg-type]
            build_request=_noop_build_request,
            parse_label="chat.ask",
        )

    message = str(excinfo.value)
    assert "network error after retries" in message
    assert "timed out" not in message
    assert excinfo.value.original_error is original
    assert excinfo.value.__cause__ is transport_exc
    core._finish_transport_post.assert_awaited_once_with(_SENTINEL_TOKEN)


@pytest.mark.asyncio
async def test_transport_server_error_with_timeout_exception_keeps_timeout_message():
    """Regression: ``httpx.TimeoutException`` is a subclass of
    ``httpx.RequestError``; without the explicit timeout branch the message
    would collapse to the generic "network error after retries" line."""
    original = httpx.ReadTimeout("read timed out", request=_make_request())
    transport_exc = _TransportServerError("timeout", original=original)
    core = _make_stub_core(perform_side_effect=transport_exc)

    with pytest.raises(NetworkError) as excinfo:
        await chat_aware_authed_post(
            core,  # type: ignore[arg-type]
            build_request=_noop_build_request,
            parse_label="chat.ask",
        )

    message = str(excinfo.value)
    assert "timed out after retries" in message
    assert "network error after retries" not in message
    assert excinfo.value.original_error is original
    assert excinfo.value.__cause__ is transport_exc
    core._finish_transport_post.assert_awaited_once_with(_SENTINEL_TOKEN)


@pytest.mark.asyncio
async def test_transport_server_error_with_unexpected_original_type_raises_type_error():
    """Defensive: ``_perform_authed_post`` should only wrap
    ``HTTPStatusError`` / ``RequestError`` into ``_TransportServerError``.
    Anything else surfaces as ``TypeError`` so an invariant drift is loud
    rather than silently mis-mapped."""

    class _UnexpectedException(Exception):
        pass

    original = _UnexpectedException("not http, not request")
    transport_exc = _TransportServerError("bogus original", original=original)
    core = _make_stub_core(perform_side_effect=transport_exc)

    with pytest.raises(TypeError) as excinfo:
        await chat_aware_authed_post(
            core,  # type: ignore[arg-type]
            build_request=_noop_build_request,
            parse_label="chat.ask",
        )

    message = str(excinfo.value)
    assert "_TransportServerError.original" in message
    # The diagnostic must include both the actual type and the expected
    # types so a future invariant drift produces an actionable error
    # (per gemini-code-assist review on PR #832).
    assert "Expected httpx.HTTPStatusError or httpx.RequestError" in message
    assert excinfo.value.__cause__ is transport_exc
    core._finish_transport_post.assert_awaited_once_with(_SENTINEL_TOKEN)


# ---------------------------------------------------------------------------
# Raw httpx.HTTPStatusError fall-through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raw_http_status_error_maps_to_chat_error():
    """Non-401 / non-429 / non-5xx status errors that fall through
    ``_perform_authed_post`` reach this layer as raw
    ``httpx.HTTPStatusError`` and get wrapped in a ``ChatError`` that
    surfaces the status code."""
    raw_exc = _make_status_error(404)
    core = _make_stub_core(perform_side_effect=raw_exc)

    with pytest.raises(ChatError) as excinfo:
        await chat_aware_authed_post(
            core,  # type: ignore[arg-type]
            build_request=_noop_build_request,
            parse_label="chat.ask",
        )

    message = str(excinfo.value)
    assert "HTTP 404" in message
    assert "chat.ask" in message
    assert excinfo.value.__cause__ is raw_exc
    core._finish_transport_post.assert_awaited_once_with(_SENTINEL_TOKEN)


# ---------------------------------------------------------------------------
# Finalization invariant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finish_transport_post_runs_even_when_perform_raises():
    """The drain-tracking ``finish`` must always run in the ``finally``
    clause; otherwise a chat-error path could leak in-flight counters."""
    transport_exc = _TransportAuthExpired(
        "auth expired",
        original=_make_status_error(401),
    )
    core = _make_stub_core(perform_side_effect=transport_exc)

    with pytest.raises(ChatError):
        await chat_aware_authed_post(
            core,  # type: ignore[arg-type]
            build_request=_noop_build_request,
            parse_label="chat.ask",
        )

    core._finish_transport_post.assert_awaited_once_with(_SENTINEL_TOKEN)
