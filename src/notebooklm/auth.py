"""Authentication handling for NotebookLM API.

This module provides authentication utilities for the NotebookLM client:

1. **Cookie-based Authentication**: Loads Google cookies from Playwright storage
   state files created by `notebooklm login`.

2. **Token Extraction**: Fetches CSRF (SNlM0e) and session (FdrFJe) tokens from
   the NotebookLM homepage, required for all RPC calls.

3. **Download Cookies**: Provides httpx-compatible cookies with domain info for
   authenticated downloads from Google content servers.

Usage:
    # Recommended: Use AuthTokens.from_storage() for full initialization
    auth = await AuthTokens.from_storage()
    async with NotebookLMClient(auth) as client:
        ...

    # For authenticated downloads
    cookies = load_httpx_cookies()
    async with httpx.AsyncClient(cookies=cookies) as client:
        response = await client.get(url)

Security Notes:
    - Storage state files contain sensitive session cookies
    - Path traversal protection is enforced on all file operations
"""

import asyncio
import logging
import os
import subprocess  # noqa: F401  # re-exported for tests that patch ``auth.subprocess.run``
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeAlias

import httpx

from ._auth import account as _auth_account
from ._auth import cookie_policy as _cookie_policy
from ._auth import cookies as _auth_cookies
from ._auth import extraction as _auth_extraction
from ._auth import headers as _auth_headers
from ._auth import keepalive as _auth_keepalive
from ._auth import paths as _auth_paths
from ._auth import refresh as _auth_refresh
from ._auth import storage as _auth_storage
from .paths import get_storage_path

logger = logging.getLogger(__name__)

CookieKey: TypeAlias = _auth_cookies.CookieKey
DomainCookieMap: TypeAlias = _auth_cookies.DomainCookieMap
FlatCookieMap: TypeAlias = _auth_cookies.FlatCookieMap
LegacyDomainCookieMap: TypeAlias = _auth_cookies.LegacyDomainCookieMap
CookieInput: TypeAlias = _auth_cookies.CookieInput

_cookie_is_http_only = _auth_cookies._cookie_is_http_only
_cookie_key_variants = _auth_cookies._cookie_key_variants
_cookie_map_from_jar = _auth_cookies._cookie_map_from_jar
_cookie_to_storage_state = _auth_cookies._cookie_to_storage_state
_find_cookie_for_storage = _auth_cookies._find_cookie_for_storage
_load_storage_state = _auth_cookies._load_storage_state
_replace_cookie_jar = _auth_cookies._replace_cookie_jar
_storage_entry_to_cookie = _auth_cookies._storage_entry_to_cookie
_update_cookie_input = _auth_cookies._update_cookie_input
build_cookie_jar = _auth_cookies.build_cookie_jar
build_httpx_cookies_from_storage = _auth_cookies.build_httpx_cookies_from_storage
convert_rookiepy_cookies_to_storage_state = _auth_cookies.convert_rookiepy_cookies_to_storage_state
extract_cookies_from_storage = _auth_cookies.extract_cookies_from_storage
extract_cookies_with_domains = _auth_cookies.extract_cookies_with_domains
flatten_cookie_map = _auth_cookies.flatten_cookie_map
load_httpx_cookies = _auth_cookies.load_httpx_cookies
normalize_cookie_map = _auth_cookies.normalize_cookie_map


