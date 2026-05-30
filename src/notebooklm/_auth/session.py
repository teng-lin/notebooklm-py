"""Auth session refresh implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .._env import get_base_url
from .._url_utils import is_google_auth_redirect
from ..exceptions import AuthExtractionError
from .account import authuser_query
from .extraction import extract_wiz_field
from .tokens import AuthTokens

if TYPE_CHECKING:
    from .._cookie_persistence import CookiePersistence
    from .._kernel import Kernel
    from .._runtime_auth import AuthRefreshCoordinator
    from .._runtime_lifecycle import ClientLifecycle


async def refresh_auth_session(
    *,
    auth: AuthTokens,
    kernel: Kernel,
    auth_coord: AuthRefreshCoordinator,
    lifecycle: ClientLifecycle,
    cookie_persistence: CookiePersistence,
) -> AuthTokens:
    """Refresh NotebookLM auth tokens through the raw homepage session path.

    This function takes five explicit keyword-only collaborators rather than
    the legacy Session-shaped core Protocol + ``ClientLifecycle`` argument
    shape. The previous shape
    required a Session-aliased core that re-declared the underlying
    private slots (``auth`` / ``_kernel`` / ``update_auth_tokens`` /
    ``update_auth_headers``) and a separate ``cast`` to satisfy the
    lifecycle's ``host``-shaped ``save_cookies`` signature; both have
    been lifted now that every collaborator the refresh path needs is
    in scope directly. The single production caller
    (:meth:`NotebookLMClient.refresh_auth`) sources the five
    collaborators from ``self._auth`` and ``self._collaborators``.
    """
    http_client = kernel.get_http_client()
    url = f"{get_base_url()}/"
    if auth.account_email or auth.authuser:
        url = f"{url}?{authuser_query(auth.authuser, auth.account_email)}"
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
    await auth_coord.update_auth_tokens(auth=auth, csrf=csrf or "", session_id=sid or "")
    auth_coord.update_auth_headers(auth=auth, kernel=kernel)
    # Persist through ``ClientLifecycle.save_cookies`` so refresh
    # serializes with keepalive and close saves. The lifecycle's
    # ``save_cookies`` takes the :class:`CookiePersistence` collaborator
    # directly — the first positional argument is the cookie-persistence
    # collaborator the caller already holds rather than a Session-shaped
    # ``host``, eliminating the prior ``cast`` to a Protocol-typed host.
    await lifecycle.save_cookies(cookie_persistence, http_client.cookies)

    return auth
