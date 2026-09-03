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
   ``src/``, ``tests/``, ``docs/`` for direct imports and binding-aware
   first-party ``auth.Name`` uses, then fails if any externally imported public
   name was added without updating ``__all__``.
"""

from __future__ import annotations

import ast
import pathlib
import re
from functools import lru_cache

import pytest

import notebooklm.auth as auth_module
import notebooklm.client as client_module
from tests._guardrails import test_auth_storage_compatibility as _auth_compat
from tests._guardrails._public_import_manifest import _DOCUMENTED_PUBLIC_IMPORTS

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

# The ``notebooklm.auth`` PUBLIC surface — exactly what docs/stability.md
# promises, and nothing else. ``test_auth_all_matches_documented_public_surface``
# pins this list to the doc and to the manifest in
# ``test_public_surface_manifest.py``, so the three can never drift apart.
#
# Do NOT add a name here just because first-party code imports it across the
# CLI/_app boundary — that is what ``AUTH_CROSS_BOUNDARY_NAMES`` below is for.
# Adding a name here is a public-API commitment and needs the docs change to
# match.
EXPECTED_AUTH_ALL: list[str] = [
    "AuthTokens",
    "convert_rookiepy_cookies_to_storage_state",
    "LockUnavailableError",
    "OPTIONAL_COOKIE_DOMAINS",
    "OPTIONAL_COOKIE_DOMAINS_BY_LABEL",
    "REQUIRED_COOKIE_DOMAINS",
]

# First-party-only names reachable at ``notebooklm.auth.<name>``: importable, NOT
# public API, NOT in ``__all__``.
#
# These exist because the CLI boundary lint (tests/_guardrails/test_cli_boundary.py)
# forbids ``cli/`` from importing ``notebooklm._*``, so ``cli/`` and ``_app/`` reach
# auth helpers through the ``notebooklm.auth`` facade. Before this list existed, the
# external-imports audit resolved that need by forcing each name into ``__all__`` —
# i.e. "the CLI needs it" auto-published it as public API, and ``auth.__all__``
# reached 38 names against 6 documented ones. Splitting the two roles keeps the
# published surface pinned to the docs while the boundary need is still tracked and
# enforced (importability + no-dead-entries below).
#
# Adding a name here does NOT publish it, and carries no stability guarantee. It is
# still a real cost — every entry is a name the auth layer cannot move freely — so
# prefer routing new CLI needs through ``_app/`` service functions instead.
AUTH_CROSS_BOUNDARY_NAMES: list[str] = [
    "Account",
    "app_host_scope_note",
    "assert_account_writable",
    "bootstrap_missing_storage_from_master_token",
    "browser_login_channels",
    "build_cookie_jar",
    "build_httpx_cookies_from_storage",
    "capture_browser_oauth_token",
    "check_headless_reauth_readiness",
    "cookie_names_from_storage",
    "ensure_browser_login_available",
    "enumerate_accounts",
    "extract_cookies_from_storage",
    "extract_cookies_with_domains",
    "fetch_tokens_passive",
    "fetch_tokens_with_domains",
    "filter_storage_state_cookies_by_domain_policy",
    "GOOGLE_REGIONAL_CCTLDS",
    "master_token_bootstrap",
    "master_token_remint",
    "MasterTokenError",
    "missing_cookies_hint",
    "read_account_metadata",
    "read_master_token",
    "repair_account_metadata_from_playwright_storage",
    "ReplaceResult",
    "replace_profile_from_login",
    "resolve_account_identity",
    "run_browser_login_capture",
    "validate_with_recovery",
]

# Names de-blessed from ``auth.__all__``: removed from the advertised surface but
# kept importable as module attributes for back-compat (the rpc-tranche mechanism —
# see scripts/api-compat-allowlist.json). The freeze test below locks that promise
# so a future change can't silently drop importability or re-bless a name.
#
# 23 entries came from PR-1 (#1592). ``KEEP_ACCOUNT`` and ``LoginWriteOutcome`` were
# added when ``__all__`` was pinned to the documented surface: they were blessed
# additively alongside the storage-writer refactor but no caller — first-party or
# test — ever imported them. ``drop_legacy_account_key`` joined here when the
# master-token-relocation PR-0 removed its last two ``src/`` call sites — the CLI
# login writers now rely on ``replace_from_login`` scrubbing the legacy key
# internally instead of calling this facade helper afterward.
# ``exchange_master_token`` / ``mint_cookies`` / ``persist_minted_jar`` /
# ``write_master_token`` / ``generate_android_id`` joined here in PR-2 of the
# same follow-up: the CLI now calls the coarse transaction ops
# (``master_token_bootstrap`` / ``master_token_remint`` / etc., tracked in
# ``AUTH_CROSS_BOUNDARY_NAMES`` above) instead of assembling these primitives
# itself, but they stay importable for the documented low-level recipe
# (docs/python-api.md) and any external caller that already depends on them.
# All entries here are covered rather than in ``AUTH_CROSS_BOUNDARY_NAMES``
# (which requires a live first-party importer).
#
# ``get_account_email_for_storage`` / ``get_authuser_for_storage`` /
# ``clear_account_metadata`` / ``extract_email_from_html`` / ``write_account_metadata``
# are the exception to the "de-blessed from __all__" history above: they were
# never public API, only cross-boundary. Their last cli/_app facade importers
# switched to coarse ops (``resolve_account_identity`` for the first two,
# ``repair_account_metadata_from_playwright_storage`` for the latter three —
# auth cross-boundary ledger shrink, follow-up to #2103), but all five names
# stay frozen on ``notebooklm.auth`` for first-party test callers
# (``tests/_guardrails/test_public_surface_manifest.py``'s
# ``_AUTH_FIRST_PARTY_COMPATIBILITY_NAMES``), so they land here rather than
# disappearing from the facade entirely.
#
# ``read_account_metadata_from_storage_state`` joined the same follow-up for a
# different reason: it is not in ``_AUTH_FIRST_PARTY_COMPATIBILITY_NAMES``, but
# ``scripts/api-compat-allowlist.json`` explicitly records it as a "de-advertisement,
# not removal" that stays importable — dropping the facade alias entirely (as an
# earlier revision of this PR did) silently broke that documented promise with no
# guardrail catching it, since the audit script only diffs ``__all__`` membership.
_AUTH_DEBLESSED_KEEP_IMPORTABLE: list[str] = [
    "AccountRecord",
    "advance_cookie_snapshot_after_save",
    "ALLOWED_COOKIE_DOMAINS",
    "authuser_query",
    "clear_account_metadata",
    "CLEAR_ACCOUNT",
    "CookieSaveResult",
    "CookieSnapshot",
    "CookieSnapshotKey",
    "CookieSnapshotValue",
    "drop_legacy_account_key",
    "exchange_master_token",
    "extract_csrf_from_html",
    "extract_email_from_html",
    "extract_session_id_from_html",
    "extract_wiz_field",
    "fetch_tokens",
    "format_authuser_value",
    "generate_android_id",
    "get_account_email_for_storage",
    "get_authuser_for_storage",
    "KEEP_ACCOUNT",
    "KEEPALIVE_ROTATE_URL",
    "load_auth_from_storage",
    "load_httpx_cookies",
    "LoginWriteOutcome",
    "MINIMUM_REQUIRED_COOKIES",
    "mint_cookies",
    "normalize_cookie_map",
    "NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV",
    "NOTEBOOKLM_REFRESH_CMD_ENV",
    "NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV",
    "persist_minted_jar",
    "read_account_metadata_from_storage_state",
    "recover_psidts_in_memory",
    "replace_from_login",
    "save_cookies_to_storage",
    "snapshot_cookie_jar",
    "write_account_metadata",
    "write_master_token",
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
    """``notebooklm.auth.__all__`` matches the documented public surface.

    If you intentionally add or remove a public name from ``auth.py``, update
    ``EXPECTED_AUTH_ALL`` above AND docs/stability.md in the same PR — the two
    are cross-checked by
    ``test_auth_all_matches_documented_public_surface``.

    Needing a name across the CLI/_app boundary is NOT a reason to add it here;
    add it to ``AUTH_CROSS_BOUNDARY_NAMES`` instead, which grants importability
    without publishing it.
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
def _collect_external_imports_by_root() -> dict[tuple[str, str], frozenset[str]]:
    """Return public names imported from ``notebooklm.<module>``, keyed ``(root, module)``.

    The auth/client audit tests both walk the same tree. Cache the scan so
    adding a second audited module does not double the unit-suite cost.

    Results stay split per scan root because the two questions differ: "is this
    name imported anywhere (incl. tests/docs)?" gates the declared-somewhere
    audit, while "does ``src/`` still import it?" is what makes a cross-boundary
    entry live rather than dead.
    """
    imports: dict[tuple[str, str], set[str]] = {}
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
                # SIBLING module (e.g. ``_web/transport/auth.py``) is NOT misattributed
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
                    imports.setdefault((root, module_basename), set()).add(alias.name)

    # Phase 13 app operations use the public facade as a qualified module
    # binding (``from .. import auth; auth.Name``). Reuse the caller ledger's
    # binding-aware, control-flow-aware detector so this liveness audit has one
    # truth source for shadowing rather than a weaker parallel scanner.
    qualified = _auth_compat._alias_callers(src_root / "notebooklm")
    imports.setdefault(("src", "auth"), set()).update(
        name for name in qualified if not name.startswith("_")
    )
    return {key: frozenset(names) for key, names in imports.items()}


