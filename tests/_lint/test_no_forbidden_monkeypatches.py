"""Meta-lint enforcing the test-monkeypatch policy from ADR-0007.

This test scans every ``.py`` file under ``tests/`` for the four
forbidden patterns documented in
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

Allowlist
---------

``_ALLOWLIST`` enumerates the files that *currently* contain at least
one of the forbidden patterns at PR-1's HEAD. The list shrinks as
D1 PR-2 (auth-side migration) and D1 PR-3 (CLI-side migration) retire
offenders. Once the list is empty, the per-file gate becomes a global
invariant.

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

import re
from collections.abc import Iterable
from pathlib import Path

# ---------------------------------------------------------------------------
# Repo discovery
# ---------------------------------------------------------------------------

_TESTS_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _TESTS_ROOT.parent

# Skip these subtrees:
#  - ``tests/_lint``: this file itself contains the regex literals as
#    string data; matching them would be a false positive.
#  - ``tests/_fixtures``: the policy's substrate; tests inside use the
#    factory directly and do not (and must not) demonstrate the forbidden
#    patterns.
#  - ``tests/cassettes``, ``tests/fixtures``: data-only directories
#    containing VCR cassettes and HTML/JSON fixtures, no Python source.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "_lint",
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

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("string-target monkeypatch (forbidden by ADR-0007)", _PATTERN_STRING_TARGET),
    ("object-attribute monkeypatch (forbidden by ADR-0007)", _PATTERN_OBJECT_ATTR),
    ("AsyncMock attribute assignment (forbidden by ADR-0007)", _PATTERN_ASYNCMOCK_ASSIGN),
    ("mock.patch string-target into private (forbidden by ADR-0007)", _PATTERN_MOCK_PATCH_PRIVATE),
    (
        "patch.object into private module (forbidden by ADR-0007)",
        _PATTERN_MOCK_PATCH_OBJECT_PRIVATE,
    ),
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
# Implementation
# ---------------------------------------------------------------------------


def _iter_python_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*.py")):
        rel_parts = path.relative_to(root).parts
        if rel_parts and rel_parts[0] in _SKIP_DIRS:
            continue
        yield path


def _scan_file(path: Path) -> list[tuple[int, str]]:
    """Return ``[(line_no, pattern_label), ...]`` for every match in *path*.

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
    text = path.read_text(encoding="utf-8")
    for label, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            # Match starts can land at column 0 of a continuation line;
            # report the line where the *match* begins, which is also
            # the line a reader will scan first when chasing the error.
            line_no = text.count("\n", 0, match.start()) + 1
            findings.append((line_no, label))
    findings.sort()
    return findings


def _rel_posix(path: Path) -> str:
    """Return *path* as a repo-relative POSIX-style string."""
    return path.relative_to(_REPO_ROOT).as_posix()


def test_no_forbidden_monkeypatches_outside_allowlist() -> None:
    """No tests file outside the allowlist may contain the forbidden patterns.

    See ``docs/adr/0007-test-monkeypatch-policy.md``.
    """

    violations: list[tuple[str, int, str]] = []
    seen_files_with_findings: set[str] = set()

    for path in _iter_python_files(_TESTS_ROOT):
        findings = _scan_file(path)
        if not findings:
            continue
        rel = _rel_posix(path)
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
            "``tests/_lint/test_no_forbidden_monkeypatches.py::_ALLOWLIST`` "
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
