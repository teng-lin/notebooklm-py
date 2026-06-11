"""Assert doc references into ``src/notebooklm`` stay fresh.

Sibling to ``scripts/check_claude_md_freshness.py`` (which guards the
``### Repository Structure`` map in ``docs/architecture.md``). This gate turns
the repo's "enforce, don't document" principle onto the *rest* of the docs:
after the #1328 refactor promoted flat ``_*.py`` modules into subpackages
(``_chat.py`` -> ``_chat/api.py``, ``_runtime_lifecycle.py`` ->
``_runtime/lifecycle.py``, ...), ~25 stale flat references survived across the
live docs because a hand audit and a scoped doc-sync PR both missed them. A gate
is the only thing that makes that class of drift un-recurrable.

Two checks, both read the docs and resolve targets against the repo:

**(1) Broken local-link check (strict, no allowlist).** Across ALL
``docs/**/*.md`` + root ``*.md``, every markdown link ``[text](target)`` whose
``target`` is a *relative path into* ``src/notebooklm/`` MUST resolve to an
existing file. A broken link into the package is never intentional, even in an
ADR or refactor-history doc.

**(2) Inline module-ref check (LIVE docs only, allowlisted).** In the *live*
docs (``docs/**/*.md`` + root ``*.md`` MINUS the historical-prose docs —
``docs/adr/**``, ``docs/refactor-history.md``, and ``CHANGELOG.md`` — which
intentionally name historical modules in prose), every inline code span
```` `<ref>` ```` whose ``<ref>`` matches a ``src/notebooklm`` module shape MUST
resolve to ``src/notebooklm/<ref>``. The rare intentional historical mention in
a live doc is carried in :data:`_ALLOWLIST` (shrink-only). CLAUDE.md is excluded
because it is an agent-instruction file, not part of the live docs set.

The detector core (:func:`find_violations`) is pure and IO-free — it takes the
already-read doc text plus a ``resolver(ref) -> bool`` — so the public test and
these CLI self-checks exercise the same logic, exactly like
``tests/_guardrails/test_v080_deprecation_coverage.py``.

Usage:
    python scripts/check_docs_module_refs.py
    python scripts/check_docs_module_refs.py --repo-root path/to/repo

Exit codes:
    0  All doc module references are fresh.
    1  One or more broken links or dead inline refs were found.
    2  Argument error / repo root or docs tree not found.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# The package every reference resolves against.
_PACKAGE_RELDIR = "src/notebooklm"

# A ``src/notebooklm`` module shape: a lowercase python module (top-level name is
# either a private ``_foo`` name or one of the known public module names), with
# optional subdirectories, ending in ``.py``. ``test_*.py`` / ``conftest.py`` and
# anything under ``tests/`` / ``scripts/`` are excluded by the caller, not here.
#
# Scope: covers the ``_*`` private modules, the known top-level public modules,
# the ``notebooklm_cli`` entry point, and the ``rpc/`` + ``cli/`` subpackages
# (broadened per review so an inline ``rpc/types.py`` / ``cli/session_cmd.py``
# ref is resolved, not silently skipped). The test
# ``test_test_and_script_refs_are_not_module_shaped`` pins this scope.
_MODULE_REF_RE = re.compile(
    r"^(_[a-z0-9_]+|client|auth|exceptions|config|io|log|migration|paths|research"
    r"|types|urls|utils|artifacts|notebooklm_cli|rpc|cli)([/][a-z0-9_]+)*\.py$"
)

# Inline code spans: ``\`...\```. Non-greedy so adjacent spans on one line are
# matched separately.
_INLINE_SPAN_RE = re.compile(r"`([^`]+)`")

# Markdown links: ``](target)``. The target may carry a ``#anchor`` we strip.
_LINK_TARGET_RE = re.compile(r"\]\(([^)]+)\)")


# Allowlist for the inline-ref check (check 2): live-doc inline mentions of a
# module that no longer exists at that path but is named intentionally (a
# historical / deliberately-deleted module, or a placeholder in a how-to). Keyed
# by ``"<doc-relpath>:<ref>"`` -> reason. This set is SHRINK-ONLY: a test asserts
# every entry is still genuinely needed (the doc still mentions it AND the ref
# still does not resolve), so a stale allowlist entry fails the gate. Do NOT add
# an entry to silence a *real* stale path — fix the path instead.
_ALLOWLIST: dict[str, str] = {
    # `_core.py` was the runtime compatibility shim deleted in v0.5.0. These live
    # docs intentionally name it to explain why `CORE_LOGGER_NAME` still reads
    # "notebooklm._core" (a logging compatibility contract), NOT to point at a
    # live module. See docs/development.md "Logger namespace compatibility".
    "docs/architecture.md:_core.py": "historical: deleted-in-v0.5.0 compat shim, named to explain CORE_LOGGER_NAME",
    "docs/configuration.md:_core.py": "historical: deleted-in-v0.5.0 compat shim, named to explain CORE_LOGGER_NAME",
    "docs/development.md:_core.py": "historical: deleted-in-v0.5.0 compat shim, named to explain CORE_LOGGER_NAME",
    # `_newfeature.py` is a placeholder in the "adding a new API class" how-to
    # ("Create `_newfeature.py` ..."), not a real module reference.
    "docs/development.md:_newfeature.py": "placeholder: example module name in the add-an-API-class how-to",
}


@dataclass(frozen=True)
class Violation:
    """One dead doc reference, ``kind`` is ``"link"`` or ``"inline"``."""

    kind: str
    doc: str  # POSIX-relative path of the doc, for stable messages
    line: int
    target: str  # the dead link target or inline ref


def _is_local_package_link(target: str) -> bool:
    """True for a *relative* markdown link target into ``src/notebooklm/``.

    Absolute URLs (``http://...``), in-page anchors (``#...``), and links that do
    not descend into the package are out of scope — only relative paths whose
    resolved form lands inside ``src/notebooklm/`` are checked. The substring test
    is intentional: doc-relative targets reach the package via ``../`` prefixes
    (``../src/notebooklm/...``, ``../../src/notebooklm/...``).
    """
    if target.startswith(("http://", "https://", "mailto:", "#")):
        return False
    return f"{_PACKAGE_RELDIR}/" in target


def _is_module_shaped(ref: str) -> bool:
    """True for an inline ref that looks like an in-package module path.

    Excludes ``test_*.py`` / ``conftest.py`` and anything under ``tests/`` or
    ``scripts/`` (those are not ``src/notebooklm`` modules even though they match
    the ``.py`` shape).
    """
    if "/" in ref:
        head = ref.split("/", 1)[0]
        if head in {"tests", "scripts"}:
            return False
    leaf = ref.rsplit("/", 1)[-1]
    if leaf.startswith("test_") or leaf == "conftest.py":
        return False
    return bool(_MODULE_REF_RE.match(ref))


def find_violations(
    doc_relpath: str,
    text: str,
    *,
    resolver: Callable[[str], bool],
    is_live: bool,
    allowlist: dict[str, str],
) -> list[Violation]:
    """Return every dead reference in one doc. Pure: no filesystem access.

    ``resolver(ref) -> bool`` answers "does ``src/notebooklm/<ref>`` exist?" for
    the inline check, and ``resolver(target) -> bool`` answers "does this
    doc-relative link target resolve?" for the link check — the CLI passes a
    filesystem-backed resolver, the tests pass a dict-backed stub.

    * Link check (always, every doc): a relative link into ``src/notebooklm/``
      that does not resolve is a violation.
    * Inline check (live docs only): a module-shaped inline span that does not
      resolve to ``src/notebooklm/<ref>`` is a violation, unless an
      ``"<doc>:<ref>"`` allowlist entry covers it.
    """
    violations: list[Violation] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in _LINK_TARGET_RE.finditer(line):
            target = match.group(1).split("#", 1)[0].strip()
            if not target or not _is_local_package_link(target):
                continue
            if not resolver(target):
                violations.append(Violation("link", doc_relpath, lineno, target))

        if not is_live:
            continue
        for match in _INLINE_SPAN_RE.finditer(line):
            ref = match.group(1)
            if not _is_module_shaped(ref):
                continue
            if resolver(ref):
                continue
            if f"{doc_relpath}:{ref}" in allowlist:
                continue
            violations.append(Violation("inline", doc_relpath, lineno, ref))
    return violations


# --- Filesystem helpers (I/O at the edge) -------------------------------------


def _is_historical_prose(rel: str) -> bool:
    """True for docs that intentionally name historical/old module paths in prose.

    These are frozen-or-by-design historical records — ADRs, the refactor history,
    and the CHANGELOG (whose entries describe edits to modules *as they were named
    at the time*, e.g. ``cli/note.py`` for a fix that predates the ``_cmd`` rename).
    The inline module-ref check skips them; the broken-link check still applies (a
    dead *link* into the package is never intentional, even in history).
    """
    return rel.startswith("docs/adr/") or rel == "docs/refactor-history.md" or rel == "CHANGELOG.md"


def _iter_docs(repo_root: Path):
    """Yield ``(path, relpath, is_live)`` for every doc the gate inspects.

    Docs = every ``docs/**/*.md`` plus every root-level ``*.md``. CLAUDE.md is
    excluded because it is an agent-instruction file, not part of the live docs
    set. ``is_live`` is False for the historical-prose docs (see
    :func:`_is_historical_prose`) so the inline check skips them while the link
    check still applies.
    """
    docs_dir = repo_root / "docs"
    md_paths: list[Path] = []
    if docs_dir.is_dir():
        md_paths.extend(sorted(docs_dir.rglob("*.md")))
    md_paths.extend(sorted(repo_root.glob("*.md")))

    for path in md_paths:
        rel = path.relative_to(repo_root).as_posix()
        if rel == "CLAUDE.md":
            continue
        yield path, rel, not _is_historical_prose(rel)


def _make_resolver(repo_root: Path, doc_path: Path) -> Callable[[str], bool]:
    """Resolver closure for one doc.

    For a module-shaped inline ref it checks ``src/notebooklm/<ref>``; for a
    doc-relative link target it resolves the target against the doc's directory.
    Both resolve through the same callable because the link target always
    contains ``src/notebooklm/`` (so it is never mistaken for a bare ref) and the
    inline ref never contains a path separator prefix like ``../``.
    """
    package_root = repo_root / _PACKAGE_RELDIR

    def resolver(ref_or_target: str) -> bool:
        if _is_local_package_link(ref_or_target):
            return (doc_path.parent / ref_or_target).resolve().exists()
        return (package_root / ref_or_target).exists()

    return resolver


def _unused_allowlist_entries(
    repo_root: Path, allowlist: dict[str, str], *, strict_missing: bool = False
) -> list[str]:
    """Return allowlist keys that are no longer justified (shrink-only guard).

    An entry ``"<doc>:<ref>"`` is justified iff the doc still exists, still
    mentions ``<ref>`` as a module-shaped inline span, and that ``<ref>`` still
    does NOT resolve under ``src/notebooklm/``. The moment any of those stops
    being true the entry is dead weight and must be removed.

    ``strict_missing`` controls how a *missing* doc is treated. The default
    (False) skips an entry whose doc does not exist under ``repo_root`` — this
    keeps :func:`main` repo-root-agnostic, since the module-level allowlist keys
    the *real* repo's docs and a caller may point ``--repo-root`` at a synthetic
    tree. With ``strict_missing=True`` a missing doc IS flagged as stale; the
    real-repo allowlist test uses this so a renamed/deleted doc can't leave a
    dangling entry behind.
    """
    package_root = repo_root / _PACKAGE_RELDIR
    unused: list[str] = []
    for key in allowlist:
        doc_rel, _, ref = key.partition(":")
        doc_path = repo_root / doc_rel
        if not doc_path.is_file():
            if strict_missing:
                unused.append(key)
            continue
        text = doc_path.read_text(encoding="utf-8")
        mentioned = any(
            ref == span and _is_module_shaped(span)
            for line in text.splitlines()
            for span in _INLINE_SPAN_RE.findall(line)
        )
        resolves = (package_root / ref).exists()
        if not mentioned or resolves:
            unused.append(key)
    return unused


def collect_violations(repo_root: Path) -> list[Violation]:
    """Read every doc and return all violations (filesystem-backed)."""
    violations: list[Violation] = []
    for path, rel, is_live in _iter_docs(repo_root):
        text = path.read_text(encoding="utf-8")
        resolver = _make_resolver(repo_root, path)
        violations.extend(
            find_violations(
                rel,
                text,
                resolver=resolver,
                is_live=is_live,
                allowlist=_ALLOWLIST,
            )
        )
    return violations


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-root", default=".")
    args = ap.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    if not (repo_root / "docs").is_dir():
        print(f"docs/ directory not found under repo root: {repo_root}", file=sys.stderr)
        return 2

    unused = _unused_allowlist_entries(repo_root, _ALLOWLIST)
    violations = collect_violations(repo_root)

    if violations or unused:
        broken_links = [v for v in violations if v.kind == "link"]
        dead_inline = [v for v in violations if v.kind == "inline"]
        if broken_links:
            print("Broken links into src/notebooklm/:", file=sys.stderr)
            for v in broken_links:
                print(f"  {v.doc}:{v.line} -> {v.target}", file=sys.stderr)
        if dead_inline:
            print("Dead inline module refs in live docs:", file=sys.stderr)
            for v in dead_inline:
                print(f"  {v.doc}:{v.line} -> `{v.target}`", file=sys.stderr)
        if unused:
            print("Stale _ALLOWLIST entries (shrink-only; remove them):", file=sys.stderr)
            for key in unused:
                print(f"  {key}", file=sys.stderr)
        return 1

    print(
        "OK: all doc links into src/notebooklm resolve; "
        "all live-doc inline module refs are fresh or allowlisted"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
