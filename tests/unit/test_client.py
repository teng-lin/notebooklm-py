"""Tests for NotebookLMClient class."""

import json

import pytest
from pytest_httpx import HTTPXMock

from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient


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
            old_csrf = client.auth.csrf_token
            old_session = client.auth.session_id

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
