"""Meta-lint enforcing the test-monkeypatch policy from ADR-0007.

This test scans every ``.py`` file under ``tests/`` for the forbidden
patterns documented in
``docs/adr/0007-test-monkeypatch-policy.md`` and fails if any file *not*
on the shrinking allowlist contains a match.

Forbidden patterns
------------------

1. **String-target patches into ``notebooklm.*``** — relies on import
   string resolution; silently no-ops when storage relocates.

   .. code-block:: python

       monkeypatch.setattr("notebooklm.auth.get_storage_path", fake)

2. **Object-attribute patches via the imported ``notebooklm`` module** —
   same failure mode, different syntax.

   .. code-block:: python

       monkeypatch.setattr(notebooklm._core, "asyncio", fake_asyncio)

3. **Direct attribute assignment of ``AsyncMock`` to the RPC/
   transport surface** — mutates an instance instead of injecting at
   construction. Caught with a negative-lookbehind so chained forms like
   ``self._client._target.rpc_call = AsyncMock(...)`` are also reported.

   .. code-block:: python

       target.rpc_call = AsyncMock(return_value=None)

4. **``unittest.mock`` string-target patches into private internals** —
   ``mock.patch("notebooklm._private…")`` / ``patch("notebooklm._private…")``
   / ``patch.object(notebooklm._private…, ...)``. Same import-string failure
   mode as (1), but routed through ``unittest.mock`` instead of
   ``monkeypatch`` — the channel where the growth happened and which the lint
   previously missed entirely (issue #1325). Scoped to private
   ``notebooklm._*`` paths: those are the implementation internals the policy
   forbids reaching into, and they silently no-op when the attribute relocates.

   .. code-block:: python

       mock.patch("notebooklm._research.ResearchAPI._poll", fake)
       patch("notebooklm._artifact.downloads.httpx", fake)

5. **Deep-leaf ``unittest.mock`` string-target patches into private
   internals** — same failure mode as (4), but the private component sits
   *behind* one or more public components (``notebooklm.cli.session_cmd._x``)
   instead of being the first component (``notebooklm._x``). Pattern (4)
   anchors on ``notebooklm._`` and is structurally blind to this population
   (issue #1377 follow-up; see also the 2026-06-08 ``#1481`` post-mortem:
   moving a CLI command body to a sibling module silently no-ops these
   patches). The two regexes are deliberately **disjoint** — (4) owns the
   drained-to-zero ``_ALLOWLIST``; (5) owns the baselined
   ``_DEEP_LEAF_ALLOWLIST`` below.

   .. code-block:: python

       patch("notebooklm.cli.session_cmd._sync_server_language_to_config")
       mock.patch("notebooklm.cli.services.login._launch_browser", fake)

6. **Private-attribute ``patch.object`` via a local alias** —
   ``patch.object(alias, "_private_attr")``. The object reference is a real
   Python object (good), but the *attribute name* is a private-layout string:
   the patch couples the test to internal attribute layout exactly like a
   private string target, and ``MagicMock``-shaped targets happily accept any
   attribute name after a rename. Only *full* dunder names (``"__aenter__"``
   etc. — leading and trailing double underscore) are exempt: they are
   Python-protocol surface, not internal layout. ``"__private"``-style
   double-underscore layout names are still flagged.

   .. code-block:: python

       patch.object(helpers, "_render", fake)
       patch.object(sources_api, "_get_or_none", new_callable=AsyncMock)

Allowlist
---------

``_ALLOWLIST`` enumerates the files that *currently* contain at least
one of the forbidden patterns (1)-(4) at PR-1's HEAD. The list shrank as
D1 PR-2 (auth-side migration) and D1 PR-3 (CLI-side migration) retired
offenders; it is now **empty and must stay empty**
(:func:`test_allowlist_stays_empty`).

Patterns (5) and (6) each carry their own *baselined* file-level
allowlist (``_DEEP_LEAF_ALLOWLIST`` / ``_PATCH_OBJECT_PRIVATE_ATTR_ALLOWLIST``)
holding the offenders measured when the pattern landed. Unlike
``_ALLOWLIST`` they start populated and drain *opportunistically* — there
is no stays-empty guard, only the standard stale-entry checks (a cleaned
or deleted file must be removed) and a hard block on new files.

The allowlist is file-level, not site-level (line-number-level), so it
survives rebases and reorderings without spurious churn. See
ADR-0007 "Alternatives considered: per-site allowlist entries".

A few path conventions:

- Paths are stored relative to the repository root and use ``/`` as the
  separator on every platform so the test runs deterministically on
  Linux, macOS, and Windows CI.
- The allowlist enforces *exact* membership: a file on the allowlist
  that has had its offenders cleaned up triggers a failure, signaling
  that the entry should be removed (otherwise the lint silently rots).
"""