def _collect_auth_imports_under(root: str) -> set[str]:
    """Return public ``notebooklm.auth`` names imported by files under ``root``."""
    return set(_collect_external_imports_by_root().get((root, "auth"), frozenset()))


def _collect_external_imports(module_basename: str) -> set[str]:
    """Return the set of public names imported from ``notebooklm.<module_basename>``.

    Unions every scan root of the cached repo-wide scan in
    :func:`_collect_external_imports_by_root`, which walks every ``.py``
    file under ``src/``, ``tests/``, ``docs/`` once per process.
    """
    by_root = _collect_external_imports_by_root()
    return {
        name
        for (_root, module), names in by_root.items()
        if module == module_basename
        for name in names
    }


def test_auth_all_matches_external_imports_audit() -> None:
    """Every name imported from ``notebooklm.auth`` is declared somewhere.

    This is the dynamic counterpart to ``test_auth_module_has_expected_all``.
    Where the former pins to a snapshotted list, this one scans the live
    codebase (``src/``, ``tests/``, ``docs/``) and fails if a name is imported
    from ``notebooklm.auth`` without appearing in either declared list.

    Note the deliberate asymmetry with the pre-#1592-era rule: an imported name
    satisfies this audit via ``AUTH_CROSS_BOUNDARY_NAMES`` *or* ``__all__``. A
    new CLI/_app import therefore no longer forces a name onto the public
    surface — publishing stays an explicit, docs-backed decision.
    """
    declared = set(auth_module.__all__) | set(AUTH_CROSS_BOUNDARY_NAMES)
    declared |= set(_AUTH_DEBLESSED_KEEP_IMPORTABLE)
    actually_imported = _collect_external_imports("auth")
    missing = actually_imported - declared
    assert not missing, (
        "Names imported from notebooklm.auth but declared in neither list: "
        f"{sorted(missing)}\n"
        "First-party (cli/ or _app/) need? Add to AUTH_CROSS_BOUNDARY_NAMES.\n"
        "Genuinely public API? Add to EXPECTED_AUTH_ALL, auth.__all__, and "
        "docs/stability.md together."
    )


