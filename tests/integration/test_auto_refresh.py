"""Integration tests for automatic token refresh."""

import asyncio
from unittest.mock import MagicMock, patch

import httpx
import pytest

from notebooklm import NotebookLMClient
from notebooklm.auth import AuthTokens
from notebooklm.rpc import RPCError


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
        assert client._core._refresh_callback is not None
        assert client._core._refresh_callback.__func__ is NotebookLMClient.refresh_auth
        assert client._core._refresh_lock is not None

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
        client._core._refresh_retry_delay = 0

        # Track refresh calls
        refresh_calls = []

        async def tracking_refresh():
            refresh_calls.append(True)
            # Simulate successful refresh
            client._core.auth.csrf_token = "new_csrf"
            client._core.update_auth_headers()
            return client._core.auth

        client._core._refresh_callback = tracking_refresh

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

        async with client:
            client._core._http_client.post = mock_post

            with patch("notebooklm._core.decode_response") as mock_decode:
                mock_decode.return_value = [[["nb1"], ["Notebook 1"]]]
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
        client._core._refresh_retry_delay = 0

        refresh_calls = []

        async def tracking_refresh():
            refresh_calls.append(True)
            client._core.auth.csrf_token = "new_csrf"
            client._core.update_auth_headers()
            return client._core.auth

        client._core._refresh_callback = tracking_refresh

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

        async with client:
            client._core._http_client.post = mock_post

            with patch("notebooklm._core.decode_response", side_effect=mock_decode):
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
        client._core._refresh_retry_delay = 0.1  # 100ms delay

        async def mock_refresh():
            return auth

        client._core._refresh_callback = mock_refresh

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

        async with client:
            client._core._http_client.post = mock_post

            start_time = asyncio.get_event_loop().time()

            with patch("notebooklm._core.decode_response", return_value=[]):
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
        client._core._refresh_retry_delay = 0

        async def failing_refresh():
            # Simulates refresh_auth detecting redirect to login
            raise ValueError("Authentication expired. Run 'notebooklm login' to re-authenticate.")

        client._core._refresh_callback = failing_refresh

        async def mock_post(*args, **kwargs):
            request = httpx.Request("POST", args[0])
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("Unauthorized", request=request, response=response)

        async with client:
            client._core._http_client.post = mock_post

            # Should raise the original HTTP error with refresh failure as cause
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                await client.notebooks.list()

            assert exc_info.value.__cause__ is not None
            assert "re-authenticate" in str(exc_info.value.__cause__)
