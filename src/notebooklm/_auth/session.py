"""Auth session refresh implementation."""

from __future__ import annotations

from collections.abc import Awaitable
from pathlib import Path
from typing import Protocol

import httpx

from .._env import get_base_url
from .._url_utils import is_google_auth_redirect
from ..auth import AuthTokens, authuser_query, extract_wiz_field
from ..exceptions import AuthExtractionError


class RefreshAuthCore(Protocol):
    """Structural core boundary required by auth session refresh."""

    auth: AuthTokens

    def get_http_client(self) -> httpx.AsyncClient:
        """Return the live HTTP client."""
        ...

    def update_auth_tokens(self, csrf: str, session_id: str) -> Awaitable[None]:
        """Atomically update auth token scalars."""
        ...

    def update_auth_headers(self) -> None:
        """Refresh auth-dependent HTTP state after token mutation."""
        ...

    def save_cookies(self, jar: httpx.Cookies, path: Path | None = None) -> Awaitable[None]:
        """Persist refreshed cookies."""
        ...


async def refresh_auth_session(core: RefreshAuthCore) -> AuthTokens:
    """Refresh NotebookLM auth tokens through the raw homepage session path."""
    http_client = core.get_http_client()
    url = f"{get_base_url()}/"
    if core.auth.account_email or core.auth.authuser:
        url = f"{url}?{authuser_query(core.auth.authuser, core.auth.account_email)}"
    response = await http_client.get(url)
    response.raise_for_status()

    final_url = str(response.url)
    if is_google_auth_redirect(final_url):
        raise ValueError("Authentication expired. Run 'notebooklm login' to re-authenticate.")

    try:
        csrf = extract_wiz_field(response.text, "SNlM0e", strict=True)
        sid = extract_wiz_field(response.text, "FdrFJe", strict=True)
    except AuthExtractionError as exc:
        label = {"SNlM0e": "CSRF token", "FdrFJe": "session ID"}.get(exc.key, exc.key)
        raise ValueError(
            f"Failed to extract {label} ({exc.key}). "
            "Page structure may have changed or authentication expired. "
            f"Preview: {exc.payload_preview!r}"
        ) from exc

    # Keep the csrf/session mutation centralized so RPC snapshots cannot
    # observe a torn token pair while refresh is in flight.
    await core.update_auth_tokens(csrf or "", sid or "")
    core.update_auth_headers()
    # Persist through ClientCore.save_cookies so refresh serializes with
    # keepalive and close saves.
    await core.save_cookies(http_client.cookies)

    return core.auth
