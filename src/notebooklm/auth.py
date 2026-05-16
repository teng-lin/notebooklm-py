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
import contextlib
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import weakref
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, TypeAlias

import httpx

from ._auth import account as _auth_account
from ._auth import cookie_policy as _cookie_policy
from ._auth import cookies as _auth_cookies
from ._auth import storage as _auth_storage
from ._env import get_base_url
from ._url_utils import contains_google_auth_redirect, is_google_auth_redirect
from .exceptions import AuthExtractionError
from .paths import get_storage_path, resolve_profile

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


_AUTH_STORAGE_FACADE_NAMES = {
    "CookieSnapshotKey",
    "CookieSnapshotValue",
    "CookieSnapshot",
    "CookieSaveResult",
    "snapshot_cookie_jar",
    "advance_cookie_snapshot_after_save",
    "_cookie_save_return",
    "save_cookies_to_storage",
    "_merge_cookies_legacy",
    "_merge_cookies_with_snapshot",
    "_cookie_snapshot_key_variants",
    "_stored_cookie_snapshot_key",
    "_cookie_is_http_only",
    "_cookie_key_variants",
    "_cookie_to_storage_state",
    "_find_cookie_for_storage",
    "_is_allowed_cookie_domain",
    "_file_lock",
    "_file_lock_exclusive",
    "_FLOCK_UNAVAILABLE_WARNED",
}

_AUTH_ACCOUNT_FACADE_NAMES = {
    "Account",
    "MAX_AUTHUSER_PROBE",
    "_ACCOUNT_CONTEXT_KEY",
    "_account_context_path",
    "extract_email_from_html",
    "_probe_authuser",
    "read_account_metadata",
    "get_authuser_for_storage",
    "get_account_email_for_storage",
    "format_authuser_value",
    "authuser_query",
    "write_account_metadata",
    "clear_account_metadata",
}


class _AuthFacadeModule(ModuleType):
    """Keep compatibility assignments to auth globals meaningful after moves."""

    def __getattribute__(self, name: str) -> Any:
        if name in _AUTH_STORAGE_FACADE_NAMES:
            return getattr(_auth_storage, name)
        if name in _AUTH_ACCOUNT_FACADE_NAMES:
            return getattr(_auth_account, name)
        return super().__getattribute__(name)

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        if name in _AUTH_STORAGE_FACADE_NAMES:
            setattr(_auth_storage, name, value)
        if name in _AUTH_ACCOUNT_FACADE_NAMES:
            setattr(_auth_account, name, value)
        if name == "_SECONDARY_BINDING_WARNED":
            _cookie_policy._SECONDARY_BINDING_WARNED = bool(value)
        elif name in {
            "MINIMUM_REQUIRED_COOKIES",
            "_EXTRACTION_HINT",
            "_has_valid_secondary_binding",
        }:
            setattr(_cookie_policy, name, value)
        if name in {"MINIMUM_REQUIRED_COOKIES", "_EXTRACTION_HINT"}:
            # Cookie loaders read these for diagnostics before delegating to policy validation.
            setattr(_auth_cookies, name, value)


sys.modules[__name__].__class__ = _AuthFacadeModule


