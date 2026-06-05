"""Self-tests + the real gate for ``scripts/check_docs_module_refs.py``.

Mirrors the sibling ``tests/unit/test_claude_md_freshness.py`` (CLAUDE.md map)
and the pure-detector idiom of ``tests/_guardrails/test_v080_deprecation_coverage.py``:
the detector core :func:`find_violations` is IO-free, so the synthetic
self-checks below feed crafted doc text + a dict-backed ``resolver`` while the
real-gate test (:func:`test_real_docs_have_no_violations`) drives the same code
over the live ``docs/`` tree.

The self-checks are non-vacuous and bidirectional: a planted dead LINK is
reported, a planted dead inline ref is reported, the correct (resolving) inline
ref is clean, ADR/refactor-history docs skip the inline check but NOT the link
check, an allowlisted dead inline ref is clean, and a bogus allowlist entry is
caught by the shrink-only guard.
"""

from __future__ import annotations

import os
import sys

import pytest

# Add project root to sys.path so we can import scripts (matches the sibling).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from scripts.check_docs_module_refs import (  # noqa: E402
    _ALLOWLIST,
    _is_historical_prose,
    _is_module_shaped,
    _unused_allowlist_entries,
    collect_violations,
    find_violations,
    main,
)

pytestmark = pytest.mark.repo_lint


# A resolver stub: a ref/target resolves iff it is in ``existing``. The CLI uses
# a filesystem-backed resolver; the pure detector only needs this predicate.
def _stub_resolver(existing: set[str]):
    return lambda ref_or_target: ref_or_target in existing


# --- find_violations: link check (every doc, no allowlist) --------------------


def test_dead_link_into_package_is_reported() -> None:
    text = "See [the helper](../src/notebooklm/_gone.py) for details.\n"
    violations = find_violations(
        "docs/foo.md",
        text,
        resolver=_stub_resolver(set()),
        is_live=True,
        allowlist={},
    )
    assert [(v.kind, v.target) for v in violations] == [("link", "../src/notebooklm/_gone.py")]


def test_resolving_link_into_package_is_clean() -> None:
    target = "../src/notebooklm/_runtime/lifecycle.py"
    text = f"See [the helper]({target}).\n"
    violations = find_violations(
        "docs/foo.md",
        text,
        resolver=_stub_resolver({target}),
        is_live=True,
        allowlist={},
    )
    assert violations == []


def test_external_and_anchor_links_are_ignored() -> None:
    text = (
        "[home](https://example.com/src/notebooklm/x.py) [anchor](#section) [other](../README.md)\n"
    )
    violations = find_violations(
        "docs/foo.md",
        text,
        resolver=_stub_resolver(set()),
        is_live=True,
        allowlist={},
    )
    # None of these are *relative paths into src/notebooklm/*, so none are checked.
    assert violations == []


# --- find_violations: inline check (live docs only, allowlisted) --------------


def test_dead_inline_flat_ref_is_reported_and_subpackage_form_is_clean() -> None:
    dead = "The runtime lives in `_runtime_lifecycle.py` today.\n"
    flagged = find_violations(
        "docs/architecture.md",
        dead,
        resolver=_stub_resolver(set()),
        is_live=True,
        allowlist={},
    )
    assert [(v.kind, v.target) for v in flagged] == [("inline", "_runtime_lifecycle.py")]

    # The correct subpackage form resolves -> clean.
    fixed = "The runtime lives in `_runtime/lifecycle.py` today.\n"
    clean = find_violations(
        "docs/architecture.md",
        fixed,
        resolver=_stub_resolver({"_runtime/lifecycle.py"}),
        is_live=True,
        allowlist={},
    )
    assert clean == []


def test_adr_doc_skips_inline_check_but_not_link_check() -> None:
    # An ADR / refactor-history doc names a historical module in prose (inline)
    # AND links to a dead target. The inline ref is NOT reported (is_live=False),
    # but the dead LINK still is.
    text = (
        "Historically `_runtime_lifecycle.py` owned this "
        "([src](../../src/notebooklm/_runtime_lifecycle.py)).\n"
    )
    violations = find_violations(
        "docs/adr/0099-example.md",
        text,
        resolver=_stub_resolver(set()),
        is_live=False,
        allowlist={},
    )
    assert [(v.kind, v.target) for v in violations] == [
        ("link", "../../src/notebooklm/_runtime_lifecycle.py")
    ]


def test_allowlisted_dead_inline_ref_is_clean() -> None:
    text = "The deleted `_core.py` shim explains the logger name.\n"
    allowlist = {"docs/development.md:_core.py": "historical: deleted compat shim"}
    violations = find_violations(
        "docs/development.md",
        text,
        resolver=_stub_resolver(set()),
        is_live=True,
        allowlist=allowlist,
    )
    assert violations == []

    # The same dead ref in a *different* doc (not keyed) is still reported.
    elsewhere = find_violations(
        "docs/architecture.md",
        text,
        resolver=_stub_resolver(set()),
        is_live=True,
        allowlist=allowlist,
    )
    assert [(v.kind, v.target) for v in elsewhere] == [("inline", "_core.py")]


