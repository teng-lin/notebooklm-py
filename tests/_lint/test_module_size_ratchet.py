"""Meta-lint: a module-size ratchet for ``src/notebooklm/``.

ADR-0008 (``docs/adr/0008-cli-services-extraction-pattern.md``) records that the
line-count gate is *owed* but not yet built: it says the ``cli/session.py``
shrink target "lands when the proxy block goes", and the only line-count code in
the tree today is **diagnostic** (``scripts/audit_test_suite.py`` prints the top
files by line count but never fails). With no enforcement, the feature modules
that absorbed bulk during the session-shrink arc re-accrete freely — a dozen of
them are 900-1500 LOC.

This lint turns that diagnostic into a **ratchet** that complements #1331 (which
tracks three concrete splits):

1. **No new fat modules.** Any module under ``src/notebooklm/`` that exceeds
   :data:`MODULE_SIZE_BUDGET` lines and is *not* in :data:`ALLOWLISTED_CEILINGS`
   fails the gate. New code must come in under budget or split.

2. **Allowlisted ceilings only ratchet down.** Each currently-oversized module
   is pinned at its *measured* current LOC. If someone grows an allowlisted
   module past its recorded ceiling, the gate fails. If someone *shrinks* one
   below its ceiling, the gate **also** fails — but with a "tighten the ceiling"
   message: the recorded ceiling must be lowered to the new (smaller) count so
   the saved ground can never be re-accreted. The allowlist can only get
   smaller and its ceilings can only get tighter.

3. **No stale allowlist entries.** Every allowlisted path must still exist (a
   rename/delete must update the allowlist).

The ceilings below were *measured*, not estimated. To regenerate them::

    python -c "from pathlib import Path; src=Path('src/notebooklm'); \
        [print(f\"{len(p.read_text(encoding='utf-8').splitlines()):>6}  {p.relative_to(src).as_posix()}\") \
         for p in sorted(src.rglob('*.py')) \
         if len(p.read_text(encoding='utf-8').splitlines()) > 900]"

Line counting uses ``str.splitlines()`` to match the diagnostic in
``scripts/audit_test_suite.py`` (``big_files``), so the two never disagree.

Modelled after the AST/path lints in ``tests/_lint/`` (e.g.
``test_no_inline_deprecation_warnings.py`` / ``test_no_module_shadowing.py``).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"

# Any *new* module is forbidden from exceeding this many lines. Chosen to sit
# just below the smallest currently-allowlisted module (``_idempotency.py`` at
# 936) so the allowlist is the *complete* set of modules over budget today and
# the gate is green on main. New work must come in at or under this budget or
# split before merge.
MODULE_SIZE_BUDGET = 900

# Every module currently over budget, pinned at its MEASURED current LOC. These
# are the only sanctioned exceedances; the map can only shrink (entries removed
# as modules drop to/under budget) and ceilings can only tighten (lowered as a
# module shrinks). Paths are POSIX-relative to ``src/notebooklm/``.
#
# DO NOT raise a ceiling to make room for new code in a fat module — split it.
# DO lower a ceiling when a module shrinks (the gate will tell you the value).
ALLOWLISTED_CEILINGS: dict[str, int] = {
    "cli/source_cmd.py": 1498,
    "exceptions.py": 1426,
    "_artifacts.py": 1420,
    "_source/upload.py": 1239,
    "cli/session_cmd.py": 1080,
    "_sources.py": 1025,
    "cli/services/playwright_login.py": 988,
    "_artifact/downloads.py": 973,
    "client.py": 973,
    "_research.py": 969,
    "cli/services/generate.py": 954,
    "_chat/api.py": 953,
    "_idempotency.py": 936,
}


def _line_count(path: Path) -> int:
    """Return the line count of ``path`` using ``splitlines`` (matches the diagnostic)."""
    return len(path.read_text(encoding="utf-8").splitlines())


def _measure_all() -> dict[str, int]:
    """Map every ``src/notebooklm/`` module (POSIX-relative) to its line count."""
    return {
        p.relative_to(SRC_ROOT).as_posix(): _line_count(p) for p in sorted(SRC_ROOT.rglob("*.py"))
    }


# --- Pure ratchet checks (no I/O) ----------------------------------------
# The helpers below take a measured ``{path: loc}`` map and the policy knobs so
# the public tests and the synthetic self-check exercise the *same* logic.
# Keeping them I/O-free means the self-check can feed crafted maps (over budget
# / grown / shrunk) without touching the filesystem.


def _over_budget_offenders(
    measured: dict[str, int], allowlist: dict[str, int], budget: int
) -> dict[str, int]:
    """Un-allowlisted modules strictly over ``budget`` → ``{path: loc}``."""
    return {rel: n for rel, n in measured.items() if n > budget and rel not in allowlist}


def _grown_offenders(
    measured: dict[str, int], allowlist: dict[str, int]
) -> dict[str, tuple[int, int]]:
    """Allowlisted modules now larger than their ceiling → ``{path: (current, ceiling)}``."""
    return {
        rel: (measured[rel], ceiling)
        for rel, ceiling in allowlist.items()
        if rel in measured and measured[rel] > ceiling
    }


def _slack_offenders(
    measured: dict[str, int], allowlist: dict[str, int]
) -> dict[str, dict[str, int]]:
    """Allowlisted modules now smaller than their ceiling → tighten-me map."""
    return {
        rel: {"current": measured[rel], "recorded_ceiling": ceiling}
        for rel, ceiling in allowlist.items()
        if rel in measured and measured[rel] < ceiling
    }


def _stale_entries(measured: dict[str, int], allowlist: dict[str, int]) -> list[str]:
    """Allowlisted paths that no longer exist under ``src/notebooklm/`` (sorted)."""
    return sorted(rel for rel in allowlist if rel not in measured)


def test_no_new_modules_over_budget() -> None:
    """No un-allowlisted module may exceed :data:`MODULE_SIZE_BUDGET` lines.

    A new (or newly-grown) module over budget that is not in the allowlist means
    the obesity the session-shrink arc pushed into feature modules is
    re-accreting unchecked. Split the module, or — only if it is a genuinely
    irreducible existing module — add it to :data:`ALLOWLISTED_CEILINGS` at its
    measured LOC with a justification in review.
    """
    offenders = _over_budget_offenders(_measure_all(), ALLOWLISTED_CEILINGS, MODULE_SIZE_BUDGET)
    assert offenders == {}, (
        f"Module(s) exceed the {MODULE_SIZE_BUDGET}-line budget and are not "
        f"allowlisted (ADR-0008 module-size ratchet). Split them, or add them to "
        f"ALLOWLISTED_CEILINGS at their measured LOC with a review justification: "
        f"{offenders}"
    )


def test_allowlisted_modules_do_not_exceed_their_ceiling() -> None:
    """Allowlisted modules must not grow past their recorded ceiling.

    The ceiling is a *fixed point*, not a moving target: an allowlisted module
    may shrink (see :func:`test_allowlisted_ceilings_ratchet_down`) but must
    never grow. Growth past the pin means new bulk landed in an already-fat
    module instead of being split out.
    """
    grown = _grown_offenders(_measure_all(), ALLOWLISTED_CEILINGS)
    assert grown == {}, (
        "Allowlisted module(s) grew past their recorded ceiling (ADR-0008 "
        "module-size ratchet). Split out the new bulk instead of growing a fat "
        f"module {{path: (current, ceiling)}}: {grown}"
    )


def test_allowlisted_ceilings_ratchet_down() -> None:
    """A shrunk allowlisted module must tighten its ceiling to the new count.

    This is the ratchet: once a fat module drops below its recorded ceiling, the
    saved ground is locked in by lowering (or removing) the ceiling. A stale
    high ceiling would silently let the reclaimed lines re-accrete, defeating the
    gate. When this fails it prints the exact value to record.
    """
    slack = _slack_offenders(_measure_all(), ALLOWLISTED_CEILINGS)
    assert slack == {}, (
        "Allowlisted module(s) shrank below their recorded ceiling — tighten the "
        "ratchet by lowering each ceiling in ALLOWLISTED_CEILINGS to the "
        "'current' value (or removing the entry entirely if 'current' is now at "
        f"or below the {MODULE_SIZE_BUDGET}-line budget): {slack}"
    )


def test_allowlist_has_no_stale_entries() -> None:
    """Every allowlisted path must still exist under ``src/notebooklm/``.

    A rename or deletion that leaves a dangling allowlist entry would silently
    weaken the gate (the missing path can never trip checks 1-3), so it must be
    pruned from :data:`ALLOWLISTED_CEILINGS`.
    """
    missing = _stale_entries(_measure_all(), ALLOWLISTED_CEILINGS)
    assert missing == [], (
        "Allowlisted path(s) no longer exist under src/notebooklm/ (renamed or "
        f"deleted). Remove the stale ALLOWLISTED_CEILINGS entries: {missing}"
    )


def test_budget_is_below_every_allowlisted_ceiling() -> None:
    """Invariant: the budget sits strictly below every allowlisted ceiling.

    If the budget were >= some ceiling, that allowlist entry would be redundant
    (the module would be under budget and need no exemption) — a sign the budget
    was raised without re-baselining. Keeps the two knobs coherent.
    """
    too_low = {
        rel: ceiling
        for rel, ceiling in ALLOWLISTED_CEILINGS.items()
        if ceiling <= MODULE_SIZE_BUDGET
    }
    assert too_low == {}, (
        f"Allowlist entries with a ceiling <= the {MODULE_SIZE_BUDGET}-line budget "
        f"are redundant — drop them (the budget already covers the module): {too_low}"
    )


def test_ratchet_checks_detect_their_offending_shapes() -> None:
    """Self-check: the pure ratchet checks flag each offending shape.

    Guards against the lint silently degrading to a no-op (which would let the
    re-accretion it exists to prevent slip through). Drives the *real* helpers
    on crafted ``{path: loc}`` maps so we verify behavior, not just that the
    live tree happens to be clean.
    """
    budget = 900
    allowlist = {"fat.py": 1000}

    # (1) Over-budget detection: un-allowlisted module over budget is flagged;
    #     an allowlisted one and an under-budget one are not.
    measured = {"new_fat.py": 950, "fat.py": 1000, "small.py": 10}
    assert _over_budget_offenders(measured, allowlist, budget) == {"new_fat.py": 950}
    # Exactly at budget is allowed (strictly-greater-than rule).
    assert _over_budget_offenders({"edge.py": budget}, allowlist, budget) == {}

    # (2) Growth detection: an allowlisted module above its ceiling is flagged
    #     as (current, ceiling); at or below the ceiling is not.
    assert _grown_offenders({"fat.py": 1001}, allowlist) == {"fat.py": (1001, 1000)}
    assert _grown_offenders({"fat.py": 1000}, allowlist) == {}
    assert _grown_offenders({"fat.py": 999}, allowlist) == {}

    # (3) Slack/ratchet-down detection: an allowlisted module below its ceiling
    #     is flagged with the tighten-to value; at or above is not.
    assert _slack_offenders({"fat.py": 950}, allowlist) == {
        "fat.py": {"current": 950, "recorded_ceiling": 1000}
    }
    assert _slack_offenders({"fat.py": 1000}, allowlist) == {}
    assert _slack_offenders({"fat.py": 1001}, allowlist) == {}

    # A path in the allowlist but absent from ``measured`` is ignored by the
    # growth/slack checks (the stale-entry check owns that case)...
    assert _grown_offenders({}, allowlist) == {}
    assert _slack_offenders({}, allowlist) == {}

    # (4) Stale-entry detection: an allowlisted path absent from ``measured`` is
    #     flagged (sorted); a path still present is not.
    assert _stale_entries({}, allowlist) == ["fat.py"]
    assert _stale_entries({"fat.py": 1000}, allowlist) == []
    assert _stale_entries({"other.py": 5}, {"b.py": 1, "a.py": 1}) == ["a.py", "b.py"]