from __future__ import annotations

import functools
import re
from collections.abc import Iterable
from pathlib import Path

# ---------------------------------------------------------------------------
# Repo discovery
# ---------------------------------------------------------------------------

_TESTS_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _TESTS_ROOT.parent

# Skip these subtrees:
#  - ``tests/_guardrails``: this file itself contains the regex literals as
#    string data; matching them would be a false positive.
#  - ``tests/_fixtures``: the policy's substrate; tests inside use the
#    factory directly and do not (and must not) demonstrate the forbidden
#    patterns.
#  - ``tests/cassettes``, ``tests/fixtures``: data-only directories
#    containing VCR cassettes and HTML/JSON fixtures, no Python source.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "_guardrails",
        "_fixtures",
        "cassettes",
        "fixtures",
    }
)


# ---------------------------------------------------------------------------
# Forbidden patterns (regex set)
# ---------------------------------------------------------------------------

# (a) ``monkeypatch.setattr("notebooklm.X.Y", ...)`` — string-target form.
_PATTERN_STRING_TARGET = re.compile(r"monkeypatch\.setattr\(\s*[\"']notebooklm\.")

# (b) ``monkeypatch.setattr(notebooklm.X, "attr", ...)`` — attribute-of-imported-module form.
_PATTERN_OBJECT_ATTR = re.compile(r"monkeypatch\.setattr\(\s*notebooklm\.")

# (c) ``<chain>.<core-method> = AsyncMock(...)`` — direct attribute assignment.
#
# The negative-lookbehind ``(?<![\w.])`` ensures the matched chain *starts*
# at a word boundary, so we match the full chain regardless of how deep
# the dotted prefix goes (``target.rpc_call`` and
# ``self._client._target.rpc_call`` both fire). Without the lookbehind,
# regex backtracking could shorten the prefix and create overlapping
# matches; with it, each occurrence is reported once with the natural
# start position.
_PATTERN_ASYNCMOCK_ASSIGN = re.compile(
    # Method-name enumeration kept INTENTIONALLY broad — not narrowed to
    # only the methods that still exist on ``Session`` (per gemini-code-
    # assist's review on PR #1078 / Wave 11c). The lint exists precisely
    # to catch dynamic attribute assignment of ``AsyncMock`` onto a fake
    # or duck-typed collaborator — those targets are bag-of-attributes
    # fakes (``MagicMock``, ``FakeSession``) that happily accept *any*
    # attribute name regardless of whether the production class still
    # defines it. Removing a deleted method name from this enumeration
    # would create a silent escape hatch: a test that re-introduces the
    # forbidden ``<chain>.transport_post = AsyncMock(...)`` pattern
    # against a ``MagicMock(spec=...)`` would no longer surface, even
    # though that is exactly the ADR-0007 violation the lint is supposed
    # to catch. ``rpc_call`` is the canonical core-RPC seam; the
    # transport-side names retained here
    # (``transport_post`` / ``_perform_authed_post`` / ``next_reqid`` /
    # ``save_cookies``) were deleted from ``Session`` in Waves 11a-11c
    # but remain in this enumeration so the lint keeps catching dynamic
    # re-assignment of them on a fake.
    r"(?<![\w.])[\w.]+\.(?:rpc_call|transport_post|_perform_authed_post|next_reqid|save_cookies)\s*=\s*(?:[\w]+\.)*AsyncMock"
)

# (d) ``mock.patch("notebooklm._private…")`` / ``patch("notebooklm._private…")``
#     — ``unittest.mock`` string-target patch into a *private* internal path.
#
# The ``(?<![\w.])(?:[\w]+\.)*`` prefix anchors ``patch`` at a word boundary
# and allows an optional dotted module qualifier, so the bare ``patch(`` (from
# ``from unittest.mock import patch``), ``mock.patch(``, and
# ``unittest.mock.patch(`` forms all match, while ``monkeypatch(`` / ``dispatch(``
# (where ``patch`` is preceded by a word char) and ``patch.object(`` (no ``(``
# immediately after ``patch``) do not. The optional ``(?:target\s*=\s*)?``
# catches the keyword-argument spelling ``patch(target="notebooklm._…")`` and
# the optional ``[rRfFuUbB]*`` catches string-literal prefixes
# (``patch(r"notebooklm._…")``), so neither can silently bypass the rule
# (gemini-code-assist review on #1336). Scoped to ``notebooklm\._`` so only
# *private* targets are flagged — patches at public facades are out of scope for
# this rule (issue #1325).
_PATTERN_MOCK_PATCH_PRIVATE = re.compile(
    r"(?<![\w.])(?:[\w]+\.)*patch\(\s*(?:target\s*=\s*)?[rRfFuUbB]*[\"']notebooklm\._"
)

