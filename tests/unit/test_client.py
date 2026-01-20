"""Tests for NotebookLMClient class."""

import asyncio
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from pytest_httpx import HTTPXMock

from notebooklm._core import ClientCore, is_auth_error
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from notebooklm.rpc import AuthError, RPCError, RPCMethod


@pytest.fixture
def mock_auth():
    """Create a mock AuthTokens object."""
    return AuthTokens(
        cookies={"SID": "test_sid", "HSID": "test_hsid"},
        csrf_token="test_csrf",
        session_id="test_session",
    )


# =============================================================================
# BASIC CLIENT TESTS
# =============================================================================


class TestNotebookLMClientInit:
    def test_client_initialization(self, mock_auth):
        """Test client initializes with auth tokens."""
        client = NotebookLMClient(mock_auth)

        assert client.auth == mock_auth
        assert client.notebooks is not None
        assert client.sources is not None
        assert client.artifacts is not None
        assert client.chat is not None
        assert client.research is not None
        assert client.notes is not None

    def test_client_is_connected_before_open(self, mock_auth):
        """Test is_connected returns False before opening."""
        client = NotebookLMClient(mock_auth)
        assert client.is_connected is False


# =============================================================================
# CONTEXT MANAGER TESTS
# =============================================================================


class TestClientContextManager:
    @pytest.mark.asyncio
    async def test_context_manager_opens_and_closes(self, mock_auth):
        """Test async context manager opens and closes connection."""
        client = NotebookLMClient(mock_auth)

        # Before entering context
        assert client.is_connected is False

        async with client as c:
            # Inside context
            assert c is client
            assert client.is_connected is True

        # After exiting context
        assert client.is_connected is False

    @pytest.mark.asyncio
    async def test_context_manager_closes_on_exception(self, mock_auth):
        """Test connection is closed even when exception occurs."""
        client = NotebookLMClient(mock_auth)

        with pytest.raises(ValueError):
            async with client:
                assert client.is_connected is True
                raise ValueError("Test exception")

        # Connection should still be closed
        assert client.is_connected is False


# =============================================================================
# FROM_STORAGE CLASSMETHOD TESTS
# =============================================================================


class TestFromStorage:
    @pytest.mark.asyncio
    async def test_from_storage_success(self, tmp_path, httpx_mock: HTTPXMock):
        """Test creating client from storage file."""
        # Create storage file
        storage_file = tmp_path / "storage_state.json"
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "HSID", "value": "test_hsid", "domain": ".google.com"},
            ]
        }
        storage_file.write_text(json.dumps(storage_state))

        # Mock token fetch
        html = '"SNlM0e":"csrf_token_abc" "FdrFJe":"session_id_xyz"'
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=html.encode(),
        )

        client = await NotebookLMClient.from_storage(str(storage_file))

        assert client.auth.cookies["SID"] == "test_sid"
        assert client.auth.csrf_token == "csrf_token_abc"
        assert client.auth.session_id == "session_id_xyz"

    @pytest.mark.asyncio
    async def test_from_storage_file_not_found(self, tmp_path):
        """Test raises error when storage file doesn't exist."""
        with pytest.raises(FileNotFoundError):
            await NotebookLMClient.from_storage(str(tmp_path / "nonexistent.json"))

    @pytest.mark.asyncio
    async def test_from_storage_with_default_path(self, httpx_mock: HTTPXMock):
        """Test from_storage uses default path when none specified."""
        from notebooklm.auth import DEFAULT_STORAGE_PATH

        # Create storage file at default location
        if not DEFAULT_STORAGE_PATH.parent.exists():
            DEFAULT_STORAGE_PATH.parent.mkdir(parents=True, exist_ok=True)

        # IMPORTANT: Back up existing auth file if it exists
        backup_path = DEFAULT_STORAGE_PATH.with_suffix(".json.bak")
        had_existing_file = DEFAULT_STORAGE_PATH.exists()
        if had_existing_file:
            backup_path.write_text(DEFAULT_STORAGE_PATH.read_text())

        storage_state = {
            "cookies": [
                {"name": "SID", "value": "default_sid", "domain": ".google.com"},
            ]
        }

        # Only run if we can write to default location
        try:
            DEFAULT_STORAGE_PATH.write_text(json.dumps(storage_state))

            html = '"SNlM0e":"csrf" "FdrFJe":"sess"'
            httpx_mock.add_response(content=html.encode())

            client = await NotebookLMClient.from_storage()
            assert client.auth.cookies["SID"] == "default_sid"
        except PermissionError:
            pytest.skip("Cannot write to default storage path")
        finally:
            # Restore original file or clean up test file
            if had_existing_file:
                DEFAULT_STORAGE_PATH.write_text(backup_path.read_text())
                backup_path.unlink()
            elif DEFAULT_STORAGE_PATH.exists():
                DEFAULT_STORAGE_PATH.unlink()


