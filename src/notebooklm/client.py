"""NotebookLM API Client - Main entry point.

This module provides the NotebookLMClient class, a modern async client
for interacting with Google NotebookLM using undocumented RPC APIs.

Example:
    async with NotebookLMClient.from_storage() as client:
        # List notebooks
        notebooks = await client.notebooks.list()

        # Add sources
        source = await client.sources.add_url(notebook_id, "https://example.com")

        # Generate artifacts
        status = await client.artifacts.generate_audio(notebook_id)
        await client.artifacts.wait_for_completion(notebook_id, status.task_id)

        # Chat with the notebook
        result = await client.chat.ask(notebook_id, "What is this about?")
"""

import logging
import re
from pathlib import Path

from ._artifacts import ArtifactsAPI
from ._chat import ChatAPI
from ._core import DEFAULT_TIMEOUT, ClientCore
from ._notebooks import NotebooksAPI
from ._notes import NotesAPI
from ._research import ResearchAPI
from ._settings import SettingsAPI
from ._sources import SourcesAPI
from ._url_utils import is_google_auth_redirect
from .auth import AuthTokens

logger = logging.getLogger(__name__)


class NotebookLMClient:
    """Async client for NotebookLM API.

    Provides access to NotebookLM functionality through namespaced sub-clients:
    - notebooks: Create, list, delete, rename notebooks
    - sources: Add, list, delete sources (URLs, text, files, YouTube, Drive)
    - artifacts: Generate and manage AI content (audio, video, reports, etc.)
    - chat: Ask questions and manage conversations
    - research: Start research sessions and import sources
    - notes: Create and manage user notes
    - settings: Manage user settings (output language, etc.)

    Usage:
        # Create from saved authentication
        async with NotebookLMClient.from_storage() as client:
            notebooks = await client.notebooks.list()

        # Create from AuthTokens directly
        auth = AuthTokens(cookies, csrf_token, session_id)
        async with NotebookLMClient(auth) as client:
            notebooks = await client.notebooks.list()

    Attributes:
        notebooks: NotebooksAPI for notebook operations
        sources: SourcesAPI for source management
        artifacts: ArtifactsAPI for AI-generated content
        chat: ChatAPI for conversations
        research: ResearchAPI for web/drive research
        notes: NotesAPI for user notes
        settings: SettingsAPI for user settings
        auth: The AuthTokens used for authentication
    """

    def __init__(self, auth: AuthTokens, timeout: float = DEFAULT_TIMEOUT):
        """Initialize the NotebookLM client.

        Args:
            auth: Authentication tokens from browser login.
            timeout: HTTP request timeout in seconds. Defaults to 30 seconds.
        """
        # Pass refresh_auth as callback for automatic retry on auth failures
        # Note: refresh_auth calls update_auth_headers internally
        self._core = ClientCore(auth, timeout=timeout, refresh_callback=self.refresh_auth)

        # Initialize sub-client APIs
        # Note: notes must be initialized before artifacts (artifacts uses notes API)
        self.notebooks = NotebooksAPI(self._core)
        self.sources = SourcesAPI(self._core)
        self.notes = NotesAPI(self._core)
        self.artifacts = ArtifactsAPI(self._core, notes_api=self.notes)
        self.chat = ChatAPI(self._core)
        self.research = ResearchAPI(self._core)
        self.settings = SettingsAPI(self._core)

    @property
    def auth(self) -> AuthTokens:
        """Get the authentication tokens."""
        return self._core.auth

    async def __aenter__(self) -> "NotebookLMClient":
        """Open the client connection."""
        logger.debug("Opening NotebookLM client")
        await self._core.open()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Close the client connection."""
        logger.debug("Closing NotebookLM client")
        await self._core.close()

    @property
    def is_connected(self) -> bool:
        """Check if the client is connected."""
        return self._core.is_open

    @classmethod
    async def from_storage(
        cls, path: str | None = None, timeout: float = DEFAULT_TIMEOUT
    ) -> "NotebookLMClient":
        """Create a client from Playwright storage state file.

        This is the recommended way to create a client for programmatic use.
        Handles all authentication setup automatically.

        Args:
            path: Path to storage_state.json. If None, uses default location
                  (~/.notebooklm/storage_state.json).
            timeout: HTTP request timeout in seconds. Defaults to 30 seconds.

        Returns:
            NotebookLMClient instance (not yet connected).

        Example:
            async with await NotebookLMClient.from_storage() as client:
                notebooks = await client.notebooks.list()
        """
        storage_path = Path(path) if path else None
        auth = await AuthTokens.from_storage(storage_path)
        return cls(auth, timeout=timeout)

    async def refresh_auth(self) -> AuthTokens:
        """Refresh authentication tokens by fetching the NotebookLM homepage.

        This helps prevent 'Session Expired' errors by obtaining a fresh CSRF
        token (SNlM0e) and session ID (FdrFJe).

        Returns:
            Updated AuthTokens.

        Raises:
            ValueError: If token extraction fails (page structure may have changed).
        """
        http_client = self._core.get_http_client()
        response = await http_client.get("https://notebooklm.google.com/")
        response.raise_for_status()

        # Check for redirect to login page
        final_url = str(response.url)
        if is_google_auth_redirect(final_url):
            raise ValueError("Authentication expired. Run 'notebooklm login' to re-authenticate.")

        # Extract SNlM0e (CSRF token) - REQUIRED
        csrf_match = re.search(r'"SNlM0e":"([^"]+)"', response.text)
        if not csrf_match:
            raise ValueError(
                "Failed to extract CSRF token (SNlM0e). "
                "Page structure may have changed or authentication expired."
            )
        self._core.auth.csrf_token = csrf_match.group(1)

        # Extract FdrFJe (Session ID) - REQUIRED
        sid_match = re.search(r'"FdrFJe":"([^"]+)"', response.text)
        if not sid_match:
            raise ValueError(
                "Failed to extract session ID (FdrFJe). "
                "Page structure may have changed or authentication expired."
            )
        self._core.auth.session_id = sid_match.group(1)

        # CRITICAL: Update the HTTP client headers with new auth tokens
        # Without this, the client continues using stale credentials
        self._core.update_auth_headers()

        return self._core.auth