# (e) ``patch.object(notebooklm._private…, "attr", …)`` — the object-target
#     ``unittest.mock`` form aimed at a private module reference. No occurrences
#     exist today; the rule guards against regressions on this second
#     ``unittest.mock`` shape. The optional ``(?:target\s*=\s*)?`` likewise
#     catches the ``patch.object(target=notebooklm._…)`` keyword spelling
#     (gemini-code-assist review on #1336).
_PATTERN_MOCK_PATCH_OBJECT_PRIVATE = re.compile(
    r"(?<![\w.])(?:[\w]+\.)*patch\.object\(\s*(?:target\s*=\s*)?[\w.]*notebooklm\._"
)

# (f) ``patch("notebooklm.<public…>._private…")`` — DEEP-LEAF string-target
#     patch into a private internal that sits behind one or more *public*
#     components. Pattern (d) anchors on ``notebooklm\._`` (private must be the
#     FIRST component) and therefore never sees this population. The
#     ``(?:[a-zA-Z]\w*\.)+`` segment requires at least one public component
#     (first char a letter, so it cannot consume a ``_private`` component)
#     before the first ``_``-prefixed one, which makes (d) and (f) **disjoint
#     by construction**: ``patch("notebooklm._x")`` matches only (d), and
#     ``patch("notebooklm.cli._x")`` matches only (f). Disjointness matters
#     because the two populations have different allowlist regimes — (d) is
#     drained-to-zero (``_ALLOWLIST`` + stays-empty guard), (f) is baselined
#     (``_DEEP_LEAF_ALLOWLIST``, drains opportunistically). Prefix handling
#     (``mock.patch`` / ``unittest.mock.patch`` / keyword ``target=`` / string
#     prefixes) mirrors (d).
_PATTERN_MOCK_PATCH_DEEP_PRIVATE = re.compile(
    r"(?<![\w.])(?:[\w]+\.)*patch\(\s*(?:target\s*=\s*)?"
    r"[rRfFuUbB]*[\"']notebooklm\.(?:[a-zA-Z]\w*\.)+_\w"
)

# (g) ``patch.object(alias, "_private_attr", …)`` — private-attribute-name
#     ``patch.object`` against a locally-imported alias. Pattern (e) only fires
#     when the *object expression* is a dotted ``notebooklm._…`` reference; it
#     misses the recommended seam-alias form when the attribute NAME itself is
#     private layout. ``[^,()]+`` keeps the first argument simple (an alias or
#     dotted name — a call expression like ``patch.object(type(x), …)`` is not
#     matched; none exist under ``tests/`` today). The optional
#     ``(?:attribute\s*=\s*)?`` catches the keyword spelling
#     ``patch.object(alias, attribute="_x")``, mirroring (d)/(e)'s ``target=``
#     handling; like those patterns, the keyword form is detected only in its
#     natural positional slot (an exotic reordered-kwargs spelling is outside
#     the regex perimeter — none exist under ``tests/`` today).
#     ``_(?!_\w*__[\"'])`` flags every ``_``-leading layout name —
#     ``"_private"``, double-underscore ``"__private"``, quasi-dunder
#     ``"__x_"``, name-mangled ``"_Cls__attr"`` — while exempting only *full*
#     dunder names (``"__aenter__"`` etc.: leading AND trailing DOUBLE
#     underscore, Python-protocol surface, not internal layout — hence the
#     ``__[\"']`` tail in the lookahead). There are no double-underscore-form
#     ``patch.object`` sites under ``tests/`` today, so the exemption is
#     precautionary, documented here and self-tested below.
_PATTERN_PATCH_OBJECT_PRIVATE_ATTR = re.compile(
    r"(?<![\w.])(?:[\w]+\.)*patch\.object\(\s*[^,()]+,\s*(?:attribute\s*=\s*)?"
    r"[rRfFuUbB]*[\"']_(?!_\w*__[\"'])"
)

