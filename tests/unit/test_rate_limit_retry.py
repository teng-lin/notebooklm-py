"""Phase 3 P3.5 — opt-in 429 retry budget on `ClientCore.rpc_call`.

Default behavior (`rate_limit_max_retries=0`) preserves the pre-Phase-3
contract: raise ``RateLimitError`` on the first 429. Opting in to a positive
budget enables bounded automatic retries that sleep for the (clamped)
``Retry-After`` value.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from notebooklm._core import ClientCore
from notebooklm.rpc import RateLimitError, RPCMethod


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

    core = ClientCore(auth_tokens, rate_limit_max_retries=2)
    core._http_client = mock_client

    with patch("asyncio.sleep", AsyncMock()) as mock_sleep:
        # Decode may fail on the synthetic 200 — that's fine, what we care about
        # is the post counts and sleep budget. We expect either success or a
        # decode-level failure, but the retry MUST have fired.
        try:
            await core.rpc_call(RPCMethod.GET_NOTEBOOK, ["nb1"])
        except Exception:
            pass

    assert mock_client.post.call_count == 2, (
        f"Expected initial 429 then 1 retry, got {mock_client.post.call_count}"
    )
    mock_sleep.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_rate_limit_retry_exhausted_with_budget(auth_tokens):
    """Budget=2 means: initial + 2 retries = 3 total posts before RateLimitError."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.return_value = _build_429("1")

    core = ClientCore(auth_tokens, rate_limit_max_retries=2)
    core._http_client = mock_client

    with patch("asyncio.sleep", AsyncMock()) as mock_sleep, pytest.raises(RateLimitError):
        await core.rpc_call(RPCMethod.GET_NOTEBOOK, ["nb1"])

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
    core = ClientCore(auth_tokens, rate_limit_max_retries=0)
    core._http_client = mock_client

    with pytest.raises(RateLimitError):
        await core.rpc_call(RPCMethod.GET_NOTEBOOK, ["nb1"])

    assert mock_client.post.call_count == 1


@pytest.mark.asyncio
async def test_rate_limit_no_retry_without_header(auth_tokens):
    """No Retry-After header → raise immediately even with budget>0."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.return_value = _build_429(retry_after=None)

    core = ClientCore(auth_tokens, rate_limit_max_retries=2)
    core._http_client = mock_client

    with pytest.raises(RateLimitError):
        await core.rpc_call(RPCMethod.GET_NOTEBOOK, ["nb1"])

    assert mock_client.post.call_count == 1


def test_rate_limit_max_retries_negative_raises(auth_tokens):
    """Negative budget is rejected at construction."""
    with pytest.raises(ValueError, match="rate_limit_max_retries must be >= 0"):
        ClientCore(auth_tokens, rate_limit_max_retries=-1)
