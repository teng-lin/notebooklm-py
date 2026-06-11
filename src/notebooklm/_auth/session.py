"""Auth session refresh implementation."""

from __future__ import annotations

import asyncio
import logging
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
    from .._runtime.auth import AuthRefreshCoordinator
    from .._runtime.lifecycle import ClientLifecycle

logger = logging.getLogger("notebooklm.auth")


async def refresh_auth_session(
    *,
    auth: AuthTokens,
    kernel: Kernel,
    auth_coord: AuthRefreshCoordinator,
    lifecycle: ClientLifecycle,
    cookie_persistence: CookiePersistence,
    allow_headless: bool = False,
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

    Layer-3 headless re-auth (the deepest recovery layer):

    When the homepage GET 302s to the Google login page the first-party
    NotebookLM cookies are fully dead, and neither this L1 token refresh nor
    the L2 ``RotateCookies`` rotation can help. ``allow_headless`` (or the
    ``NOTEBOOKLM_HEADLESS_REAUTH=1`` env opt-in) lets this function fall
    through to :func:`notebooklm._auth.headless_reauth.attempt_headless_reauth`,
    which drives an unattended headless browser against the persistent profile
    to silently re-mint cookies. On a successful re-mint the fresh cookies are
    reloaded into the live HTTP client and the homepage GET is retried ONCE; if
    L3 is unavailable (no opt-in / no profile / playwright missing) or fails
    (the profile's Google session is also dead) the original dead-cookie
    ``ValueError`` stands unchanged — so default behavior with no opt-in and no
    profile is byte-identical to before.

    Coalescing: the mid-RPC cascade reaches this function through
    :meth:`AuthRefreshCoordinator.await_refresh` (the bound ``client.refresh_auth``
    callback), whose single-flight task creation means N concurrent failing
    RPCs trigger at most ONE refresh — and therefore at most one browser. The
    explicit ``client.refresh_auth(allow_headless=True)`` entry passes
    ``allow_headless`` straight through.
    """
    http_client = kernel.get_http_client()
    url = f"{get_base_url()}/"
    if auth.account_email or auth.authuser:
        url = f"{url}?{authuser_query(auth.authuser, auth.account_email)}"

    async def _get_and_extract() -> tuple[str, str] | None:
        """GET the homepage + extract tokens; ``None`` signals a dead-cookie 302."""
        response = await http_client.get(url)
        response.raise_for_status()
        if is_google_auth_redirect(str(response.url)):
            return None
        try:
            csrf_value = extract_wiz_field(response.text, "SNlM0e", strict=True)
            sid_value = extract_wiz_field(response.text, "FdrFJe", strict=True)
        except AuthExtractionError as exc:
            label = {"SNlM0e": "CSRF token", "FdrFJe": "session ID"}.get(exc.key, exc.key)
            raise ValueError(
                f"Failed to extract {label} ({exc.key}). "
                "Page structure may have changed or authentication expired. "
                f"Preview: {exc.payload_preview!r}"
            ) from exc
        return csrf_value or "", sid_value or ""

    extracted = await _get_and_extract()
    if extracted is None:
        # Dead first-party cookies. Try layer-3 headless re-auth (opt-in /
        # env-gated); on a successful re-mint, reload cookies and retry once.
        if await _try_headless_reauth(auth=auth, kernel=kernel, allow_headless=allow_headless):
            extracted = await _get_and_extract()
        if extracted is None:
            raise ValueError("Authentication expired. Run 'notebooklm login' to re-authenticate.")
    csrf, sid = extracted

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


async def _try_headless_reauth(
    *,
    auth: AuthTokens,
    kernel: Kernel,
    allow_headless: bool,
) -> bool:
    """Drive layer-3 headless re-auth and reload cookies on success.

    Returns ``True`` only when the headless re-auth succeeded AND the freshly
    persisted ``storage_state.json`` cookies were reloaded into the live HTTP
    client (so the caller's retry GET uses them). Returns ``False`` for every
    honest non-success outcome (unavailable / failed), leaving the original
    dead-cookie error to stand.

    The browser drive in
    :func:`notebooklm._auth.headless_reauth.attempt_headless_reauth` is a
    blocking, ``playwright``-sync call, so it is offloaded to a worker thread
    via :func:`asyncio.to_thread` and never stalls the event loop. The import
    is function-local so the ``browser`` extra stays optional.

    L3 requires a writeable on-disk storage path to (re)mint into. Env-var auth
    (``NOTEBOOKLM_AUTH_JSON``, ``auth.storage_path is None``) has no backing
    file, so L3 declines there — symmetric with the PSIDTS-recovery decline in
    :func:`notebooklm._auth.psidts_recovery._resolve_recovery_path`.

    Profile binding (CRITICAL): the headless browser is driven against the
    persistent profile that is a sibling of THIS client's ``storage_state.json``
    (``<storage_path>/../browser_profile``), NOT the ambient/default profile.
    A ``from_storage(profile="work")`` or ``--storage <path>`` client therefore
    re-mints from and into ITS OWN profile, never silently harvesting another
    account's session or overwriting the wrong storage file. When no such
    sibling profile exists, ``attempt_headless_reauth`` returns UNAVAILABLE.

    Cookie-domain policy (LIMITATION): the re-mint captures only the DEFAULT
    cookie-domain set (required Google cookies + regional ccTLDs). The optional
    ``--include-domains`` labels a user may have passed at ``notebooklm login``
    time are NOT persisted anywhere, so an L3 re-mint cannot reproduce them: a
    profile originally logged in with ``--include-domains=mail`` will, after a
    headless re-auth, hold a ``storage_state.json`` WITHOUT those optional
    sibling-product cookies. The re-auth still SUCCEEDS for NotebookLM itself
    (the required cookies are present); only opt-in extras are dropped.
    Operators relying on optional domains should re-run ``notebooklm login
    --include-domains=...`` after an L3 re-mint, or inspect their cookie domains.
    Persisting the login-time domain set (a small sidecar metadata file)
    remains a tracked follow-up.
    """
    storage_path = auth.storage_path
    if storage_path is None:
        logger.debug("Headless re-auth skipped: env-var auth has no writeable storage path.")
        return False

    from .cookies import _replace_cookie_jar, build_httpx_cookies_from_storage
    from .headless_reauth import HeadlessReauthStatus, attempt_headless_reauth

    # Bind the browser profile to the SAME profile as this storage file: the
    # persistent profile dir is the ``browser_profile`` sibling of
    # ``storage_state.json`` (see ``notebooklm.paths`` layout). Resolving it
    # explicitly here — rather than letting ``attempt_headless_reauth`` fall
    # back to the active/default profile — keeps a non-default client (custom
    # ``--storage`` or ``profile="work"``) from re-minting against the wrong
    # account's session.
    browser_profile = storage_path.parent / "browser_profile"

    result = await asyncio.to_thread(
        attempt_headless_reauth,
        storage_path=storage_path,
        allow_headless=allow_headless,
        browser_profile=browser_profile,
    )
    if result.status is not HeadlessReauthStatus.SUCCESS:
        # UNAVAILABLE / FAILED — honest non-success; the dead-cookie error
        # stands. ``reason`` is credential-free and safe to log at debug.
        logger.debug(
            "Headless re-auth did not succeed (%s): %s", result.status.value, result.reason
        )
        return False

    # Re-mint succeeded: reload the freshly-persisted cookies into the live
    # HTTP client's jar so the caller's retry GET authenticates with them. If
    # the reload/validation fails — e.g. the domain filter dropped a required
    # cookie, or the capture wrote a degenerate state the landing classifier
    # didn't catch — treat it as a NON-success: do NOT claim a heal we can't
    # back up. The caller's dead-cookie ``ValueError`` then stands honestly
    # rather than being replaced by a lower-level cookie-load error.
    try:
        fresh_jar = await asyncio.to_thread(build_httpx_cookies_from_storage, storage_path)
    except (ValueError, OSError) as exc:
        logger.warning(
            "Headless re-auth wrote storage but the re-minted cookies failed to "
            "load/validate (%s); treating as a non-success.",
            type(exc).__name__,
        )
        return False
    _replace_cookie_jar(kernel.get_http_client().cookies, fresh_jar)
    logger.info("Headless re-auth succeeded; reloaded re-minted cookies for retry.")
    return True
