"""Recurrence gate: the retired ``session_cmd`` patch-surface names stay gone.

#1367 deleted the ``notebooklm.cli.session_cmd`` patch-surface bridge —
the pure re-exports that existed *only* so tests could
``patch("notebooklm.cli.session_cmd.X")``, never referenced from the
module's own body. Each was migrated to the real consumer/home module
(``services.playwright_login``, ``services.login``, or ``notebooklm.paths``).

This lint is a targeted denylist, NOT a "no ``# noqa`` comment" rule. A
"no noqa" rule would false-positive on the body-used imports that legitimately
stay (``get_storage_path``, ``resolve_notebook_id``, the body-used login
privates), so it would be both wrong and brittle. Instead this asserts the
exact removed names are no longer attributes of the live ``session_cmd``
module — the precise invariant the bridge retirement establishes.

Why a runtime ``hasattr`` check (not an AST scan): the failure mode being
guarded is "someone re-adds the re-export", which is observable directly on
the module surface. Importing the module and checking ``hasattr`` tests the
real attribute set (re-imports, ``__getattr__`` forwards, and reassignments
all count) with no risk of an AST scanner missing an aliased form.

Note: ``get_storage_path`` is intentionally absent from the denylist. The
service-path lookups that used to patch it on ``session_cmd`` were migrated,
but the name itself is body-used at ``session_cmd.py`` (``auth refresh``) and
correctly remains a module attribute — it is Category-1 KEEP, not a removed
pure surface.
"""

from __future__ import annotations

import importlib

import pytest

SESSION_CMD_MODULE = "notebooklm.cli.session_cmd"

# The exact names removed from the ``session_cmd`` patch-surface bridge in
# #1367. Grouped by category from the retirement plan; every one of these was
# a pure re-export (never called from ``session_cmd``'s body).
#
# Category 2 — paths-style pure surfaces (migrated to consumer modules /
# ``notebooklm.paths``):
_CATEGORY_2 = frozenset(
    {
        "get_browser_profile_dir",
        "get_context_path",
        "_ensure_chromium_installed",
    }
)

# Category 4 — re-exports with direct-import consumers (repointed to the real
# home module):
_CATEGORY_4 = frozenset(
    {
        "_url_matches_base_host",
        "_connection_error_help",
        "_filter_storage_state_cookies_by_domain_policy",
        "_build_google_cookie_domains",
        "_resolve_optional_cookie_domains",
        "_enumerate_one_jar",
        "_select_account",
        "_login_with_browser_cookies",
        "_write_extracted_cookies",
    }
)

# Category 5 — dead as a ``session_cmd`` surface (0 patches, 0 imports); the
# functions stay live in their home modules, only the re-export is gone:
_CATEGORY_5 = frozenset(
    {
        "get_path_info",
        "get_current_notebook",
        "_is_navigation_interrupted_error",
        "_recover_page",
    }
)

# The stdlib modules that were kept solely as ``patch("...session_cmd.time.sleep")``
# style surfaces and removed in #1367 (repointed to the scoped service-module
# ``time``/``shutil``/``sys`` targets).
_REMOVED_STDLIB = frozenset({"shutil", "sys", "time"})

# Deliberately NOT guarded here: the ``_ORIGINAL_*`` import-time capture
# constants (``_ORIGINAL_GET_BROWSER_PROFILE_DIR`` / ``_ORIGINAL_GET_STORAGE_PATH``
# / ``_ORIGINAL_GET_CONTEXT_PATH`` / ``_ORIGINAL_GET_PATH_INFO``) that #1367
# deleted. They lived inside ``services.playwright_login`` and
# ``services.session_context`` as private helpers for ``_resolve_paths_helper``'s
# patched-vs-default comparison — they were never re-exported on, nor attributes
# of, ``notebooklm.cli.session_cmd`` (verified: zero ``_ORIGINAL_*`` references in
# ``session_cmd.py`` at the pre-#1367 base). This gate guards the ``session_cmd``
# attribute surface specifically, so a name that was never on that surface cannot
# "reappear" there; denylisting it would assert a non-fact. (Per CodeRabbit on
# PR #1374.)

REMOVED_PATCH_SURFACE_NAMES: frozenset[str] = (
    _CATEGORY_2 | _CATEGORY_4 | _CATEGORY_5 | _REMOVED_STDLIB
)


@pytest.fixture(scope="module")
def session_cmd():
    return importlib.import_module(SESSION_CMD_MODULE)


@pytest.mark.parametrize("name", sorted(REMOVED_PATCH_SURFACE_NAMES))
def test_removed_name_is_not_a_session_cmd_attribute(name: str, session_cmd) -> None:
    """Each retired patch-surface name must NOT reappear on ``session_cmd``."""
    assert not hasattr(session_cmd, name), (
        f"{SESSION_CMD_MODULE}.{name} reappeared as a module attribute. "
        "This name was a pure patch-surface re-export retired in #1367; "
        "tests must patch it on its real home module instead "
        "(services.playwright_login / services.login / notebooklm.paths). "
        "Do not re-add the re-export."
    )


def test_body_used_names_are_still_present(session_cmd) -> None:
    """Sanity: the Category-1 names that stay re-exported must still resolve.

    Guards against the denylist being over-broad — if a future edit deletes a
    body-used name, this fails loudly rather than the denylist silently
    passing on an empty module.
    """
    for name in ("get_storage_path", "resolve_notebook_id"):
        assert hasattr(session_cmd, name), (
            f"{SESSION_CMD_MODULE}.{name} is body-used and must stay a module "
            "attribute; it was NOT part of the #1367 patch-surface retirement."
        )


def test_denylist_and_keptlist_are_disjoint() -> None:
    """The removed-names denylist must not overlap the body-used kept names.

    ``get_storage_path`` is the trap: it had a service-path patch subset that
    was migrated, but the name itself is body-used and stays. It must never
    land on the removed denylist.
    """
    kept = {"get_storage_path", "resolve_notebook_id"}
    overlap = REMOVED_PATCH_SURFACE_NAMES & kept
    assert not overlap, f"denylist wrongly includes body-used kept names: {sorted(overlap)}"