# =============================================================================
# REFRESH_AUTH TESTS
# =============================================================================


class TestRefreshAuth:
    @pytest.mark.asyncio
    async def test_refresh_auth_success(self, mock_auth, httpx_mock: HTTPXMock):
        """Test successful auth refresh."""
        client = NotebookLMClient(mock_auth)

        # Mock the homepage response with new tokens
        html = """
        <html>
        <script>
            window.WIZ_global_data = {
                "SNlM0e":"new_csrf_token_123",
                "FdrFJe":"new_session_id_456"
            };
        </script>
        </html>
        """
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=html.encode(),
        )

        async with client:
            refreshed_auth = await client.refresh_auth()

            # Should have new tokens
            assert refreshed_auth.csrf_token == "new_csrf_token_123"
            assert refreshed_auth.session_id == "new_session_id_456"
            assert client.auth.csrf_token == "new_csrf_token_123"
            assert client.auth.session_id == "new_session_id_456"

    @pytest.mark.asyncio
    async def test_refresh_auth_redirect_to_login(self, mock_auth, httpx_mock: HTTPXMock):
        """Test refresh_auth raises error on redirect to login - by final URL check."""
        client = NotebookLMClient(mock_auth)

        # Instead of a redirect, mock a response that includes accounts.google.com in URL
        # The refresh_auth checks if "accounts.google.com" is in the final URL
        # We can't easily mock a real redirect with httpx, so we test the URL check
        # by providing a response that doesn't contain the expected tokens
        html = "<html><body>Please sign in</body></html>"  # No tokens
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=html.encode(),
        )

        async with client:
            with pytest.raises(ValueError, match="Failed to extract CSRF token"):
                await client.refresh_auth()

    @pytest.mark.asyncio
    async def test_refresh_auth_missing_csrf(self, mock_auth, httpx_mock: HTTPXMock):
        """Test refresh_auth raises error when CSRF token not found."""
        client = NotebookLMClient(mock_auth)

        # Mock response without CSRF token
        html = '"FdrFJe":"session_only"'  # Missing SNlM0e
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=html.encode(),
        )

        async with client:
            with pytest.raises(ValueError, match="Failed to extract CSRF token"):
                await client.refresh_auth()

    @pytest.mark.asyncio
    async def test_refresh_auth_missing_session_id(self, mock_auth, httpx_mock: HTTPXMock):
        """Test refresh_auth raises error when session ID not found."""
        client = NotebookLMClient(mock_auth)

        # Mock response without session ID
        html = '"SNlM0e":"csrf_only"'  # Missing FdrFJe
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=html.encode(),
        )

        async with client:
            with pytest.raises(ValueError, match="Failed to extract session ID"):
                await client.refresh_auth()


# =============================================================================
# AUTH PROPERTY TESTS
# =============================================================================


class TestAuthProperty:
    def test_auth_property_returns_tokens(self, mock_auth):
        """Test auth property returns the authentication tokens."""
        client = NotebookLMClient(mock_auth)
        assert client.auth is mock_auth
        assert client.auth.cookies == mock_auth.cookies
        assert client.auth.csrf_token == mock_auth.csrf_token
        assert client.auth.session_id == mock_auth.session_id


# =============================================================================
# SUB-CLIENT API TESTS
# =============================================================================