def test_test_and_script_refs_are_not_module_shaped() -> None:
    # The inline check excludes test_*.py / conftest.py and tests/ + scripts/.
    assert not _is_module_shaped("test_client.py")
    assert not _is_module_shaped("conftest.py")
    assert not _is_module_shaped("tests/unit/test_x.py")
    assert not _is_module_shaped("scripts/check_docs_module_refs.py")
    # ...but real package modules (flat and subpackage) are, including the
    # rpc/ + cli/ subpackages and the notebooklm_cli entry point (broadened
    # per review so their inline refs are resolved, not silently skipped).
    assert _is_module_shaped("_runtime_lifecycle.py")
    assert _is_module_shaped("_runtime/lifecycle.py")
    assert _is_module_shaped("client.py")
    assert _is_module_shaped("types.py")
    assert _is_module_shaped("rpc/types.py")
    assert _is_module_shaped("cli/session_cmd.py")
    assert _is_module_shaped("cli/services/generate.py")
    assert _is_module_shaped("notebooklm_cli.py")


def test_historical_prose_docs_are_classified() -> None:
    # ADRs, refactor-history, and the CHANGELOG are historical-prose (inline
    # check skipped); ordinary live docs are not.
    assert _is_historical_prose("docs/adr/0014-x.md")
    assert _is_historical_prose("docs/refactor-history.md")
    assert _is_historical_prose("CHANGELOG.md")
    assert not _is_historical_prose("docs/architecture.md")
    assert not _is_historical_prose("README.md")


def test_changelog_skips_inline_check_but_enforces_links(tmp_path) -> None:
    # CHANGELOG entries name modules as they were at the time (e.g. cli/note.py
    # pre-_cmd-rename); the inline check must skip them, but a dead LINK into the
    # package is still reported.
    _write(tmp_path / "src/notebooklm/__init__.py", "")
    _write(tmp_path / "src/notebooklm/cli/note_cmd.py", "")
    (tmp_path / "docs").mkdir()  # main() requires a docs/ tree to exist
    # Historical inline ref to a now-renamed module -> allowed.
    _write(tmp_path / "CHANGELOG.md", "Fixed `cli/note.py` exit codes.\n")
    assert main(["--repo-root", str(tmp_path)]) == 0

    # ...but a dead link into the package in the CHANGELOG still fails.
    _write(tmp_path / "CHANGELOG.md", "See [src](src/notebooklm/cli/note.py).\n")
    assert main(["--repo-root", str(tmp_path)]) == 1


def test_inline_ref_to_tests_path_is_not_flagged() -> None:
    # A live doc may legitimately cite a test file inline; it must not be
    # checked against src/notebooklm/.
    text = "Pinned in `tests/unit/test_row_adapters.py`.\n"
    violations = find_violations(
        "docs/architecture.md",
        text,
        resolver=_stub_resolver(set()),
        is_live=True,
        allowlist={},
    )
    assert violations == []


# --- Shrink-only allowlist guard ----------------------------------------------


def test_unused_allowlist_entry_for_missing_doc_is_flagged(tmp_path) -> None:
    # An allowlist entry whose doc does not exist is dead weight under
    # strict_missing (the real-repo guard); under the default it is skipped so
    # main() stays repo-root-agnostic.
    (tmp_path / "docs").mkdir()
    entry = {"docs/ghost.md:_core.py": "stale entry, doc is gone"}
    assert _unused_allowlist_entries(tmp_path, entry, strict_missing=True) == [
        "docs/ghost.md:_core.py"
    ]
    assert _unused_allowlist_entries(tmp_path, entry) == []


def test_unused_allowlist_entry_for_now_resolving_ref_is_flagged(tmp_path) -> None:
    # If the ref now resolves under src/notebooklm/, the allowlist entry is
    # obsolete — the gate should force its removal even though the doc still
    # mentions it.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("We use `_now_real.py` here.\n", encoding="utf-8")
    pkg = tmp_path / "src" / "notebooklm"
    pkg.mkdir(parents=True)
    (pkg / "_now_real.py").touch()

    unused = _unused_allowlist_entries(
        tmp_path, {"docs/guide.md:_now_real.py": "was missing, now exists"}
    )
    assert unused == ["docs/guide.md:_now_real.py"]


def test_unused_allowlist_entry_for_unmentioned_ref_is_flagged(tmp_path) -> None:
    # The doc no longer mentions the ref at all -> entry is dead weight.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("No module here.\n", encoding="utf-8")
    (tmp_path / "src" / "notebooklm").mkdir(parents=True)

    unused = _unused_allowlist_entries(
        tmp_path, {"docs/guide.md:_gone.py": "doc dropped the mention"}
    )
    assert unused == ["docs/guide.md:_gone.py"]


