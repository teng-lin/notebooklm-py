"""Meta-lint: a shrink-only population ratchet for string-target ``patch("notebooklm…")``.

ADR-0007 (``docs/adr/0007-test-monkeypatch-policy.md``) forbids string-target
patches into *private* ``notebooklm._*`` paths — they resolve by import string
and silently no-op when the internal relocates. But the policy's failure mode
does not stop at privates: the #1481 CLI command-move post-mortem showed ~137
``patch("notebooklm.cli.source_cmd.NotebookLMClient")``-style *public-leaf*
string patches no-op'ing in exactly the same way when a command body moved to
a sibling module. Every string-target patch — public or private — couples a
test to module *layout* rather than to an object reference, and that coupling
is invisible until a refactor breaks it.

The companion lint (``tests/_guardrails/test_no_forbidden_monkeypatches.py``)
hard-forbids the private-target subsets. This file freezes the **whole**
string-target ``patch("notebooklm…")`` population per test file so it can only
shrink:

1. **No growth.** Each baselined file is pinned at its *measured* site count.
   Exceeding the ceiling fails. Files absent from the baseline have a budget
   of **zero** — new test files must not use string-target patches at all.

2. **Ceilings only ratchet down.** A file that drops below its ceiling fails
   with a "tighten the ceiling to N" message (or "remove the entry" at zero),
   so reclaimed ground can never be re-accreted.

3. **No stale entries.** Every baselined path must still exist; a rename or
   delete must update the baseline.

Scope: string-target ``patch("notebooklm…")`` forms only (bare ``patch(``,
``mock.patch(`` / ``unittest.mock.patch(``, the ``target=`` keyword spelling,
and string-literal prefixes, including multi-line forms). ``patch.object`` is
deliberately NOT counted: ``patch.object`` on a **public** attribute of a
locally-imported alias and constructor injection via
``tests/_fixtures/make_fake_core(...)`` are the sanctioned alternatives this
ratchet pushes toward. (``patch.object`` with a *private* attribute name is
forbidden separately by the companion lint — do not "fix" a ceiling overflow
by converting to that form.)

The count is **lexical**, not syntactic: it counts pattern occurrences in the
raw file text — the same single-string scan the companion lint uses — so a
spelled-out ``patch("notebooklm…")`` inside a comment or docstring counts as a
site. That trade-off is deliberate (regexes survive unparseable snippets, run
identically on every OS, and stay rebase-stable); the cost is that prose
mentions consume budget, which is acceptable because the remediation for both
is the same — don't write the shape at all. Quote offending shapes only in
``tests/_guardrails/`` files, which are excluded from the scan (mirroring the
companion lint's ``_SKIP_DIRS``): gate files quote offending shapes as string
data and must not count as live sites.

The ceilings below were *measured*, not estimated. To regenerate one::

    python -c "import re, pathlib; t = pathlib.Path('tests/unit/cli/test_source.py').read_text(encoding='utf-8'); \
        print(len(re.findall(r'(?<![\\w.])(?:[\\w]+\\.)*patch\\(\\s*(?:target\\s*=\\s*)?[rRfFuUbB]*[\"\\']notebooklm\\.', t)))"

Modelled after ``tests/_guardrails/test_module_size_ratchet.py`` (the numeric
shrink-only ratchet mechanics) and
``tests/_guardrails/test_no_forbidden_monkeypatches.py`` (the scan scope and
string-target pattern family).
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _TESTS_ROOT.parent

# Mirrors ``test_no_forbidden_monkeypatches._SKIP_DIRS``: ``_guardrails``
# (this directory quotes offending shapes as data), ``_fixtures`` (the
# policy's substrate), and the data-only directories.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "_guardrails",
        "_fixtures",
        "cassettes",
        "fixtures",
    }
)

# String-target ``patch("notebooklm…")`` site. The prefix handling mirrors the
# companion lint's pattern (d): ``(?<![\w.])(?:[\w]+\.)*patch\(`` accepts bare
# ``patch(`` / ``mock.patch(`` / ``unittest.mock.patch(`` while rejecting
# ``monkeypatch(`` / ``dispatch(`` lookalikes and ``patch.object(`` (no ``(``
# immediately after ``patch``); ``(?:target\s*=\s*)?`` catches the keyword
# spelling and ``[rRfFuUbB]*`` the string-literal prefixes. ``\s*`` spans
# newlines, so multi-line ``patch(\n    "notebooklm…"`` forms are counted.
_PATTERN_STRING_PATCH = re.compile(
    r"(?<![\w.])(?:[\w]+\.)*patch\(\s*(?:target\s*=\s*)?[rRfFuUbB]*[\"']notebooklm\."
)

# Every test file with at least one string-target ``patch("notebooklm…")``
# site, pinned at its MEASURED count (2026-06-10 baseline: 52 files, 768
# sites). Paths are POSIX-relative to the repository root. The map can only
# shrink (entries removed as files reach zero) and ceilings can only tighten.
#
# DO NOT raise a ceiling or add an entry to make room for a new string patch —
# use ``patch.object`` on a PUBLIC attribute of a locally-imported alias, or
# constructor injection via ``tests/_fixtures/make_fake_core(...)``.
# DO lower a ceiling when a file sheds sites (the gate prints the value).
STRING_PATCH_CEILINGS: dict[str, int] = {}

# Shared remediation tail for the growth-side failures: the gate must steer
# violators toward the sanctioned seams and AWAY from the companion lint's
# forbidden shapes, so a contributor never ping-pongs between the two gates.
_REMEDIATION = (
    "Remove the new string-target patch site(s): use ``patch.object`` on a "
    "PUBLIC attribute of a locally-imported alias, or constructor injection "
    "via ``tests/_fixtures/make_fake_core(...)``. Do NOT switch to a private "
    "string target or a private ``patch.object`` attribute name — those forms "
    "fail ``tests/_guardrails/test_no_forbidden_monkeypatches.py``."
)


def _count_string_patch_sites(text: str) -> int:
    """Return the number of string-target ``patch("notebooklm…")`` sites in *text*."""
    return len(_PATTERN_STRING_PATCH.findall(text))


@functools.cache
def _measure_all() -> tuple[tuple[str, int], ...]:
    """``(repo-relative POSIX path, site count)`` for every scanned test file.

    Includes zero-count files, so the pure checks below can distinguish "file
    exists with no sites" (tighten/remove the ceiling) from "file is gone"
    (stale entry). Cached: the tree is read once per session. Returns a tuple
    of pairs (not a dict) so the cached value cannot be mutated by a caller —
    the gates rebuild a fresh ``dict(_measure_all())`` at each call site.
    """
    measured: list[tuple[str, int]] = []
    for path in sorted(_TESTS_ROOT.rglob("*.py")):
        rel_parts = path.relative_to(_TESTS_ROOT).parts
        if rel_parts and rel_parts[0] in _SKIP_DIRS:
            continue
        measured.append(
            (
                path.relative_to(_REPO_ROOT).as_posix(),
                _count_string_patch_sites(path.read_text(encoding="utf-8")),
            )
        )
    return tuple(measured)


# --- Pure ratchet checks (no I/O) ----------------------------------------
# The helpers take a measured ``{path: count}`` map and the ceilings so the
# live gates and the synthetic self-check exercise the *same* logic on
# crafted maps without touching the filesystem (modelled on
# ``test_module_size_ratchet.py``).


def _grown_offenders(
    measured: dict[str, int], ceilings: dict[str, int]
) -> dict[str, tuple[int, int]]:
    """Files whose count exceeds their ceiling → ``{path: (current, ceiling)}``.

    Files absent from *ceilings* have a budget of zero, so any site in a new
    (or fully-drained-then-regrown) file is growth.
    """
    return {
        rel: (count, ceilings.get(rel, 0))
        for rel, count in measured.items()
        if count > ceilings.get(rel, 0)
    }


def _slack_offenders(
    measured: dict[str, int], ceilings: dict[str, int]
) -> dict[str, dict[str, int]]:
    """Baselined files now below their ceiling → tighten-me map."""
    return {
        rel: {"current": measured[rel], "recorded_ceiling": ceiling}
        for rel, ceiling in ceilings.items()
        if rel in measured and measured[rel] < ceiling
    }


def _stale_entries(measured: dict[str, int], ceilings: dict[str, int]) -> list[str]:
    """Baselined paths that no longer exist in the scanned tree (sorted)."""
    return sorted(rel for rel in ceilings if rel not in measured)


def test_no_string_patch_population_growth() -> None:
    """No test file may exceed its string-patch ceiling (zero if unbaselined).

    Growth here means a test re-coupled itself to ``notebooklm`` module layout
    through an import-string — the exact seam fragility ADR-0007 exists to
    drain. The baseline holds today's debt; it never absorbs new debt.
    """
    grown = _grown_offenders(dict(_measure_all()), STRING_PATCH_CEILINGS)
    assert grown == {}, (
        'String-target ``patch("notebooklm…")`` site count grew past the '
        "recorded ceiling (ADR-0007 string-patch population ratchet; files "
        "not in STRING_PATCH_CEILINGS have a budget of zero). "
        + _REMEDIATION
        + f"\n\n{{path: (current, ceiling)}}: {grown}"
    )


def test_string_patch_ceilings_ratchet_down() -> None:
    """A file that sheds string-patch sites must tighten its ceiling.

    This is the ratchet: reclaimed ground gets locked in by lowering the
    ceiling (or deleting the entry at zero). A stale high ceiling would let
    the drained sites silently re-accrete. The failure prints the exact value
    to record.
    """
    slack = _slack_offenders(dict(_measure_all()), STRING_PATCH_CEILINGS)
    assert slack == {}, (
        "Test file(s) dropped below their recorded string-patch ceiling — "
        "tighten the ratchet by lowering each STRING_PATCH_CEILINGS entry to "
        "the 'current' value (or removing the entry entirely when 'current' "
        f"is 0): {slack}"
    )


def test_ceilings_have_no_stale_entries() -> None:
    """Every baselined path must still exist in the scanned tests tree.

    A rename or deletion that leaves a dangling entry would silently weaken
    the gate (the missing path can never trip the growth/slack checks), so it
    must be pruned (or re-pointed at the renamed file at its measured count).
    """
    missing = _stale_entries(dict(_measure_all()), STRING_PATCH_CEILINGS)
    assert missing == [], (
        "STRING_PATCH_CEILINGS entries point at files that no longer exist in "
        f"the scanned tests tree (renamed or deleted). Remove them: {missing}"
    )


def test_ceilings_are_all_positive() -> None:
    """Invariant: a recorded ceiling of zero (or less) is redundant.

    Unbaselined files already have a budget of zero, so a ``0`` entry adds
    nothing and a negative entry is nonsense — both signal a botched
    re-baseline. Keeps the dict meaningful: an entry exists iff the file still
    carries debt.
    """
    redundant = {rel: c for rel, c in STRING_PATCH_CEILINGS.items() if c <= 0}
    assert redundant == {}, (
        "STRING_PATCH_CEILINGS entries with a ceiling <= 0 are redundant "
        f"(unbaselined files already have budget zero) — drop them: {redundant}"
    )


def test_detector_counts_known_shapes() -> None:
    """Self-check: the site counter sees every counted spelling and no other.

    Guards against the regex silently going vacuous (matching nothing would
    make every ceiling look slack and the growth gate a no-op). The known-bad
    snippets are inline literals; this file lives in ``_SKIP_DIRS`` so they
    are never scanned as live sites.
    """
    # Counted: bare / qualified / keyword-target / prefixed / multi-line, and
    # both public and private dotted targets (the population is layout
    # coupling, not privacy).
    assert _count_string_patch_sites('patch("notebooklm.cli.helpers.fn")') == 1
    assert _count_string_patch_sites('mock.patch("notebooklm._sources.X")') == 1
    assert _count_string_patch_sites('unittest.mock.patch("notebooklm.auth.Y")') == 1
    assert _count_string_patch_sites('patch(target="notebooklm.types.Z")') == 1
    assert _count_string_patch_sites('patch(r"notebooklm.cli.session_cmd._go")') == 1
    assert _count_string_patch_sites('with patch(\n    "notebooklm.client.C"\n):') == 1
    assert _count_string_patch_sites('patch("notebooklm.a.b")\nmock.patch("notebooklm.c.d")\n') == 2

    # Not counted: ``patch.object`` (sanctioned alternative when the attribute
    # is public), ``monkeypatch.setattr`` (its own forbidden pattern in the
    # companion lint), non-``notebooklm`` targets, and ``patch``-suffixed
    # lookalikes.
    assert _count_string_patch_sites('patch.object(client, "ask")') == 0
    assert _count_string_patch_sites('monkeypatch.setattr("notebooklm.auth.X", f)') == 0
    assert _count_string_patch_sites('patch("os.path.exists")') == 0
    assert _count_string_patch_sites('dispatch("notebooklm.cli.helpers.fn")') == 0
    assert _count_string_patch_sites("") == 0


def test_ratchet_checks_detect_their_offending_shapes() -> None:
    """Self-check: the pure ratchet checks flag each offending shape.

    Drives the *real* helpers on crafted ``{path: count}`` maps so the gate
    verifies behavior, not just that the live tree happens to be clean
    (modelled on ``test_module_size_ratchet.py``'s synthetic self-check).
    """
    ceilings = {"baselined.py": 5}

    # (1) Growth detection: over-ceiling and new-file (budget zero) are
    #     flagged; at-ceiling and zero-count files are not.
    assert _grown_offenders({"baselined.py": 6}, ceilings) == {"baselined.py": (6, 5)}
    assert _grown_offenders({"new.py": 1}, ceilings) == {"new.py": (1, 0)}
    assert _grown_offenders({"baselined.py": 5, "clean.py": 0}, ceilings) == {}

    # (2) Slack/ratchet-down detection: below-ceiling is flagged with the
    #     tighten-to value (including the remove-at-zero case); at-ceiling is
    #     not, and unbaselined files never are.
    assert _slack_offenders({"baselined.py": 3}, ceilings) == {
        "baselined.py": {"current": 3, "recorded_ceiling": 5}
    }
    assert _slack_offenders({"baselined.py": 0}, ceilings) == {
        "baselined.py": {"current": 0, "recorded_ceiling": 5}
    }
    assert _slack_offenders({"baselined.py": 5, "clean.py": 0}, ceilings) == {}

    # A baselined path absent from ``measured`` is ignored by growth/slack
    # (the stale-entry check owns that case)…
    assert _grown_offenders({}, ceilings) == {}
    assert _slack_offenders({}, ceilings) == {}

    # (3) …and stale-entry detection flags it (sorted), while a present path
    #     (even at zero count) is not stale.
    assert _stale_entries({}, ceilings) == ["baselined.py"]
    assert _stale_entries({"baselined.py": 0}, ceilings) == []
    assert _stale_entries({"x.py": 1}, {"b.py": 1, "a.py": 2}) == ["a.py", "b.py"]
