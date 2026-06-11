"""Authentication token container and storage loader."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeAlias

import httpx

from ..paths import get_storage_path
from . import account as _auth_account
from . import cookies as _auth_cookies
from . import psidts_recovery as _auth_psidts_recovery
from . import refresh as _auth_refresh
from . import storage as _auth_storage

DomainCookieMap: TypeAlias = _auth_cookies.DomainCookieMap
FlatCookieMap: TypeAlias = _auth_cookies.FlatCookieMap
CookieSnapshot: TypeAlias = _auth_storage.CookieSnapshot


@dataclass
class AuthTokens:
    """Authentication tokens for NotebookLM API.

    Attributes:
        cookies: Required Google auth cookies keyed by ``(name, domain, path)``
            per RFC 6265 §5.3 (issue #369). Legacy 2-tuple ``(name, domain)``
            and flat ``name -> value`` shapes are still accepted on
            construction and widened to the path-aware shape by
            :func:`normalize_cookie_map` during ``__post_init__``.
        csrf_token: CSRF token (SNlM0e) extracted from page
        session_id: Session ID (FdrFJe) extracted from page
        storage_path: Path to the storage_state.json file, if file-based auth was used
        cookie_jar: Domain-preserving httpx.Cookies jar. Preferred over flat cookies dict
            for HTTP operations as it retains original cookie domains (e.g.,
            .googleusercontent.com vs .google.com).
        authuser: Google ``authuser`` index this profile authenticates as.
            ``0`` (the default account) is used when no account metadata is
            present in ``storage_state.json`` (or legacy sibling
            ``context.json``), matching pre-multi-account behavior.
        account_email: Stable Google account identity for routing. When set,
            NotebookLM requests use it as the ``authuser`` value instead of the
            integer index, because Google account indices can change when other
            accounts sign out.
        cookie_snapshot: Internal save baseline used when a pre-client token
            fetch mutates cookies but persistence fails or CAS-rejects. This
            lets the eventual client retry the unpersisted delta instead
            of snapshotting the already-mutated jar as clean state.
    """

    # Secret fields are excluded from the dataclass-generated ``__repr__`` via
    # ``field(repr=False)`` and re-surfaced as redacted placeholders by the
    # custom ``__repr__`` below. This prevents accidental secret
    # leakage through ``logger.debug("%r", auth)``, ``pytest -vv`` failure
    # diffs, and any third-party tooling that calls ``repr()`` on the dataclass.
    cookies: DomainCookieMap = field(repr=False)
    csrf_token: str = field(repr=False)
    session_id: str = field(repr=False)
    storage_path: Path | None = None
    cookie_jar: httpx.Cookies | None = field(default=None, repr=False)
    authuser: int = 0
    cookie_snapshot: CookieSnapshot | None = field(default=None, repr=False)
    account_email: str | None = None

    def __post_init__(self) -> None:
        """Normalize legacy flat cookie mappings into domain-keyed mappings."""
        self.cookies = _auth_cookies.normalize_cookie_map(self.cookies)
        if self.cookie_jar is None:
            self.cookie_jar = _auth_cookies.build_cookie_jar(
                cookies=self.cookies,
                storage_path=self.storage_path,
            )

    def __repr__(self) -> str:
        """Return a redacted representation safe for logs and pytest diffs.

        Cookie values, CSRF + session tokens, the live ``cookie_jar``, and the
        ``cookie_snapshot`` are all credential-equivalent and never appear
        verbatim. The cookie count is preserved so reprs remain useful for
        debugging (e.g. "expected 4 cookies, got 2"). Non-secret identity
        fields (``authuser``, ``account_email``, ``storage_path``) are kept
        for the same reason — they help identify *which* profile is involved
        without leaking *how to impersonate it*.
        """
        jar_summary = "<redacted>" if self.cookie_jar is not None else "None"
        snapshot_summary = "<redacted>" if self.cookie_snapshot is not None else "None"
        return (
            "AuthTokens("
            f"cookies=<{len(self.cookies)} redacted>, "
            "csrf_token=<redacted>, "
            "session_id=<redacted>, "
            f"storage_path={self.storage_path!r}, "
            f"cookie_jar={jar_summary}, "
            f"authuser={self.authuser!r}, "
            f"cookie_snapshot={snapshot_summary}, "
            f"account_email={self.account_email!r}"
            ")"
        )

    @property
    def cookie_header(self) -> str:
        """Generate Cookie header value for HTTP requests.

        Returns:
            Semicolon-separated cookie string (e.g., "SID=abc; HSID=def")
        """
        return "; ".join(f"{k}={v}" for k, v in self.flat_cookies.items())

    @property
    def account_route(self) -> str:
        """Return the value to send in NotebookLM ``authuser`` routing fields."""
        return _auth_account.format_authuser_value(self.authuser, self.account_email)

    @property
    def flat_cookies(self) -> FlatCookieMap:
        """Return a legacy name→value cookie mapping.

        Duplicate-name resolution follows :func:`_auth_domain_priority` so the
        result matches what :func:`load_auth_from_storage` produces for the same
        storage state (see issue #375). Domain-aware HTTP operations should use
        ``cookie_jar`` or ``cookies`` directly instead.
        """
        return _auth_cookies.flatten_cookie_map(self.cookies)

    @classmethod
    async def from_storage(cls, path: Path | None = None, profile: str | None = None) -> AuthTokens:
        """Create AuthTokens from Playwright storage state file.

        This is the recommended way to create AuthTokens for programmatic use.
        It loads cookies from storage and fetches CSRF/session tokens automatically.

        Args:
            path: Path to storage_state.json. If provided, takes precedence over profile.
            profile: Profile name to load auth from (e.g., "work", "personal").
                If None, uses the active profile (from CLI flag, env var, or config).

        Returns:
            Fully initialized AuthTokens ready for API calls.

        Raises:
            FileNotFoundError: If storage file doesn't exist
            ValueError: If required cookies are missing or tokens can't be extracted
            httpx.HTTPError: If token fetch request fails

        Example:
            auth = await AuthTokens.from_storage()
            async with NotebookLMClient(auth) as client:
                notebooks = await client.list_notebooks()

            # Load from a specific profile
            auth = await AuthTokens.from_storage(profile="work")
        """
        if path is None and (profile is not None or not os.environ.get("NOTEBOOKLM_AUTH_JSON")):
            path = get_storage_path(profile=profile)

        if path is None:
            authuser = 0
            account_email = None
            account_metadata = _auth_account.read_account_metadata_from_storage_state(
                _auth_cookies._load_storage_state(path)
            )
            raw_authuser = account_metadata.get("authuser")
            raw_email = account_metadata.get("email")
            if isinstance(raw_authuser, int) and raw_authuser >= 0:
                authuser = raw_authuser
            if isinstance(raw_email, str) and raw_email.strip():
                account_email = raw_email.strip()
        else:
            authuser = _auth_account.get_authuser_for_storage(path)
            account_email = _auth_account.get_account_email_for_storage(path)
        # Build the cookie jar via the lossless loader so path/secure/httpOnly
        # survive into the live jar. The earlier
        # extract_cookies_with_domains -> build_cookie_jar pipeline only carried
        # (name, domain) -> value and dropped the same attributes the load
        # paths in #365 fixed.
        jar = _auth_cookies.build_httpx_cookies_from_storage(path)
        # Snapshot before token fetch can rotate cookies; the snapshot/delta
        # merge in save_cookies_to_storage will then write only what this
        # process actually rotated, preserving sibling-process state.
        snapshot = _auth_storage.snapshot_cookie_jar(jar)
        route_kwargs: dict[str, Any] = {"authuser": authuser}
        if account_email is not None:
            route_kwargs["account_email"] = account_email
        (
            csrf_token,
            session_id,
            refreshed,
            post_refresh_snapshot,
        ) = await _auth_refresh._fetch_tokens_with_refresh(jar, path, profile, **route_kwargs)

        # If NOTEBOOKLM_REFRESH_CMD ran, ``_fetch_tokens_with_refresh`` captured
        # a snapshot immediately after the jar was wholesale-replaced from
        # disk — before the retry fetch could mutate it with redirect
        # Set-Cookies. Use that snapshot so the retry's rotations land on
        # disk as deltas instead of being silently absorbed into the baseline.
        if refreshed and post_refresh_snapshot is not None:
            snapshot = post_refresh_snapshot

        # Persist any refreshed cookies from the token fetch. If the save
        # fails, carry the old baseline into the returned AuthTokens so a
        # later client can retry the delta instead of treating the mutated
        # jar as clean state.
        # ``save_cookies_to_storage`` performs atomic-replace + fsync + flock
        # under a synchronous file lock; offload to a worker thread so a
        # slow filesystem (network FS, encrypted home, fcntl contention)
        # can't freeze the event loop.
        post_save_snapshot = _auth_storage.snapshot_cookie_jar(jar)
        save_result = await asyncio.to_thread(
            _auth_storage.save_cookies_to_storage,
            jar,
            path,
            original_snapshot=snapshot,
            return_result=True,
        )
        if isinstance(save_result, _auth_storage.CookieSaveResult):
            if save_result.ok:
                cookie_snapshot = None
            elif save_result.cas_rejected_keys:
                cookie_snapshot = _auth_storage.advance_cookie_snapshot_after_save(
                    snapshot, post_save_snapshot, save_result.cas_rejected_keys
                )
            else:
                cookie_snapshot = snapshot
        else:
            cookie_snapshot = None if save_result else snapshot
        cookies = _auth_cookies._cookie_map_from_jar(jar)

        return cls(
            cookies=cookies,
            csrf_token=csrf_token,
            session_id=session_id,
            storage_path=path,
            cookie_jar=jar,
            authuser=authuser,
            cookie_snapshot=cookie_snapshot,
            account_email=account_email,
        )


AuthTokens.__module__ = "notebooklm.auth"


def load_auth_from_storage(path: Path | None = None) -> dict[str, str]:
    """Load Google cookies from storage as a flat name→value dict.

    Loads authentication cookies with the following precedence:
    1. Explicit path argument (from --storage CLI flag)
    2. NOTEBOOKLM_AUTH_JSON environment variable (inline JSON, no file needed)
    3. Profile storage path from :func:`notebooklm.paths.get_storage_path`
       (``$NOTEBOOKLM_HOME/profiles/<profile>/storage_state.json`` with legacy
       home-root fallback for the default profile)

    Duplicate-name resolution follows
    :func:`notebooklm._auth.cookie_policy._auth_domain_priority`, matching
    :attr:`AuthTokens.flat_cookies` for the same storage state — previously the
    two paths disagreed on names that live only on non-base hosts (e.g.
    ``OSID`` on ``myaccount.google.com`` vs ``notebooklm.google.com``). See
    issue #375.

    Args:
        path: Path to storage_state.json. If provided, takes precedence over env vars.

    Returns:
        Dict mapping cookie names to values (e.g., {"SID": "...", "HSID": "..."}).

    Raises:
        FileNotFoundError: If storage file doesn't exist (when using file-based auth).
        ValueError: If required cookies (``SID`` + ``__Secure-1PSIDTS``) are
            missing, or if storage JSON is malformed.

    Example::

        # CLI flag takes precedence
        cookies = load_auth_from_storage(Path("/custom/path.json"))

        # Or use NOTEBOOKLM_AUTH_JSON for CI/CD (no file writes needed)
        # export NOTEBOOKLM_AUTH_JSON='{"cookies":[...]}'
        cookies = load_auth_from_storage()
    """
    storage_state = _auth_cookies._load_storage_state(path)
    try:
        return _auth_cookies.extract_cookies_from_storage(storage_state)
    except ValueError:
        # Inline ``__Secure-1PSIDTS`` recovery (issue #865). Playwright login
        # can land a ``storage_state.json`` that carries SID + secondary
        # binding but lacks PSIDTS, because Google only mints PSIDTS
        # deterministically in response to the dedicated ``RotateCookies``
        # POST — not on the passive ``goto()`` navigations the login flow
        # uses. The preflight then rejects before the keepalive's RotateCookies
        # path can heal the state. When the recovery preconditions hold, fire
        # one POST + persist before re-raising — see
        # :mod:`notebooklm._auth.psidts_recovery` for the precondition list.
        # ``_recover_psidts_inline`` resolves the effective storage path
        # itself (default file when ``path is None`` and env-var unset), so
        # we pass ``path`` through verbatim — including ``None`` for the
        # default-profile case.
        if not _auth_psidts_recovery._recover_psidts_inline(path):
            raise
        storage_state = _auth_cookies._load_storage_state(path)
        return _auth_cookies.extract_cookies_from_storage(storage_state)


__all__ = ["AuthTokens", "load_auth_from_storage"]
