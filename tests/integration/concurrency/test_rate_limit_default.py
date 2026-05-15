"""Regression test for T7.H2 — default ``rate_limit_max_retries=3`` with backoff.

Audit item #11 (``thread-safety-concurrency-audit.md`` §11):
Pre-fix, ``rate_limit_max_retries`` defaulted to ``0`` so any 429 raised
``RateLimitError`` immediately. The CLI silently bumped the value, but
programmatic users had to discover and opt in. Diverges from "smart
retry" SDK norms.

Post-fix (T7.H2):
- ``ClientCore.__init__`` defaults ``rate_limit_max_retries`` to ``3``.
- ``NotebookLMClient.__init__`` and ``NotebookLMClient.from_storage``
  match the new default.
- ``_perform_authed_post`` falls back to capped exponential backoff
  (start 1s, cap 30s, ±20% jitter) when a 429 lacks a parseable
  ``Retry-After`` header, so the new default is useful even when the
  server omits the hint.
- ``disable_internal_retries=True`` (T7.B2) still suppresses BOTH the
  429 and the 5xx/network retry loops for mutating create RPCs whose
  retries would risk duplicate-resource creation.

Test plan:
1. Default budget retries 3 times on 429 then succeeds on the 4th response.
2. Default budget exhausts: four 429s in a row raise ``RateLimitError``
   after exactly 3 sleeps (initial + 3 retries = 4 total POSTs).
3. ``disable_internal_retries=True`` suppresses the 429 retry loop even
   under the new positive default (B2 coordination check).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from notebooklm import NotebookLMClient, RateLimitError
from notebooklm.rpc import RPCMethod

# T7.H2 uses synthetic HTTPX responses via mock — no cassette, no real HTTP.
pytestmark = pytest.mark.allow_no_vcr


_DUMMY_REQUEST = httpx.Request("POST", "https://example.invalid/batchexecute")


def _build_429(retry_after: str | None = "1") -> httpx.Response:
    """Construct a 429 response with an optional Retry-After header.

    Returns a real ``httpx.Response`` (not a MagicMock) so the transport
    pipeline's ``raise_for_status()`` / ``response.headers.get`` paths
    behave exactly as in production. A dummy ``httpx.Request`` is
    attached so ``raise_for_status()`` can raise ``HTTPStatusError``
    instead of complaining about a missing request.
    """
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    return httpx.Response(429, headers=headers, text="rate limited", request=_DUMMY_REQUEST)


def _build_200_list_notebooks() -> httpx.Response:
    """Minimal valid batchexecute response that decodes to an empty list."""
    inner = json.dumps([[]])
    chunk = json.dumps([["wrb.fr", RPCMethod.LIST_NOTEBOOKS.value, inner, None, None]])
    body = f")]}}'\n{len(chunk)}\n{chunk}\n"
    return httpx.Response(200, text=body, request=_DUMMY_REQUEST)


@pytest.mark.asyncio
async def test_default_retries_succeed_after_three_429s(auth_tokens) -> None:
    """Default ``rate_limit_max_retries=3`` retries 3 times then succeeds.

    Sequence: 429 → 429 → 429 → 200. Without explicit configuration,
    the client must absorb the three 429s and succeed on the 4th POST.
    """
    mock_post = AsyncMock(
        side_effect=[
            _build_429("1"),
            _build_429("1"),
            _build_429("1"),
            _build_200_list_notebooks(),
        ]
    )

    # NotebookLMClient default — NO ``rate_limit_max_retries`` kwarg.
    client = NotebookLMClient(auth_tokens)
    assert client._core._rate_limit_max_retries == 3, (
        "T7.H2 default must be 3; check that NotebookLMClient.__init__ "
        "forwards the new ClientCore default."
    )

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.post = mock_post
    client._core._http_client = mock_http

    with patch("asyncio.sleep", AsyncMock()) as mock_sleep:
        result = await client.notebooks.list()

    assert result == []
    assert mock_post.await_count == 4, (
        f"Expected 1 initial POST + 3 retries = 4 total, got {mock_post.await_count}"
    )
    assert mock_sleep.await_count == 3, (
        f"Expected 3 sleeps (one per retry), got {mock_sleep.await_count}"
    )


@pytest.mark.asyncio
async def test_default_retries_exhausted_raises_rate_limit_error(auth_tokens) -> None:
    """Default budget exhausted on the 4th 429 raises ``RateLimitError``.

    Sequence: 429 × 4. With default=3, initial + 3 retries = 4 total
    POSTs. The 4th 429 has no remaining budget so ``RateLimitError``
    bubbles up.
    """
    mock_post = AsyncMock(return_value=_build_429("1"))

    client = NotebookLMClient(auth_tokens)
    assert client._core._rate_limit_max_retries == 3

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.post = mock_post
    client._core._http_client = mock_http

    with patch("asyncio.sleep", AsyncMock()) as mock_sleep, pytest.raises(RateLimitError):
        await client.notebooks.list()

    assert mock_post.await_count == 4, (
        f"Initial + 3 retries = 4 POSTs before raise; got {mock_post.await_count}"
    )
    assert mock_sleep.await_count == 3, (
        f"3 retries -> 3 sleeps before raise; got {mock_sleep.await_count}"
    )


@pytest.mark.asyncio
async def test_default_retries_use_exponential_backoff_when_header_missing(
    auth_tokens,
) -> None:
    """No ``Retry-After`` header on 429 → capped exponential backoff fallback.

    Pre-T7.H2, a 429 without ``Retry-After`` raised immediately even
    with budget>0. Post-fix, the retry loop falls back to ``min(2 **
    attempt, 30)`` seconds with ±20% jitter so the new positive default
    is useful when Google omits the hint.

    Sleeps are checked to be in the ``[0.8, 1.2]``, ``[1.6, 2.4]``,
    ``[3.2, 4.8]`` ranges (1, 2, 4 ± 20%).
    """
    mock_post = AsyncMock(
        side_effect=[
            _build_429(retry_after=None),
            _build_429(retry_after=None),
            _build_429(retry_after=None),
            _build_200_list_notebooks(),
        ]
    )

    client = NotebookLMClient(auth_tokens)
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.post = mock_post
    client._core._http_client = mock_http

    sleep_calls: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    with patch("asyncio.sleep", side_effect=_record_sleep):
        result = await client.notebooks.list()

    assert result == []
    assert mock_post.await_count == 4
    assert len(sleep_calls) == 3, f"Expected 3 backoff sleeps, got {len(sleep_calls)}"
    # Backoff schedule: 2**0=1, 2**1=2, 2**2=4 — each with ±20% jitter,
    # floored at 0.1s.
    assert 0.8 <= sleep_calls[0] <= 1.2, f"attempt 1 backoff out of range: {sleep_calls[0]}"
    assert 1.6 <= sleep_calls[1] <= 2.4, f"attempt 2 backoff out of range: {sleep_calls[1]}"
    assert 3.2 <= sleep_calls[2] <= 4.8, f"attempt 3 backoff out of range: {sleep_calls[2]}"


@pytest.mark.asyncio
async def test_disable_internal_retries_skips_429_loop_under_new_default(
    auth_tokens,
) -> None:
    """B2 coordination: ``disable_internal_retries=True`` skips 429 retries.

    T7.B2 introduced ``disable_internal_retries`` for mutating create
    RPCs (CREATE_NOTEBOOK, ADD_SOURCE) where naive re-POST risks
    duplicating the resource. T7.H2 raises the default ``rate_limit
    _max_retries`` to 3, so B2 must continue to gate the 429 path —
    otherwise mutating creates would silently inherit the new default
    and the idempotency safety net would no longer apply.

    This test exercises ``_perform_authed_post`` directly with
    ``disable_internal_retries=True`` and verifies the very first 429
    raises ``_TransportRateLimited`` (which the API layer translates
    into ``RateLimitError``) without sleeping.
    """
    from notebooklm._core import _TransportRateLimited

    mock_post = AsyncMock(return_value=_build_429("1"))

    client = NotebookLMClient(auth_tokens)
    assert client._core._rate_limit_max_retries == 3

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.post = mock_post
    client._core._http_client = mock_http

    def _build_request(_snap):
        return ("https://example.invalid/x", b"body", None)

    with patch("asyncio.sleep", AsyncMock()) as mock_sleep, pytest.raises(_TransportRateLimited):
        await client._core._perform_authed_post(
            build_request=_build_request,
            log_label="test",
            disable_internal_retries=True,
        )

    assert mock_post.await_count == 1, (
        f"disable_internal_retries=True must short-circuit the 429 retry "
        f"loop even with budget=3; got {mock_post.await_count} POSTs"
    )
    assert mock_sleep.await_count == 0, "No backoff sleep should occur when retries are disabled"
