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
    from .._session_lifecycle import ClientLifecycle, _LifecycleHost


class RefreshAuthCore(Protocol):
    """Structural core boundary required by auth session refresh.

    Wave 11b of session-decoupling (ADR-014): the live HTTP client is
    sourced via ``core._kernel.get_http_client()`` instead of a
    ``core.get_http_client()`` forward on ``Session``. The underscore-
    prefixed ``_kernel`` slot mirrors the live ``Session._kernel``
    attribute so structural conformance does not require renaming the
    Session slot.

    Wave 11c of session-decoupling: the ``save_cookies`` forward on
    ``Session`` was deleted — :func:`refresh_auth_session` reached the
    cookie persistence chokepoint through ``core.collaborators.lifecycle``.

    Stage B1 PR 2 of the post-refactoring plan further narrowed this
    Protocol: the ``collaborators`` property was deleted from
    :class:`Session` along with the other Stage A accessors, so
    :func:`refresh_auth_session` now takes ``lifecycle: ClientLifecycle``
    as an explicit argument supplied by the caller
    (:meth:`NotebookLMClient.refresh_auth` passes
    ``self._collaborators.lifecycle``). The Protocol no longer needs a
    ``collaborators`` field.
    """

    auth: AuthTokens
    _kernel: Kernel

    def update_auth_tokens(self, csrf: str, session_id: str) -> Awaitable[None]:
        """Atomically update auth token scalars."""
        ...

    def update_auth_headers(self) -> None:
        """Refresh auth-dependent HTTP state after token mutation."""
        ...


async def refresh_auth_session(
    core: RefreshAuthCore,
    lifecycle: ClientLifecycle,
) -> AuthTokens:
    """Refresh NotebookLM auth tokens through the raw homepage session path.

    Stage B1 PR 2 of the post-refactoring plan made ``lifecycle`` an
    explicit second argument. Previously this function reached the
    canonical cookie-persistence chokepoint through
    ``core.collaborators.lifecycle`` (a Stage A accessor on
    :class:`Session`). PR 2 deleted that accessor — :class:`Session` no
    longer carries a ``collaborators`` property — so callers now pass
    ``ClientLifecycle`` directly. The single production caller
    (:meth:`NotebookLMClient.refresh_auth`) supplies
    ``self._collaborators.lifecycle``.
    """
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
    # Persist through ``ClientLifecycle.save_cookies`` so refresh
    # serializes with keepalive and close saves. The ``host`` argument
    # of :meth:`ClientLifecycle.save_cookies` is read for its
    # ``_metrics_obj`` / ``cookie_persistence`` attributes; ``Session``
    # (the only production caller) satisfies that shape, and unit-test
    # fakes mirror those attributes via a recording lifecycle. The
    # ``cast`` is the typing-level acknowledgement that
    # :class:`RefreshAuthCore` deliberately stays narrow (only declares
    # what :func:`refresh_auth_session` reads); widening the Protocol
    # would couple this module to the lifecycle's broader collaborator
    # surface, which is what Stage B1 PR 2 is moving away from.
    await lifecycle.save_cookies(cast("_LifecycleHost", core), http_client.cookies)

    return core.auth
