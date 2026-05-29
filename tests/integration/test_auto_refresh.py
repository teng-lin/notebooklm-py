"""Integration tests for automatic token refresh."""

import asyncio
from unittest.mock import MagicMock

import httpx
import pytest

from conftest import install_post_as_stream
from notebooklm import NotebookLMClient
from notebooklm.auth import AuthTokens
from notebooklm.rpc import RPCError, RPCMethod

# mock-based refresh-callback wiring tests; no HTTP, no cassette.
# Opt out of the tier-enforcement hook in tests/integration/conftest.py.
pytestmark = pytest.mark.allow_no_vcr


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
        assert client._collaborators.auth_coord._refresh_callback is not None
        assert (
            client._collaborators.auth_coord._refresh_callback.__func__
            is NotebookLMClient.refresh_auth
        )
        # ``_refresh_lock`` is lazily created on first ``_await_refresh``.
        # At construction time it is ``None`` so the client can be
        # instantiated outside a running loop; the helper allocates the
        # lock on demand inside the async refresh path.
        assert client._collaborators.auth_coord._refresh_lock is None

    @pytest.mark.asyncio
    async def test_full_refresh_flow_http_error(self):
        """Test complete auto-refresh flow for HTTP 401 errors."""
        auth = AuthTokens(
            cookies={"SID": "test"},
            csrf_token="old_csrf",
            session_id="sid",
        )

        client = NotebookLMClient(auth)
        # Override retry delay for faster tests
        client._composed.chain_host._refresh_retry_delay = 0

        # Track refresh calls
        refresh_calls = []

        async def tracking_refresh():
            refresh_calls.append(True)
            # Simulate successful refresh
            client._auth.csrf_token = "new_csrf"
            # Wave 3 of plan ``host-protocol-removal`` deleted the
            # Session-level ``update_auth_headers`` forward; call the
            # canonical coordinator method directly with explicit kwargs.
            client._collaborators.auth_coord.update_auth_headers(
                auth=client._auth,
                kernel=client._collaborators.kernel,
            )
            return client._auth

        client._collaborators.auth_coord._refresh_callback = tracking_refresh

        # Mock HTTP responses
        call_count = [0]

        async def mock_post(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: simulate HTTP 401
                request = httpx.Request("POST", args[0])
                response = httpx.Response(401, request=request)
                raise httpx.HTTPStatusError("Unauthorized", request=request, response=response)
            # Second call: success
            response = MagicMock()
            response.text = ')]}\'\\n[["wrb.fr","wXbhsf",[[[["nb1"],["Notebook 1"]]]]]]'
            response.raise_for_status = MagicMock()
            return response

        # Override the runtime decode-response seam before the RPC fires.
        client._seams.decode_response = lambda *a, **kw: [[["nb1"], ["Notebook 1"]]]

        async with client:
            install_post_as_stream(None, client._collaborators.kernel.get_http_client(), mock_post)
            await client.notebooks.list()

        assert len(refresh_calls) == 1, "Should have refreshed once"
        assert call_count[0] == 2, "Should have retried once"

    @pytest.mark.asyncio
    async def test_full_refresh_flow_rpc_error(self):
        """Test complete auto-refresh flow for RPC auth errors."""
        auth = AuthTokens(
            cookies={"SID": "test"},
            csrf_token="old_csrf",
            session_id="sid",
        )

        client = NotebookLMClient(auth)
        client._composed.chain_host._refresh_retry_delay = 0

        refresh_calls = []

        async def tracking_refresh():
            refresh_calls.append(True)
            client._auth.csrf_token = "new_csrf"
            # Wave 3 of plan ``host-protocol-removal`` deleted the
            # Session-level ``update_auth_headers`` forward; call the
            # canonical coordinator method directly with explicit kwargs.
            client._collaborators.auth_coord.update_auth_headers(
                auth=client._auth,
                kernel=client._collaborators.kernel,
            )
            return client._auth

        client._collaborators.auth_coord._refresh_callback = tracking_refresh

        # Mock HTTP to succeed, but decode_response to fail with auth error first
        async def mock_post(*args, **kwargs):
            response = MagicMock()
            response.text = "mock response"
            response.raise_for_status = MagicMock()
            return response

        decode_count = [0]

        def mock_decode(*args, **kwargs):
            decode_count[0] += 1
            if decode_count[0] == 1:
                raise RPCError("Authentication expired")
            return [[["nb1"], ["Notebook 1"]]]

        # Override the runtime decode-response seam before the RPC fires.
        client._seams.decode_response = mock_decode

        async with client:
            install_post_as_stream(None, client._collaborators.kernel.get_http_client(), mock_post)
            await client.notebooks.list()

        assert len(refresh_calls) == 1, "Should have refreshed once"
        assert decode_count[0] == 2, "Should have retried once"

    @pytest.mark.asyncio
    async def test_refresh_delay_is_applied(self):
        """Test that retry delay is actually applied."""
        auth = AuthTokens(
            cookies={"SID": "test"},
            csrf_token="csrf",
            session_id="sid",
        )

        client = NotebookLMClient(auth)
        client._composed.chain_host._refresh_retry_delay = 0.1  # 100ms delay

        async def mock_refresh():
            return auth

        client._collaborators.auth_coord._refresh_callback = mock_refresh

        call_count = [0]

        async def mock_post(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                request = httpx.Request("POST", args[0])
                response = httpx.Response(401, request=request)
                raise httpx.HTTPStatusError("Unauthorized", request=request, response=response)
            response = MagicMock()
            response.text = "mock"
            response.raise_for_status = MagicMock()
            return response

        # Override the runtime decode-response seam before the RPC fires.
        client._seams.decode_response = lambda *a, **kw: []

        async with client:
            install_post_as_stream(None, client._collaborators.kernel.get_http_client(), mock_post)

            start_time = asyncio.get_event_loop().time()
            await client.notebooks.list()
            elapsed = asyncio.get_event_loop().time() - start_time

        # Should have taken at least the delay time
        assert elapsed >= 0.09, f"Delay should be applied, elapsed: {elapsed}"

    @pytest.mark.asyncio
    async def test_no_retry_on_cookie_expiration(self):
        """Test that full cookie expiration is not retried (requires re-login)."""
        auth = AuthTokens(
            cookies={"SID": "test"},
            csrf_token="csrf",
            session_id="sid",
        )

        client = NotebookLMClient(auth)
        client._composed.chain_host._refresh_retry_delay = 0

        async def failing_refresh():
            # Simulates refresh_auth detecting redirect to login
            raise ValueError("Authentication expired. Run 'notebooklm login' to re-authenticate.")

        client._collaborators.auth_coord._refresh_callback = failing_refresh

        async def mock_post(*args, **kwargs):
            request = httpx.Request("POST", args[0])
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("Unauthorized", request=request, response=response)

        async with client:
            install_post_as_stream(None, client._collaborators.kernel.get_http_client(), mock_post)

            # Should raise the original HTTP error with refresh failure as cause
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                await client.notebooks.list()

            assert exc_info.value.__cause__ is not None
            assert "re-authenticate" in str(exc_info.value.__cause__)

    @pytest.mark.asyncio
    async def test_http_auth_error_does_not_replay_non_idempotent_write(self):
        """A mid-flight 401 on a non-idempotent create is NOT replayed.

        Regression for issue #1157. ``CREATE_NOTEBOOK`` is PROBE_THEN_CREATE,
        so ``resolve_effective_disable_internal_retries`` forces the effective
        disable flag True. The server may have committed the notebook before
        the 401 surfaced, so ``AuthRefreshMiddleware`` must NOT refresh and
        re-POST — that would duplicate the notebook. The original auth error
        propagates so ``NotebooksAPI.create``'s probe-then-create wrapper can
        disambiguate. Driven through the public ``client.notebooks.create``
        surface so the regression is pinned end-to-end.
        """
        auth = AuthTokens(
            cookies={"SID": "test"},
            csrf_token="old_csrf",
            session_id="sid",
        )

        client = NotebookLMClient(auth)
        client._composed.chain_host._refresh_retry_delay = 0

        refresh_calls = []

        async def tracking_refresh():
            refresh_calls.append(True)
            return client._auth

        client._collaborators.auth_coord._refresh_callback = tracking_refresh

        create_post_count = [0]

        async def mock_post(*args, **kwargs):
            url = args[0]
            # ``NotebooksAPI.create`` first lists notebooks to capture a
            # baseline; that LIST_NOTEBOOKS POST must succeed so only the
            # CREATE_NOTEBOOK leg exercises the auth-error path.
            if RPCMethod.LIST_NOTEBOOKS.value in str(url):
                response = MagicMock()
                response.text = "list-ok"
                response.raise_for_status = MagicMock()
                return response
            create_post_count[0] += 1
            request = httpx.Request("POST", url)
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("Unauthorized", request=request, response=response)

        # Baseline ``list()`` decodes to an empty notebook list; the create's
        # decode never runs because the POST raises a 401 first.
        client._seams.decode_response = lambda *a, **kw: []

        async with client:
            install_post_as_stream(None, client._collaborators.kernel.get_http_client(), mock_post)

            with pytest.raises(RPCError):
                await client.notebooks.create("My Notebook")

        assert refresh_calls == [], "non-idempotent write must not trigger an auth refresh"
        assert create_post_count[0] == 1, "CREATE_NOTEBOOK must POST exactly once (no replay)"

    @pytest.mark.asyncio
    async def test_rpc_auth_error_does_not_replay_non_idempotent_write(self):
        """A decoded auth-shaped ``RPCError`` is NOT replayed for a create.

        Regression for issue #1157 — the decode-time refresh-and-retry leg in
        ``RpcExecutor`` must honor the effective disable classification just
        like the HTTP-status leg. ``CREATE_NOTEBOOK`` resolves to disabled
        retries, so the decoded auth error surfaces without a second POST.
        Driven through the public ``client.notebooks.create`` surface.
        """
        auth = AuthTokens(
            cookies={"SID": "test"},
            csrf_token="old_csrf",
            session_id="sid",
        )

        client = NotebookLMClient(auth)
        client._composed.chain_host._refresh_retry_delay = 0

        refresh_calls = []

        async def tracking_refresh():
            refresh_calls.append(True)
            return client._auth

        client._collaborators.auth_coord._refresh_callback = tracking_refresh

        create_post_count = [0]

        async def mock_post(*args, **kwargs):
            if RPCMethod.CREATE_NOTEBOOK.value in str(args[0]):
                create_post_count[0] += 1
            response = MagicMock()
            response.text = "mock response"
            response.raise_for_status = MagicMock()
            return response

        create_decode_count = [0]

        def mock_decode(raw, rpc_id, *args, **kwargs):
            # The baseline ``list()`` decodes to an empty list; the create's
            # decode raises an auth-shaped RPCError to exercise the
            # decode-time refresh-and-retry leg.
            if rpc_id == RPCMethod.CREATE_NOTEBOOK.value:
                create_decode_count[0] += 1
                raise RPCError("Authentication expired")
            return []

        client._seams.decode_response = mock_decode

        async with client:
            install_post_as_stream(None, client._collaborators.kernel.get_http_client(), mock_post)

            with pytest.raises(RPCError):
                await client.notebooks.create("My Notebook")

        assert refresh_calls == [], "non-idempotent write must not trigger an auth refresh"
        assert create_post_count[0] == 1, "CREATE_NOTEBOOK must POST exactly once (no replay)"
        assert create_decode_count[0] == 1, (
            "CREATE_NOTEBOOK decode must run once — no retried decode"
        )