# (h) ``patch(f"{MODULE}.attr")`` — computed string-target ``patch``.
#
# The string-patch ratchet counts literal ``patch("notebooklm…")`` targets,
# but f-string targets can hide the same module-layout coupling behind a
# constant such as ``REFRESH = "notebooklm.cli.services.login.refresh"``.
# They also defeat the private-target regexes when the private leaf is
# assembled as ``f"{REFRESH}._helper"``. Any test that needs this should use
# a locally-imported object reference or an explicit dependency seam instead.
_PATTERN_MOCK_PATCH_FSTRING_TARGET = re.compile(
    r"(?<![\w.])(?:[\w]+\.)*patch\(\s*(?:target\s*=\s*)?"
    r"[rRuUbB]*[fF][rRuUbB]*[\"']"
)

# NOTE: patterns (f) and (g) are deliberately NOT in this tuple — it drives
# the drained-to-zero global gate (``_ALLOWLIST`` + stays-empty guard), while
# (f)/(g) run under their own baselined-allowlist regime via
# ``test_no_deep_leaf_private_string_patches_outside_allowlist`` /
# ``test_no_private_attr_patch_object_outside_allowlist``
# (``_assert_tier_clean``). A future pattern must be wired into exactly one of
# the two regimes — appending here alone would put a baselined population
# under the stays-empty rule (false alarms); writing only a regex with
# neither gate would leave it unenforced.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("string-target monkeypatch (forbidden by ADR-0007)", _PATTERN_STRING_TARGET),
    ("object-attribute monkeypatch (forbidden by ADR-0007)", _PATTERN_OBJECT_ATTR),
    ("AsyncMock attribute assignment (forbidden by ADR-0007)", _PATTERN_ASYNCMOCK_ASSIGN),
    ("mock.patch string-target into private (forbidden by ADR-0007)", _PATTERN_MOCK_PATCH_PRIVATE),
    (
        "patch.object into private module (forbidden by ADR-0007)",
        _PATTERN_MOCK_PATCH_OBJECT_PRIVATE,
    ),
    ("mock.patch f-string target (forbidden by ADR-0007)", _PATTERN_MOCK_PATCH_FSTRING_TARGET),
)


# ---------------------------------------------------------------------------
# File-level allowlist — DRAINED TO ZERO (issue #1376).
#
# The allowlist was baked at PR-start (2026-05-18) with 33 offending files and
# shrank wave-by-wave as each file was migrated to constructor injection /
# locally-imported seam aliases. With the final wave merged it is **empty**:
# every test file under ``tests/`` now satisfies the ADR-0007 monkeypatch
# policy with zero exemptions, so the per-file gate is now a *global*
# invariant — any new forbidden pattern fails the lint unconditionally.
#
# The allowlist MUST stay empty. The migration is complete; ADR-0007 is now
# plain ``Accepted``. Re-adding an entry would silently re-open the escape
# hatch the drain closed, so ``test_allowlist_stays_empty`` below asserts
# ``_ALLOWLIST == frozenset()`` as a hardening guard — new offenders must be
# migrated, never allowlisted.
# ---------------------------------------------------------------------------

_ALLOWLIST: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# Baselined allowlists for patterns (f) and (g) — measured at gate-landing
# time (2026-06-10), file-level for the same rebase-stability reasons as
# ``_ALLOWLIST`` (ADR-0007 "Alternatives considered: per-site allowlist
# entries"). Both FILE SETS may only drain: the stale-entry checks force
# removal of cleaned/deleted files, and the gates block any file not listed
# here. Being file-level, the lists do not cap site counts *inside* an
# allowlisted file — for pattern (f) that growth is still capped externally,
# because every (f) site is also a string-target ``patch("notebooklm…")``
# site counted by ``tests/_guardrails/test_string_patch_ratchet.py``'s
# per-file ceilings; pattern (g) sites have no count cap (accepted file-level
# trade-off, same as the original ``_ALLOWLIST`` during its drain).
# There is deliberately NO stays-empty guard — these start populated and
# shrink opportunistically as files migrate to constructor injection.
#
# DO NOT add entries. A new offending file must be migrated, not allowlisted;
# see the remediation text in the gate assertions below.
# ---------------------------------------------------------------------------

# Files containing deep-leaf private string-target patches (pattern f).
_DEEP_LEAF_ALLOWLIST: frozenset[str] = frozenset()

# Files containing private-attribute ``patch.object`` patches (pattern g).
_PATCH_OBJECT_PRIVATE_ATTR_ALLOWLIST: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


