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

    # For authenticated artifact downloads, use the client's download methods
    # (e.g. ``await client.artifacts.download_audio(...)``) rather than building
    # an httpx client by hand.

Security Notes:
    - Storage state files contain sensitive session cookies
    - Profile names are constrained by ``notebooklm.paths`` to prevent
      profile-directory traversal; explicit storage paths are used as provided
"""

import logging
import subprocess  # noqa: F401  # re-exported for tests that patch ``auth.subprocess.run``
from typing import TypeAlias

import httpx

from ._auth import account as _auth_account
from ._auth import cookie_policy as _cookie_policy
from ._auth import cookies as _auth_cookies
from ._auth import extraction as _auth_extraction
from ._auth import headers as _auth_headers
from ._auth import keepalive as _auth_keepalive
from ._auth import paths as _auth_paths
from ._auth import psidts_recovery as _auth_psidts_recovery
from ._auth import refresh as _auth_refresh
from ._auth import storage as _auth_storage
from ._auth import tokens as _auth_tokens

# Master-token (headless) auth. Re-exported here as the public boundary the CLI
# consumes (cli/ may not import private ``_auth.*`` modules — see
# tests/_guardrails/test_cli_boundary.py) and as the documented programmatic
# headless-auth surface (docs/python-api.md). Blessed in ``__all__`` below.
from ._auth.master_token import (  # noqa: F401
    MasterTokenError,
    exchange_master_token,
    generate_android_id,
    mint_cookies,
    persist_minted_jar,
    read_master_token,
    write_master_token,
)
from ._auth.tokens import AuthTokens
from .paths import get_storage_path  # noqa: F401  # kept as a module-level compat alias

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
# static-analysis tools (mypy, ruff F401 checks). ``notebooklm.auth.*`` is internal
# (docs/stability.md) except the documented power-user imports plus the cohesive
# operations the CLI/_app need across the public boundary. The 23 core/test-only
# re-exports de-blessed in #1592 were removed from this list but remain importable
# as module attributes for back-compat — first-party code now imports them from
# their ``notebooklm._auth.<sub>`` home, and one ``removed-export`` allowance each
# lives in scripts/api-compat-allowlist.json. Underscore-prefixed names remain
# accessible on the module as whitebox test affordances but are intentionally NOT
# blessed here. See ``tests/_guardrails/test_public_surface.py``:
# ``test_auth_module_has_expected_all`` snapshot-checks the exact ordering, and
# ``test_auth_all_matches_external_imports_audit`` AST-scans ``src/``/``tests/``/
# ``docs/`` to fail if a public name is imported externally from ``notebooklm.auth``
# without being added here.
__all__ = [
    "Account",
    "AuthTokens",
    "build_cookie_jar",
    "build_httpx_cookies_from_storage",
    "clear_account_metadata",
    "convert_rookiepy_cookies_to_storage_state",
    "cookie_names_from_storage",
    "enumerate_accounts",
    "exchange_master_token",
    "extract_cookies_from_storage",
    "extract_cookies_with_domains",
    "extract_email_from_html",
    "fetch_tokens_passive",
    "fetch_tokens_with_domains",
    "generate_android_id",
    "get_account_email_for_storage",
    "get_authuser_for_storage",
    "GOOGLE_REGIONAL_CCTLDS",
    "MasterTokenError",
    "mint_cookies",
    "missing_cookies_hint",
    "OPTIONAL_COOKIE_DOMAINS",
    "OPTIONAL_COOKIE_DOMAINS_BY_LABEL",
    "persist_minted_jar",
    "read_account_metadata",
    "read_account_metadata_from_storage_state",
    "read_master_token",
    "REQUIRED_COOKIE_DOMAINS",
    "validate_with_recovery",
    "write_account_metadata",
    "write_master_token",
]


# Per ADR-0014, ``_validate_required_cookies`` is a direct re-export of
# ``_auth.cookie_policy._validate_required_cookies``.
# The prior write-through that copy-forwarded facade-level rebindings of
# ``MINIMUM_REQUIRED_COOKIES`` / ``_EXTRACTION_HINT`` /
# ``_has_valid_secondary_binding`` into ``_cookie_policy`` (and mirrored
# ``_SECONDARY_BINDING_WARNED`` back) was removed as a behaviour-change
# masquerading as a refactor. Tests that need to rebind policy names now
# patch the canonical home in ``_auth.cookie_policy`` directly — see
# ``tests/unit/test_public_shims.py::test_auth_validation_uses_cookie_policy_rebindings_directly``.
#
# There is no reverse-assignment back onto ``_auth.cookies``: that module
# already imports the canonical validator from ``_cookie_policy`` (see
# ``_auth/cookies.py:40``), and ``auth._validate_required_cookies`` IS that
# same object — so any reverse-assignment would be a no-op.
_validate_required_cookies = _cookie_policy._validate_required_cookies


# WIZ field token extraction (CSRF, session ID, generic WIZ data) lives in
# ``notebooklm._auth.extraction``. Re-exported here so ``extract_csrf_from_html``
# / ``extract_session_id_from_html`` / ``extract_wiz_field`` (de-blessed from
# ``__all__`` in #1592 but kept importable) and white-box test affordances
# (``_safe_url``, ``_build_wiz_field_patterns``) keep resolving against
# ``notebooklm.auth``.
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
read_account_metadata_from_storage_state = _auth_account.read_account_metadata_from_storage_state
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


# ``load_auth_from_storage`` lives in ``_auth/tokens.py`` (see ADR-0014).
# Re-exported so ``notebooklm.auth.load_auth_from_storage`` stays importable
# (de-blessed from ``__all__`` in #1592; first-party callers use ``_auth.tokens``).
load_auth_from_storage = _auth_tokens.load_auth_from_storage


# Env-var name constants live in ``notebooklm._auth.paths``. Re-exported so both
# ``NOTEBOOKLM_REFRESH_CMD_ENV`` / ``NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV``
# (de-blessed from ``__all__`` in #1592 but kept importable) and the white-box
# surface (``_REFRESH_ATTEMPTED_ENV``, used by tests) keep resolving against
# ``notebooklm.auth``.
NOTEBOOKLM_REFRESH_CMD_ENV = _auth_paths.NOTEBOOKLM_REFRESH_CMD_ENV
NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV = _auth_paths.NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV
_REFRESH_ATTEMPTED_ENV = _auth_paths._REFRESH_ATTEMPTED_ENV


# --- Keepalive poke ----------------------------------------------------------
# Rotation throttle + ``RotateCookies`` POST bodies live in
# ``notebooklm._auth.keepalive``. Re-exported here so every name that was
# previously module-level on ``notebooklm.auth`` (constants, the per-loop /
# per-profile lock registry, ``KEEPALIVE_ROTATE_URL`` (de-blessed from ``__all__``
# in #1592 but kept importable), and white-box helpers like ``_poke_session`` /
# ``_rotate_cookies``) keeps resolving against this module. Tests that
# need to substitute a moved body should patch the canonical home directly
# (``_auth.keepalive.X``) — production code no longer mirrors writes
# (``_AuthFacadeModule`` retired per ADR-0003).
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
# Inline PSIDTS recovery (issue #865). Static facade alias for public-surface
# symmetry; the load path in ``load_auth_from_storage`` and
# ``_auth/cookies.build_httpx_cookies_from_storage`` calls
# ``_auth_psidts_recovery._recover_psidts_inline`` directly, so monkeypatches
# against ``notebooklm.auth._recover_psidts_inline`` do NOT affect runtime
# behavior. Tests that need to substitute the recovery body should patch
# ``notebooklm._auth.psidts_recovery._recover_psidts_inline``.
_recover_psidts_inline = _auth_psidts_recovery._recover_psidts_inline
# In-memory variant for the browser-cookies extraction path (issue #990).
# De-blessed from ``__all__`` in #1592 (kept importable); it has no first-party
# importer today. Mutates the caller's rookiepy cookie list in place; no file
# lock / throttle.
recover_psidts_in_memory = _auth_psidts_recovery.recover_psidts_in_memory
# Validate-with-recovery convenience: convert + validate rookiepy cookies and
# transparently retry through ``recover_psidts_in_memory`` on the recoverable
# PSIDTS-missing case (issue #990). Used by the CLI browser-extraction paths.
validate_with_recovery = _auth_psidts_recovery.validate_with_recovery
# Missing-cookies diagnostic hint (issue #990). Inspects which Tier-1/Tier-2
# cookies are missing and returns a scenario-specific recovery message that
# the CLI uses in place of the generic "Make sure you are logged in" tail.
missing_cookies_hint = _cookie_policy.missing_cookies_hint
# Helper: extract cookie names from a Playwright storage_state. Shared by
# all three CLI browser-extraction paths to feed ``missing_cookies_hint``.
cookie_names_from_storage = _cookie_policy.cookie_names_from_storage
# Rotation sentinel path lives in ``_auth.paths``; the keepalive module also
# aliases it locally. Re-exported here for white-box callers that resolve it
# against ``notebooklm.auth``.
_rotation_lock_path = _auth_paths._rotation_lock_path


# --- Refresh-cmd + token-fetch entry points ---------------------------------
# All refresh coordination and the token-fetch entry points live in
# ``notebooklm._auth.refresh``. Re-exported so the kept public
# ``fetch_tokens_with_domains`` / ``fetch_tokens_passive`` entry points,
# ``fetch_tokens`` (de-blessed from ``__all__`` in #1592 but kept importable),
# and the white-box surface (lock registries, ContextVar, ``_run_refresh_cmd``
# carrying the redaction logic, etc.) keep resolving against
# ``notebooklm.auth``. Tests that need to substitute a moved body should
# patch the canonical home directly (``_auth.refresh.X``) — production
# code no longer mirrors writes (``_AuthFacadeModule`` retired per ADR-0003).
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
fetch_tokens_passive = _auth_refresh.fetch_tokens_passive
