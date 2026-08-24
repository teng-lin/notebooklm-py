"""Integration tests for automatic token refresh."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from notebooklm import NotebookLMClient
from notebooklm._auth.cookie_types import CookieJar
from notebooklm._client_metrics import ClientMetrics
from notebooklm._kernel import Kernel
from notebooklm._request_types import AuthSnapshot
from notebooklm._rpc_semaphore import RpcSemaphore
from notebooklm._runtime.helpers import is_auth_error
from notebooklm._runtime.rpc_call import RpcRequest, RpcResponse
from notebooklm._runtime.transport import RuntimeTransport
from notebooklm._transport_drain import TransportDrainTracker
from notebooklm._web.runtime import WebExecutionRuntime
from notebooklm._web_cookie_provider import WebCookieGeneration
from notebooklm.auth import AuthTokens
from notebooklm.rpc import RPCError, RPCMethod

# mock-based refresh-callback wiring tests; no HTTP, no cassette.
# Opt out of the tier-enforcement hook in tests/integration/conftest.py.
pytestmark = pytest.mark.allow_no_vcr


def _generation(epoch: int = 0) -> WebCookieGeneration:
    return WebCookieGeneration(
        cookies=CookieJar(),
        csrf_token=f"csrf-{epoch}",
        session_id=f"session-{epoch}",
        authuser=0,
        account_email=None,
        generation=epoch,
    )


def _runtime(
    *,
    terminal: Callable[[RpcRequest], Awaitable[RpcResponse]],
    decode_response: Callable[..., Any],
    refresh: Callable[[], Awaitable[Any]],
    refresh_retry_delay: float = 0.0,
) -> tuple[WebExecutionRuntime, ClientMetrics]:
    """Build the smallest explicit transport/decoder refresh graph."""
    generations = [_generation()]
    metrics = ClientMetrics()

    async def snapshot() -> AuthSnapshot:
        return generations[-1]

    async def refresh_generation() -> None:
        await refresh()
        generations.append(_generation(len(generations)))

    transport = RuntimeTransport(
        kernel=Kernel(),
        snapshot_provider=snapshot,
        metrics=metrics,
        bound_loop_check=lambda: None,
        logger=logging.getLogger(__name__),
        drain_tracker=TransportDrainTracker(),
        rpc_semaphore=RpcSemaphore(None),
        rate_limit_max_retries=0,
        server_error_max_retries=0,
        retry_timeout_provider=lambda: 30.0,
        refresh_retry_delay=refresh_retry_delay,
        refresh_callable=refresh_generation,
        is_auth_error=is_auth_error,
        refresh_callback_enabled_provider=lambda: True,
        terminal=terminal,
    )
    runtime = WebExecutionRuntime(
        assert_open=lambda: None,
        transport=transport,
        refresh=refresh_generation,
        metrics=metrics,
        decode_response=decode_response,
        is_auth_error=is_auth_error,
        sleep=asyncio.sleep,
        timeout_provider=lambda: 30.0,
        refresh_callback_enabled_provider=lambda: True,
        refresh_retry_delay_provider=lambda: refresh_retry_delay,
    )
    return runtime, metrics


def _success(request: RpcRequest, text: str = "mock response") -> RpcResponse:
    response = httpx.Response(
        200,
        text=text,
        request=httpx.Request("POST", request.url),
    )
    return RpcResponse(response=response, state=request.state)


def _unauthorized(request: RpcRequest) -> httpx.HTTPStatusError:
    wire_request = httpx.Request("POST", request.url)
    response = httpx.Response(401, request=wire_request)
    return httpx.HTTPStatusError("Unauthorized", request=wire_request, response=response)


class TestAutoRefreshIntegration:
    @pytest.mark.asyncio
    async def test_client_has_refresh_callback_wired(self):
        """NotebookLMClient should wire refresh_auth as callback."""
        auth = AuthTokens(
            cookies={"SID": "test"},
            csrf_token="csrf",
            session_id="sid",
        )

        client = NotebookLMClient(auth)
        # Bound methods aren't identical, so compare underlying function
        assert client._provider._coordinator._refresh_callback is not None
        assert (
            client._provider._coordinator._refresh_callback.__func__
            is NotebookLMClient.refresh_auth
        )
        # ``_refresh_lock`` is lazily created on first ``_await_refresh``.
        # At construction time it is ``None`` so the client can be
        # instantiated outside a running loop; the helper allocates the
        # lock on demand inside the async refresh path.
        assert client._provider._coordinator._refresh_lock is None

    @pytest.mark.asyncio
    async def test_full_refresh_flow_http_error(self):
        """Test complete auto-refresh flow for HTTP 401 errors."""
        refresh_calls: list[bool] = []

        async def tracking_refresh() -> None:
            refresh_calls.append(True)

        attempts = 0

        async def terminal(request: RpcRequest) -> RpcResponse:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise _unauthorized(request)
            return _success(request)

        runtime, _ = _runtime(
            terminal=terminal,
            decode_response=lambda *_a, **_kw: [[["nb1"], ["Notebook 1"]]],
            refresh=tracking_refresh,
        )

        result = await runtime.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

        assert result == [[["nb1"], ["Notebook 1"]]]
        assert refresh_calls == [True]
        assert attempts == 2

    @pytest.mark.asyncio
    async def test_full_refresh_flow_rpc_error(self):
        """Test complete auto-refresh flow for RPC auth errors."""
        refresh_calls: list[bool] = []

        async def tracking_refresh() -> None:
            refresh_calls.append(True)

        decode_count = 0

        def mock_decode(*_args: Any, **_kwargs: Any) -> Any:
            nonlocal decode_count
            decode_count += 1
            if decode_count == 1:
                raise RPCError("Authentication expired", rpc_code=401)
            return [[["nb1"], ["Notebook 1"]]]

        runtime, _ = _runtime(
            terminal=lambda request: asyncio.sleep(0, result=_success(request)),
            decode_response=mock_decode,
            refresh=tracking_refresh,
        )

        result = await runtime.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

        assert result == [[["nb1"], ["Notebook 1"]]]
        assert refresh_calls == [True]
        assert decode_count == 2

    @pytest.mark.asyncio
    async def test_wire_401_then_decoded_auth_error_refreshes_once(self):
        """Issue #1205: a wire-401 followed by a decoded auth error on the SAME
        logical call must drive exactly ONE refresh.

        Before consolidation the HTTP-status layer (``AuthRefreshBehavior``)
        and the decoded-RPC layer (``RpcExecutor``) tracked their once-per-call
        guard independently — the chain's per-request ``auth_refreshed`` flag
        and the executor's ``_is_retry`` flag could not see each other. So a
        ``401 → refresh#1 → 200 → decoded-auth-error → refresh#2`` sequence
        refreshed twice. The shared :class:`RefreshBudget` threaded through both
        layers now bounds the logical call to a single refresh; the decoded
        auth error surfaces to the caller instead of triggering a second
        refresh.
        """
        refresh_calls: list[bool] = []

        async def tracking_refresh() -> None:
            refresh_calls.append(True)

        attempts = 0

        async def terminal(request: RpcRequest) -> RpcResponse:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                # Wire 401 → HTTP-status layer refreshes (refresh #1) and
                # retries the POST.
                raise _unauthorized(request)
            # The post-refresh retry returns HTTP 200; the decoded payload
            # still carries an auth error.
            return _success(request)

        auth_rpc_error = RPCError(
            "authentication expired",
            method_id="wXbhsf",
            raw_response="RAWBODY",
            rpc_code=401,
            found_ids=["auth-id"],
        )
        auth_rpc_error.unconfirmed = True

        def mock_decode(*_args: Any, **_kwargs: Any) -> Any:
            raise auth_rpc_error

        runtime, _ = _runtime(
            terminal=terminal,
            decode_response=mock_decode,
            refresh=tracking_refresh,
        )

        # The decoded auth error surfaces — the shared budget was already
        # spent by the HTTP-status refresh, so the decoded layer does NOT
        # refresh again and re-raises the original auth error.
        with pytest.raises(RPCError) as raised:
            await runtime.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

        assert raised.value is auth_rpc_error
        assert refresh_calls == [True]
        # Two POSTs: the initial 401 and the single post-refresh retry. No
        # third POST, because the decoded layer did not refresh-and-retry.
        assert attempts == 2

    @pytest.mark.asyncio
    async def test_decoded_auth_retry_increments_auth_retry_metric(self):
        """Issue #1205: the decoded-RPC refresh layer now counts the auth retry.

        Before consolidation only the HTTP-status layer incremented
        ``rpc_auth_retries``; the decode-time refresh-and-retry leg silently
        skipped it. The shared refresh body counts on both layers.
        """

        async def tracking_refresh() -> None:
            return None

        decode_count = 0

        def mock_decode(*_args: Any, **_kwargs: Any) -> Any:
            nonlocal decode_count
            decode_count += 1
            if decode_count == 1:
                raise RPCError("Authentication expired", rpc_code=401)
            return [[["nb1"], ["Notebook 1"]]]

        async def terminal(request: RpcRequest) -> RpcResponse:
            return _success(request)

        runtime, metrics = _runtime(
            terminal=terminal,
            decode_response=mock_decode,
            refresh=tracking_refresh,
        )

        await runtime.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

        assert metrics.snapshot().rpc_auth_retries == 1

    @pytest.mark.asyncio
    async def test_refresh_delay_is_applied(self):
        """Test that retry delay is actually applied."""

        async def refresh() -> None:
            return None

        attempts = 0

        async def terminal(request: RpcRequest) -> RpcResponse:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise _unauthorized(request)
            return _success(request)

        runtime, _ = _runtime(
            terminal=terminal,
            decode_response=lambda *_a, **_kw: [],
            refresh=refresh,
            refresh_retry_delay=0.1,
        )

        loop = asyncio.get_running_loop()
        start_time = loop.time()
        await runtime.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])
        elapsed = loop.time() - start_time

        # Should have taken at least the delay time
        assert elapsed >= 0.09, f"Delay should be applied, elapsed: {elapsed}"
        assert attempts == 2

    @pytest.mark.asyncio
    async def test_no_retry_on_cookie_expiration(self):
        """Test that full cookie expiration is not retried (requires re-login)."""

        async def failing_refresh() -> None:
            # Simulates refresh_auth detecting redirect to login
            raise ValueError("Authentication expired. Run 'notebooklm login' to re-authenticate.")

        async def terminal(request: RpcRequest) -> RpcResponse:
            raise _unauthorized(request)

        runtime, _ = _runtime(
            terminal=terminal,
            decode_response=lambda *_a, **_kw: [],
            refresh=failing_refresh,
        )

        # Should raise the original HTTP error with refresh failure as cause.
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await runtime.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

        assert exc_info.value.__cause__ is not None
        assert "re-authenticate" in str(exc_info.value.__cause__)

    @pytest.mark.asyncio
    async def test_http_auth_error_does_not_replay_non_idempotent_write(self):
        """A mid-flight 401 on a non-idempotent create is NOT replayed.

        Regression for issue #1157. ``CREATE_NOTEBOOK`` is PROBE_THEN_CREATE,
        so ``resolve_effective_disable_internal_retries`` forces the effective
        disable flag True. The server may have committed the notebook before
        the 401 surfaced, so ``AuthRefreshBehavior`` must NOT refresh and
        re-POST — that would duplicate the notebook. Drive the explicit
        execution runtime so its retry owner is tested directly.
        """
        refresh_calls: list[bool] = []

        async def tracking_refresh() -> None:
            refresh_calls.append(True)

        create_post_count = 0

        async def terminal(request: RpcRequest) -> RpcResponse:
            nonlocal create_post_count
            create_post_count += 1
            raise _unauthorized(request)

        runtime, _ = _runtime(
            terminal=terminal,
            decode_response=lambda *_a, **_kw: [],
            refresh=tracking_refresh,
        )

        with pytest.raises(RPCError):
            await runtime.rpc_call(RPCMethod.CREATE_NOTEBOOK, [])

        assert refresh_calls == [], "non-idempotent write must not trigger an auth refresh"
        assert create_post_count == 1, "CREATE_NOTEBOOK must POST exactly once (no replay)"

    @pytest.mark.asyncio
    async def test_rpc_auth_error_does_not_replay_non_idempotent_write(self):
        """A decoded auth-shaped ``RPCError`` is NOT replayed for a create.

        Regression for issue #1157 — the decode-time refresh-and-retry leg in
        ``RpcExecutor`` must honor the effective disable classification just
        like the HTTP-status leg. ``CREATE_NOTEBOOK`` resolves to disabled
        retries, so the decoded auth error surfaces without a second POST.
        Driven through the explicit execution runtime.
        """
        refresh_calls: list[bool] = []

        async def tracking_refresh() -> None:
            refresh_calls.append(True)

        create_post_count = 0

        async def terminal(request: RpcRequest) -> RpcResponse:
            nonlocal create_post_count
            create_post_count += 1
            return _success(request)

        create_decode_count = 0

        def mock_decode(*_args: Any, **_kwargs: Any) -> Any:
            nonlocal create_decode_count
            create_decode_count += 1
            raise RPCError("Authentication expired", rpc_code=401)

        runtime, _ = _runtime(
            terminal=terminal,
            decode_response=mock_decode,
            refresh=tracking_refresh,
        )

        with pytest.raises(RPCError):
            await runtime.rpc_call(RPCMethod.CREATE_NOTEBOOK, [])

        assert refresh_calls == [], "non-idempotent write must not trigger an auth refresh"
        assert create_post_count == 1, "CREATE_NOTEBOOK must POST exactly once (no replay)"
        assert create_decode_count == 1, "CREATE_NOTEBOOK decode must run once — no retry"