def _iter_python_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*.py")):
        rel_parts = path.relative_to(root).parts
        if rel_parts and rel_parts[0] in _SKIP_DIRS:
            continue
        yield path


def _scan_text(text: str) -> list[tuple[int, str]]:
    """Return ``[(line_no, pattern_label), ...]`` for every match in *text*.

    Scans the file as a single string (not line-by-line) so multi-line
    forms like::

        monkeypatch.setattr(
            "notebooklm.auth.X",
            fake,
        )

    are caught. ``\\s`` already spans newlines in Python's regex engine,
    so no flag changes are needed — the regexes were authored against
    "any whitespace, including newlines" semantics.
    """
    findings: list[tuple[int, str]] = []
    for label, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            # Match starts can land at column 0 of a continuation line;
            # report the line where the *match* begins, which is also
            # the line a reader will scan first when chasing the error.
            findings.append((_line_of(text, match.start()), label))
    findings.sort()
    return findings


def _line_of(text: str, pos: int) -> int:
    """Return the 1-based line number of character offset *pos* in *text*."""
    return text.count("\n", 0, pos) + 1


def _match_lines(text: str, pattern: re.Pattern[str]) -> list[int]:
    """Return the sorted 1-based line numbers where *pattern* matches *text*.

    Pure (string in, line numbers out) so the per-pattern self-tests below can
    drive it on crafted snippets without touching the filesystem.
    """
    return sorted(_line_of(text, m.start()) for m in pattern.finditer(text))


def _tier_offenders(
    files: Iterable[tuple[str, str]], pattern: re.Pattern[str]
) -> dict[str, list[int]]:
    """Map ``rel-path -> matching line numbers`` for every offending file.

    Pure: *files* is ``(rel_posix_path, text)`` pairs, so the self-tests can
    feed crafted file sets and the live gates feed :func:`_test_file_texts`.
    """
    offenders: dict[str, list[int]] = {}
    for rel, text in files:
        lines = _match_lines(text, pattern)
        if lines:
            offenders[rel] = lines
    return offenders


def _rel_posix(path: Path) -> str:
    """Return *path* as a repo-relative POSIX-style string."""
    return path.relative_to(_REPO_ROOT).as_posix()


@functools.cache
def _test_file_texts() -> tuple[tuple[str, str], ...]:
    """``(rel_posix_path, text)`` for every scanned test file, sorted by path.

    Cached: three gate families (the drained global gate plus the two
    baselined tiers) scan the same tree, so :func:`functools.cache` memoises
    the single whole-tree read. Returns a tuple so the cached value cannot be
    mutated by a caller.
    """
    return tuple(
        (_rel_posix(path), path.read_text(encoding="utf-8"))
        for path in _iter_python_files(_TESTS_ROOT)
    )


def test_no_forbidden_monkeypatches_outside_allowlist() -> None:
    """No tests file outside the allowlist may contain the forbidden patterns.

    See ``docs/adr/0007-test-monkeypatch-policy.md``.
    """

    violations: list[tuple[str, int, str]] = []
    seen_files_with_findings: set[str] = set()

    for rel, text in _test_file_texts():
        findings = _scan_text(text)
        if not findings:
            continue
        seen_files_with_findings.add(rel)
        if rel in _ALLOWLIST:
            continue
        for line_no, label in findings:
            violations.append((rel, line_no, label))

    # Surface stale allowlist entries: a file that has been cleaned up
    # should be removed from the allowlist so the lint keeps tightening.
    stale = sorted(_ALLOWLIST - seen_files_with_findings)
    extra_messages: list[str] = []
    if stale:
        extra_messages.append(
            "Stale allowlist entries (no forbidden patterns found; remove from _ALLOWLIST):\n"
            + "\n".join(f"  - {entry}" for entry in stale)
        )

    if violations:
        formatted = "\n".join(
            f"  {file}:{line}  {label}" for file, line, label in sorted(violations)
        )
        msg = (
            "Forbidden test-monkeypatch patterns detected outside the "
            "ADR-0007 allowlist. Migrate the test(s) to constructor "
            "injection via ``tests/_fixtures/make_fake_core(...)`` or, "
            "if migration must defer, add the file path to "
            "``tests/_guardrails/test_no_forbidden_monkeypatches.py::_ALLOWLIST`` "
            "with a justification in the PR description.\n\n"
            f"Violations ({len(violations)}):\n{formatted}"
        )
        if extra_messages:
            msg = msg + "\n\n" + "\n\n".join(extra_messages)
        raise AssertionError(msg)

    if stale:
        raise AssertionError("\n\n".join(extra_messages))