def _validate_required_cookies(
    cookie_names: set[str],
    *,
    context: str = "",
    extra_diagnostics: list[str] | None = None,
) -> None:
    """Delegate required-cookie validation through the compatibility facade."""
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
            lets the eventual ClientCore retry the unpersisted delta instead
            of snapshotting the already-mutated jar as clean state.
    """

    cookies: DomainCookieMap
    csrf_token: str
    session_id: str
    storage_path: Path | None = None
    cookie_jar: httpx.Cookies | None = None
    authuser: int = 0
    cookie_snapshot: CookieSnapshot | None = None
    account_email: str | None = None

    def __post_init__(self) -> None:
        """Normalize legacy flat cookie mappings into domain-keyed mappings."""
        self.cookies = normalize_cookie_map(self.cookies)
        if self.cookie_jar is None:
            self.cookie_jar = build_cookie_jar(cookies=self.cookies, storage_path=self.storage_path)

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
        # later ClientCore can retry the delta instead of treating the mutated
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


def _build_wiz_field_patterns(key: str) -> list[re.Pattern[str]]:
    """Build the ordered list of regex patterns used to locate a Wiz field.

    Patterns are tried in priority order: canonical double-quoted form first,
    then single-quoted, then HTML-escaped. Each pattern captures the value
    (which may be empty — empty tokens are legitimate, not a drift signal).

    All three variants tolerate backslash-escaped delimiters inside the value
    so JSON-style escapes like ``"key":"a\\"b"`` parse correctly. The inner
    character class ``[^"\\\\]*(?:\\\\.[^"\\\\]*)*`` is the standard
    "string with escapes" idiom: consume runs of non-quote/non-backslash
    chars, optionally followed by an escape pair (``\\.``) and another run.

    The HTML-escaped variant uses a tempered-dot lookahead so the capture
    stops only at a literal ``&quot;`` terminator (not at any ``&`` — values
    legitimately contain ``&amp;`` and similar entities).

    Whitespace tolerance (``\\s*``) around the colon mirrors the original
    ``extract_csrf_from_html`` regex so we don't regress.
    """
    escaped = re.escape(key)
    return [
        # 1. Canonical double-quoted: "key":"value"  (or  "key" : "value")
        #    Captures escaped quotes: "key":"a\"b" -> a\"b
        re.compile(rf'"{escaped}"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"'),
        # 2. Single-quoted variant: 'key':'value' with escaped-quote support.
        re.compile(rf"'{escaped}'\s*:\s*'([^'\\]*(?:\\.[^'\\]*)*)'"),
        # 3. HTML-escaped: &quot;key&quot;:&quot;value&quot;
        #    Tempered dot so the value can contain other entities like &amp;.
        re.compile(rf"&quot;{escaped}&quot;\s*:\s*&quot;((?:(?!&quot;).)*)&quot;"),
    ]


def extract_wiz_field(html: str, key: str, *, strict: bool = True) -> str | None:
    """Extract a ``WIZ_global_data[key]`` value from a NotebookLM HTML response.

    NotebookLM (and other Google products) embed a JavaScript object literal
    named ``WIZ_global_data`` in the page chrome. Tokens like ``SNlM0e``
    (CSRF) and ``FdrFJe`` (session ID) live inside that object. This helper
    is the single place that knows how to parse the embedding so all callers
    benefit from the same drift tolerance and diagnostics.

    Tolerated input variants, tried in priority order:

    1. Canonical double-quoted ``"key":"value"`` (typical raw HTML).
    2. Single-quoted ``'key':'value'`` (rare, observed in some debug renders).
    3. HTML-escaped ``&quot;key&quot;:&quot;value&quot;`` (when the script
       block is rendered inside an attribute or escaped fragment).

    Empty values are passed through verbatim: ``"SNlM0e":""`` returns the
    empty string. Some Google endpoints legitimately emit empty tokens (e.g.
    for unauthenticated probes) and the caller — not this helper — should
    decide whether an empty value is acceptable.

    Args:
        html: The page HTML to search.
        key: Field name to extract from ``WIZ_global_data``.
        strict: When True (default) and no pattern matches, raise
            :class:`AuthExtractionError` with a sanitized preview. When False,
            return ``None`` on drift so callers can fall back gracefully.

    Returns:
        The extracted value (possibly empty), or ``None`` when ``strict=False``
        and no pattern matched.

    Raises:
        AuthExtractionError: ``strict=True`` and the key was not found.
    """
    for pattern in _build_wiz_field_patterns(key):
        match = pattern.search(html)
        if match is not None:
            return match.group(1)
    if strict:
        raise AuthExtractionError(key, html)
    return None


def extract_csrf_from_html(html: str, final_url: str = "") -> str:
    """
    Extract CSRF token (SNlM0e) from NotebookLM page HTML.

    The CSRF token is embedded in the page's WIZ_global_data JavaScript object.
    It's required for all RPC calls to prevent cross-site request forgery.

    Args:
        html: Page HTML content from notebooklm.google.com
        final_url: The final URL after redirects (for error messages)

    Returns:
        CSRF token value (typically starts with "AF1_QpN-")

    Raises:
        ValueError: Preserved for backward compatibility — raised both when
            redirected to a Google login page and when the token is missing
            from a non-redirect response. Existing callers and tests rely on
            the ``"CSRF token not found"`` / ``"Authentication expired"``
            message substrings, so we intentionally keep the legacy type.
            Internally we delegate to :func:`extract_wiz_field` so the regex
            matrix (double-quoted / single-quoted / HTML-escaped) is shared.
    """
    # Tolerant extraction via the unified helper — accepts canonical,
    # single-quoted, and HTML-escaped variants of the WIZ_global_data field.
    token = extract_wiz_field(html, "SNlM0e", strict=False)
    if token is not None:
        return token
    # Drift path: differentiate "auth expired" from "shape changed" because
    # the remediation differs (re-login vs file a bug).
    if is_google_auth_redirect(final_url) or contains_google_auth_redirect(html):
        raise ValueError(
            "Authentication expired or invalid. Run 'notebooklm login' to re-authenticate."
        )
    raise ValueError(
        f"CSRF token not found in HTML. Final URL: {final_url}\n"
        "This may indicate the page structure has changed."
    )


def extract_session_id_from_html(html: str, final_url: str = "") -> str:
    """
    Extract session ID (FdrFJe) from NotebookLM page HTML.

    The session ID is embedded in the page's WIZ_global_data JavaScript object.
    It's passed in URL query parameters for RPC calls.

    Args:
        html: Page HTML content from notebooklm.google.com
        final_url: The final URL after redirects (for error messages)

    Returns:
        Session ID value

    Raises:
        ValueError: Preserved for backward compatibility — raised both when
            redirected to a Google login page and when the session ID is
            missing from a non-redirect response. See
            :func:`extract_csrf_from_html` for the rationale.
    """
    sid = extract_wiz_field(html, "FdrFJe", strict=False)
    if sid is not None:
        return sid
    if is_google_auth_redirect(final_url) or contains_google_auth_redirect(html):
        raise ValueError(
            "Authentication expired or invalid. Run 'notebooklm login' to re-authenticate."
        )
    raise ValueError(
        f"Session ID not found in HTML. Final URL: {final_url}\n"
        "This may indicate the page structure has changed."
    )


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


NOTEBOOKLM_REFRESH_CMD_ENV = "NOTEBOOKLM_REFRESH_CMD"
NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV = "NOTEBOOKLM_REFRESH_CMD_USE_SHELL"
_REFRESH_ATTEMPTED_ENV = "_NOTEBOOKLM_REFRESH_ATTEMPTED"
# The ContextVar prevents same-task retry loops in the parent process. The env
# flag is passed only to child refresh commands so recursive CLI calls skip refresh.
_REFRESH_ATTEMPTED_CONTEXT: ContextVar[bool] = ContextVar(
    "_REFRESH_ATTEMPTED_CONTEXT", default=False
)
# In-process state for refresh coordination, keyed per resolved storage path.
#
# Two layers of protection are required:
#
# - ``_REFRESH_STATE_LOCK`` (sync ``threading.Lock``) makes the
#   ``_REFRESH_GENERATIONS`` check-and-update atomic ACROSS event loops.
#   Two loops sharing a storage path each hold their own ``asyncio.Lock``
#   (see below), so the asyncio lock alone cannot serialize the generation
#   bump.
#
# - ``_REFRESH_LOCKS_BY_LOOP`` mirrors the keepalive ``_POKE_LOCKS_BY_LOOP``
#   pattern: ``asyncio.Lock`` is loop-bound, so a per-loop / per-resolved-
#   storage-path registry avoids the cross-loop / cross-thread hazard of a
#   module-global ``asyncio.Lock`` that binds to the first event loop that
#   uses it. The outer ``WeakKeyDictionary`` is keyed on the loop object so
#   the inner dict is reclaimed when the loop is garbage-collected.
_REFRESH_STATE_LOCK = threading.Lock()
_REFRESH_LOCKS_BY_LOOP: "weakref.WeakKeyDictionary[Any, dict[Path | None, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)
_REFRESH_GENERATIONS: dict[str, int] = {}

# In-flight ``asyncio.Future`` registry for refresh-cmd coalescing.
#
# Same-loop concurrent callers that both encounter auth-expiry coalesce on a
# single in-flight subprocess by sharing a per-resolved-storage-path
# ``asyncio.Future``. The future is keyed per-loop because ``asyncio.Future``
# is loop-bound; cross-loop coordination falls back to the
# ``_REFRESH_GENERATIONS`` counter guarded by ``_REFRESH_STATE_LOCK``.
#
# The strong-ref ``_REFRESH_INFLIGHT_TASKS`` set keeps the shielded subprocess
# Tasks alive so the asyncio GC does not collect them. The task self-removes
# via ``add_done_callback(set.discard)`` once settled.
_REFRESH_INFLIGHT_BY_LOOP: "weakref.WeakKeyDictionary[Any, dict[str, asyncio.Future[None]]]" = (
    weakref.WeakKeyDictionary()
)
_REFRESH_INFLIGHT_TASKS: set[asyncio.Task[None]] = set()


def _get_inflight_registry() -> dict[str, asyncio.Future[None]]:
    """Return the per-loop in-flight refresh-cmd future registry.

    Mirrors ``_get_refresh_lock``: ``asyncio.Future`` is loop-bound, so we
    need a per-loop registry. ``_REFRESH_STATE_LOCK`` makes the lookup /
    insert atomic across threads (different loops on different threads can
    each populate their own per-loop dict concurrently).
    """
    loop = asyncio.get_running_loop()
    with _REFRESH_STATE_LOCK:
        per_loop = _REFRESH_INFLIGHT_BY_LOOP.get(loop)
        if per_loop is None:
            per_loop = {}
            _REFRESH_INFLIGHT_BY_LOOP[loop] = per_loop
        return per_loop


async def _coalesced_run_refresh_cmd(
    refresh_key: str,
    resolved_storage_path: Path,
    profile: str | None,
) -> None:
    """Run ``_run_refresh_cmd`` once per ``refresh_key`` on this event loop.

    Same-loop concurrent callers that hit this function while a refresh is
    in flight will await the same underlying ``asyncio.Future`` rather than
    spawning their own subprocess.

    Cancel-safety design:

    - The subprocess is driven by a strongly-referenced background
      ``asyncio.Task`` (registered in ``_REFRESH_INFLIGHT_TASKS``) so it
      survives cancellation of any individual awaiter.
    - Each awaiter wraps the future in ``asyncio.shield`` so local
      cancellation of the awaiter does NOT cancel the shared subprocess —
      mirrors the ``ClientCore._await_refresh`` pattern used for the RPC
      refresh path.
    - The caller in ``_fetch_tokens_with_refresh`` keeps re-awaiting the
      shielded future under the per-loop asyncio lock so the lock is not
      released until the subprocess settles. This prevents a duplicate
      subprocess from being spawned if the lock is released mid-refresh
      and a second caller observes a partially-completed state.
    """
    loop = asyncio.get_running_loop()
    registry = _get_inflight_registry()
    with _REFRESH_STATE_LOCK:
        existing = registry.get(refresh_key)
        leader = existing is None or existing.done()
        if leader:
            future: asyncio.Future[None] = loop.create_future()
            registry[refresh_key] = future
        else:
            future = existing  # type: ignore[assignment]

    if leader:
        task = asyncio.create_task(_run_refresh_cmd(resolved_storage_path, profile))
        # Strong-ref pattern: without ``add_done_callback`` the task can be
        # collected by the asyncio GC before completion if no awaiter is
        # holding a reference.
        _REFRESH_INFLIGHT_TASKS.add(task)
        task.add_done_callback(_REFRESH_INFLIGHT_TASKS.discard)

        def _settle(t: asyncio.Task[None]) -> None:
            # ``Future.set_*`` is loop-affine; the callback runs on the owning
            # loop (same loop that created the future and the task), so direct
            # ``set_result`` / ``set_exception`` is safe.
            if not future.done():
                if t.cancelled():
                    future.cancel()
                else:
                    exc = t.exception()
                    if exc is not None:
                        future.set_exception(exc)
                    else:
                        future.set_result(None)
            # Intentionally LEAVE the (now-done) future in the registry so the
            # caller's CancelledError handler in ``_fetch_tokens_with_refresh``
            # can still inspect ``inflight.exception()`` after a cancel/settle
            # race (CodeRabbit PR #621 finding). The leader-check at the
            # get-or-create site (``existing is None or existing.done()``)
            # treats a done future as overwritable, so the next refresh
            # cycle's leader replaces this slot — no accumulation.

        task.add_done_callback(_settle)

    # All callers (leader + followers) await the shared future under shield.
    # Re-raises subprocess exception to every awaiter.
    await asyncio.shield(future)


def _get_refresh_lock(resolved_storage_path: Path | None) -> asyncio.Lock:
    """Return the ``asyncio.Lock`` for ``(running event loop, resolved storage path)``.

    Mirrors ``_get_poke_lock``. Keyed on the RESOLVED storage path so callers
    passing ``(None, profile="foo")`` share the lock with callers passing the
    explicit profile-resolved path.
    """
    loop = asyncio.get_running_loop()
    with _REFRESH_STATE_LOCK:
        per_loop = _REFRESH_LOCKS_BY_LOOP.get(loop)
        if per_loop is None:
            per_loop = {}
            _REFRESH_LOCKS_BY_LOOP[loop] = per_loop
        lock = per_loop.get(resolved_storage_path)
        if lock is None:
            lock = asyncio.Lock()
            per_loop[resolved_storage_path] = lock
        return lock


_AUTH_ERROR_SIGNALS = (
    "authentication expired",
    "redirected to",
    "run 'notebooklm login'",
)


def _should_try_refresh(err: Exception) -> bool:
    """True when an auth failure should trigger NOTEBOOKLM_REFRESH_CMD."""
    if _REFRESH_ATTEMPTED_CONTEXT.get() or os.environ.get(_REFRESH_ATTEMPTED_ENV) == "1":
        return False
    if not os.environ.get(NOTEBOOKLM_REFRESH_CMD_ENV):
        return False
    msg = str(err).lower()
    return any(sig in msg for sig in _AUTH_ERROR_SIGNALS)


def _split_refresh_cmd(cmd: str) -> list[str]:
    """Parse ``NOTEBOOKLM_REFRESH_CMD`` into an argv for ``shell=False`` exec.

    On POSIX systems, defers to :func:`shlex.split`. On Windows, uses
    ``CommandLineToArgvW`` so quoted paths like
    ``"C:\\Program Files\\Python\\python.exe"`` produce a properly unquoted
    argv that ``subprocess.run(argv, shell=False)`` can locate. ``shlex``
    in non-POSIX mode preserves the literal quote characters and would
    leave the OS unable to find the executable.

    Raises:
        ValueError: If the command is malformed (e.g., unterminated quote).
    """
    if os.name != "nt":
        return shlex.split(cmd)

    import ctypes
    from ctypes import wintypes

    CommandLineToArgvW = ctypes.windll.shell32.CommandLineToArgvW  # type: ignore[attr-defined]
    CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    LocalFree = ctypes.windll.kernel32.LocalFree  # type: ignore[attr-defined]
    LocalFree.argtypes = [wintypes.HLOCAL]
    LocalFree.restype = wintypes.HLOCAL

    argc = ctypes.c_int(0)
    argv_ptr = CommandLineToArgvW(cmd, ctypes.byref(argc))
    if not argv_ptr:
        # CommandLineToArgvW returns NULL for some empty-input edge cases.
        # Mirror shlex.split's behavior and return an empty list; the caller
        # surfaces this as ``RuntimeError("...parsed to empty argv")``.
        return []
    try:
        # On Windows, ``CommandLineToArgvW`` is documented to return a single
        # empty-string entry (argc=1, argv[0]="") for whitespace-only input,
        # rather than NULL. Filter out empty entries so the caller's
        # ``if not argv`` empty-argv guard catches this case the same way
        # ``shlex.split("   ") == []`` does on POSIX.
        return [argv_ptr[i] for i in range(argc.value) if argv_ptr[i]]
    finally:
        LocalFree(ctypes.cast(argv_ptr, wintypes.HLOCAL))


async def _run_refresh_cmd(storage_path: Path | None = None, profile: str | None = None) -> None:
    """Run ``NOTEBOOKLM_REFRESH_CMD`` to refresh stored cookies.

    By default, the command string is parsed with :func:`shlex.split` and
    executed with ``shell=False`` to avoid shell-injection footguns when the
    env var is sourced from CI configs or container env files. Set
    ``NOTEBOOKLM_REFRESH_CMD_USE_SHELL=1`` to opt back into the legacy
    ``shell=True`` behavior (e.g., when the command relies on shell features
    like pipes, redirection, or env-var expansion).

    Raises:
        RuntimeError: If the refresh command is missing, parses to an empty
            argv, is malformed (unterminated quote), times out, or exits
            non-zero.
    """
    cmd = os.environ.get(NOTEBOOKLM_REFRESH_CMD_ENV)
    if not cmd:
        raise RuntimeError(f"{NOTEBOOKLM_REFRESH_CMD_ENV} is not set; cannot refresh cookies.")
    refresh_env = os.environ.copy()
    refresh_env[_REFRESH_ATTEMPTED_ENV] = "1"
    refresh_env["NOTEBOOKLM_REFRESH_PROFILE"] = resolve_profile(profile)
    refresh_env["NOTEBOOKLM_REFRESH_STORAGE_PATH"] = str(
        storage_path or get_storage_path(profile=profile)
    )

    use_shell = os.environ.get(NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV) == "1"
    run_target: str | list[str]
    run_shell: bool
    if use_shell:
        logger.warning("Using shell-mode for %s (opt-in)", NOTEBOOKLM_REFRESH_CMD_ENV)
        # Deliberately do NOT log a basename/preview of ``cmd`` here: in
        # shell-mode the entire string is forwarded to ``/bin/sh -c`` and
        # may contain pipes, redirection, ``$VAR`` expansion, or inline
        # tokens. We can't extract a single "first token" without risking
        # leaking the rest, so we stay silent past the opt-in warning.
        run_target = cmd
        run_shell = True
    else:
        try:
            # POSIX → shlex.split. Windows → CommandLineToArgvW so quoted
            # paths like ``"C:\\Program Files\\..."`` arrive unquoted.
            argv = _split_refresh_cmd(cmd)
        except ValueError as split_err:
            raise RuntimeError(
                f"{NOTEBOOKLM_REFRESH_CMD_ENV} could not be parsed: {split_err}"
            ) from split_err
        if not argv:
            raise RuntimeError(f"{NOTEBOOKLM_REFRESH_CMD_ENV} parsed to empty argv")
        # Log basename only — full argv may carry tokens and absolute paths
        # can leak secrets-directory layouts.
        logger.info("Running refresh command: %s ...", os.path.basename(argv[0]))
        run_target = argv
        run_shell = False

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            run_target,
            shell=run_shell,
            capture_output=True,
            text=True,
            timeout=60,
            env=refresh_env,
        )
    except (subprocess.TimeoutExpired, OSError) as refresh_err:
        raise RuntimeError(
            f"{NOTEBOOKLM_REFRESH_CMD_ENV} failed to execute: {refresh_err}"
        ) from refresh_err
    if result.returncode != 0:
        output = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"{NOTEBOOKLM_REFRESH_CMD_ENV} exited {result.returncode}: {output}")
    logger.info("NotebookLM cookies refreshed via %s", NOTEBOOKLM_REFRESH_CMD_ENV)


async def _fetch_tokens_with_refresh(
    cookie_jar: httpx.Cookies,
    storage_path: Path | None = None,
    profile: str | None = None,
    *,
    authuser: int = 0,
    account_email: str | None = None,
    force_authuser_query: bool = False,
) -> tuple[str, str, bool, CookieSnapshot | None]:
    """Fetch tokens, optionally running NOTEBOOKLM_REFRESH_CMD on auth expiry.

    Returns ``(csrf, session_id, refreshed, post_refresh_snapshot)``.

    When ``refreshed`` is ``True``, ``post_refresh_snapshot`` is a snapshot
    captured **immediately after** ``_replace_cookie_jar`` swaps in the
    refresh-cmd output and **before** the retry token fetch can mutate the
    jar with redirect Set-Cookies. Callers must use that snapshot as the
    save baseline; re-snapshotting the jar after this function returns
    would include the retry's rotations in the baseline (so they would
    never reach disk on the subsequent save).

    When ``refreshed`` is ``False`` the snapshot is ``None`` (no refresh
    happened; caller's pre-fetch snapshot is still the right baseline).
    """
    try:
        route_kwargs: dict[str, Any] = {"authuser": authuser}
        if account_email is not None:
            route_kwargs["account_email"] = account_email
        if force_authuser_query:
            route_kwargs["force_authuser_query"] = True
        csrf, session_id = await _fetch_tokens_with_jar(cookie_jar, storage_path, **route_kwargs)
        return csrf, session_id, False, None
    except ValueError as err:
        if not _should_try_refresh(err):
            raise
        logger.warning(
            "NotebookLM auth failed (%s). Running %s to refresh cookies.",
            err,
            NOTEBOOKLM_REFRESH_CMD_ENV,
        )
        # Canonicalize the storage path so different representations of the
        # same physical file (relative vs absolute, with or without symlinks,
        # ``~`` shorthand) hash to the same lock-registry / generation key.
        # ``get_storage_path`` already returns a resolved path, but a
        # caller-supplied ``storage_path`` may be relative or a symlink.
        refresh_storage_path = (
            (storage_path or get_storage_path(profile=profile)).expanduser().resolve()
        )
        refresh_key = str(refresh_storage_path)
        # Snapshot the generation BEFORE acquiring the async lock so we can
        # detect whether a concurrent refresh (potentially on a different
        # event loop) bumped it while we were waiting. ``_REFRESH_STATE_LOCK``
        # makes this read atomic with the later check-and-update below.
        with _REFRESH_STATE_LOCK:
            refresh_generation = _REFRESH_GENERATIONS.get(refresh_key, 0)
        refresh_token = _REFRESH_ATTEMPTED_CONTEXT.set(True)
        try:
            async with _get_refresh_lock(refresh_storage_path):
                # Bump generation ONLY after the subprocess succeeds AND
                # storage is reloaded. An earlier implementation bumped the
                # generation eagerly BEFORE ``_run_refresh_cmd`` — when the
                # subprocess failed, the phantom bump made concurrent waiters
                # short-circuit and proceed with stale storage.
                #
                # Re-check under the sync state lock so the read is atomic
                # ACROSS event loops. The per-loop asyncio lock only
                # serializes within a single loop; a second loop sharing this
                # storage path holds its own asyncio.Lock.
                with _REFRESH_STATE_LOCK:
                    current_generation = _REFRESH_GENERATIONS.get(refresh_key, 0)
                    # ``current > refresh_generation`` means another caller
                    # (any loop) has SUCCESSFULLY refreshed since we observed
                    # auth-expiry — we can skip ``_run_refresh_cmd`` and just
                    # reload the freshly-written storage.
                    should_run_refresh = current_generation <= refresh_generation
                if should_run_refresh:
                    # Cancel-safety: drive the subprocess through the shared
                    # in-flight future. Same-loop concurrent callers coalesce
                    # on the same subprocess. If THIS caller is cancelled
                    # while the subprocess is in flight, we keep awaiting the
                    # shielded future so the asyncio lock is NOT released
                    # until the subprocess settles — otherwise a second
                    # caller could spawn a duplicate concurrent refresh by
                    # observing the mid-flight lock release.
                    caller_cancelled = False
                    subprocess_exc: BaseException | None = None
                    while True:
                        try:
                            await _coalesced_run_refresh_cmd(
                                refresh_key, refresh_storage_path, profile
                            )
                            break
                        except asyncio.CancelledError:
                            # Caller-side cancellation. Re-enter the await
                            # so the shielded subprocess can settle while we
                            # still hold the asyncio lock.
                            caller_cancelled = True
                            registry = _get_inflight_registry()
                            with _REFRESH_STATE_LOCK:
                                inflight = registry.get(refresh_key)
                            if inflight is None or inflight.done():
                                # Subprocess already settled; we absorbed the
                                # cancellation. Inspect its terminal state.
                                # Per the CodeRabbit finding on PR #621, the
                                # ``_settle`` callback intentionally leaves
                                # the done future in the registry so this
                                # branch can still observe ``inflight.
                                # exception()`` after a cancel/settle race.
                                if inflight is not None:
                                    if inflight.cancelled():
                                        # Subprocess itself was cancelled —
                                        # treat as failure (do not bump gen).
                                        subprocess_exc = asyncio.CancelledError()
                                    else:
                                        subprocess_exc = inflight.exception()
                                break
                            # Otherwise loop — re-await the shielded future.
                        except BaseException as exc:  # noqa: BLE001
                            subprocess_exc = exc
                            break

                    if subprocess_exc is not None:
                        # Subprocess failed — DO NOT bump generation.
                        # Concurrent / subsequent waiters re-attempt the
                        # refresh instead of short-circuiting on a phantom
                        # bump.
                        if caller_cancelled:
                            # Caller cancellation takes priority for THIS
                            # caller.
                            raise asyncio.CancelledError() from subprocess_exc
                        raise subprocess_exc

                    # Subprocess succeeded AND we're about to reload
                    # storage. Bump the generation now so other callers
                    # (any loop) see the success and skip their own
                    # subprocess. The bump is atomic across loops via
                    # ``_REFRESH_STATE_LOCK``.
                    with _REFRESH_STATE_LOCK:
                        # ``max(...)`` defends against the rare interleaving
                        # where another loop's pre-lock capture was AFTER
                        # ours and bumped past us.
                        existing = _REFRESH_GENERATIONS.get(refresh_key, 0)
                        _REFRESH_GENERATIONS[refresh_key] = max(existing, refresh_generation + 1)

                    if caller_cancelled:
                        # Subprocess succeeded; generation bump persists for
                        # other callers' benefit. THIS caller still
                        # propagates cancellation rather than completing the
                        # retry.
                        raise asyncio.CancelledError()
                fresh_jar = build_httpx_cookies_from_storage(refresh_storage_path)
                _replace_cookie_jar(cookie_jar, fresh_jar)
                # Capture the baseline NOW — after the wholesale replacement
                # but before the retry fetch can mutate the jar.
                post_refresh_snapshot = snapshot_cookie_jar(cookie_jar)
            route_kwargs = {"authuser": authuser}
            if account_email is not None:
                route_kwargs["account_email"] = account_email
            if force_authuser_query:
                route_kwargs["force_authuser_query"] = True
            csrf, session_id = await _fetch_tokens_with_jar(
                cookie_jar, refresh_storage_path, **route_kwargs
            )
            return csrf, session_id, True, post_refresh_snapshot
        finally:
            _REFRESH_ATTEMPTED_CONTEXT.reset(refresh_token)


def _update_cookie_input(target: CookieInput, fresh: DomainCookieMap) -> None:
    """Update caller-provided cookies in place while preserving key style.

    The caller's ``target`` may use any of the three accepted shapes (flat
    ``name -> value``, legacy ``(name, domain) -> value``, or path-aware
    ``(name, domain, path) -> value``). The freshly-fetched delta is always the
    path-aware shape; we collapse it back to the caller's original shape so
    they don't observe an in-place type change.
    """
    if any(isinstance(key, tuple) and len(key) == 2 for key in target):
        # Legacy 2-tuple caller. Collapse the path dimension by keeping the
        # first occurrence per (name, domain); for cookies that share name and
        # domain at distinct paths this is lossy, but legacy callers had no
        # way to express path either, so this matches their original contract.
        legacy: dict[tuple[str, str], str] = {}
        for (name, domain, _path), value in fresh.items():
            legacy.setdefault((name, domain), value)
        target.clear()
        target.update(legacy)  # type: ignore[arg-type]
        return

    use_domain_keys = any(isinstance(key, tuple) for key in target)
    target.clear()
    if use_domain_keys:
        target.update(fresh)  # type: ignore[arg-type]
    else:
        target.update(flatten_cookie_map(fresh))  # type: ignore[arg-type]


# --- Keepalive poke ----------------------------------------------------------
# Google's __Secure-1PSIDTS / __Secure-3PSIDTS cookies are the rotating freshness
# partners of __Secure-1PSID / __Secure-3PSID. Their server-side validity window
# is short (minutes-to-hours scale) and Google only emits a rotated value when
# the client asks the identity surface to rotate. Pure RPC traffic against
# notebooklm.google.com never triggers rotation, so a long-lived storage_state
# silently stales out and every subsequent call fails with the
# "Authentication expired or invalid" redirect (see issue #312).
#
# We POST to ``accounts.google.com/RotateCookies`` — the dedicated rotation
# endpoint Chrome itself calls for legacy cookie rotation. Empirically validated
# against both DBSC-bound (Playwright-minted) and unbound (Firefox-imported)
# profiles in #345: a single POST returns 200 and sets fresh
# ``__Secure-1PSIDTS`` / ``__Secure-3PSIDTS`` for either session type. The
# response body declares the next-rotation interval (`["identity.hfcr",600]` —
# 10 minutes), which sets the floor for how often this is worth firing.
KEEPALIVE_ROTATE_URL = "https://accounts.google.com/RotateCookies"
_KEEPALIVE_ROTATE_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://accounts.google.com",
}
# Observed unbound RotateCookies request body — a placeholder pair Chrome sends
# when there is no DBSC binding token to attest. Validated across Gemini-API and
# the in-house experiments referenced in #345; kept in one place so it can be
# changed if Google ever changes the contract.
_KEEPALIVE_ROTATE_BODY = '[000,"-0000000000000000000"]'
NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV = "NOTEBOOKLM_DISABLE_KEEPALIVE_POKE"
_KEEPALIVE_POKE_TIMEOUT = 15.0
# Skip the poke if storage_state.json was rewritten within this window — protects
# accounts.google.com from rapid CLI loops (e.g. 10 sequential `notebooklm`
# invocations) that would each fire their own rotation. Google's own declared
# rotation cadence is 600 s, so 60 s is well under the useful interval.
_KEEPALIVE_RATE_LIMIT_SECONDS = 60.0
# Sub-second drift between ``time.time()`` and filesystem mtime can land a
# freshly-written file fractionally in the future on some platforms (notably
# Windows + older Python where the clock is coarser than NTFS mtime). Tolerate
# that without re-opening the "future mtime wedges the guard" bug.
_KEEPALIVE_PRECISION_TOLERANCE = 2.0
# In-process state for rotation throttling, keyed per-profile and per-loop.
#
# - Per-profile (``storage_path``) so a rotation against profile A doesn't
#   suppress profile B for the rate-limit window. A ``None`` key represents
#   env-var auth.
# - Per event loop because ``asyncio.Lock`` is loop-bound: a lock created in
#   loop X cannot be safely awaited from loop Y. Multiple ``asyncio.run()``
#   invocations in the same process, or worker threads each running their
#   own loop, would otherwise trip ``RuntimeError`` or leave waiters in
#   inconsistent state.
#
# The outer registry is a ``WeakKeyDictionary`` keyed on the loop *object* (not
# its ``id()``): when a loop is garbage-collected, its inner dict is reclaimed
# automatically. This bounds the lock cache for hosts that repeatedly create
# short-lived loops, and avoids the ``id()``-reuse hazard where a closed loop's
# stale lock could be returned to a new loop that happens to allocate at the
# same address.
#
# ``_POKE_STATE_LOCK`` (sync ``threading.Lock``) protects two module-level
# operations that must be atomic across threads:
#   1. ``_get_poke_lock``: get-or-create the per-(loop, profile) async lock
#      so two threads with their own loops don't race on dict insertion.
#   2. ``_try_claim_rotation``: atomic check-and-stamp of the per-profile
#      timestamp. Without this, two direct ``_rotate_cookies`` callers (e.g.
#      two layer-2 keepalive loops on the same profile, or a layer-1 +
#      layer-2 pair on different event loops) can each read a stale 0.0
#      and both fire the POST.
# It is held briefly, never across an ``await``, so it cannot deadlock against
# any asyncio primitive.
_POKE_STATE_LOCK = threading.Lock()
_POKE_LOCKS_BY_LOOP: "weakref.WeakKeyDictionary[Any, dict[Path | None, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)
# Monotonic timestamp of the last in-process poke *attempt* (success or
# failure), keyed by storage_path. Stamped under ``_POKE_STATE_LOCK`` inside
# ``_try_claim_rotation`` so the check-and-set is atomic across event loops
# and across direct ``_rotate_cookies`` callers. Failure-stampede protection
# comes for free: even a POST that times out has already claimed the slot,
# so 10 fanned-out callers don't each wait 15 s on a hung server.
_LAST_POKE_ATTEMPT_MONOTONIC: dict[Path | None, float] = {}


def _get_poke_lock(storage_path: Path | None) -> asyncio.Lock:
    """Return the ``asyncio.Lock`` for ``(running event loop, storage_path)``.

    Lazily created on first call from each loop/profile pair so the lock binds
    to the current loop. The dict mutation runs under the sync state lock so
    concurrent threads with their own loops don't tear the registry.
    """
    loop = asyncio.get_running_loop()
    with _POKE_STATE_LOCK:
        per_loop = _POKE_LOCKS_BY_LOOP.get(loop)
        if per_loop is None:
            per_loop = {}
            _POKE_LOCKS_BY_LOOP[loop] = per_loop
        lock = per_loop.get(storage_path)
        if lock is None:
            lock = asyncio.Lock()
            per_loop[storage_path] = lock
        return lock


def _try_claim_rotation(storage_path: Path | None) -> bool:
    """Atomic check-and-claim of the per-profile rotation slot.

    Returns ``True`` if the caller may proceed with the POST, ``False`` if
    another in-process call has claimed the slot within the rate-limit
    window. The claim and the timestamp update happen under one sync lock,
    so this is safe across event loops and across direct
    ``_rotate_cookies`` callers (layer-2 keepalive loops, etc.) — neither
    of which holds the per-loop async lock used by layer-1 ``_poke_session``.
    """
    with _POKE_STATE_LOCK:
        last = _LAST_POKE_ATTEMPT_MONOTONIC.get(storage_path, 0.0)
        now = time.monotonic()
        if last > 0 and (now - last) < _KEEPALIVE_RATE_LIMIT_SECONDS:
            return False
        _LAST_POKE_ATTEMPT_MONOTONIC[storage_path] = now
        return True


def _rotation_lock_path(storage_path: Path | None) -> Path | None:
    """Sibling sentinel used by ``_poke_session`` for cross-process coordination.

    Distinct from the ``.storage_state.json.lock`` used by ``save_cookies_to_storage``
    so a long-running save doesn't block rotations or vice versa.
    """
    if storage_path is None:
        return None
    return storage_path.with_name(f".{storage_path.name}.rotate.lock")


@contextlib.contextmanager
def _file_lock_try_exclusive(lock_path: Path) -> Iterator[bool]:
    """Non-blocking exclusive flock. Yields ``True`` if caller should proceed.

    Mirrors :func:`_file_lock_exclusive` but with ``LOCK_NB`` semantics:
      - genuine contention (another process holds the lock) → yield ``False``,
        caller skips its work (the holder is rotating; we don't need to)
      - lock infrastructure unavailable (read-only dir, NFS without flock,
        permission denied) → yield ``True``, caller **fails open** and
        proceeds without coordination, since waiting forever for an
        unworkable lock would permanently suppress rotation.
    """
    with _file_lock(lock_path, blocking=False, log_prefix="rotate lock") as state:
        # "held" → True (proceed, we own it); "unavailable" → True (fail open);
        # "contended" → False (someone else is rotating, skip).
        yield state != "contended"


def _is_recently_rotated(storage_path: Path | None) -> bool:
    """Return True if ``storage_path`` was modified within the rate-limit window.

    A meaningfully-future mtime (clock skew, NTP step, restored file, NFS drift)
    is treated as **not recent**: we'd rather fire one extra rotation than wedge
    the guard until wall time catches up. The lower bound is a small negative
    tolerance to absorb sub-second drift between ``time.time()`` and filesystem
    mtime resolution (notably Windows NTFS at lower clock granularity), which
    can otherwise classify a freshly-written file as future-dated. A
    missing/unreadable file falls through to the not-recent default.
    """
    if storage_path is None:
        return False
    try:
        mtime = storage_path.stat().st_mtime
    except OSError:
        return False
    age = time.time() - mtime
    return -_KEEPALIVE_PRECISION_TOLERANCE <= age <= _KEEPALIVE_RATE_LIMIT_SECONDS


async def _poke_session(client: httpx.AsyncClient, storage_path: Path | None = None) -> None:
    """Best-effort POST to ``accounts.google.com/RotateCookies`` to rotate SIDTS.

    Failures are logged at DEBUG and swallowed: this is purely a freshness
    optimisation. The caller's request to notebooklm.google.com is the
    authoritative health check.

    Three layered guards keep the POST from stampeding ``accounts.google.com``:

    1. **Disk mtime fast path.** If ``storage_state.json`` was rewritten within
       the rate-limit window, skip without any locking. Covers the common
       sequential-CLI case at zero cost.
    2. **In-process ``asyncio.Lock``.** Inside the lock, re-check the disk
       mtime (a sibling task may have rotated and saved during the wait) and
       a monotonic in-memory timestamp (a sibling may have rotated but not
       yet saved). Together these dedupe an ``asyncio.gather`` fan-out so
       only one POST fires per process per rate-limit window.
    3. **Cross-process non-blocking flock.** When ``storage_path`` is set, try
       to acquire ``.storage_state.json.rotate.lock`` with ``LOCK_NB``. If
       another process holds it, skip — they're rotating right now. This
       handles ``xargs -P``, parallel MCP workers, and similar parallel
       launches without queueing.

       Known gap: the flock is released as soon as the POST returns, but the
       caller's storage-state save happens *after* this function returns. A
       second process that starts in that narrow window observes the still-
       stale on-disk mtime and an unheld flock, and will fire its own POST.
       Worst case is two pokes back-to-back across processes — bounded, not
       a stampede. Closing this fully would require holding the flock past
       ``_poke_session`` until the save completes, which would entangle this
       throttle with the caller's lifecycle. Not worth the complexity here.

    Args:
        client: Live ``httpx.AsyncClient`` whose cookie jar should receive the
            rotated ``Set-Cookie``.
        storage_path: Optional path to the on-disk ``storage_state.json``. When
            provided, gates the poke via the disk mtime and the cross-process
            flock; when ``None`` (env-var auth) only the in-process serializer
            applies.

    Set ``NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`` to disable (e.g., environments
    that block ``accounts.google.com``).
    """
    if os.environ.get(NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV) == "1":
        return
    if _is_recently_rotated(storage_path):
        logger.debug(
            "Keepalive RotateCookies skipped: %s rotated within %.0fs",
            storage_path,
            _KEEPALIVE_RATE_LIMIT_SECONDS,
        )
        return

    async with _get_poke_lock(storage_path):
        # Re-check after acquiring the per-(loop, profile) async lock — another
        # task in this loop may have rotated and persisted while we were waiting.
        if _is_recently_rotated(storage_path):
            logger.debug(
                "Keepalive RotateCookies skipped: storage refreshed while waiting for lock"
            )
            return

        rotate_lock_path = _rotation_lock_path(storage_path)
        if rotate_lock_path is None:
            # No on-disk path → cross-process flock has no anchor. The
            # atomic claim inside ``_rotate_cookies`` is the only gate.
            await _rotate_cookies(client, storage_path)
            return

        with _file_lock_try_exclusive(rotate_lock_path) as acquired:
            if not acquired:
                logger.debug(
                    "Keepalive RotateCookies skipped: %s held by another process",
                    rotate_lock_path,
                )
                return
            # One last disk recheck: another process may have completed its
            # rotation + save between our top-of-function check and acquiring
            # this flock.
            if _is_recently_rotated(storage_path):
                logger.debug(
                    "Keepalive RotateCookies skipped: storage refreshed before flock acquired"
                )
                return
            # ``_rotate_cookies`` does its own atomic claim — if another
            # in-process caller (e.g. a sibling layer-2 keepalive loop on a
            # different event loop) just claimed this profile, the POST is
            # skipped here too.
            await _rotate_cookies(client, storage_path)


async def _rotate_cookies(client: httpx.AsyncClient, storage_path: Path | None = None) -> None:
    """Fire the ``RotateCookies`` POST. Bare operation; no guards.

    Used directly by the layer-2 keepalive loop, which is already self-paced
    via ``keepalive_min_interval`` and does not need the layer-1 dedup
    serialization. ``_poke_session`` calls this through its guard stack.

    Honours ``NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`` so a single env-var disables
    every rotation path (the layer-1 wrapper *and* the layer-2 loop).

    Stamps the per-profile attempt timestamp **before** the network await so
    that concurrent layer-1 callers (and concurrent layer-2 keepalive loops on
    other ``NotebookLMClient`` instances watching the same profile) see "this
    profile is rotating right now" and skip the POST. Stamping early covers:
      - the layer-1/layer-2 overlap where one is mid-flight and another arrives
      - failure stampedes — a 15 s timeout against a hung accounts.google.com
        does not let 10 fanned-out callers each wait the full timeout

    Does not propagate ``httpx.HTTPError``: this is a best-effort freshness
    call, not a health check.

    Args:
        client: Live ``httpx.AsyncClient`` whose cookie jar should receive the
            rotated ``Set-Cookie``.
        storage_path: Optional storage_state.json path used to key the
            in-process attempt timestamp by profile. ``None`` = env-var auth.
    """
    if os.environ.get(NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV) == "1":
        return
    # Atomic check-and-claim: another caller (a sibling layer-2 keepalive
    # loop, a layer-1 ``_poke_session`` on a different event loop, etc.) may
    # have already taken the slot for this profile within the rate-limit
    # window. ``_try_claim_rotation`` is the *only* authoritative gate;
    # everything above it in ``_poke_session`` is a fast-path optimisation.
    if not _try_claim_rotation(storage_path):
        logger.debug(
            "Keepalive RotateCookies skipped: %s claimed by another in-process caller",
            storage_path,
        )
        return
    try:
        # ``follow_redirects=True`` is defensive: empirically RotateCookies
        # answers 200 directly with the rotated Set-Cookie, but if Google ever
        # routes a 30x through an identity hop we still pick up cookies from
        # the terminal response.
        response = await client.post(
            KEEPALIVE_ROTATE_URL,
            headers=_KEEPALIVE_ROTATE_HEADERS,
            content=_KEEPALIVE_ROTATE_BODY,
            follow_redirects=True,
            timeout=_KEEPALIVE_POKE_TIMEOUT,
        )
        # httpx does not auto-raise on 4xx/5xx; without this, a 429 or 5xx from
        # Google would log nothing and the caller would proceed assuming the
        # rotation happened.
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.debug("Keepalive RotateCookies POST failed (non-fatal): %s", exc)


async def _fetch_tokens_with_jar(
    cookie_jar: httpx.Cookies,
    storage_path: Path | None = None,
    *,
    authuser: int = 0,
    account_email: str | None = None,
    force_authuser_query: bool = False,
) -> tuple[str, str]:
    """Internal: fetch CSRF and session tokens using a pre-built cookie jar.

    This is the single implementation for all token-fetch paths. All public
    functions (fetch_tokens, fetch_tokens_with_domains) delegate to this.

    Before fetching tokens, makes a best-effort POST to accounts.google.com to
    rotate __Secure-1PSIDTS; see ``_poke_session``. The poke may be skipped if
    ``storage_path`` was modified within the rate-limit window — that path
    relies on the existing on-disk cookies still being fresh.

    Args:
        cookie_jar: httpx.Cookies jar with auth cookies (domain-preserving or fallback).
        storage_path: Optional storage_state.json path, forwarded to
            ``_poke_session`` to gate the rotation poke.
        authuser: Google account index to authenticate as. ``0`` is the
            default account.
        account_email: Stable account email to use instead of the integer
            index when known.
        force_authuser_query: Append ``?authuser=0`` when callers explicitly
            requested account index 0. Implicit default-account calls leave the
            URL byte-identical to pre-multi-account behavior.

    Returns:
        Tuple of (csrf_token, session_id)

    Raises:
        httpx.HTTPError: If request fails
        ValueError: If tokens cannot be extracted from response
    """
    logger.debug("Fetching CSRF and session tokens from NotebookLM")

    async with httpx.AsyncClient(cookies=cookie_jar) as client:
        await _poke_session(client, storage_path)

        url = f"{get_base_url()}/"
        if account_email or authuser or force_authuser_query:
            url = f"{url}?{authuser_query(authuser, account_email)}"
        response = await client.get(
            url,
            follow_redirects=True,
            timeout=30.0,
        )
        response.raise_for_status()

        final_url = str(response.url)

        # Check if we were redirected to login
        if is_google_auth_redirect(final_url):
            raise ValueError(
                "Authentication expired or invalid. "
                "Redirected to: " + final_url + "\n"
                "Run 'notebooklm login' to re-authenticate."
            )

        csrf = extract_csrf_from_html(response.text, final_url)
        session_id = extract_session_id_from_html(response.text, final_url)

        # httpx copies the input Cookies object into the client. Copy any
        # redirect Set-Cookie updates back to the caller's jar before it is
        # persisted.
        _replace_cookie_jar(cookie_jar, client.cookies)

        logger.debug("Authentication tokens obtained successfully")
        return csrf, session_id


def _resolve_token_route_kwargs(
    storage_path: Path | None,
    *,
    authuser: int | None,
    account_email: str | None,
) -> dict[str, Any]:
    """Resolve token-fetch routing while preserving explicit caller intent."""
    explicit_authuser = authuser is not None
    resolved_authuser = get_authuser_for_storage(storage_path) if authuser is None else authuser
    if account_email is not None:
        resolved_account_email = account_email
    elif explicit_authuser:
        resolved_account_email = None
    else:
        resolved_account_email = get_account_email_for_storage(storage_path)

    route_kwargs: dict[str, Any] = {"authuser": resolved_authuser}
    if resolved_account_email is not None:
        route_kwargs["account_email"] = resolved_account_email
    if explicit_authuser:
        route_kwargs["force_authuser_query"] = True
    return route_kwargs


async def fetch_tokens(
    cookies: CookieInput,
    storage_path: Path | None = None,
    profile: str | None = None,
    *,
    authuser: int | None = None,
    account_email: str | None = None,
) -> tuple[str, str]:
    """Fetch tokens from a cookie mapping. For backward compatibility.

    Prefer AuthTokens.from_storage() which preserves cookie domains. If
    ``NOTEBOOKLM_REFRESH_CMD`` is set and auth has expired, the command is run
    through the platform shell, cookies are reloaded from ``storage_path`` or
    the active profile storage path, and token fetch is retried once. Refresh
    commands receive ``NOTEBOOKLM_REFRESH_STORAGE_PATH`` and
    ``NOTEBOOKLM_REFRESH_PROFILE`` in their environment.

    Args:
        cookies: Google auth cookies. Mutated in place on refresh.
        storage_path: Optional storage_state.json path to reload after refresh.
        profile: Optional profile name exposed to the refresh command.
        authuser: Optional explicit Google account index. Defaults to the
            persisted profile value, or 0 when none exists.
        account_email: Optional explicit Google account email. When provided,
            it is used as the auth routing value instead of the integer index.

    Returns:
        Tuple of (csrf_token, session_id)

    Raises:
        httpx.HTTPError: If request fails
        ValueError: If tokens cannot be extracted from response
        RuntimeError: If ``NOTEBOOKLM_REFRESH_CMD`` is set but fails
    """
    jar = build_cookie_jar(cookies=cookies, storage_path=storage_path)
    route_kwargs = _resolve_token_route_kwargs(
        storage_path,
        authuser=authuser,
        account_email=account_email,
    )
    csrf, session_id, refreshed, _post_refresh_snapshot = await _fetch_tokens_with_refresh(
        jar, storage_path, profile, **route_kwargs
    )
    if refreshed:
        fresh = _cookie_map_from_jar(jar)
        _update_cookie_input(cookies, fresh)
    return csrf, session_id


async def fetch_tokens_with_domains(
    path: Path | None = None,
    profile: str | None = None,
    *,
    authuser: int | None = None,
    account_email: str | None = None,
) -> tuple[str, str]:
    """Fetch tokens with domain-preserving cookies from storage.

    Used by CLI helpers. Loads storage, builds jar, fetches tokens, optionally
    runs NOTEBOOKLM_REFRESH_CMD on auth expiry, and persists any refreshed
    cookies back.

    Args:
        path: Path to storage_state.json. If provided, takes precedence over env vars.
        profile: Optional profile name exposed to the refresh command.
        authuser: Optional explicit Google account index. Defaults to the
            persisted profile value, or 0 when none exists.
        account_email: Optional explicit Google account email. When provided,
            it is used as the auth routing value instead of the integer index.

    Returns:
        Tuple of (csrf_token, session_id)

    Raises:
        FileNotFoundError: If storage file doesn't exist.
        httpx.HTTPError: If request fails.
        ValueError: If tokens cannot be extracted from response.
        RuntimeError: If ``NOTEBOOKLM_REFRESH_CMD`` is set but fails.
    """
    if path is None and (profile is not None or "NOTEBOOKLM_AUTH_JSON" not in os.environ):
        path = get_storage_path(profile=profile)
    jar = build_httpx_cookies_from_storage(path)
    route_kwargs = _resolve_token_route_kwargs(path, authuser=authuser, account_email=account_email)
    # Capture the open-time snapshot before any rotation could fire. The
    # snapshot is the input to the dirty-flag/delta merge that closes the
    # stale-overwrite-fresh race (docs/auth-keepalive.md §3.4.1).
    snapshot = snapshot_cookie_jar(jar)
    csrf, session_id, refreshed, post_refresh_snapshot = await _fetch_tokens_with_refresh(
        jar, path, profile, **route_kwargs
    )
    if refreshed and post_refresh_snapshot is not None:
        # NOTEBOOKLM_REFRESH_CMD replaced the jar wholesale. Use the snapshot
        # captured immediately after the replacement (before the retry fetch
        # added redirect Set-Cookies); re-snapshotting here would let those
        # retry rotations be absorbed into the baseline and never reach disk.
        snapshot = post_refresh_snapshot
    # Offload the blocking storage save to a worker thread so the
    # atomic-replace + fsync + flock can't stall the event loop on
    # slow filesystems.
    await asyncio.to_thread(save_cookies_to_storage, jar, path, original_snapshot=snapshot)
    return csrf, session_id
