"""Authentication handling for NotebookLM API.

This module provides authentication utilities for the NotebookLM client:

1. **Cookie-based Authentication**: Loads Google cookies from Playwright storage
   state files created by `notebooklm login`.

2. **Token Extraction**: Fetches CSRF (SNlM0e) and session (FdrFJe) tokens from
   the NotebookLM homepage, required for all RPC calls.

3. **Download Cookies**: Provides httpx-compatible cookies with domain info for
   authenticated downloads from Google content servers.

Usage:
    # Recommended: use the managed storage-backed client lifecycle
    async with NotebookLMClient.from_storage() as client:
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
from collections.abc import Awaitable as _Awaitable
from collections.abc import Callable as _Callable
from pathlib import Path as _Path
from typing import Any as _Any
from typing import NoReturn as _NoReturn
from typing import TypeAlias

import httpx

from ._auth import account as _auth_account
from ._auth import cookie_policy as _cookie_policy
from ._auth import cookies as _auth_cookies
from ._auth import extraction as _auth_extraction
from ._auth import keepalive as _auth_keepalive
from ._auth import paths as _auth_paths
from ._auth import psidts_recovery as _auth_psidts_recovery
from ._auth import recovery_rungs as _auth_recovery_rungs
from ._auth import refresh as _auth_refresh
from ._auth import storage as _auth_storage
from ._auth import tokens as _auth_tokens

# Master-token (headless) auth. Re-exported here as the public boundary the CLI
# consumes (cli/ may not import private ``_auth.*`` modules — see
# tests/_guardrails/test_cli_boundary.py) and as the documented programmatic
# headless-auth surface (docs/python-api.md). Blessed in ``__all__`` below.
#
# #2103 PR-2 structural follow-up: the CLI invokes whole audited TRANSACTIONS
# (``master_token_bootstrap`` / ``master_token_remint`` /
# ``bootstrap_missing_storage_from_master_token`` / ``assert_account_writable``)
# rather than assembling them from primitives itself. ADR-0034 Phase 11D keeps
# these v0.x adapters in ``_auth.master_token`` while the concrete transaction
# owner is ``_auth.master_token_bootstrap.MasterTokenBootstrapper``.
# ``exchange_master_token`` /
# ``mint_cookies`` / ``persist_minted_jar`` / ``write_master_token`` /
# ``generate_android_id`` are de-blessed accordingly (kept importable —
# ``_AUTH_DEBLESSED_KEEP_IMPORTABLE`` — for the documented low-level recipe and
# any existing external caller, but no first-party importer remains). Import
# them from ``notebooklm._auth.master_token`` below for that reason: they stay
# reachable via ``notebooklm.auth.<name>`` (attribute access, unaffected by
# ``__all__``) without being re-blessed as this facade's primary surface.
#
# ``BootstrapOutcome`` is deliberately NOT re-exported here (auth
# cross-boundary ledger shrink): its only real first-party importer collapsed
# it to a bool immediately, so ``bootstrap_missing_storage_from_master_token``
# below does that collapse inside ``_auth`` instead of publishing the enum
# across the boundary for one caller that never needed the fine-grained type.
from ._auth.master_token import (  # noqa: F401
    MasterTokenError,
    assert_account_writable,  # noqa: F401
    bootstrap_missing_storage_from_master_token,
    exchange_master_token,
    generate_android_id,
    mint_cookies,
    persist_minted_jar,
    read_master_token,
    write_master_token,
)
from ._auth.master_token import bootstrap_from_oauth_token as master_token_bootstrap  # noqa: F401
from ._auth.master_token import remint_from_stored_token as master_token_remint  # noqa: F401
from ._auth.profile_migration import replace_profile_from_login  # noqa: F401
from ._auth.profile_store import ReplaceResult  # noqa: F401

# v0.x login/import compatibility values remain importable through this facade.
# First-party app/CLI callers use the native ``replace_profile_from_login`` and
# ``ReplaceResult`` aliases above; neither alias is part of ``__all__``.
from ._auth.storage import (  # noqa: F401
    CLEAR_ACCOUNT,
    KEEP_ACCOUNT,
    AccountRecord,
    LoginWriteOutcome,
    replace_from_login,
)
from ._auth.tokens import AuthTokens

# Public re-export: the fail-closed storage writers (persist_minted_jar /
# write_account_metadata, reached via this facade) raise LockUnavailableError on
# a bounded-lock timeout. Canonical home is notebooklm.exceptions; re-exported
# here so CLI/facade callers can catch it without importing exceptions directly
# (it is also an OSError via TimeoutError — see ADR-0029).
from .exceptions import LockUnavailableError  # noqa: F401
from .paths import get_storage_path  # noqa: F401  # kept as a module-level compat alias


def _default_headless_rung(
    *,
    storage_path: _Path,
    allow_headless: bool,
) -> _auth_recovery_rungs.HeadlessRungOutcome:
    """Load the optional browser implementation only when L3 actually fires."""
    from ._browser.headless_reauth import headless_rung

    return headless_rung(storage_path=storage_path, allow_headless=allow_headless)


_auth_recovery_rungs.install_headless_rung(_default_headless_rung)


class _BrowserFacadeIO:
    """Adapt first-party callbacks to the private browser IO protocol."""

    def __init__(
        self,
        emit: _Callable[..., None],
        fail: _Callable[[int], _NoReturn],
    ) -> None:
        self._emit = emit
        self._fail = fail

    def emit(self, *args: _Any, **kwargs: _Any) -> None:
        self._emit(*args, **kwargs)

    def fail(self, code: int) -> _NoReturn:
        self._fail(code)

    def run_async(self, coro: _Awaitable[_Any]) -> _Any:
        raise AssertionError("browser capture must not invoke the facade IO async bridge")


def browser_login_channels() -> tuple[tuple[str, str], ...]:
    """Return immutable ``(channel, display label)`` pairs for first-party login UI."""
    from ._browser.browser_launch_errors import CHANNEL_BROWSERS

    return tuple(
        (channel, display_label) for channel, (display_label, *_) in CHANNEL_BROWSERS.items()
    )


def ensure_browser_login_available(
    browser: str,
    *,
    emit: _Callable[..., None],
    fail: _Callable[[int], _NoReturn],
) -> None:
    """Fail with the browser-extra hint before first-party login emits a banner."""
    from ._browser.browser_capture import ensure_playwright_available

    ensure_playwright_available(_BrowserFacadeIO(emit, fail), browser=browser)


def run_browser_login_capture(
    *,
    browser: str,
    browser_profile: _Path,
    storage_path: _Path,
    include_domains: set[str] | None,
    login_timeout_s: int,
    emit: _Callable[..., None],
    fail: _Callable[[int], _NoReturn],
) -> str | None:
    """Run headed browser capture and return only the page HTML needed for repair."""
    from ._browser.browser_capture import BrowserCapturePlan, run_browser_capture

    result = run_browser_capture(
        BrowserCapturePlan(
            browser=browser,
            browser_profile=browser_profile,
            storage_path=storage_path,
            include_domains=include_domains,
            login_timeout_s=login_timeout_s,
        ),
        _BrowserFacadeIO(emit, fail),
        headless=False,
        interactive=True,
    )
    return result.page_html


def check_headless_reauth_readiness(*, browser_profile: _Path) -> tuple[bool, str]:
    """Return a transport-neutral readiness projection for the optional L3 rung."""
    from ._browser.headless_reauth import headless_reauth_readiness

    result = headless_reauth_readiness(browser_profile=browser_profile)
    return result.available, result.detail


def capture_browser_oauth_token(
    *,
    browser: str = "chromium",
    cdp_url: str | None = None,
    timeout_s: float = 300.0,
) -> str:
    """Capture the single-use OAuth token through the optional browser implementation."""
    from ._browser.oauth_token import capture_oauth_token

    try:
        return capture_oauth_token(browser=browser, cdp_url=cdp_url, timeout_s=timeout_s)
    finally:
        del browser, cdp_url, timeout_s


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
filter_storage_state_cookies_by_domain_policy = (
    _auth_storage.filter_storage_state_cookies_by_domain_policy
)
_merge_cookies_legacy = _auth_storage._merge_cookies_legacy
_merge_cookies_with_snapshot = _auth_storage._merge_cookies_with_snapshot
_cookie_snapshot_key_variants = _auth_storage._cookie_snapshot_key_variants
_stored_cookie_snapshot_key = _auth_storage._stored_cookie_snapshot_key
_file_lock = _auth_storage._file_lock
_file_lock_exclusive = _auth_storage._file_lock_exclusive

REQUIRED_COOKIE_DOMAINS = _cookie_policy.REQUIRED_COOKIE_DOMAINS
OPTIONAL_COOKIE_DOMAINS_BY_LABEL = _cookie_policy.OPTIONAL_COOKIE_DOMAINS_BY_LABEL
OPTIONAL_COOKIE_DOMAINS = _cookie_policy.OPTIONAL_COOKIE_DOMAINS
ALLOWED_COOKIE_DOMAINS = _cookie_policy.ALLOWED_COOKIE_DOMAINS
GOOGLE_REGIONAL_CCTLDS = _cookie_policy.GOOGLE_REGIONAL_CCTLDS
MINIMUM_REQUIRED_COOKIES = _cookie_policy.MINIMUM_REQUIRED_COOKIES
app_host_scope_note = _cookie_policy.app_host_scope_note
_EXTRACTION_HINT = _cookie_policy._EXTRACTION_HINT
_SECONDARY_BINDING_WARNED = _cookie_policy._SECONDARY_BINDING_WARNED
_has_valid_secondary_binding = _cookie_policy._has_valid_secondary_binding
_auth_domain_priority = _cookie_policy._auth_domain_priority
_is_google_domain = _cookie_policy._is_google_domain
_is_allowed_auth_domain = _cookie_policy._is_allowed_auth_domain
_is_allowed_cookie_domain = _cookie_policy._is_allowed_cookie_domain


# Public surface for ``from notebooklm.auth import *`` and for downstream
# static-analysis tools (mypy, ruff F401 checks). This list is EXACTLY the
# ``notebooklm.auth`` surface documented in docs/stability.md — nothing else.
# Everything else in this module is internal (docs/stability.md: "notebooklm.auth.*
# — Auth internals").
#
# ``__all__`` used to double as the CLI/_app cross-boundary import allowlist: the
# CLI boundary lint (tests/_guardrails/test_cli_boundary.py) forbids ``cli/`` from
# importing ``notebooklm._*``, so every helper the CLI needed was reached through
# this facade — and the external-imports audit then FORCED that name into
# ``__all__``. "The CLI needs it" silently became "it is public API", and the list
# grew to 38 names, 32 of which docs/stability.md never promised. Those 32 are
# de-blessed here (the #1592 mechanism: dropped from ``__all__``, still importable
# as module attributes, one ``removed-export`` allowance each in
# scripts/api-compat-allowlist.json) and are now tracked by their own
# first-party-only list, ``AUTH_CROSS_BOUNDARY_NAMES`` in
# ``tests/_guardrails/test_public_surface.py``. Adding a name there does NOT
# publish it. Underscore-prefixed names remain accessible on the module as whitebox
# test affordances but are intentionally NOT blessed here.
#
# See ``tests/_guardrails/test_public_surface.py``:
# ``test_auth_module_has_expected_all`` snapshot-checks the exact ordering,
# ``test_auth_all_matches_documented_public_surface`` pins this list to
# docs/stability.md and the manifest in ``test_public_surface_manifest.py``, and
# ``test_auth_all_matches_external_imports_audit`` AST-scans ``src/``/``tests/``/
# ``docs/`` to fail if a name is imported externally from ``notebooklm.auth``
# without being in ``__all__`` OR ``AUTH_CROSS_BOUNDARY_NAMES``.
__all__ = [
    "AuthTokens",
    "convert_rookiepy_cookies_to_storage_state",
    "LockUnavailableError",
    "OPTIONAL_COOKIE_DOMAINS",
    "OPTIONAL_COOKIE_DOMAINS_BY_LABEL",
    "REQUIRED_COOKIE_DOMAINS",
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
_RequiredCookieValidationError = _cookie_policy.RequiredCookieValidationError
_validate_cookie_shape = _auth_cookies._validate_cookie_shape
_sanitize_cookie_entry = _auth_cookies._sanitize_cookie_entry
_sanitized_auth_entries = _auth_cookies._sanitized_auth_entries
_validate_routable_entries = _auth_cookies._validate_routable_entries


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

# Token-route resolver. It used to live in ``notebooklm._auth.headers``; that
# module was folded into ``_auth.refresh`` (ADR-0033 sanctioned merge) because
# its sole function's only call sites are the token-fetch entry points there.
# Still re-exported here so internal callers (``fetch_tokens``,
# ``fetch_tokens_with_domains``) and white-box tests keep resolving the helper
# against ``notebooklm.auth``.
_resolve_token_route_kwargs = _auth_refresh._resolve_token_route_kwargs


# ADR-0033 PR 5.2 split ``_auth.account`` along its real seam: the NETWORK
# identity half (probing ``?authuser=N``, parsing the page email, formatting the
# wire routing value) stayed in ``_auth.account``; the account RECORD half
# (reading/writing/promoting/scrubbing the persisted ``{authuser, email}``
# binding) moved next to the other ``storage_state.json`` readers and writers in
# ``_auth.storage``. Every facade NAME below is unchanged and every one still
# resolves to the very same function object — only the module the alias reads it
# from moved, which is exactly what ``test_public_surface_manifest`` asserts
# identity on.
Account = _auth_account.Account
MAX_AUTHUSER_PROBE = _auth_account.MAX_AUTHUSER_PROBE
_ACCOUNT_CONTEXT_KEY = _auth_storage._ACCOUNT_CONTEXT_KEY
# ``_account_context_path`` is no longer aliased here: it survives in
# ``_auth.storage`` solely as the private site of the legacy-key scrub and the
# one-shot promotion (whitebox tests patch the canonical home directly).
extract_email_from_html = _auth_account.extract_email_from_html
repair_account_metadata_from_playwright_storage = (
    _auth_account.repair_account_metadata_from_playwright_storage
)
_probe_authuser = _auth_account._probe_authuser
read_account_metadata = _auth_storage.read_account_metadata
# ``read_account_metadata_from_storage_state``'s only facade importers
# (cli/auth_runtime.py, _app/auth_check.py) now call ``resolve_account_identity``
# instead (auth cross-boundary ledger shrink, follow-up to #2103), so it drops
# out of ``AUTH_CROSS_BOUNDARY_NAMES`` — but the alias below stays: unlike the
# other names this PR moved to ``_AUTH_DEBLESSED_KEEP_IMPORTABLE``,
# ``scripts/api-compat-allowlist.json`` explicitly records this one as
# retained/importable, a promise dropping the alias would silently break
# (caught in review — PR #2139).
read_account_metadata_from_storage_state = _auth_storage.read_account_metadata_from_storage_state
get_authuser_for_storage = _auth_storage.get_authuser_for_storage
get_account_email_for_storage = _auth_storage.get_account_email_for_storage
# Both kept importable here for the frozen first-party compatibility manifest
# (tests/_guardrails/test_public_surface_manifest.py::_AUTH_FIRST_PARTY_COMPATIBILITY_NAMES)
# even though no cli/_app caller reaches them through the facade anymore — see
# ``resolve_account_identity`` below and ``_AUTH_DEBLESSED_KEEP_IMPORTABLE`` in
# test_public_surface.py.
resolve_account_identity = _auth_storage.resolve_account_identity
format_authuser_value = _auth_account.format_authuser_value
authuser_query = _auth_account.authuser_query
write_account_metadata = _auth_storage.write_account_metadata
clear_account_metadata = _auth_storage.clear_account_metadata
# ``write_account_metadata`` / ``clear_account_metadata`` / ``extract_email_from_html``
# above: the remaining first-party composition path lives in
# ``_app/profile.py::_repair_playwright_account`` and uses the coarse
# ``repair_account_metadata_from_playwright_storage`` operation (auth
# cross-boundary ledger shrink, follow-up to #2103); all three stay importable
# here for the frozen first-party compatibility manifest
# (``_AUTH_FIRST_PARTY_COMPATIBILITY_NAMES``) and existing test callers.
# The legacy sibling ``context.json[account]`` READ path was removed (the reader
# derives an in-band-shaped record instead of passing the sibling through);
# ``promote_legacy_account`` in ``_auth.storage`` owns the durable one-shot
# in-band migration, run off the read path by a detached worker (ADR-0033
# PR 5.1) and also by the startup layout migration. The legacy-key scrub
# survives INSIDE ``write_account_metadata`` / ``clear_account_metadata``
# (privacy: a stale key must not leave the account email at rest), so the CLI
# login writers no longer call a facade helper after their writes —
# ``drop_legacy_account_key`` remains importable here for back-compat only
# (de-blessed; no first-party importer).
drop_legacy_account_key = _auth_storage._drop_legacy_account_key


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
# Mid-session refresh-cmd rung opt-in + captured-output opt-in (c-PR4). Kept
# importable via the facade like the other refresh env-var names.
NOTEBOOKLM_REFRESH_CMD_MIDSESSION_ENV = _auth_paths.NOTEBOOKLM_REFRESH_CMD_MIDSESSION_ENV
NOTEBOOKLM_REFRESH_CMD_LOG_OUTPUT_ENV = _auth_paths.NOTEBOOKLM_REFRESH_CMD_LOG_OUTPUT_ENV
_REFRESH_ATTEMPTED_ENV = _auth_paths._REFRESH_ATTEMPTED_ENV


# --- Keepalive poke ----------------------------------------------------------
# Rotation policy lives in ``_auth.keepalive``; the raw wire lives in
# ``_auth.mint_service``. This facade re-exports through keepalive so every name formerly
# module-level on ``notebooklm.auth`` (constants, the per-loop /
# per-profile lock registry, ``KEEPALIVE_ROTATE_URL`` (de-blessed from ``__all__``
# in #1592 but kept importable), and white-box helpers like ``_poke_session`` /
# ``_rotate_cookies``) keeps resolving against this module. Tests that
# need to substitute policy or its import-time wire binding should patch keepalive;
# direct ``MintService`` wire substitutions patch ``_auth.mint_service.X``. Production
# code no longer mirrors writes (``_AuthFacadeModule`` retired per ADR-0003).
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
# The cross-loop coalescing machinery (per-loop future maps + the
# ``_REFRESH_GENERATIONS`` counter + ``_get_refresh_lock`` /
# ``_get_inflight_registry`` / ``_REFRESH_STATE_LOCK`` / ``_REFRESH_INFLIGHT_*``)
# was replaced by ``notebooklm._auth.single_flight`` in c-PR2; the five
# underscore-private facade test-bindings that mirrored it are removed (they
# were never part of the supported facade surface). Tests substitute the new
# behaviour by patching ``_auth.single_flight`` / ``_auth.refresh`` directly.
_AUTH_ERROR_SIGNALS = _auth_refresh._AUTH_ERROR_SIGNALS
_coalesced_run_refresh_cmd = _auth_refresh._coalesced_run_refresh_cmd
# L2.5 mid-session refresh-cmd rung adapter (c-PR4). Consumed by
# ``_auth.session.refresh_auth_session``; exposed here for white-box tests.
try_refresh_cmd_reauth = _auth_refresh.try_refresh_cmd_reauth
_midsession_refresh_cmd_enabled = _auth_refresh._midsession_refresh_cmd_enabled
_should_try_refresh = _auth_refresh._should_try_refresh
_split_refresh_cmd = _auth_refresh._split_refresh_cmd
_run_refresh_cmd = _auth_refresh._run_refresh_cmd
_fetch_tokens_with_refresh = _auth_refresh._fetch_tokens_with_refresh
_fetch_tokens_with_jar = _auth_refresh._fetch_tokens_with_jar
fetch_tokens = _auth_refresh.fetch_tokens
fetch_tokens_with_domains = _auth_refresh.fetch_tokens_with_domains
fetch_tokens_passive = _auth_refresh.fetch_tokens_passive