def test_allowlist_stays_empty() -> None:
    """Hardening guard: the ADR-0007 allowlist must remain empty (issue #1376).

    The migration drained every offending file (33 → 0); ADR-0007 is now plain
    ``Accepted``. This invariant prevents the allowlist from silently
    re-growing: a regression that adds a forbidden pattern must be fixed by
    migrating the test to a constructor seam, **not** by re-allowlisting the
    file. Any non-empty ``_ALLOWLIST`` fails here.
    """

    # Assert the exact ``frozenset()`` sentinel, not mere falsiness: ``assert
    # not _ALLOWLIST`` would also pass for an empty mutable ``set()``, so a
    # future refactor that reintroduces mutability would silently weaken this
    # guard. Pin both the immutable type and the empty value.
    assert isinstance(_ALLOWLIST, frozenset) and len(_ALLOWLIST) == 0, (
        "The ADR-0007 monkeypatch allowlist was drained to zero (issue #1376) "
        "and must stay an empty ``frozenset``. New forbidden patterns must be "
        "migrated to constructor injection via "
        "``tests/_fixtures/make_fake_core(...)`` or a locally-imported seam "
        "alias — not added back to ``_ALLOWLIST``.\n\n"
        f"Unexpected entries ({len(_ALLOWLIST)}):\n"
        + "\n".join(f"  - {entry}" for entry in sorted(_ALLOWLIST))
    )


# ---------------------------------------------------------------------------
# Baselined tier gates — patterns (f) and (g)
# ---------------------------------------------------------------------------


def _assert_tier_clean(
    pattern: re.Pattern[str],
    allowlist: frozenset[str],
    allowlist_name: str,
    label: str,
    remediation: str,
) -> None:
    """Shared gate body for the two baselined tiers.

    Fails on (a) any offending file NOT in *allowlist* — new offenders must be
    migrated, never allowlisted — and (b) any *stale* allowlist entry (a file
    that no longer offends, or no longer exists, must be removed so the list
    only drains). Mirrors the violations+stale structure of
    :func:`test_no_forbidden_monkeypatches_outside_allowlist`.
    """
    offenders = _tier_offenders(_test_file_texts(), pattern)

    failures: list[str] = []
    violations = {rel: lines for rel, lines in offenders.items() if rel not in allowlist}
    if violations:
        formatted = "\n".join(
            f"  {rel}:{line}" for rel, lines in sorted(violations.items()) for line in lines
        )
        failures.append(
            f"{label} detected outside the baselined allowlist "
            f"(ADR-0007).\n{remediation}\n"
            f"Do NOT add the file to ``{allowlist_name}`` — the baseline only "
            f"drains.\n\nViolations:\n{formatted}"
        )

    stale = sorted(allowlist - offenders.keys())
    if stale:
        failures.append(
            f"Stale ``{allowlist_name}`` entries (file no longer contains the "
            "pattern, or was deleted/renamed; remove the entries so the "
            "baseline keeps draining):\n" + "\n".join(f"  - {entry}" for entry in stale)
        )

    assert not failures, "\n\n".join(failures)


def test_no_deep_leaf_private_string_patches_outside_allowlist() -> None:
    """Pattern (f) gate: deep-leaf private string patches only in the baseline.

    A ``patch("notebooklm.<public…>._private…")`` string target silently
    no-ops when the private leaf relocates (the #1481 CLI command-move
    post-mortem), exactly like the top-level form pattern (d) already forbids.
    """
    _assert_tier_clean(
        _PATTERN_MOCK_PATCH_DEEP_PRIVATE,
        _DEEP_LEAF_ALLOWLIST,
        "_DEEP_LEAF_ALLOWLIST",
        'Deep-leaf ``patch("notebooklm.<public…>._private…")`` string target(s)',
        "Migrate the test to constructor injection via "
        "``tests/_fixtures/make_fake_core(...)`` or to ``patch.object`` on a "
        "locally-imported alias with a PUBLIC attribute name. Re-pointing the "
        "string at a public leaf is NOT a fix: every string-target "
        '``patch("notebooklm…")`` site is population-capped by '
        "``tests/_guardrails/test_string_patch_ratchet.py``, and a private "
        "attribute name on ``patch.object`` trips pattern (g) in this file.",
    )


