"""Enforce the declared ``__all__`` on ``client.py`` and ``auth.py``.

Both modules curate a public surface that the rest of the codebase, the
documented API, and external integrators depend on. ``__all__`` is the
machine-checkable contract:

* ``notebooklm.client`` exports exactly ``NotebookLMClient``. Other names in
  that module are pulled in for typing / re-export reasons but are not part of
  the public surface.

* ``notebooklm.auth`` exports the audited set of names externally imported
  across ``src/``, ``tests/``, ``docs/`` as of 2026-05-17. Underscore-prefixed
  names remain accessible on the module — some tests poke at them as whitebox
  affordances — but are intentionally excluded from ``__all__``.

This test pins both lists so a future refactor that drops a name from
``__all__`` (silently breaking ``from notebooklm.auth import *`` callers, or
making the symbol invisible to static-analysis surfacing) fails loudly here
with the specific name that went missing.
"""

from __future__ import annotations

import notebooklm.auth as auth_module
import notebooklm.client as client_module

# ---------------------------------------------------------------------------
# Expected public surface — keep in sync with the audited externally-imported
# set. When adding a new public name to one of these modules, add it to this
# test in the same PR.
# ---------------------------------------------------------------------------

EXPECTED_CLIENT_ALL: list[str] = ["NotebookLMClient"]

EXPECTED_AUTH_ALL: list[str] = [
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_client_module_has_expected_all() -> None:
    """``notebooklm.client.__all__`` is exactly ``["NotebookLMClient"]``."""
    assert hasattr(client_module, "__all__"), (
        "notebooklm.client must declare __all__ to pin its public surface."
    )
    assert list(client_module.__all__) == EXPECTED_CLIENT_ALL


def test_client_all_entries_resolve_on_module() -> None:
    """Every name in ``client.__all__`` must be importable from the module."""
    for name in client_module.__all__:
        assert hasattr(client_module, name), (
            f"{name!r} listed in client.__all__ but not present on the module"
        )


def test_auth_module_has_expected_all() -> None:
    """``notebooklm.auth.__all__`` matches the audited externally-imported set.

    This test is the canonical record of what the audit found on 2026-05-17.
    If you intentionally add or remove a public name from ``auth.py``, update
    ``EXPECTED_AUTH_ALL`` above to match and re-run the audit to confirm no
    external caller is broken (search for ``from notebooklm.auth import`` and
    ``from ..auth import`` across ``src/``, ``tests/``, ``docs/``).
    """
    assert hasattr(auth_module, "__all__"), (
        "notebooklm.auth must declare __all__ to pin its public surface."
    )
    actual = list(auth_module.__all__)
    assert actual == EXPECTED_AUTH_ALL, (
        "auth.__all__ drift detected.\n"
        f"  missing from __all__: {sorted(set(EXPECTED_AUTH_ALL) - set(actual))}\n"
        f"  unexpected in __all__: {sorted(set(actual) - set(EXPECTED_AUTH_ALL))}"
    )


def test_auth_all_entries_resolve_on_module() -> None:
    """Every name in ``auth.__all__`` must be importable from the module.

    The facade-module ``__getattribute__`` proxy in ``auth.py`` means a stale
    ``__all__`` entry would not surface as a normal ``AttributeError`` at
    import time. Force-evaluate every entry here so the test catches drift.
    """
    sentinel = object()
    for name in auth_module.__all__:
        value = getattr(auth_module, name, sentinel)
        assert value is not sentinel, (
            f"{name!r} listed in auth.__all__ but not present on the module"
        )


def test_auth_all_is_sorted_case_insensitively() -> None:
    """Keep ``auth.__all__`` reviewable — alphabetized case-insensitively."""
    actual = list(auth_module.__all__)
    expected_sorted = sorted(actual, key=str.lower)
    assert actual == expected_sorted, (
        "auth.__all__ must be alphabetized (case-insensitive) for diff review"
    )


def test_auth_all_has_no_duplicates() -> None:
    """``auth.__all__`` must not contain duplicate entries."""
    actual = list(auth_module.__all__)
    assert len(actual) == len(set(actual)), (
        "auth.__all__ contains duplicate entries: "
        f"{sorted({n for n in actual if actual.count(n) > 1})}"
    )
