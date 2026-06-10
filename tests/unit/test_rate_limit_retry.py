"""429 retry budget on ``RpcExecutor.rpc_call``.

The rate-limit fix raises the ``rate_limit_max_retries`` default from
``0`` to ``3`` and adds capped exponential backoff as the sleep fallback
when 429 arrives without a parseable ``Retry-After`` header. Setting
``rate_limit_max_retries=0`` still restores raise-immediately behavior.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import notebooklm._deadline as _deadline
from notebooklm.rpc import RateLimitError, RPCError, RPCMethod
from tests._fixtures.kernel_test_helpers import install_http_client_for_test
from tests._helpers.client_factory import build_client_shell_for_tests
from tests.unit.conftest import install_post_as_stream


@pytest.fixture
def auth_tokens():
    auth = MagicMock()
    auth.csrf_token = "fake_csrf"
    return auth


def _build_429(retry_after: str | None = "1") -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 429
    resp.headers = {"retry-after": retry_after} if retry_after is not None else {}
    resp.reason_phrase = "Too Many Requests"

    def raise_429():
        raise httpx.HTTPStatusError("Rate Limit", request=MagicMock(), response=resp)

    resp.raise_for_status.side_effect = raise_429
    return resp


def _build_200(payload: list) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.text = ")]}'\n[null,[" + str(payload).replace("'", '"') + "]]"
    resp.raise_for_status = MagicMock()
    return resp


@pytest.mark.asyncio
async def test_rate_limit_retry_success_with_budget(auth_tokens):
    """With budget>0 and a parseable Retry-After, the second call succeeds."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.side_effect = [_build_429("1"), _build_200([["result"]])]

    core = build_client_shell_for_tests(auth_tokens, rate_limit_max_retries=2)
    install_http_client_for_test(core._collaborators.kernel, mock_client)
    install_post_as_stream(None, mock_client, mock_client.post)

    # Decode may fail on the synthetic 200 — that's fine, what we care about
    # is the post counts and sleep budget. We expect either success or an
    # RPCError-tree decode failure, but the retry MUST have fired. Narrowed
    # from `except Exception` to keep unrelated programming errors visible.
    with patch("asyncio.sleep", AsyncMock()) as mock_sleep, contextlib.suppress(RPCError):
        await core._rpc_executor.rpc_call(RPCMethod.GET_NOTEBOOK, ["nb1"])

    assert mock_client.post.call_count == 2, (
        f"Expected initial 429 then 1 retry, got {mock_client.post.call_count}"
    )
    mock_sleep.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_rate_limit_retry_after_larger_than_client_timeout_does_not_sleep(
    auth_tokens, monkeypatch
):
    """The middleware enforces the client timeout as the aggregate retry budget.

    A controlled monotonic clock starts the retry deadline at ``t=0`` and then
    jumps past the ``timeout`` budget before the budget check runs, so the
    middleware refuses to sleep (the requested retry would exceed the remaining
    budget) and re-raises ``RateLimitError`` after the single initial POST. The
    fake clock is *load-bearing*: with the real clock the near-zero elapsed time
    would leave the full budget intact and the middleware would sleep + retry.
    """
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    # Retry-After (1s) is *within* the configured 10s budget, so the no-sleep
    # decision here is driven entirely by the controlled clock exhausting the
    # deadline — not by a header that trivially outruns the timeout.
    mock_client.post.return_value = _build_429("1")

    core = build_client_shell_for_tests(auth_tokens, timeout=10.0, rate_limit_max_retries=2)
    install_http_client_for_test(core._collaborators.kernel, mock_client)
    install_post_as_stream(None, mock_client, mock_client.post)

    sleeps: list[float] = []
    # A clock that leaps far past the 10s budget on *every* successive read, so
    # whichever read the retry deadline captures as its start, the next read
    # (the budget check) is already > timeout past it and ``remaining()``
    # collapses to 0. Robust to the deadline clock being shared with other
    # consumers in the call (the exact read ordering must not matter).
    clock = {"t": 0.0}

    def _monotonic() -> float:
        now = clock["t"]
        clock["t"] += 1000.0
        return now

    # ADR-0007: patch the deadline clock through a locally-imported module alias
    # (object form) rather than a ``notebooklm._deadline...`` string target.
    # ``MagicMock`` wraps the fake clock so the bite-check can assert the
    # injected seam was actually consulted. We replace ``_deadline.time`` with a
    # mock that wraps the real ``time`` module and overrides only ``monotonic``,
    # so the patch is isolated to ``_deadline``'s consumer-side binding and never
    # mutates the shared stdlib ``time`` module object for the rest of the process.
    fake_monotonic = MagicMock(side_effect=_monotonic)
    fake_time = MagicMock(wraps=_deadline.time)
    fake_time.monotonic = fake_monotonic
    monkeypatch.setattr(_deadline, "time", fake_time)

    async def _record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with (
        patch("asyncio.sleep", side_effect=_record_sleep),
        pytest.raises(RateLimitError),
    ):
        await core._rpc_executor.rpc_call(RPCMethod.GET_NOTEBOOK, ["nb1"])

    # The controlled clock exhausted the budget before any retry sleep fired.
    assert mock_client.post.call_count == 1
    assert sleeps == []
    # Bite-check: the injected deadline clock was actually consulted.
    fake_monotonic.assert_called()


