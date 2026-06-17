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

Two complementary tests guard the contract:

1. The snapshot test (``test_*_module_has_expected_all``) pins the exact
   list, so accidental drift in shape or ordering fails loudly.
2. The audit test (``test_*_all_matches_external_imports_audit``) AST-scans
   ``src/``, ``tests/``, ``docs/`` for ``from notebooklm.<module> import X``
   patterns and fails if any externally imported public name was added
   without updating ``__all__``.
"""

from __future__ import annotations

import ast
import pathlib
from functools import lru_cache

import pytest

import notebooklm.auth as auth_module
import notebooklm.client as client_module

pytestmark = pytest.mark.repo_lint

# Repository root, derived from this test file's location:
# tests/_guardrails/test_public_surface.py -> parents[2] == repo root.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCAN_ROOTS = ("src", "tests", "docs")

# ---------------------------------------------------------------------------
# Expected public surface — keep in sync with the audited externally-imported
# set. When adding a new public name to one of these modules, add it to this
# test in the same PR.
# ---------------------------------------------------------------------------

EXPECTED_CLIENT_ALL: list[str] = ["NotebookLMClient"]

EXPECTED_AUTH_ALL: list[str] = [
    "Account",
    "AuthTokens",
    "build_cookie_jar",
    "build_httpx_cookies_from_storage",
    "clear_account_metadata",
    "convert_rookiepy_cookies_to_storage_state",
    "cookie_names_from_storage",
    "enumerate_accounts",
    "extract_cookies_from_storage",
    "extract_cookies_with_domains",
    "extract_email_from_html",
    "fetch_tokens_passive",
    "fetch_tokens_with_domains",
    "get_account_email_for_storage",
    "get_authuser_for_storage",
    "GOOGLE_REGIONAL_CCTLDS",
    "missing_cookies_hint",
    "OPTIONAL_COOKIE_DOMAINS",
    "OPTIONAL_COOKIE_DOMAINS_BY_LABEL",
    "read_account_metadata",
    "REQUIRED_COOKIE_DOMAINS",
    "validate_with_recovery",
    "write_account_metadata",
]

# Names de-blessed from ``auth.__all__`` in PR-1 (#1592): removed from the
# advertised surface but kept importable as module attributes for back-compat
# (the rpc-tranche mechanism — see scripts/api-compat-allowlist.json). The freeze
# test below locks that promise so a future change can't silently drop importability
# or re-bless a name.
_AUTH_DEBLESSED_KEEP_IMPORTABLE: list[str] = [
    "advance_cookie_snapshot_after_save",
    "ALLOWED_COOKIE_DOMAINS",
    "authuser_query",
    "CookieSaveResult",
    "CookieSnapshot",
    "CookieSnapshotKey",
    "CookieSnapshotValue",
    "extract_csrf_from_html",
    "extract_session_id_from_html",
    "extract_wiz_field",
    "fetch_tokens",
    "format_authuser_value",
    "KEEPALIVE_ROTATE_URL",
    "load_auth_from_storage",
    "load_httpx_cookies",
    "MINIMUM_REQUIRED_COOKIES",
    "normalize_cookie_map",
    "NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV",
    "NOTEBOOKLM_REFRESH_CMD_ENV",
    "NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV",
    "recover_psidts_in_memory",
    "save_cookies_to_storage",
    "snapshot_cookie_jar",
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


def test_auth_all_excludes_private_names() -> None:
    """``auth.__all__`` must not bless underscore-prefixed helpers.

    Private helpers (``_is_allowed_cookie_domain``, ``_safe_url``, etc.) remain
    accessible on the module — some tests treat them as whitebox affordances —
    but they are deliberately excluded from the public surface. Adding one
    here would silently promote it to documented API.
    """
    private = [name for name in auth_module.__all__ if name.startswith("_")]
    assert not private, f"underscore-prefixed names must not appear in auth.__all__: {private}"


@lru_cache(maxsize=1)
def _collect_external_imports_by_module() -> dict[str, frozenset[str]]:
    """Return public names imported from ``notebooklm.<module>`` by module.

    The auth/client audit tests both walk the same tree. Cache the scan so
    adding a second audited module does not double the unit-suite cost.
    """
    imports_by_module: dict[str, set[str]] = {}
    src_root = _REPO_ROOT / "src"
    for root in _SCAN_ROOTS:
        for path in (_REPO_ROOT / root).rglob("*.py"):
            try:
                tree = ast.parse(path.read_text())
            except (SyntaxError, UnicodeDecodeError):
                continue
            # Package parts of the file, rooted at ``notebooklm`` when the file
            # lives under ``src/`` (so relative imports can be resolved to their
            # true target). Files outside ``src/`` (tests/docs) only use absolute
            # ``notebooklm.<module>`` imports for this audit.
            file_pkg_parts: list[str] | None = None
            try:
                rel = path.resolve().relative_to(src_root.resolve())
                file_pkg_parts = list(rel.parts[:-1])  # drop filename
            except ValueError:
                file_pkg_parts = None
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                module = node.module or ""
                module_basename: str | None = None
                if module.startswith("notebooklm.") and module.count(".") == 1:
                    module_basename = module.rsplit(".", 1)[1]
                # Relative imports (``from .auth import X`` / ``from ..auth``):
                # resolve against the importing file's package so a same-named
                # SIBLING module (e.g. ``_runtime/auth.py``) is NOT misattributed
                # to the top-level ``notebooklm.auth`` facade.
                elif node.level > 0 and module and file_pkg_parts is not None:
                    pops = node.level - 1
                    if pops <= len(file_pkg_parts):
                        base = file_pkg_parts[: len(file_pkg_parts) - pops]
                        target = [*base, *module.split(".")]
                        # Only attribute to ``notebooklm.<basename>`` when the
                        # import resolves to a TOP-LEVEL notebooklm module.
                        if len(target) == 2 and target[0] == "notebooklm":
                            module_basename = target[1]
                if module_basename is None:
                    continue
                for alias in node.names:
                    if alias.name == "*" or alias.name.startswith("_"):
                        continue
                    imports_by_module.setdefault(module_basename, set()).add(alias.name)
    return {name: frozenset(names) for name, names in imports_by_module.items()}


def _collect_external_imports(module_basename: str) -> set[str]:
    """Return the set of public names imported from ``notebooklm.<module_basename>``.

    Reads from the cached repo-wide scan in
    :func:`_collect_external_imports_by_module`, which walks every ``.py``
    file under ``src/``, ``tests/``, ``docs/`` once per process.
    """
    return set(_collect_external_imports_by_module().get(module_basename, frozenset()))


def test_auth_all_matches_external_imports_audit() -> None:
    """``auth.__all__`` is a superset of every public name actually imported.

    This is the dynamic counterpart to ``test_auth_module_has_expected_all``.
    Where the former pins to a snapshotted list, this one scans the live
    codebase (``src/``, ``tests/``, ``docs/``) and fails if any externally
    imported public name has been added without updating ``__all__``.
    """
    declared = set(auth_module.__all__)
    actually_imported = _collect_external_imports("auth")
    missing = actually_imported - declared
    assert not missing, (
        "Public names imported from notebooklm.auth but missing from "
        f"auth.__all__: {sorted(missing)}\n"
        "Add them to __all__ (and to EXPECTED_AUTH_ALL above) so the public "
        "surface stays explicit."
    )


def test_auth_deblessed_names_stay_importable_but_unblessed() -> None:
    """PR-1 (#1592): de-blessed auth names stay importable for back-compat but are
    absent from ``__all__`` — the rpc-tranche freeze guard, applied to auth."""
    assert len(_AUTH_DEBLESSED_KEEP_IMPORTABLE) == 23
    assert len(_AUTH_DEBLESSED_KEEP_IMPORTABLE) == len(set(_AUTH_DEBLESSED_KEEP_IMPORTABLE)), (
        "_AUTH_DEBLESSED_KEEP_IMPORTABLE must not contain duplicates"
    )
    declared = set(auth_module.__all__)
    sentinel = object()
    for name in _AUTH_DEBLESSED_KEEP_IMPORTABLE:
        assert getattr(auth_module, name, sentinel) is not sentinel, (
            f"de-blessed {name!r} must stay importable from notebooklm.auth"
        )
        assert name not in declared, f"de-blessed {name!r} must not be back in auth.__all__"


def test_client_all_matches_external_imports_audit() -> None:
    """``client.__all__`` is a superset of every public name actually imported."""
    declared = set(client_module.__all__)
    actually_imported = _collect_external_imports("client")
    missing = actually_imported - declared
    assert not missing, (
        "Public names imported from notebooklm.client but missing from "
        f"client.__all__: {sorted(missing)}\n"
        "Add them to __all__ (and to EXPECTED_CLIENT_ALL above) so the public "
        "surface stays explicit."
    )