def test_auth_all_matches_documented_public_surface() -> None:
    """``auth.__all__`` == docs/stability.md == the public-import manifest.

    The forcing function that kept ``auth.__all__`` honest used to be "is it
    imported somewhere?", which is a question about first-party convenience, not
    about what was promised to users. This test replaces it with the right
    question: the published surface must equal what the docs promise.

    Three sources are cross-checked so none can drift alone:

    1. ``notebooklm.auth.__all__`` (the runtime surface),
    2. every ``notebooklm.auth.<Name>`` reference in docs/stability.md (the
       user-facing promise — the ``notebooklm.auth.*`` internals line does not
       match, ``*`` not being an identifier),
    3. ``_DOCUMENTED_PUBLIC_IMPORTS["notebooklm.auth"]`` (the machine-readable
       manifest, shared with ``test_public_surface_manifest.py``).
    """
    declared = set(auth_module.__all__)

    doc_path = _REPO_ROOT / "docs" / "stability.md"
    documented = set(
        re.findall(r"notebooklm\.auth\.([A-Za-z_][A-Za-z0-9_]*)", doc_path.read_text())
    )
    assert declared == documented, (
        "notebooklm.auth public surface drifted from docs/stability.md.\n"
        f"  in __all__ but undocumented: {sorted(declared - documented)}\n"
        f"  documented but not in __all__: {sorted(documented - declared)}\n"
        "Publishing a name means updating both — that is the point of this gate.\n"
        "Note the scan is doc-wide: writing a bare `notebooklm.auth.some_internal` "
        "anywhere in stability.md reads as a promise. Refer to internals as "
        "`notebooklm._auth.<sub>.some_internal` in prose instead."
    )

    manifest = set(_DOCUMENTED_PUBLIC_IMPORTS["notebooklm.auth"])
    assert declared == manifest, (
        "notebooklm.auth public surface drifted from the documented public-import "
        "manifest in tests/_guardrails/test_public_surface_manifest.py.\n"
        f"  in __all__ but not in the manifest: {sorted(declared - manifest)}\n"
        f"  in the manifest but not in __all__: {sorted(manifest - declared)}"
    )