def test_genuinely_needed_allowlist_entry_is_not_flagged(tmp_path) -> None:
    # Doc exists, mentions the ref inline, and the ref does NOT resolve -> kept.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("The deleted `_core.py` shim.\n", encoding="utf-8")
    (tmp_path / "src" / "notebooklm").mkdir(parents=True)

    unused = _unused_allowlist_entries(
        tmp_path, {"docs/guide.md:_core.py": "historical: deleted compat shim"}
    )
    assert unused == []


# --- main(): end-to-end exit codes on a synthetic repo ------------------------


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_main_fails_on_dead_link(tmp_path, capsys) -> None:
    _write(tmp_path / "src/notebooklm/__init__.py", "")
    _write(
        tmp_path / "docs/foo.md",
        "[dead](../src/notebooklm/_gone.py)\n",
    )
    assert main(["--repo-root", str(tmp_path)]) == 1
    assert "Broken links into src/notebooklm/" in capsys.readouterr().err


def test_main_fails_on_dead_inline_ref(tmp_path, capsys) -> None:
    _write(tmp_path / "src/notebooklm/__init__.py", "")
    _write(tmp_path / "docs/foo.md", "Runtime in `_runtime_lifecycle.py`.\n")
    assert main(["--repo-root", str(tmp_path)]) == 1
    assert "Dead inline module refs in live docs" in capsys.readouterr().err


def test_main_fails_on_stale_allowlist(tmp_path, capsys, monkeypatch) -> None:
    # Completes main()'s exit-code coverage: the third error branch fires when an
    # _ALLOWLIST entry is no longer justified (its doc exists but no longer
    # mentions the ref). Patch the module-level allowlist with a bogus entry.
    import scripts.check_docs_module_refs as mod

    _write(tmp_path / "src/notebooklm/__init__.py", "")
    _write(tmp_path / "docs/foo.md", "No module mention here.\n")
    monkeypatch.setattr(mod, "_ALLOWLIST", {"docs/foo.md:_old.py": "stale: doc dropped it"})

    assert mod.main(["--repo-root", str(tmp_path)]) == 1
    assert "Stale _ALLOWLIST entries" in capsys.readouterr().err


def test_main_succeeds_when_all_refs_resolve(tmp_path) -> None:
    _write(tmp_path / "src/notebooklm/__init__.py", "")
    _write(tmp_path / "src/notebooklm/_runtime/lifecycle.py", "")
    _write(
        tmp_path / "docs/foo.md",
        "Runtime in `_runtime/lifecycle.py` ([src](../src/notebooklm/_runtime/lifecycle.py)).\n",
    )
    assert main(["--repo-root", str(tmp_path)]) == 0


def test_main_returns_2_when_docs_dir_missing(tmp_path) -> None:
    assert main(["--repo-root", str(tmp_path)]) == 2


def test_main_skips_inline_check_in_adr_but_enforces_links(tmp_path) -> None:
    _write(tmp_path / "src/notebooklm/__init__.py", "")
    # ADR names a dead flat module inline (allowed) but its link resolves.
    _write(tmp_path / "src/notebooklm/_runtime/lifecycle.py", "")
    _write(
        tmp_path / "docs/adr/0099-x.md",
        "Historically `_runtime_lifecycle.py` "
        "([src](../../src/notebooklm/_runtime/lifecycle.py)).\n",
    )
    assert main(["--repo-root", str(tmp_path)]) == 0

    # Now make the ADR's link dead -> must fail even though inline is skipped.
    _write(
        tmp_path / "docs/adr/0099-x.md",
        "Historically `_runtime_lifecycle.py` "
        "([src](../../src/notebooklm/_runtime_lifecycle.py)).\n",
    )
    assert main(["--repo-root", str(tmp_path)]) == 1


# --- The real gate ------------------------------------------------------------


def test_real_docs_have_no_violations() -> None:
    """The live ``docs/`` tree has zero dead links / dead inline refs.

    This is the gate that forces the #1328 stale-ref burn-down to stay done.
    """
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    assert main(["--repo-root", repo_root]) == 0


def test_real_allowlist_is_not_stale() -> None:
    """Every ``_ALLOWLIST`` entry is still genuinely needed (shrink-only).

    A live entry must point at a doc that still mentions the ref inline AND a ref
    that still does not resolve under ``src/notebooklm/``. The moment a fix lands,
    the entry must be removed; this catches a bogus/stale entry.
    """
    from pathlib import Path

    repo_root = Path(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
    unused = _unused_allowlist_entries(repo_root, _ALLOWLIST, strict_missing=True)
    assert unused == [], (
        "Stale _ALLOWLIST entries in scripts/check_docs_module_refs.py — the "
        "allowlist is shrink-only. Remove these (the doc no longer needs the "
        f"exemption): {unused}"
    )


def test_collect_violations_over_real_tree_is_empty() -> None:
    """Direct call to the filesystem-backed collector returns no violations."""
    from pathlib import Path

    repo_root = Path(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
    assert collect_violations(repo_root) == []
