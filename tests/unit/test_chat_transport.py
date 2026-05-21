"""Unit tests for :func:`notebooklm._chat_transport.chat_aware_authed_post`.

Exercises the chat-domain error-mapping seam over the generic transport
primitives. Each test injects a stub ``session`` whose ``transport_post``
raises one of the transport-layer exceptions (or the raw ``httpx``
status error) and asserts the function maps the failure to the expected
``ChatError`` / ``NetworkError`` shape, message, and exception chain.

As of Tier-12 PR 12.5 the drain-tracking bookkeeping
(``_begin_transport_post`` / ``_finish_transport_post``) has moved into
``DrainMiddleware`` at the outermost chain position around
``Session.transport_post``. ``chat_aware_authed_post`` no longer brackets
its own transport call with explicit drain calls —
admission and finalization are middleware concerns now. The tests
correspondingly stub only ``transport_post`` on the session.

The stub ``session`` is a lightweight ``SimpleNamespace`` rather than a
``MagicMock(spec=Session)`` so the tests stay independent of the
protocol's exact member set — they only need the transport
primitive the function actually calls. The drain-fires-on-exception
invariant is now covered by
``tests/unit/test_drain_middleware.py::test_finish_fires_on_exception``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from notebooklm._authed_transport import (
    TransportAuthExpired,
    TransportRateLimited,
    TransportServerError,
)
from notebooklm._chat_transport import chat_aware_authed_post
from notebooklm.exceptions import ChatError, NetworkError

# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


def _make_request() -> httpx.Request:
    return httpx.Request("POST", "https://example.test/x")


def _make_status_error(code: int, *, retry_after: str | None = None) -> httpx.HTTPStatusError:
    headers = {"retry-after": retry_after} if retry_after else {}
    request = _make_request()
    response = httpx.Response(code, request=request, headers=headers)
    return httpx.HTTPStatusError(f"HTTP {code}", request=request, response=response)


def _make_stub_session(
    *,
    transport_side_effect: Any = None,
    transport_return_value: Any = None,
) -> SimpleNamespace:
    """Build a stub ``session`` matching the slice of ``Session`` we exercise.

    Pass ``transport_side_effect`` to make ``transport_post`` raise
    (exception instance) or invoke a callable; pass ``transport_return_value``
    to make it return that response unchanged. Exactly one of the two
    should be supplied per test — they are mutually exclusive.

    PR 12.5 lifted ``_begin_transport_post`` / ``_finish_transport_post``
    into DrainMiddleware, so the stub no longer needs to mock them —
    ``chat_aware_authed_post`` does not call them.
    """
    return SimpleNamespace(
        transport_post=AsyncMock(
            side_effect=transport_side_effect,
            return_value=transport_return_value,
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
    session = _make_stub_session(transport_return_value=expected_response)

    result = await chat_aware_authed_post(
        session,  # type: ignore[arg-type]
        build_request=_noop_build_request,
        parse_label="chat.ask",
    )

    assert result is expected_response
    session.transport_post.assert_awaited_once_with(
        build_request=_noop_build_request,
        parse_label="chat.ask",
    )


# ---------------------------------------------------------------------------
# TransportAuthExpired → ChatError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_auth_expired_maps_to_chat_error():
    original = _make_status_error(401)
    transport_exc = TransportAuthExpired("auth refresh failed", original=original)
    session = _make_stub_session(transport_side_effect=transport_exc)

    with pytest.raises(ChatError) as excinfo:
        await chat_aware_authed_post(
            session,  # type: ignore[arg-type]
            build_request=_noop_build_request,
            parse_label="chat.ask",
        )

    assert "authentication expired" in str(excinfo.value).lower()
    assert "chat.ask" in str(excinfo.value)
    assert excinfo.value.__cause__ is transport_exc


# ---------------------------------------------------------------------------
# TransportRateLimited → ChatError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_rate_limited_with_retry_after_includes_retry_seconds():
    original = _make_status_error(429, retry_after="42")
    response = original.response
    transport_exc = TransportRateLimited(
        "rate-limited",
        retry_after=42,
        response=response,
        original=original,
    )
    session = _make_stub_session(transport_side_effect=transport_exc)

    with pytest.raises(ChatError) as excinfo:
        await chat_aware_authed_post(
            session,  # type: ignore[arg-type]
            build_request=_noop_build_request,
            parse_label="chat.ask",
        )

    message = str(excinfo.value)
    assert "rate-limited" in message
    assert "HTTP 429" in message
    assert "Retry after 42 seconds" in message
    assert excinfo.value.__cause__ is transport_exc


@pytest.mark.asyncio
async def test_transport_rate_limited_without_retry_after_omits_retry_clause():
    original = _make_status_error(429)
    response = original.response
    transport_exc = TransportRateLimited(
        "rate-limited",
        retry_after=None,
        response=response,
        original=original,
    )
    session = _make_stub_session(transport_side_effect=transport_exc)

    with pytest.raises(ChatError) as excinfo:
        await chat_aware_authed_post(
            session,  # type: ignore[arg-type]
            build_request=_noop_build_request,
            parse_label="chat.ask",
        )

    message = str(excinfo.value)
    assert "rate-limited" in message
    assert "HTTP 429" in message
    assert "Retry after" not in message  # No "Retry after N seconds" clause.
    assert excinfo.value.__cause__ is transport_exc


# ---------------------------------------------------------------------------
# TransportServerError variants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_server_error_with_http_status_error_maps_to_chat_error():
    original = _make_status_error(503)
    transport_exc = TransportServerError(
        "5xx after retries",
        original=original,
        response=original.response,
        status_code=503,
    )
    session = _make_stub_session(transport_side_effect=transport_exc)

    with pytest.raises(ChatError) as excinfo:
        await chat_aware_authed_post(
            session,  # type: ignore[arg-type]
            build_request=_noop_build_request,
            parse_label="chat.ask",
        )

    message = str(excinfo.value)
    assert "HTTP 503" in message
    assert "after retries" in message
    assert excinfo.value.__cause__ is transport_exc


@pytest.mark.asyncio
async def test_transport_server_error_with_request_error_maps_to_network_error():
    original = httpx.RequestError("connect failed", request=_make_request())
    transport_exc = TransportServerError("network failure", original=original)
    session = _make_stub_session(transport_side_effect=transport_exc)

    with pytest.raises(NetworkError) as excinfo:
        await chat_aware_authed_post(
            session,  # type: ignore[arg-type]
            build_request=_noop_build_request,
            parse_label="chat.ask",
        )

    message = str(excinfo.value)
    assert "network error after retries" in message
    assert "timed out" not in message
    assert excinfo.value.original_error is original
    assert excinfo.value.__cause__ is transport_exc


@pytest.mark.asyncio
async def test_transport_server_error_with_timeout_exception_keeps_timeout_message():
    """Regression: ``httpx.TimeoutException`` is a subclass of
    ``httpx.RequestError``; without the explicit timeout branch the message
    would collapse to the generic "network error after retries" line."""
    original = httpx.ReadTimeout("read timed out", request=_make_request())
    transport_exc = TransportServerError("timeout", original=original)
    session = _make_stub_session(transport_side_effect=transport_exc)

    with pytest.raises(NetworkError) as excinfo:
        await chat_aware_authed_post(
            session,  # type: ignore[arg-type]
            build_request=_noop_build_request,
            parse_label="chat.ask",
        )

    message = str(excinfo.value)
    assert "timed out after retries" in message
    assert "network error after retries" not in message
    assert excinfo.value.original_error is original
    assert excinfo.value.__cause__ is transport_exc


@pytest.mark.asyncio
async def test_transport_server_error_with_unexpected_original_type_raises_type_error():
    """Defensive: the transport layer should only wrap
    ``HTTPStatusError`` / ``RequestError`` into ``TransportServerError``.
    Anything else surfaces as ``TypeError`` so an invariant drift is loud
    rather than silently mis-mapped."""

    class _UnexpectedException(Exception):
        pass

    original = _UnexpectedException("not http, not request")
    transport_exc = TransportServerError("bogus original", original=original)
    session = _make_stub_session(transport_side_effect=transport_exc)

    with pytest.raises(TypeError) as excinfo:
        await chat_aware_authed_post(
            session,  # type: ignore[arg-type]
            build_request=_noop_build_request,
            parse_label="chat.ask",
        )

    message = str(excinfo.value)
    assert "TransportServerError.original" in message
    # The diagnostic must include both the actual type and the expected
    # types so a future invariant drift produces an actionable error
    # (per gemini-code-assist review on PR #832).
    assert "Expected httpx.HTTPStatusError or httpx.RequestError" in message
    assert excinfo.value.__cause__ is transport_exc


# ---------------------------------------------------------------------------
# Raw httpx.HTTPStatusError fall-through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raw_http_status_error_maps_to_chat_error():
    """Non-401 / non-429 / non-5xx status errors that fall through
    ``Session.transport_post`` reach this layer as raw
    ``httpx.HTTPStatusError`` and get wrapped in a ``ChatError`` that
    surfaces the status code."""
    raw_exc = _make_status_error(404)
    session = _make_stub_session(transport_side_effect=raw_exc)

    with pytest.raises(ChatError) as excinfo:
        await chat_aware_authed_post(
            session,  # type: ignore[arg-type]
            build_request=_noop_build_request,
            parse_label="chat.ask",
        )

    message = str(excinfo.value)
    assert "HTTP 404" in message
    assert "chat.ask" in message
    assert excinfo.value.__cause__ is raw_exc


# ---------------------------------------------------------------------------
# Finalization invariant (PR 12.5: moved into DrainMiddleware)
# ---------------------------------------------------------------------------
#
# The pre-PR-12.5 contract that ``chat_aware_authed_post`` ran
# ``_finish_transport_post`` in its own ``finally`` is no longer this
# function's responsibility — drain admission/finalization moved into
# ``DrainMiddleware`` at the outermost chain position. The exception-
# path finalization invariant is now pinned by
# ``tests/unit/test_drain_middleware.py::test_finish_fires_on_exception``,
# which exercises a real ``TransportDrainTracker`` end-to-end rather
# than mocking the bookkeeping.
#
# What remains here as a chat-specific invariant: the error-mapping
# still raises ``ChatError`` even when the underlying transport raises.
# The error-mapping tests above already cover that path
# (`test_transport_auth_expired_maps_to_chat_error` exercises the same
# transport exception used by the deleted finalization test).