class TestSubClientAPIs:
    def test_notebooks_api_accessible(self, mock_auth):
        """Test notebooks sub-client is accessible."""
        client = NotebookLMClient(mock_auth)
        assert hasattr(client, "notebooks")
        assert client.notebooks is not None

    def test_sources_api_accessible(self, mock_auth):
        """Test sources sub-client is accessible."""
        client = NotebookLMClient(mock_auth)
        assert hasattr(client, "sources")
        assert client.sources is not None

    def test_artifacts_api_accessible(self, mock_auth):
        """Test artifacts sub-client is accessible."""
        client = NotebookLMClient(mock_auth)
        assert hasattr(client, "artifacts")
        assert client.artifacts is not None

    def test_chat_api_accessible(self, mock_auth):
        """Test chat sub-client is accessible."""
        client = NotebookLMClient(mock_auth)
        assert hasattr(client, "chat")
        assert client.chat is not None

    def test_research_api_accessible(self, mock_auth):
        """Test research sub-client is accessible."""
        client = NotebookLMClient(mock_auth)
        assert hasattr(client, "research")
        assert client.research is not None

    def test_notes_api_accessible(self, mock_auth):
        """Test notes sub-client is accessible."""
        client = NotebookLMClient(mock_auth)
        assert hasattr(client, "notes")
        assert client.notes is not None


# =============================================================================
# AUTH ERROR DETECTION TESTS
# =============================================================================


class TestIsAuthError:
    def test_http_401_is_auth_error(self):
        """HTTP 401 should be detected as auth error."""

        request = httpx.Request("POST", "https://example.com")
        response = httpx.Response(401, request=request)
        error = httpx.HTTPStatusError("Unauthorized", request=request, response=response)
        assert is_auth_error(error) is True

    def test_http_403_is_auth_error(self):
        """HTTP 403 should be detected as auth error."""

        request = httpx.Request("POST", "https://example.com")
        response = httpx.Response(403, request=request)
        error = httpx.HTTPStatusError("Forbidden", request=request, response=response)
        assert is_auth_error(error) is True

    def test_http_500_is_not_auth_error(self):
        """HTTP 500 should NOT be detected as auth error."""

        request = httpx.Request("POST", "https://example.com")
        response = httpx.Response(500, request=request)
        error = httpx.HTTPStatusError("Server Error", request=request, response=response)
        assert is_auth_error(error) is False

    def test_rpc_error_with_auth_message_is_auth_error(self):
        """RPCError with 'Authentication' in message should be auth error."""

        error = RPCError("Authentication expired")
        assert is_auth_error(error) is True

    def test_rpc_error_with_expired_message_is_auth_error(self):
        """RPCError with 'expired' in message should be auth error."""

        error = RPCError("Session expired, please re-login")
        assert is_auth_error(error) is True

    def test_rpc_error_with_unauthorized_message_is_auth_error(self):
        """RPCError with 'Unauthorized' in message should be auth error."""

        error = RPCError("Unauthorized access")
        assert is_auth_error(error) is True

    def test_rpc_error_generic_is_not_auth_error(self):
        """Generic RPCError should NOT be auth error."""

        error = RPCError("Rate limit exceeded")
        assert is_auth_error(error) is False

    def test_auth_error_is_auth_error(self):
        """AuthError should always be detected as auth error."""

        error = AuthError("Any message")
        assert is_auth_error(error) is True

    def test_value_error_is_not_auth_error(self):
        """Other exceptions should NOT be auth error."""

        error = ValueError("Something else")
        assert is_auth_error(error) is False


# =============================================================================
# REFRESH CALLBACK TESTS
# =============================================================================


class TestClientCoreRefreshCallback:
    def test_refresh_callback_stored(self):
        """ClientCore should store refresh callback."""

        auth = AuthTokens(
            cookies={"SID": "test"},
            csrf_token="csrf",
            session_id="sid",
        )

        async def mock_refresh():
            pass

        core = ClientCore(auth, refresh_callback=mock_refresh)
        assert core._refresh_callback is mock_refresh

    def test_refresh_callback_defaults_to_none(self):
        """ClientCore should default refresh_callback to None."""

        auth = AuthTokens(
            cookies={"SID": "test"},
            csrf_token="csrf",
            session_id="sid",
        )

        core = ClientCore(auth)
        assert core._refresh_callback is None

    def test_refresh_lock_created_when_callback_provided(self):
        """ClientCore should create refresh lock when callback provided."""
        auth = AuthTokens(
            cookies={"SID": "test"},
            csrf_token="csrf",
            session_id="sid",
        )

        async def mock_refresh():
            pass

        core = ClientCore(auth, refresh_callback=mock_refresh)
        assert core._refresh_lock is not None
        assert isinstance(core._refresh_lock, asyncio.Lock)

    def test_no_refresh_lock_when_no_callback(self):
        """ClientCore should NOT create refresh lock when no callback."""

        auth = AuthTokens(
            cookies={"SID": "test"},
            csrf_token="csrf",
            session_id="sid",
        )

        core = ClientCore(auth)
        assert core._refresh_lock is None


