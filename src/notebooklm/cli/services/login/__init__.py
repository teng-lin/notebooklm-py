"""CLI-internal login services for browser-cookie auth flows.

This package was split out of the single-file ``cli/services/login.py``
in P3.T4 to keep every implementation module ≤ 400 LOC, eliminate the
``_read_browser_cookies`` / ``_enumerate_browser_accounts`` circular-
dispatch trap, and document the leaf-ward import DAG (chromium-family
and firefox-family modules share ``cookie_jar`` + ``rookiepy_errors``
helpers; ``browser_accounts`` sits above both as the dispatcher;
``refresh`` is the top of the graph).

Module body intentionally re-exports only. The AST guard
``tests/_lint/test_login_init_is_reexport_only.py`` enforces this; the
DAG guard ``tests/_lint/test_login_package_dag.py`` enforces the
leaf-ward shape declared in ``phase-3.md`` P3.T4.

The re-exports cover three concerns:

1. **Patch surface for the session module's re-export block.** The 13
   names in ``tests/_fixtures/session_reexport_baseline.txt`` (mirrored
   by ``from .services.login import (…)`` in ``cli/session_cmd.py``) MUST
   stay reachable from this package's namespace so the 37+ legacy
   ``monkeypatch.setattr("notebooklm.cli.session_cmd.<name>", …)`` sites
   keep working byte-for-byte.

2. **Profile-cmd helpers** (``_PROFILE_NAME_RE``,
   ``_validate_profile_name``, ``email_to_profile_name``) — read from
   :mod:`notebooklm.cli.profile_cmd` via ``login_service.<name>``.

3. **External dependency re-exports** (``console``, ``get_storage_path``,
   ``run_async``, ``NotebookLMClient``, ``fetch_tokens_with_domains``) —
   preserved as package attributes so legacy ``monkeypatch.setattr(
   "notebooklm.cli.services.login.<name>", …)`` patch targets continue
   to resolve. **Important caveat:** patching these attributes here does
   NOT intercept calls from inside this package's implementation
   modules — they bind their own imports. Tests that need to intercept
   internal call sites use ``tests/_fixtures/cli_session.py``'s
   ``patch_session_login_dual`` helper, which fans out the patch across
   every submodule that binds the name.
"""

from __future__ import annotations

# External dependency re-exports — kept as package attributes so legacy
# patch paths like ``notebooklm.cli.services.login.get_storage_path``
# continue to resolve. See module docstring for caveats.
from ....auth import fetch_tokens_with_domains
from ....client import NotebookLMClient
from ....paths import get_storage_path
from ...rendering import console
from ...runtime import run_async

# Internal implementations — re-exported for the session-side patch
# surface (baseline fixture) and for `profile_cmd` helper access.
from .browser_accounts import _enumerate_browser_accounts, _read_browser_cookies
from .cookie_domains import (
    _INCLUDE_DOMAINS_ALL,
    _build_google_cookie_domains,
    _parse_include_domains,
    _resolve_optional_cookie_domains,
    _warn_missing_optional_domains,
)
from .cookie_jar import _ROOKIEPY_BROWSER_ALIASES, _enumerate_one_jar
from .cookie_writes import (
    _select_account,
    _select_refresh_account,
    _write_extracted_cookies,
)
from .profile_targets import (
    _PROFILE_NAME_RE,
    _validate_profile_name,
    email_to_profile_name,
)
from .refresh import (
    _login_all_accounts_from_browser,
    _login_browser_cookies_single,
    _login_with_browser_cookies,
    _refresh_from_browser_cookies,
    _sync_server_language_to_config,
)

__all__ = [
    # External re-exports (patch-target preservation).
    "console",
    "fetch_tokens_with_domains",
    "get_storage_path",
    "NotebookLMClient",
    "run_async",
    # Cookie domain policy.
    "_INCLUDE_DOMAINS_ALL",
    "_build_google_cookie_domains",
    "_parse_include_domains",
    "_resolve_optional_cookie_domains",
    "_warn_missing_optional_domains",
    # Cookie jar enumeration.
    "_ROOKIEPY_BROWSER_ALIASES",
    "_enumerate_one_jar",
    # Browser-account discovery + reading.
    "_enumerate_browser_accounts",
    "_read_browser_cookies",
    # Profile name allocation.
    "_PROFILE_NAME_RE",
    "_validate_profile_name",
    "email_to_profile_name",
    # Cookie writes + account selection.
    "_select_account",
    "_select_refresh_account",
    "_write_extracted_cookies",
    # Refresh + bulk-login drivers + post-login server-language sync.
    "_login_all_accounts_from_browser",
    "_login_browser_cookies_single",
    "_login_with_browser_cookies",
    "_refresh_from_browser_cookies",
    "_sync_server_language_to_config",
]