@pytest.mark.asyncio
async def test_rate_limit_retry_exhausted_with_budget(auth_tokens):
    """Budget=2 means: initial + 2 retries = 3 total posts before RateLimitError."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.return_value = _build_429("1")

    core = build_client_shell_for_tests(auth_tokens, rate_limit_max_retries=2)
    install_http_client_for_test(core._collaborators.kernel, mock_client)
    install_post_as_stream(None, mock_client, mock_client.post)

    with patch("asyncio.sleep", AsyncMock()) as mock_sleep, pytest.raises(RateLimitError):
        await core._rpc_executor.rpc_call(RPCMethod.GET_NOTEBOOK, ["nb1"])

    assert mock_client.post.call_count == 3
    assert mock_sleep.call_count == 2


@pytest.mark.asyncio
async def test_rate_limit_no_retry_if_disabled(auth_tokens):
    mock_client = AsyncMock(spec=httpx.AsyncClient)

    resp_429 = MagicMock(spec=httpx.Response)
    resp_429.status_code = 429
    resp_429.headers = {"retry-after": "1"}

    def raise_429():
        raise httpx.HTTPStatusError("Rate Limit", request=MagicMock(), response=resp_429)

    resp_429.raise_for_status.side_effect = raise_429

    mock_client.post.return_value = resp_429

    # Explicitly disable retries
    core = build_client_shell_for_tests(auth_tokens, rate_limit_max_retries=0)
    install_http_client_for_test(core._collaborators.kernel, mock_client)
    install_post_as_stream(None, mock_client, mock_client.post)

    with pytest.raises(RateLimitError):
        await core._rpc_executor.rpc_call(RPCMethod.GET_NOTEBOOK, ["nb1"])

    assert mock_client.post.call_count == 1


@pytest.mark.asyncio
async def test_rate_limit_exp_backoff_fallback_without_header(auth_tokens):
    """No Retry-After header → fall back to capped exponential backoff.

    Pre-fix, a 429 without ``Retry-After`` raised immediately even with
    budget>0. Audit §11 widened the retry circle: when the header is
    missing, the loop sleeps for ``min(2 ** attempt, 30)`` seconds with
    ±20% jitter and retries until the budget is exhausted, matching the
    5xx path.
    """
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.return_value = _build_429(retry_after=None)

    core = build_client_shell_for_tests(auth_tokens, rate_limit_max_retries=2)
    install_http_client_for_test(core._collaborators.kernel, mock_client)
    install_post_as_stream(None, mock_client, mock_client.post)

    sleeps: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with patch("asyncio.sleep", side_effect=_record_sleep), pytest.raises(RateLimitError):
        await core._rpc_executor.rpc_call(RPCMethod.GET_NOTEBOOK, ["nb1"])

    # Initial + 2 retries = 3 POSTs before RateLimitError raises.
    assert mock_client.post.call_count == 3
    # 2 retries -> 2 backoff sleeps. Schedule: 2**0=1, 2**1=2, ±20% jitter.
    assert len(sleeps) == 2
    assert 0.8 <= sleeps[0] <= 1.2, f"attempt 1 backoff out of range: {sleeps[0]}"
    assert 1.6 <= sleeps[1] <= 2.4, f"attempt 2 backoff out of range: {sleeps[1]}"


@pytest.mark.asyncio
async def test_rate_limit_no_retry_without_header_when_disabled(auth_tokens):
    """``rate_limit_max_retries=0`` short-circuits even without a header."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.return_value = _build_429(retry_after=None)

    core = build_client_shell_for_tests(auth_tokens, rate_limit_max_retries=0)
    install_http_client_for_test(core._collaborators.kernel, mock_client)
    install_post_as_stream(None, mock_client, mock_client.post)

    with pytest.raises(RateLimitError):
        await core._rpc_executor.rpc_call(RPCMethod.GET_NOTEBOOK, ["nb1"])

    assert mock_client.post.call_count == 1


def test_rate_limit_max_retries_negative_raises(auth_tokens):
    """Negative budget is rejected at construction."""
    with pytest.raises(ValueError, match="rate_limit_max_retries must be >= 0"):
        build_client_shell_for_tests(auth_tokens, rate_limit_max_retries=-1)