def test_auth_cross_boundary_names_stay_importable_and_unblessed() -> None:
    """First-party boundary names resolve on the facade but are not published.

    Both halves matter: dropping importability breaks ``cli/``/``_app/`` (which
    the CLI boundary lint forbids from reaching ``notebooklm._auth`` directly),
    and re-blessing a name silently re-publishes it as API.
    """
    assert len(AUTH_CROSS_BOUNDARY_NAMES) == len(set(AUTH_CROSS_BOUNDARY_NAMES)), (
        "AUTH_CROSS_BOUNDARY_NAMES must not contain duplicates: "
        f"{sorted({n for n in AUTH_CROSS_BOUNDARY_NAMES if AUTH_CROSS_BOUNDARY_NAMES.count(n) > 1})}"
    )
    private = [name for name in AUTH_CROSS_BOUNDARY_NAMES if name.startswith("_")]
    assert not private, (
        "AUTH_CROSS_BOUNDARY_NAMES tracks the non-underscore facade surface; the "
        f"CLI boundary lint already forbids private-name imports: {private}"
    )

    sentinel = object()
    declared = set(auth_module.__all__)
    for name in AUTH_CROSS_BOUNDARY_NAMES:
        assert getattr(auth_module, name, sentinel) is not sentinel, (
            f"cross-boundary {name!r} must stay importable from notebooklm.auth"
        )
        assert name not in declared, (
            f"cross-boundary {name!r} is in auth.__all__ — it is first-party-only. "
            "If it is genuinely public API, move it to EXPECTED_AUTH_ALL and "
            "document it in docs/stability.md."
        )


