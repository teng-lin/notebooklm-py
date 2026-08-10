"""Auth session refresh implementation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx

from .._env import get_base_url
from .._url_utils import is_google_auth_redirect
from ..exceptions import AuthExtractionError
from ..paths import profile_from_storage_path
from .account import authuser_query
from .cookie_types import CookieJar
from .extraction import extract_wiz_field
from .recovery import try_headless_reauth, try_master_token_reauth, try_storage_cookie_reload
from .refresh import try_refresh_cmd_reauth
from .tokens import AuthTokens

if TYPE_CHECKING:
    from .._cookie_persistence import CookiePersistence
    from .._kernel import Kernel
    from .._runtime.auth import AuthRefreshCoordinator
    from .._runtime.lifecycle import ClientLifecycle


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
    rejected_cookie_jar: CookieJar | None = None

    async def _get_and_extract() -> tuple[str, str] | None:
        """GET the homepage + extract tokens; ``None`` signals a dead-cookie 302."""
        nonlocal rejected_cookie_jar
        url = f"{get_base_url()}/"
        if auth.account_email or auth.authuser:
            url = f"{url}?{authuser_query(auth.authuser, auth.account_email)}"
        request_cookie_jar = CookieJar.from_httpx(http_client.cookies)
        response = await http_client.get(url)
        response.raise_for_status()
        if is_google_auth_redirect(str(response.url)):
            rejected_cookie_jar = request_cookie_jar
            return None
        rejected_cookie_jar = None
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
        # Dead first-party cookies. Mid-session recovery ladder, in order:
        # persisted-profile reload (default) → L2.5 refresh-cmd (opt-in) →
        # L3 headless re-mint → L4 master-token.
        # Each rung, on success, reloads cookies and retries the homepage GET.
        #
        # A sibling CLI/server process may already have refreshed the same
        # storage_state.json. Re-read it before invoking any credential-bearing
        # or operator-configured recovery mechanism.
        # File-backed recovery has three bounded attempts: retry one live-jar
        # change; force a disk sample while preserving a new auth-bearing live
        # candidate; if that candidate is also rejected, use the final sample.
        # Inline auth has no disk candidate and retains two live-jar retries.
        attempt_count = 3 if auth.storage_path is not None else 2
        for _attempt in range(attempt_count):
            if not await _try_storage_cookie_reload(
                auth=auth,
                kernel=kernel,
                auth_coord=auth_coord,
                cookie_persistence=cookie_persistence,
                # Preserve a post-request jar mutation for the first retry. If
                # that jar is also rejected, force the bounded second attempt
                # to sample an available disk profile even when the response
                # mutated another cookie. With no profile, retain the second
                # response mutation as the only local recovery evidence.
                rejected_cookie_jar=rejected_cookie_jar,
                force_disk_read=_attempt > 0 and auth.storage_path is not None,
                preserve_auth_material_change=_attempt < 2,
            ):
                break
            extracted = await _get_and_extract()
            if extracted is not None:
                break
        # Layer-2.5: NOTEBOOKLM_REFRESH_CMD, promoted from cold-start-only into
        # the mid-session ladder (audit refresh-4). Gated OPT-IN for one release
        # by NOTEBOOKLM_REFRESH_CMD_MIDSESSION=1 (default OFF); it reuses the
        # SAME single-flight-coalesced cold-start machinery + per-path flock.
        if extracted is None and await _try_refresh_cmd_reauth(auth=auth, kernel=kernel):
            extracted = await _get_and_extract()
        # Layer-3 headless re-auth (opt-in / env-gated); on a successful re-mint,
        # reload cookies and retry once.
        if extracted is None and await _try_headless_reauth(
            auth=auth, kernel=kernel, allow_headless=allow_headless
        ):
            extracted = await _get_and_extract()
        # Layer-4: master-token re-mint. When a master_token.json sits beside
        # this profile's storage, re-mint a fresh session from the durable token
        # — the headless-auth recovery that replaces "run 'notebooklm login'"
        # (directive A). Fires only after L1/L2/L3 are exhausted.
        if extracted is None and await _try_master_token_reauth(auth=auth, kernel=kernel):
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


async def _try_storage_cookie_reload(
    *,
    auth: AuthTokens,
    kernel: Kernel,
    auth_coord: AuthRefreshCoordinator,
    cookie_persistence: CookiePersistence,
    rejected_cookie_jar: CookieJar | None,
    force_disk_read: bool = False,
    preserve_auth_material_change: bool = True,
) -> bool:
    """Reload newer/different file-backed cookies without external recovery."""
    cookie_jar = kernel.get_http_client().cookies
    expected_authuser = auth.authuser
    expected_account_email = auth.account_email
    expected_generation = auth._profile_session_generation

    async def install_profile(
        target: httpx.Cookies,
        source: httpx.Cookies,
        expected: CookieJar,
        authuser: int,
        account_email: str | None,
    ) -> bool | None:
        return await auth_coord.install_profile_session(
            auth=auth,
            target_cookie_jar=target,
            source_cookie_jar=source,
            expected_cookie_jar=expected,
            expected_authuser=expected_authuser,
            expected_account_email=expected_account_email,
            expected_generation=expected_generation,
            authuser=authuser,
            account_email=account_email,
        )

    try:
        return await try_storage_cookie_reload(
            storage_path=auth.storage_path,
            cookie_jar=cookie_jar,
            rejected_cookie_jar=rejected_cookie_jar,
            force_disk_read=force_disk_read,
            preserve_auth_material_change=preserve_auth_material_change,
            install_profile=install_profile,
            adopt_baseline=lambda path, baseline: cookie_persistence._adopt_reloaded_baseline(
                path,
                baseline,
                to_thread=asyncio.to_thread,
            ),
        )
    finally:
        # The reload mutates the authoritative HTTP jar before its optional
        # adoption await. Keep public compatibility views synchronized even if
        # cancellation lands while adoption is waiting on disk or save_lock.
        auth.replace_cookie_jar(cookie_jar)


async def _try_refresh_cmd_reauth(*, auth: AuthTokens, kernel: Kernel) -> bool:
    """Compatibility wrapper over the client-neutral L2.5 refresh-cmd adapter.

    The rung is opt-in mid-session (``NOTEBOOKLM_REFRESH_CMD_MIDSESSION=1``) and
    reuses the cold-start refresh-cmd machinery; see
    :func:`notebooklm._auth.refresh.try_refresh_cmd_reauth`.

    The profile is derived from ``auth.storage_path`` (not left as ``None``) so a
    client built via ``from_storage(profile="work")`` — without the process-wide
    default set — refreshes the WORK profile, not "default"
    (``NOTEBOOKLM_REFRESH_PROFILE``). A non-profile storage path (explicit
    ``--storage``/legacy) yields ``None`` and keeps the path-based routing.
    """
    return await try_refresh_cmd_reauth(
        storage_path=auth.storage_path,
        cookie_jar=kernel.get_http_client().cookies,
        profile=profile_from_storage_path(auth.storage_path),
    )


async def _try_headless_reauth(
    *,
    auth: AuthTokens,
    kernel: Kernel,
    allow_headless: bool,
) -> bool:
    """Compatibility wrapper over the client-neutral L3 adapter."""
    return await try_headless_reauth(
        storage_path=auth.storage_path,
        cookie_jar=kernel.get_http_client().cookies,
        allow_headless=allow_headless,
    )


async def _try_master_token_reauth(*, auth: AuthTokens, kernel: Kernel) -> bool:
    """Compatibility wrapper over the client-neutral L4 adapter."""
    return await try_master_token_reauth(
        storage_path=auth.storage_path,
        cookie_jar=kernel.get_http_client().cookies,
    )