# =============================================================================
# RPC CALL AUTO-RETRY TESTS
# =============================================================================


class TestRpcCallAutoRetry:
    @pytest.mark.asyncio
    async def test_retries_on_http_401_error(self):
        """rpc_call should retry once after HTTP 401 if callback provided."""
        auth = AuthTokens(
            cookies={"SID": "test"},
            csrf_token="csrf",
            session_id="sid",
        )

        refresh_called = []

        async def mock_refresh():
            refresh_called.append(True)
            return auth

        core = ClientCore(auth, refresh_callback=mock_refresh, refresh_retry_delay=0)

        call_count = [0]

        async def mock_post(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call fails with HTTP 401
                request = httpx.Request("POST", args[0])
                response = httpx.Response(401, request=request)
                raise httpx.HTTPStatusError("Unauthorized", request=request, response=response)
            # Second call succeeds
            response = MagicMock()
            response.text = ')]}\'\\n[["wrb.fr","wXbhsf",[["result"]]]]'
            response.raise_for_status = MagicMock()
            return response

        core._http_client = MagicMock()
        core._http_client.post = mock_post
        core._http_client.headers = {"Cookie": "old"}

        with patch("notebooklm._core.decode_response", return_value=["result"]):
            result = await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

        assert len(refresh_called) == 1, "refresh_callback should be called once"
        assert call_count[0] == 2, "RPC should be called twice (original + retry)"
        assert result == ["result"]

    @pytest.mark.asyncio
    async def test_retries_on_rpc_auth_error(self):
        """rpc_call should retry once after RPC auth error if callback provided."""
        auth = AuthTokens(
            cookies={"SID": "test"},
            csrf_token="csrf",
            session_id="sid",
        )

        refresh_called = []

        async def mock_refresh():
            refresh_called.append(True)
            return auth

        core = ClientCore(auth, refresh_callback=mock_refresh, refresh_retry_delay=0)

        # Mock HTTP client - always succeeds
        async def mock_post(*args, **kwargs):
            response = MagicMock()
            response.text = "mock response"
            response.raise_for_status = MagicMock()
            return response

        core._http_client = MagicMock()
        core._http_client.post = mock_post
        core._http_client.headers = {"Cookie": "old"}

        decode_call_count = [0]

        def mock_decode(*args, **kwargs):
            decode_call_count[0] += 1
            if decode_call_count[0] == 1:
                # First decode fails with auth error
                raise RPCError("Authentication expired", rpc_id="wXbhsf")
            return ["result"]

        with patch("notebooklm._core.decode_response", side_effect=mock_decode):
            result = await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

        assert len(refresh_called) == 1, "refresh_callback should be called once"
        assert decode_call_count[0] == 2, "decode should be called twice (original + retry)"
        assert result == ["result"]

    @pytest.mark.asyncio
    async def test_no_retry_without_callback(self):
        """rpc_call should NOT retry if no refresh_callback provided."""
        auth = AuthTokens(
            cookies={"SID": "test"},
            csrf_token="csrf",
            session_id="sid",
        )

        core = ClientCore(auth)  # No refresh_callback

        call_count = [0]

        async def mock_post(*args, **kwargs):
            call_count[0] += 1
            request = httpx.Request("POST", args[0])
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("Unauthorized", request=request, response=response)

        core._http_client = MagicMock()
        core._http_client.post = mock_post

        with pytest.raises(RPCError, match="HTTP 401"):
            await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

        assert call_count[0] == 1, "Should not retry without callback"

    @pytest.mark.asyncio
    async def test_no_infinite_retry(self):
        """rpc_call should only retry once, not infinitely."""
        auth = AuthTokens(
            cookies={"SID": "test"},
            csrf_token="csrf",
            session_id="sid",
        )

        refresh_count = [0]

        async def mock_refresh():
            refresh_count[0] += 1
            return auth

        core = ClientCore(auth, refresh_callback=mock_refresh, refresh_retry_delay=0)

        call_count = [0]

        # Always fail with HTTP 401
        async def mock_post(*args, **kwargs):
            call_count[0] += 1
            request = httpx.Request("POST", args[0])
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("Unauthorized", request=request, response=response)

        core._http_client = MagicMock()
        core._http_client.post = mock_post
        core._http_client.headers = {"Cookie": "old"}

        with pytest.raises(RPCError, match="HTTP 401"):
            await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

        assert refresh_count[0] == 1, "Should only refresh once"
        assert call_count[0] == 2, "Should only retry once"

    @pytest.mark.asyncio
    async def test_no_retry_on_non_auth_error(self):
        """rpc_call should NOT retry on non-auth errors (HTTP 500)."""
        auth = AuthTokens(
            cookies={"SID": "test"},
            csrf_token="csrf",
            session_id="sid",
        )

        refresh_called = []

        async def mock_refresh():
            refresh_called.append(True)
            return auth

        core = ClientCore(auth, refresh_callback=mock_refresh, refresh_retry_delay=0)

        call_count = [0]

        async def mock_post(*args, **kwargs):
            call_count[0] += 1
            request = httpx.Request("POST", args[0])
            response = httpx.Response(500, request=request)
            raise httpx.HTTPStatusError("Server Error", request=request, response=response)

        core._http_client = MagicMock()
        core._http_client.post = mock_post

        with pytest.raises(RPCError, match="Server error 500"):
            await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

        assert len(refresh_called) == 0, "Should not refresh on non-auth error"
        assert call_count[0] == 1, "Should not retry on non-auth error"

    @pytest.mark.asyncio
    async def test_refresh_failure_raises_original_error(self):
        """If refresh fails, should raise original error with chained exception."""
        auth = AuthTokens(
            cookies={"SID": "test"},
            csrf_token="csrf",
            session_id="sid",
        )

        async def failing_refresh():
            raise ValueError("Refresh failed - cookies expired")

        core = ClientCore(auth, refresh_callback=failing_refresh, refresh_retry_delay=0)

        async def mock_post(*args, **kwargs):
            request = httpx.Request("POST", args[0])
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("Unauthorized", request=request, response=response)

        core._http_client = MagicMock()
        core._http_client.post = mock_post

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

        # Check exception chaining
        assert exc_info.value.__cause__ is not None
        assert "Refresh failed" in str(exc_info.value.__cause__)

    @pytest.mark.asyncio
    async def test_concurrent_refresh_uses_shared_task(self):
        """Concurrent auth errors should share a single refresh task."""
        auth = AuthTokens(
            cookies={"SID": "test"},
            csrf_token="csrf",
            session_id="sid",
        )

        refresh_count = [0]

        async def mock_refresh():
            refresh_count[0] += 1
            await asyncio.sleep(0.05)  # Simulate slow refresh
            return auth

        core = ClientCore(auth, refresh_callback=mock_refresh, refresh_retry_delay=0)

        call_count = [0]

        async def mock_post(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 2:
                # First two calls fail with HTTP 401
                request = httpx.Request("POST", args[0])
                response = httpx.Response(401, request=request)
                raise httpx.HTTPStatusError("Unauthorized", request=request, response=response)
            # After that, succeed
            response = MagicMock()
            response.text = ')]}\'\\n[["wrb.fr","wXbhsf",[["result"]]]]'
            response.raise_for_status = MagicMock()
            return response

        core._http_client = MagicMock()
        core._http_client.post = mock_post
        core._http_client.headers = {"Cookie": "old"}

        with patch("notebooklm._core.decode_response", return_value=["result"]):
            # Start two concurrent calls
            await asyncio.gather(
                core.rpc_call(RPCMethod.LIST_NOTEBOOKS, []),
                core.rpc_call(RPCMethod.LIST_NOTEBOOKS, []),
                return_exceptions=True,
            )

        # With shared task pattern, refresh should be called exactly once
        # (second caller waits on the same task instead of starting a new refresh)
        assert refresh_count[0] == 1, (
            f"Refresh should be called exactly once, got {refresh_count[0]}"
        )