CookieSnapshotKey = _auth_storage.CookieSnapshotKey
CookieSnapshotValue = _auth_storage.CookieSnapshotValue
CookieSnapshot = _auth_storage.CookieSnapshot
CookieSaveResult = _auth_storage.CookieSaveResult
snapshot_cookie_jar = _auth_storage.snapshot_cookie_jar
advance_cookie_snapshot_after_save = _auth_storage.advance_cookie_snapshot_after_save
_cookie_save_return = _auth_storage._cookie_save_return
save_cookies_to_storage = _auth_storage.save_cookies_to_storage
_merge_cookies_legacy = _auth_storage._merge_cookies_legacy
_merge_cookies_with_snapshot = _auth_storage._merge_cookies_with_snapshot
_cookie_snapshot_key_variants = _auth_storage._cookie_snapshot_key_variants
_stored_cookie_snapshot_key = _auth_storage._stored_cookie_snapshot_key
_file_lock = _auth_storage._file_lock
_file_lock_exclusive = _auth_storage._file_lock_exclusive
_FLOCK_UNAVAILABLE_WARNED = _auth_storage._FLOCK_UNAVAILABLE_WARNED

REQUIRED_COOKIE_DOMAINS = _cookie_policy.REQUIRED_COOKIE_DOMAINS
OPTIONAL_COOKIE_DOMAINS_BY_LABEL = _cookie_policy.OPTIONAL_COOKIE_DOMAINS_BY_LABEL
OPTIONAL_COOKIE_DOMAINS = _cookie_policy.OPTIONAL_COOKIE_DOMAINS
ALLOWED_COOKIE_DOMAINS = _cookie_policy.ALLOWED_COOKIE_DOMAINS
GOOGLE_REGIONAL_CCTLDS = _cookie_policy.GOOGLE_REGIONAL_CCTLDS
MINIMUM_REQUIRED_COOKIES = _cookie_policy.MINIMUM_REQUIRED_COOKIES
_EXTRACTION_HINT = _cookie_policy._EXTRACTION_HINT
_SECONDARY_BINDING_WARNED = _cookie_policy._SECONDARY_BINDING_WARNED
_has_valid_secondary_binding = _cookie_policy._has_valid_secondary_binding
_auth_domain_priority = _cookie_policy._auth_domain_priority
_is_google_domain = _cookie_policy._is_google_domain
_is_allowed_auth_domain = _cookie_policy._is_allowed_auth_domain
_is_allowed_cookie_domain = _cookie_policy._is_allowed_cookie_domain


# Public surface for ``from notebooklm.auth import *`` and for downstream
# static-analysis tools (mypy, ruff F401 checks). This is the audited set of
# names externally imported by the package, tests, docs, and the CLI as of
# 2026-05-17. Underscore-prefixed names remain accessible on the module — some
# tests reach for them as whitebox affordances — but are intentionally NOT
# blessed here. See ``tests/unit/test_public_surface.py``: two complementary
# tests pin this list — ``test_auth_module_has_expected_all`` snapshot-checks
# the exact ordering, and ``test_auth_all_matches_external_imports_audit``
# AST-scans ``src/``, ``tests/``, ``docs/`` to fail if a new public name is
# imported externally without being added here.
__all__ = [
    "Account",
    "advance_cookie_snapshot_after_save",
    "ALLOWED_COOKIE_DOMAINS",
    "AuthTokens",
    "authuser_query",
    "build_cookie_jar",
    "build_httpx_cookies_from_storage",
    "clear_account_metadata",
    "convert_rookiepy_cookies_to_storage_state",
    "CookieSaveResult",
    "CookieSnapshot",
    "CookieSnapshotKey",
    "CookieSnapshotValue",
    "enumerate_accounts",
    "extract_cookies_from_storage",
    "extract_cookies_with_domains",
    "extract_csrf_from_html",
    "extract_email_from_html",
    "extract_session_id_from_html",
    "extract_wiz_field",
    "fetch_tokens",
    "fetch_tokens_with_domains",
    "format_authuser_value",
    "get_account_email_for_storage",
    "get_authuser_for_storage",
    "GOOGLE_REGIONAL_CCTLDS",
    "KEEPALIVE_ROTATE_URL",
    "load_auth_from_storage",
    "load_httpx_cookies",
    "MINIMUM_REQUIRED_COOKIES",
    "normalize_cookie_map",
    "NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV",
    "NOTEBOOKLM_REFRESH_CMD_ENV",
    "NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV",
    "OPTIONAL_COOKIE_DOMAINS",
    "OPTIONAL_COOKIE_DOMAINS_BY_LABEL",
    "read_account_metadata",
    "REQUIRED_COOKIE_DOMAINS",
    "save_cookies_to_storage",
    "snapshot_cookie_jar",
    "write_account_metadata",
]


