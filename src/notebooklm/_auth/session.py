"""Auth session refresh implementation."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import TYPE_CHECKING, Protocol, cast

from .._env import get_base_url
from .._url_utils import is_google_auth_redirect
from ..exceptions import AuthExtractionError
from .account import authuser_query
from .extraction import extract_wiz_field
from .tokens import AuthTokens

if TYPE_CHECKING:
    from .._kernel import Kernel
    from .._session_init import SessionCollaborators
    from .._session_lifecycle import _LifecycleHost


class RefreshAuthCore(Protocol):
    """Structural core boundary required by auth session refresh.

    Wave 11b of session-decoupling (ADR-014): the live HTTP client is
    sourced via ``core._kernel.get_http_client()`` instead of a
    ``core.get_http_client()`` forward on ``Session``. The underscore-
    prefixed ``_kernel`` slot mirrors the live ``Session._kernel``
    attribute so structural conformance does not require renaming the
    Session slot.

    Wave 11c of session-decoupling: the ``save_cookies`` forward on
    ``Session`` was also deleted — :func:`refresh_auth_session` now
    reaches the cookie persistence chokepoint through
    ``core.collaborators.lifecycle`` directly
    (``ClientLifecycle.save_cookies``), which is the canonical home
    identified by ADR-014.
    """

    auth: AuthTokens
    _kernel: Kernel

    def update_auth_tokens(self, csrf: str, session_id: str) -> Awaitable[None]:
        """Atomically update auth token scalars."""
        ...

    def update_auth_headers(self) -> None:
        """Refresh auth-dependent HTTP state after token mutation."""
        ...

    @property
    def collaborators(self) -> SessionCollaborators:
        """Access the constructed collaborator bundle (Stage A accessor)."""
        ...


async def refresh_auth_session(core: RefreshAuthCore) -> AuthTokens:
    """Refresh NotebookLM auth tokens through the raw homepage session path."""
    http_client = core._kernel.get_http_client()
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
    # Persist through ClientLifecycle.save_cookies so refresh serializes
    # with keepalive and close saves. The ``Session.save_cookies`` forward
    # was deleted in Wave 11c of session-decoupling; callers now reach the
    # lifecycle collaborator directly. The ``cast`` widens the
    # ``RefreshAuthCore`` shape to the lifecycle's ``_LifecycleHost`` shape —
    # ``Session`` (the only production caller) satisfies both Protocols
    # structurally; the cast is a typing-level acknowledgement that
    # ``RefreshAuthCore`` deliberately stays narrow (it doesn't pull in
    # the lifecycle host's metrics / drain / reqid / auth-coord fields
    # because none of those are read by ``refresh_auth_session``).
    await core.collaborators.lifecycle.save_cookies(
        cast("_LifecycleHost", core), http_client.cookies
    )

    return core.auth