def test_auth_cross_boundary_names_have_first_party_importers(tmp_path: pathlib.Path) -> None:
    """No dead entries: every cross-boundary name is really imported by ``src/``.

    The list is a cost, not a catalogue — each entry pins a name the auth layer
    cannot move freely. An entry whose last caller went away should be deleted,
    not carried forever (this is how the surface silently re-inflates).
    """
    imported_by_src = _collect_auth_imports_under("src")
    unused = [name for name in AUTH_CROSS_BOUNDARY_NAMES if name not in imported_by_src]
    assert not unused, (
        "AUTH_CROSS_BOUNDARY_NAMES entries with no importer under src/: "
        f"{sorted(unused)}\n"
        "Drop them from the list (they stay importable via "
        "_AUTH_DEBLESSED_KEEP_IMPORTABLE only if an external caller still needs them)."
    )

    path = tmp_path / "src" / "notebooklm" / "feature" / "synthetic.py"
    path.parent.mkdir(parents=True)

    def qualified_attributes(source: str) -> set[str]:
        old_root = _auth_compat.REPO_ROOT
        try:
            _auth_compat.REPO_ROOT = tmp_path
            collector = _auth_compat._AliasUseCollector(path)
            collector.visit(ast.parse(source))
            return collector.attributes
        finally:
            _auth_compat.REPO_ROOT = old_root

    recognized = qualified_attributes(
        "import notebooklm.auth as module\n"
        "from notebooklm import auth as absolute\n"
        "from .. import auth as relative\n"
        "module.AbsoluteModule\n"
        "@absolute.Decorator\n"
        "def outer(value: relative.Parameter = absolute.Default) -> relative.Return:\n"
        "    if value:\n"
        "        relative.ControlFlow\n"
        "    def inner():\n"
        "        return absolute.Closure\n"
        "    return inner\n"
    )
    assert recognized == {
        "AbsoluteModule",
        "Closure",
        "ControlFlow",
        "Decorator",
        "Default",
        "Parameter",
        "Return",
    }

    shadowed = qualified_attributes(
        "from .. import auth as facade\n"
        "def parameter(facade):\n    return facade.ParameterShadow\n"
        "def assigned():\n    facade = object()\n    return facade.AssignmentShadow\n"
        "def imported():\n    import math as facade\n    return facade.ImportShadow\n"
        "def local_scope():\n    facade.LocalShadow\n    facade = object()\n"
        "[facade.ComprehensionShadow for facade in values]\n"
        "match value:\n    case facade:\n        facade.PatternShadow\n"
        "try:\n    operation()\nexcept Exception as facade:\n    facade.ExceptionShadow\n"
        "(facade := object()).WalrusShadow\n"
    )
    assert shadowed == set()

    dynamic = qualified_attributes(
        "from .. import auth as facade\n"
        "getattr(facade, 'Getattr')\n"
        "vars(facade)['Vars']\n"
        "globals()['facade'].Globals\n"
        "facade.__dict__['Dict']\n"
        "facade['Subscript']\n"
        "facade['Computed' + 'Key']\n"
    )
    assert dynamic == {"__dict__"}


def test_auth_deblessed_names_stay_importable_but_unblessed() -> None:
    """De-blessed auth names stay importable for back-compat but are absent from
    ``__all__`` — the rpc-tranche freeze guard, applied to auth.

    23 from PR-1 (#1592), plus ``KEEP_ACCOUNT`` / ``LoginWriteOutcome``, which had
    no importer at all when ``__all__`` was pinned to the documented surface, plus
    ``drop_legacy_account_key``, whose last ``src/`` importers were removed by the
    master-token-relocation PR-0 (issue #2103), plus 5 master-token minting
    primitives (``exchange_master_token`` / ``mint_cookies`` /
    ``persist_minted_jar`` / ``write_master_token`` / ``generate_android_id``)
    whose last ``src/`` importers were removed by the same relocation's PR-2, plus
    ``get_account_email_for_storage`` / ``get_authuser_for_storage`` / ``clear_account_metadata``
    / ``extract_email_from_html`` / ``write_account_metadata``, whose last cli/_app
    facade importers switched to ``resolve_account_identity`` /
    ``repair_account_metadata_from_playwright_storage`` in the auth
    cross-boundary ledger shrink (follow-up to #2103), plus
    ``read_account_metadata_from_storage_state``, whose facade alias the same
    follow-up must keep per ``scripts/api-compat-allowlist.json``'s explicit
    retained-and-importable promise, plus ``AccountRecord``, ``CLEAR_ACCOUNT``,
    and ``replace_from_login``, whose last first-party callers migrated to the
    native primitive-shaped login operation in B3.
    """
    assert len(_AUTH_DEBLESSED_KEEP_IMPORTABLE) == 40
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