def _validate_required_cookies(
    cookie_names: set[str],
    *,
    context: str = "",
    extra_diagnostics: list[str] | None = None,
) -> None:
    """Copy-forward shim into ``_cookie_policy`` for legacy patch sites.

    Propagates test-patched policy names (``MINIMUM_REQUIRED_COOKIES``,
    ``_EXTRACTION_HINT``, ``_has_valid_secondary_binding``) bound on
    ``auth.py`` into ``_cookie_policy`` before delegating, then mirrors
    ``_SECONDARY_BINDING_WARNED`` back in the ``finally`` block. Tests that
    patch ``auth.MINIMUM_REQUIRED_COOKIES`` (or sibling names) continue to
    work without going through ``patch_auth_seam``; modern tests should
    prefer the seam helper. The ``_AuthFacadeModule`` write-through was
    retired in D1 PR-2 (ADR-003).
    """
    global _SECONDARY_BINDING_WARNED
    _cookie_policy.MINIMUM_REQUIRED_COOKIES = MINIMUM_REQUIRED_COOKIES
    _cookie_policy._EXTRACTION_HINT = _EXTRACTION_HINT
    _cookie_policy._has_valid_secondary_binding = _has_valid_secondary_binding
    try:
        _cookie_policy._validate_required_cookies(
            cookie_names,
            context=context,
            extra_diagnostics=extra_diagnostics,
        )
    finally:
        _SECONDARY_BINDING_WARNED = _cookie_policy._SECONDARY_BINDING_WARNED