def test_no_private_attr_patch_object_outside_allowlist() -> None:
    """Pattern (g) gate: private-attr ``patch.object`` only in the baseline.

    ``patch.object(alias, "_private_attr")`` pins internal attribute layout by
    string even though the object reference itself is sound; after a rename it
    keeps "passing" against bag-of-attribute fakes while patching nothing real.
    """
    _assert_tier_clean(
        _PATTERN_PATCH_OBJECT_PRIVATE_ATTR,
        _PATCH_OBJECT_PRIVATE_ATTR_ALLOWLIST,
        "_PATCH_OBJECT_PRIVATE_ATTR_ALLOWLIST",
        'Private-attribute ``patch.object(<alias>, "_private…")`` patch(es)',
        "Migrate the test to constructor injection via "
        "``tests/_fixtures/make_fake_core(...)`` or patch a PUBLIC attribute "
        "of the collaborator. Converting to a string-target "
        '``patch("notebooklm…")`` form is NOT a fix: private string targets '
        "trip patterns (d)/(f) in this file, and the overall string-patch "
        "population is growth-capped by "
        "``tests/_guardrails/test_string_patch_ratchet.py``.",
    )


def test_baselined_allowlist_paths_exist() -> None:
    """Every baselined allowlist entry must point at an existing file.

    A rename/delete that leaves a dangling entry would silently shrink the
    gate's coverage claim (the stale check in the tier gates also fires, but
    this check names the failure precisely: prune the dead path).
    """
    missing = sorted(
        entry
        for entry in _DEEP_LEAF_ALLOWLIST | _PATCH_OBJECT_PRIVATE_ATTR_ALLOWLIST
        if not (_REPO_ROOT / entry).is_file()
    )
    assert missing == [], (
        "Baselined allowlist entries point at files that no longer exist "
        "(renamed or deleted). Remove the stale entries:\n"
        + "\n".join(f"  - {entry}" for entry in missing)
    )


# ---------------------------------------------------------------------------
# Pattern self-tests — guard against the new regexes silently going vacuous
# (a pattern that matches nothing must fail here, not pass the whole tree).
# The known-bad snippets are inline string literals; this file sits in
# ``_SKIP_DIRS`` so they are never scanned as live offenders.
# ---------------------------------------------------------------------------


def test_deep_leaf_pattern_detects_known_shapes() -> None:
    """Pattern (f) matches deep-leaf privates and stays disjoint from (d)."""
    deep = 'patch("notebooklm.cli.session_cmd._sync_server_language_to_config")'
    assert _match_lines(deep, _PATTERN_MOCK_PATCH_DEEP_PRIVATE) == [1]
    # Qualified / keyword / prefixed / multi-line spellings all match.
    assert _match_lines(
        'mock.patch("notebooklm.cli.services.login._launch_browser", fake)',
        _PATTERN_MOCK_PATCH_DEEP_PRIVATE,
    ) == [1]
    assert _match_lines(
        'patch(target=r"notebooklm.rpc.decoder._extract_rows")',
        _PATTERN_MOCK_PATCH_DEEP_PRIVATE,
    ) == [1]
    assert _match_lines(
        'with patch(\n    "notebooklm.cli.helpers._render_table"\n):',
        _PATTERN_MOCK_PATCH_DEEP_PRIVATE,
    ) == [1]

    # Disjointness: a TOP-LEVEL private target belongs to pattern (d) only…
    top_level = 'patch("notebooklm._x")'
    assert _match_lines(top_level, _PATTERN_MOCK_PATCH_DEEP_PRIVATE) == []
    assert _match_lines(top_level, _PATTERN_MOCK_PATCH_PRIVATE) == [1]
    # …and the deep-leaf target belongs to pattern (f) only.
    assert _match_lines(deep, _PATTERN_MOCK_PATCH_PRIVATE) == []

    # A fully PUBLIC dotted path matches no pattern in this module at all
    # (it is the string-patch ratchet's population, not this lint's).
    public_leaf = 'patch("notebooklm.cli.helpers.get_context_path")'
    for _label, pattern in _PATTERNS:
        assert _match_lines(public_leaf, pattern) == []
    for pattern in (_PATTERN_MOCK_PATCH_DEEP_PRIVATE, _PATTERN_PATCH_OBJECT_PRIVATE_ATTR):
        assert _match_lines(public_leaf, pattern) == []

    # ``monkeypatch.setattr`` / ``dispatch(`` lookalikes do not fire (f).
    assert (
        _match_lines(
            'monkeypatch.setattr("notebooklm.cli.helpers._x", fake)',
            _PATTERN_MOCK_PATCH_DEEP_PRIVATE,
        )
        == []
    )
    assert (
        _match_lines('dispatch("notebooklm.cli.helpers._x")', _PATTERN_MOCK_PATCH_DEEP_PRIVATE)
        == []
    )


