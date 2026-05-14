"""NotebookLM API Client - Main entry point.

This module provides the NotebookLMClient class, a modern async client
for interacting with Google NotebookLM using undocumented RPC APIs.

Example:
    async with await NotebookLMClient.from_storage() as client:
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

import dataclasses
import logging
import os
import re
from pathlib import Path
from types import TracebackType

from ._artifacts import ArtifactsAPI
from ._chat import ChatAPI
from ._core import DEFAULT_KEEPALIVE_MIN_INTERVAL, DEFAULT_TIMEOUT, ClientCore
from ._env import get_base_url
from ._notebooks import NotebooksAPI
from ._notes import NotesAPI
from ._research import ResearchAPI
from ._settings import SettingsAPI
from ._sharing import SharingAPI
from ._sources import SourcesAPI
from ._url_utils import is_google_auth_redirect
from .auth import AuthTokens, authuser_query

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
    - sharing: Manage notebook sharing and permissions

    Usage:
        # Create from saved authentication
        async with await NotebookLMClient.from_storage() as client:
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
        sharing: SharingAPI for notebook sharing
        auth: The AuthTokens used for authentication
    """

    def __init__(
        self,
        auth: AuthTokens,
        timeout: float = DEFAULT_TIMEOUT,
        storage_path: Path | None = None,
        keepalive: float | None = None,
        keepalive_min_interval: float = DEFAULT_KEEPALIVE_MIN_INTERVAL,
        rate_limit_max_retries: int = 0,
        server_error_max_retries: int = 3,
    ):
        """Initialize the NotebookLM client.

        Args:
            auth: Authentication tokens from browser login.
            timeout: HTTP request timeout in seconds. Defaults to 30 seconds.
            storage_path: Path to the storage state file for loading download cookies.
            keepalive: Optional interval in seconds for a background task that
                pokes ``accounts.google.com`` while the client is open, eliciting
                ``__Secure-1PSIDTS`` rotation so long-lived clients (e.g. agents,
                long-running workers) don't silently stale out. ``None`` (default)
                disables the task — preserving existing CLI semantics. Values
                below ``keepalive_min_interval`` are clamped up to that floor.
            keepalive_min_interval: Lower bound for ``keepalive`` (defaults to
                60 s) to avoid accidentally rate-limiting Google's identity
                surface.
            rate_limit_max_retries: Max automatic retries on HTTP 429 with a
                parseable ``Retry-After``. ``0`` (default) preserves the
                pre-Phase-3 contract of raising immediately. See
                :class:`ClientCore` for the per-attempt sleep semantics.
            server_error_max_retries: Max automatic retries for retryable
                transient failures: HTTP 5xx and network-layer
                ``httpx.RequestError`` (timeouts, connect errors). Defaults to
                ``3``. Uses exponential backoff ``min(2 ** attempt, 30)``
                seconds. Set to ``0`` to disable.
        """
        # Normalize the effective storage path onto the auth object so every
        # downstream code path (refresh_auth, ClientCore.close on-close save,
        # the keepalive loop) writes to the same file. Without this, an
        # explicit ``storage_path=`` kwarg only reaches the keepalive loop
        # while ``auth.storage_path is None`` causes refresh and on-close
        # saves to silently skip persistence. ``dataclasses.replace`` instead
        # of in-place mutation so a caller reusing ``AuthTokens`` across
        # multiple clients (with different storage paths) doesn't see one
        # client's path leak into another.
        if storage_path is not None and auth.storage_path != storage_path:
            auth = dataclasses.replace(auth, storage_path=storage_path)

        # Pass refresh_auth as callback for automatic retry on auth failures
        # Note: refresh_auth calls update_auth_headers internally
        self._core = ClientCore(
            auth,
            timeout=timeout,
            refresh_callback=self.refresh_auth,
            keepalive=keepalive,
            keepalive_min_interval=keepalive_min_interval,
            keepalive_storage_path=auth.storage_path,
            rate_limit_max_retries=rate_limit_max_retries,
            server_error_max_retries=server_error_max_retries,
        )

        # Initialize sub-client APIs
        # Note: notes must be initialized before artifacts (artifacts uses notes API)
        self.notebooks = NotebooksAPI(self._core)
        self.sources = SourcesAPI(self._core)
        self.notes = NotesAPI(self._core)
        self.artifacts = ArtifactsAPI(self._core, notes_api=self.notes, storage_path=storage_path)
        self.chat = ChatAPI(self._core)
        self.research = ResearchAPI(self._core)
        self.settings = SettingsAPI(self._core)
        self.sharing = SharingAPI(self._core)

    @property
    def auth(self) -> AuthTokens:
        """Get the authentication tokens."""
        return self._core.auth

    async def __aenter__(self) -> "NotebookLMClient":
        """Open the client connection."""
        logger.debug("Opening NotebookLM client")
        await self._core.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Close the client connection."""
        logger.debug("Closing NotebookLM client")
        await self._core.close()

    @property
    def is_connected(self) -> bool:
        """Check if the client is connected."""
        return self._core.is_open

    @classmethod
    async def from_storage(
        cls,
        path: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        profile: str | None = None,
        keepalive: float | None = None,
        keepalive_min_interval: float = DEFAULT_KEEPALIVE_MIN_INTERVAL,
        rate_limit_max_retries: int = 0,
        server_error_max_retries: int = 3,
    ) -> "NotebookLMClient":
        """Create a client from Playwright storage state file.

        This is the recommended way to create a client for programmatic use.
        Handles all authentication setup automatically.

        Args:
            path: Path to storage_state.json. If provided, takes precedence over profile.
            timeout: HTTP request timeout in seconds. Defaults to 30 seconds.
            profile: Profile name to load auth from (e.g., "work", "personal").
                If None, uses the active profile (from CLI flag, env var, or config).
            keepalive: Optional interval in seconds for the background SIDTS
                rotation poke. ``None`` disables it (default). See
                :class:`NotebookLMClient` for full semantics.
            keepalive_min_interval: Floor for ``keepalive`` (defaults to 60 s).
            rate_limit_max_retries: Max automatic retries on HTTP 429. ``0``
                (default) preserves pre-Phase-3 raise-immediately behavior.
            server_error_max_retries: Max automatic retries for HTTP 5xx /
                network errors with exponential backoff. Defaults to ``3``.

        Returns:
            NotebookLMClient instance (not yet connected).

        Example:
            async with await NotebookLMClient.from_storage() as client:
                notebooks = await client.notebooks.list()

            # Use a specific profile
            async with await NotebookLMClient.from_storage(profile="work") as client:
                notebooks = await client.notebooks.list()

            # Long-lived client with periodic keepalive (e.g. an agent worker)
            async with await NotebookLMClient.from_storage(keepalive=600) as client:
                ...
        """
        storage_path = Path(path) if path else None
        auth = await AuthTokens.from_storage(storage_path, profile=profile)
        # Always resolve the storage path so downstream cookie loading
        # (e.g. artifact downloads) uses the correct file, whether the
        # caller provided an explicit path, a named profile, or neither.
        if storage_path is None and not os.environ.get("NOTEBOOKLM_AUTH_JSON"):
            from .paths import get_storage_path

            storage_path = get_storage_path(profile)
        return cls(
            auth,
            timeout=timeout,
            storage_path=storage_path,
            keepalive=keepalive,
            keepalive_min_interval=keepalive_min_interval,
            rate_limit_max_retries=rate_limit_max_retries,
            server_error_max_retries=server_error_max_retries,
        )

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
        url = f"{get_base_url()}/"
        if self.auth.account_email or self.auth.authuser:
            url = f"{url}?{authuser_query(self.auth.authuser, self.auth.account_email)}"
        response = await http_client.get(url)
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

        # Persist refreshed cookies back to disk so the next CLI invocation
        # picks up the updated short-lived tokens (e.g., __Secure-1PSIDCC).
        # Routed through ClientCore.save_cookies so it serializes with the
        # keepalive worker and the on-close save via ``_save_lock`` — without
        # that, refresh_auth's synchronous save can race with an in-flight
        # keepalive save and an older snapshot can clobber the freshly
        # refreshed tokens.
        await self._core.save_cookies(http_client.cookies)

        return self._core.auth