_auth_cookies._validate_required_cookies = _validate_required_cookies


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
            present in ``context.json`` — matching pre-multi-account behavior.
        account_email: Stable Google account identity for routing. When set,
            NotebookLM requests use it as the ``authuser`` value instead of the
            integer index, because Google account indices can change when other
            accounts sign out.
        cookie_snapshot: Internal save baseline used when a pre-client token
            fetch mutates cookies but persistence fails or CAS-rejects. This
            lets the eventual Session retry the unpersisted delta instead
            of snapshotting the already-mutated jar as clean state.
    """

    # Secret fields are excluded from the dataclass-generated ``__repr__`` via
    # ``field(repr=False)`` and re-surfaced as redacted placeholders by the
    # custom ``__repr__`` below (P1-1). This prevents accidental secret
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
        self.cookies = normalize_cookie_map(self.cookies)
        if self.cookie_jar is None:
            self.cookie_jar = build_cookie_jar(cookies=self.cookies, storage_path=self.storage_path)

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
        return format_authuser_value(self.authuser, self.account_email)

    @property
    def flat_cookies(self) -> FlatCookieMap:
        """Return a legacy name→value cookie mapping.

        Duplicate-name resolution follows :func:`_auth_domain_priority` so the
        result matches what :func:`load_auth_from_storage` produces for the same
        storage state (see issue #375). Domain-aware HTTP operations should use
        ``cookie_jar`` or ``cookies`` directly instead.
        """
        return flatten_cookie_map(self.cookies)

    @classmethod
    async def from_storage(
        cls, path: Path | None = None, profile: str | None = None
    ) -> "AuthTokens":
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
        if path is None and (profile is not None or "NOTEBOOKLM_AUTH_JSON" not in os.environ):
            path = get_storage_path(profile=profile)

        authuser = get_authuser_for_storage(path)
        account_email = get_account_email_for_storage(path)
        # Build the cookie jar via the lossless loader so path/secure/httpOnly
        # survive into the live jar. The earlier
        # extract_cookies_with_domains -> build_cookie_jar pipeline only carried
        # (name, domain) -> value and dropped the same attributes the load
        # paths in #365 fixed.
        jar = build_httpx_cookies_from_storage(path)
        # Snapshot before token fetch can rotate cookies; the snapshot/delta
        # merge in save_cookies_to_storage will then write only what this
        # process actually rotated, preserving sibling-process state.
        snapshot = snapshot_cookie_jar(jar)
        route_kwargs: dict[str, Any] = {"authuser": authuser}
        if account_email is not None:
            route_kwargs["account_email"] = account_email
        csrf_token, session_id, refreshed, post_refresh_snapshot = await _fetch_tokens_with_refresh(
            jar, path, profile, **route_kwargs
        )

        # If NOTEBOOKLM_REFRESH_CMD ran, ``_fetch_tokens_with_refresh`` captured
        # a snapshot immediately after the jar was wholesale-replaced from
        # disk — before the retry fetch could mutate it with redirect
        # Set-Cookies. Use that snapshot so the retry's rotations land on
        # disk as deltas instead of being silently absorbed into the baseline.
        if refreshed and post_refresh_snapshot is not None:
            snapshot = post_refresh_snapshot

        # Persist any refreshed cookies from the token fetch. If the save
        # fails, carry the old baseline into the returned AuthTokens so a
        # later Session can retry the delta instead of treating the mutated
        # jar as clean state.
        # ``save_cookies_to_storage`` performs atomic-replace + fsync + flock
        # under a synchronous file lock; offload to a worker thread so a
        # slow filesystem (network FS, encrypted home, fcntl contention)
        # can't freeze the event loop.
        post_save_snapshot = snapshot_cookie_jar(jar)
        save_result = await asyncio.to_thread(
            save_cookies_to_storage,
            jar,
            path,
            original_snapshot=snapshot,
            return_result=True,
        )
        if isinstance(save_result, CookieSaveResult):
            if save_result.ok:
                cookie_snapshot = None
            elif save_result.cas_rejected_keys:
                cookie_snapshot = advance_cookie_snapshot_after_save(
                    snapshot, post_save_snapshot, save_result.cas_rejected_keys
                )
            else:
                cookie_snapshot = snapshot
        else:
            cookie_snapshot = None if save_result else snapshot
        cookies = _cookie_map_from_jar(jar)

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


# WIZ field token extraction (CSRF, session ID, generic WIZ data) lives in
# ``notebooklm._auth.extraction``. Re-exported here so the public surface
# (``notebooklm.auth.extract_csrf_from_html`` etc., listed in ``__all__``) and
# white-box test affordances (``_safe_url``, ``_build_wiz_field_patterns``)
# keep resolving against ``notebooklm.auth``.
_build_wiz_field_patterns = _auth_extraction._build_wiz_field_patterns
_safe_url = _auth_extraction._safe_url
extract_csrf_from_html = _auth_extraction.extract_csrf_from_html
extract_session_id_from_html = _auth_extraction.extract_session_id_from_html
extract_wiz_field = _auth_extraction.extract_wiz_field

# Token-route resolver lives in ``notebooklm._auth.headers``; re-exported so
# internal callers (``fetch_tokens``, ``fetch_tokens_with_domains`` — now in
# ``_auth.refresh``) and white-box tests keep resolving the helper against
# ``notebooklm.auth``.
_resolve_token_route_kwargs = _auth_headers._resolve_token_route_kwargs


Account = _auth_account.Account
MAX_AUTHUSER_PROBE = _auth_account.MAX_AUTHUSER_PROBE
_ACCOUNT_CONTEXT_KEY = _auth_account._ACCOUNT_CONTEXT_KEY
_account_context_path = _auth_account._account_context_path
extract_email_from_html = _auth_account.extract_email_from_html
_probe_authuser = _auth_account._probe_authuser
read_account_metadata = _auth_account.read_account_metadata
get_authuser_for_storage = _auth_account.get_authuser_for_storage
get_account_email_for_storage = _auth_account.get_account_email_for_storage
format_authuser_value = _auth_account.format_authuser_value
authuser_query = _auth_account.authuser_query
write_account_metadata = _auth_account.write_account_metadata
clear_account_metadata = _auth_account.clear_account_metadata


async def enumerate_accounts(
    cookie_jar: httpx.Cookies, *, max_authuser: int = MAX_AUTHUSER_PROBE
) -> list[Account]:
    """Enumerate Google accounts visible to the given cookie jar."""
    return await _auth_account.enumerate_accounts(
        cookie_jar,
        max_authuser=max_authuser,
        poke_session=_poke_session,
    )


def load_auth_from_storage(path: Path | None = None) -> dict[str, str]:
    """Load Google cookies from storage as a flat name→value dict.

    Loads authentication cookies with the following precedence:
    1. Explicit path argument (from --storage CLI flag)
    2. NOTEBOOKLM_AUTH_JSON environment variable (inline JSON, no file needed)
    3. File at $NOTEBOOKLM_HOME/storage_state.json (or ~/.notebooklm/storage_state.json)

    Duplicate-name resolution follows :func:`_auth_domain_priority`, matching
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
        ValueError: If required cookies (SID) are missing or JSON is malformed.

    Example:
        # CLI flag takes precedence
        cookies = load_auth_from_storage(Path("/custom/path.json"))

        # Or use NOTEBOOKLM_AUTH_JSON for CI/CD (no file writes needed)
        # export NOTEBOOKLM_AUTH_JSON='{"cookies":[...]}'
        cookies = load_auth_from_storage()
    """
    storage_state = _load_storage_state(path)
    return extract_cookies_from_storage(storage_state)


# Env-var name constants live in ``notebooklm._auth.paths``. Re-exported so
# both the public surface (``NOTEBOOKLM_REFRESH_CMD_ENV``,
# ``NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV`` — listed in ``__all__``) and the
# white-box surface (``_REFRESH_ATTEMPTED_ENV``, used by tests) keep resolving
# against ``notebooklm.auth``.
NOTEBOOKLM_REFRESH_CMD_ENV = _auth_paths.NOTEBOOKLM_REFRESH_CMD_ENV
NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV = _auth_paths.NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV
_REFRESH_ATTEMPTED_ENV = _auth_paths._REFRESH_ATTEMPTED_ENV


# --- Keepalive poke ----------------------------------------------------------
# Rotation throttle + ``RotateCookies`` POST bodies live in
# ``notebooklm._auth.keepalive``. Re-exported here so every name that was
# previously module-level on ``notebooklm.auth`` (constants, the per-loop /
# per-profile lock registry, the public ``KEEPALIVE_ROTATE_URL`` listed in
# ``__all__``, and white-box helpers like ``_poke_session`` /
# ``_rotate_cookies``) keeps resolving against this module. Tests that
# need to substitute a moved body should patch the seam directly via
# ``tests/_fixtures/auth_seam.py::patch_auth_seam`` — production code no
# longer mirrors writes (``_AuthFacadeModule`` retired in D1 PR-2,
# ADR-003).
KEEPALIVE_ROTATE_URL = _auth_keepalive.KEEPALIVE_ROTATE_URL
_KEEPALIVE_ROTATE_HEADERS = _auth_keepalive._KEEPALIVE_ROTATE_HEADERS
_KEEPALIVE_ROTATE_BODY = _auth_keepalive._KEEPALIVE_ROTATE_BODY
_KEEPALIVE_POKE_TIMEOUT = _auth_keepalive._KEEPALIVE_POKE_TIMEOUT
_KEEPALIVE_RATE_LIMIT_SECONDS = _auth_keepalive._KEEPALIVE_RATE_LIMIT_SECONDS
_KEEPALIVE_PRECISION_TOLERANCE = _auth_keepalive._KEEPALIVE_PRECISION_TOLERANCE
NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV = _auth_paths.NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV
# The state dicts and locks are SHARED by identity with the moved module so
# ``tests/conftest.py`` invariants — which clear these dicts on the
# ``notebooklm.auth`` attribute — propagate into the keepalive module's own
# bodies. (Direct assignment from the same object preserves identity.)
_POKE_STATE_LOCK = _auth_keepalive._POKE_STATE_LOCK
_POKE_LOCKS_BY_LOOP = _auth_keepalive._POKE_LOCKS_BY_LOOP
_LAST_POKE_ATTEMPT_MONOTONIC = _auth_keepalive._LAST_POKE_ATTEMPT_MONOTONIC
_get_poke_lock = _auth_keepalive._get_poke_lock
_try_claim_rotation = _auth_keepalive._try_claim_rotation
_file_lock_try_exclusive = _auth_keepalive._file_lock_try_exclusive
_is_recently_rotated = _auth_keepalive._is_recently_rotated
_poke_session = _auth_keepalive._poke_session
_rotate_cookies = _auth_keepalive._rotate_cookies
# Rotation sentinel path lives in ``_auth.paths``; the keepalive module also
# aliases it locally. Re-exported here for white-box callers that resolve it
# against ``notebooklm.auth``.
_rotation_lock_path = _auth_paths._rotation_lock_path


# --- Refresh-cmd + token-fetch entry points ---------------------------------
# All refresh coordination and the public ``fetch_tokens`` /
# ``fetch_tokens_with_domains`` entry points live in
# ``notebooklm._auth.refresh``. Re-exported so the public surface
# (``fetch_tokens`` + ``fetch_tokens_with_domains`` listed in ``__all__``) and
# the white-box surface (lock registries, ContextVar, ``_run_refresh_cmd``
# carrying the tier-9 E (P1-18) redaction logic, etc.) keep resolving against
# ``notebooklm.auth``. Tests that need to substitute a moved body should
# patch the seam directly via
# ``tests/_fixtures/auth_seam.py::patch_auth_seam`` — production code no
# longer mirrors writes (``_AuthFacadeModule`` retired in D1 PR-2,
# ADR-003).
_REFRESH_ATTEMPTED_CONTEXT = _auth_refresh._REFRESH_ATTEMPTED_CONTEXT
_REFRESH_STATE_LOCK = _auth_refresh._REFRESH_STATE_LOCK
_REFRESH_LOCKS_BY_LOOP = _auth_refresh._REFRESH_LOCKS_BY_LOOP
_REFRESH_GENERATIONS = _auth_refresh._REFRESH_GENERATIONS
_REFRESH_INFLIGHT_BY_LOOP = _auth_refresh._REFRESH_INFLIGHT_BY_LOOP
_REFRESH_INFLIGHT_TASKS = _auth_refresh._REFRESH_INFLIGHT_TASKS
_AUTH_ERROR_SIGNALS = _auth_refresh._AUTH_ERROR_SIGNALS
_get_inflight_registry = _auth_refresh._get_inflight_registry
_coalesced_run_refresh_cmd = _auth_refresh._coalesced_run_refresh_cmd
_get_refresh_lock = _auth_refresh._get_refresh_lock
_should_try_refresh = _auth_refresh._should_try_refresh
_split_refresh_cmd = _auth_refresh._split_refresh_cmd
_run_refresh_cmd = _auth_refresh._run_refresh_cmd
_fetch_tokens_with_refresh = _auth_refresh._fetch_tokens_with_refresh
_fetch_tokens_with_jar = _auth_refresh._fetch_tokens_with_jar
fetch_tokens = _auth_refresh.fetch_tokens
fetch_tokens_with_domains = _auth_refresh.fetch_tokens_with_domains