def test_fstring_patch_pattern_detects_computed_targets() -> None:
    """Pattern (h) catches computed string-target ``patch`` calls."""
    assert _match_lines('patch(f"{REFRESH}._helper")', _PATTERN_MOCK_PATCH_FSTRING_TARGET) == [1]
    assert _match_lines(
        'mock.patch(target=fr"{MODULE}.public_attr")',
        _PATTERN_MOCK_PATCH_FSTRING_TARGET,
    ) == [1]
    assert (
        _match_lines(
            'patch.object(module, "public_attr")',
            _PATTERN_MOCK_PATCH_FSTRING_TARGET,
        )
        == []
    )
    assert (
        _match_lines(
            'patch("notebooklm.cli.helpers.get_context_path")',
            _PATTERN_MOCK_PATCH_FSTRING_TARGET,
        )
        == []
    )


def test_patch_object_private_attr_pattern_detects_known_shapes() -> None:
    """Pattern (g) matches private attr names, exempts public names + dunders."""
    assert _match_lines(
        'patch.object(helpers, "_render", fake)', _PATTERN_PATCH_OBJECT_PRIVATE_ATTR
    ) == [1]
    # Qualified, new_callable, and multi-line spellings all match.
    assert _match_lines(
        'mock.patch.object(sources_api, "_get_or_none", new_callable=AsyncMock)',
        _PATTERN_PATCH_OBJECT_PRIVATE_ATTR,
    ) == [1]
    assert _match_lines(
        'with patch.object(\n    downloads_module, "_resolve_path"\n):',
        _PATTERN_PATCH_OBJECT_PRIVATE_ATTR,
    ) == [1]
    # The ``attribute=`` keyword spelling matches (mirrors (d)/(e) ``target=``).
    assert _match_lines(
        'patch.object(helpers, attribute="_render")', _PATTERN_PATCH_OBJECT_PRIVATE_ATTR
    ) == [1]
    # Double-underscore layout names are NOT protocol dunders — still flagged.
    assert _match_lines(
        'patch.object(helpers, "__private", fake)', _PATTERN_PATCH_OBJECT_PRIVATE_ATTR
    ) == [1]
    # …including the quasi-dunder with a SINGLE trailing underscore.
    assert _match_lines(
        'patch.object(helpers, "__x_", fake)', _PATTERN_PATCH_OBJECT_PRIVATE_ATTR
    ) == [1]
    # The decorator spelling matches too (``@`` is outside ``[\w.]``, so the
    # word-boundary lookbehind passes).
    assert _match_lines(
        '@patch.object(LoginFlow, "_resolve_profile")', _PATTERN_PATCH_OBJECT_PRIVATE_ATTR
    ) == [1]

    # Public attribute names are the sanctioned seam-alias form — no match.
    assert (
        _match_lines('patch.object(client, "ask", fake)', _PATTERN_PATCH_OBJECT_PRIVATE_ATTR) == []
    )
    # Full dunder names (leading AND trailing ``__``) are Python-protocol
    # surface, not internal layout — exempt.
    assert (
        _match_lines(
            'patch.object(fake_browser, "__aenter__", fake)',
            _PATTERN_PATCH_OBJECT_PRIVATE_ATTR,
        )
        == []
    )
    # Plain ``patch("…")`` string targets are patterns (d)/(f), not (g).
    assert (
        _match_lines('patch("notebooklm.cli.helpers._x")', _PATTERN_PATCH_OBJECT_PRIVATE_ATTR) == []
    )


def test_tier_offenders_detects_and_clears() -> None:
    """Self-check the pure tier scanner on crafted file sets.

    Drives :func:`_tier_offenders` (the same function the live gates use) so
    the violations+stale plumbing cannot silently go vacuous.
    """
    files = (
        ("tests/unit/bad.py", 'x = 1\npatch("notebooklm.cli.session_cmd._go")\n'),
        ("tests/unit/good.py", 'patch("notebooklm.cli.helpers.public_fn")\n'),
    )
    assert _tier_offenders(files, _PATTERN_MOCK_PATCH_DEEP_PRIVATE) == {"tests/unit/bad.py": [2]}
    assert _tier_offenders((), _PATTERN_MOCK_PATCH_DEEP_PRIVATE) == {}
