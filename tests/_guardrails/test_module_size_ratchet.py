"""Meta-lint: a module-size ratchet for ``src/notebooklm/``.

ADR-0008 (``docs/adr/0008-cli-services-extraction-pattern.md``) recorded the
missing line-count gate at the time this guard was introduced: the session
command shrink target "lands when the proxy block goes", while the existing
diagnostic (``scripts/audit_test_suite.py``) only printed the top files by line
count. This lint is the enforcement that closes that gap and prevents oversized
modules from re-accreting. It complements #1331 (which tracks three concrete
splits):

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

   *One sanctioned exception* (``docs/adr/0033-auth-consolidation-policy.md``):
   a **structural consolidation under** ``src/notebooklm/_auth/`` may add its
   merged module at its *measured* LOC — and may raise an existing ``_auth``
   entry when a **later** sanctioned merge lands in the same module — annotated
   ``# sanctioned merge (ADR-0033)`` with the absorbed modules and the PR.
   "Structural consolidation" is narrow and means one of exactly three things:
   the recipient **absorbs a sibling** ``_auth`` **module in full** (the sibling
   is deleted, or reduced to a one-line re-export shim, in the same PR); it takes
   a **relocation that shrinks the donor by what it grows the recipient** — the
   gate only ever sees the recipient, so the annotation must name the donor for
   the reviewer to check; or it is a **template adoption**, where hand-rolled
   logic inside the module converts onto a shared template. That third class has
   **no donor** (the change is intra-module) and is still net-additive in lines,
   because a template call site plus its ``body`` closure header and explicit
   success return cost more than the inline preamble they replace — measured:
   #2152 grew the same code 22 lines while converting three writers. What keeps
   it from becoming "any growth I call a refactor" is that its evidence must be
   machine-checkable: a raise under this class is legitimate **only** when a
   companion ratchet's exception list shrinks in the same commit (for the storage
   writers, ``test_storage_transaction_ratchet._UNCONVERTED``). Inbound code that
   leaves no donor smaller and converts nothing is ordinary growth and is
   forbidden. The
   cap was acting as the module boundary inside ``_auth`` rather than the seams
   (several modules' own docstrings cite this budget as their reason to exist),
   so ADR-0033 scopes the cap to *file size* and lets the seams decide the
   files. Nothing else moves: :data:`MODULE_SIZE_BUDGET` is unchanged and still
   global, un-merged ``_auth`` modules stay under it (``cookies.py`` must not
   silently grow), and each entry is **shrink-locked at its pin** the moment it
   lands — from then on it ratchets down like any other. Entries are pinned at
   *measured* LOC in the PR that lands the merge, never pre-registered at an
   end-state estimate: check 2's slack arm below fails on any ceiling above the
   current count, so ceiling and measurement must agree in the same commit.
   Outside ``_auth/`` there is no exception — split the module.

3. **No stale allowlist entries.** Every allowlisted path must still exist (a
   rename/delete must update the allowlist).

The ceilings below were *measured*, not estimated. To regenerate them (the
``> 1500`` filter must track ``MODULE_SIZE_BUDGET`` below)::

    python -c "from pathlib import Path; src=Path('src/notebooklm'); \
        [print(f\"{len(p.read_text(encoding='utf-8').splitlines()):>6}  {p.relative_to(src).as_posix()}\") \
         for p in sorted(src.rglob('*.py')) \
         if len(p.read_text(encoding='utf-8').splitlines()) > 1500]"

Line counting uses ``str.splitlines()`` to match the diagnostic in
``scripts/audit_test_suite.py`` (``big_files``), so the two never disagree.

Modelled after the AST/path lints in ``tests/_guardrails/`` (e.g.
``test_no_inline_deprecation_warnings.py`` / ``test_no_module_shadowing.py``).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"

# Any *new* module is forbidden from exceeding this many lines: a round 1500-line
# cap that new work must come in at/under or split before merge. The allowlist
# below is the *complete* set of modules over budget today, so the gate is green
# on main. (Raised from 900 -> 1000 once ``_chat/api.py``, ``_research.py`` and
# ``cli/source_cmd.py`` were trimmed below the round cap.)
#
# Raised 1000 -> 1500 because the headroom left under the old cap had shrunk to
# the point where ordinary feature work tripped it. The concrete case:
# ``cli/session_cmd.py`` sat at 999 on main, and #2192 — a bug fix to
# ``Notebook.is_owner`` that merely rendered a role — pushed it to 1006 and
# failed all 15 test-matrix jobs. It was resolved by extracting the ``use``
# one-row table into ``cli/_session_render.py``, which was a genuine improvement,
# but the gate fired on a module the PR was not about.
#
# NOTE the trigger is 7 lines, not 1: this gate rejects only ``n > budget``, and
# ``test_ratchet_checks_detect_their_offending_shapes`` pins that exactly-at-budget
# passes. A 999-line module absorbs one more line and stays green. An earlier draft
# of this comment claimed a one-line failure; that was wrong (caught in review) and
# is corrected here rather than quietly dropped, because the false version overstated
# the case for weakening the cap by 500 lines.
MODULE_SIZE_BUDGET = 1500

# Every module currently over budget, pinned at its ratchet ceiling. These are
# the only sanctioned exceedances; the map can only shrink and ceilings can only
# tighten when the ratchet reports slack. Paths are POSIX-relative to
# ``src/notebooklm/``.
#
# DO NOT raise a ceiling to make room for new code in a fat module — split it.
# DO lower a ceiling when a module shrinks (the gate will tell you the value).
#
# The ONE exception (ADR-0033, ``docs/adr/0033-auth-consolidation-policy.md``):
# a deliberate consolidation under ``src/notebooklm/_auth/`` may ADD its merged
# module here — or RAISE its existing entry when a later sanctioned merge lands
# in the same module — at the *measured* LOC of that PR, annotated
# ``# sanctioned merge (ADR-0033)`` naming the absorbed modules and the PR.
# That annotation is what distinguishes a sanctioned merge from ordinary
# growth; an unannotated raise is the thing this comment forbids. After the
# entry lands it is shrink-only like every other, :data:`MODULE_SIZE_BUDGET` is
# unchanged, and un-merged ``_auth`` modules stay under it. No other directory
# has this exception.
#
# The merge PR must be a PURE MOVE: new code, simplifications, and behavior
# changes land in separate commits (ideally separate PRs) so the pin measures
# the merge and not the merge plus growth. Nothing mechanical enforces that —
# this gate compares a count to a number and cannot see WHY lines exist, so a
# ceiling raise is only as trustworthy as the reviewer who read the diff.
# The pin is also EXACT, so it leaves zero headroom: a module that will keep
# growing under a governing plan takes its pin AFTER that growth, or takes the
# growth in the same PR as the pin.
ALLOWLISTED_CEILINGS: dict[str, int] = {
    # The single remaining oversized module, pinned at its measured LOC.
    # ``_chat/api.py``, ``_research.py``, ``cli/source_cmd.py``, ``_sources.py``,
    # ``_artifact/downloads.py``, and ``_source/upload.py`` were drained below the
    # 1000-line budget and removed (one-way ratchet); ``client.py`` dropped out
    # earlier when its ``__init__`` body moved to ``_client_assembly.py``.
    # ``_source/upload.py`` shed its pure decode/validation helpers to the sibling
    # ``_source/_upload_decode.py``. ``_artifacts.py`` dropped out as generation
    # and management paths moved behind the semantic Studio services; P5.8 then
    # retired the temporary verb service.
    #
    # ``exceptions.py`` is the canonical public exception home — ``__all__`` and
    # the public-surface manifest pin every class to ``notebooklm.exceptions``, so
    # the classes cannot move to sibling files without forking that home. Bumped
    # 1512 -> 1524 for ``MissingDependencyError`` (the new DEPENDENCY category's
    # public exception; #1959), then 1524 -> 1577 for ``CollectionError`` +
    # ``CollectionNotFoundError`` (the Collections domain; #2006), then
    # 1577 -> 1599 for ``LockUnavailableError`` (the canonical-storage-writer
    # fail-closed lock exception; ADR-0029 — replaces ``filelock.Timeout`` and
    # must be public so callers catch it), then 1599 -> 1575 after the private
    # response-preview helper moved to its credential-redaction home (#2132).
    "exceptions.py": 1575,
    # ``mcp/tools/sources.py`` was allowlisted at 1020 (over the then-1000-line budget
    # after #1871's shared source-policy wiring + the await_upload era). #1890 folded
    # source_add_and_wait + source_upload_bytes BACK into source_add — removing the two
    # tool bodies (and ``_add_source_to_wait_on``) drained it to ~970 LOC, back UNDER the
    # budget, so its exemption is dropped (one-way ratchet). The module is now gated
    # by MODULE_SIZE_BUDGET like any other; keep it there.
    #
    # The four ``_auth`` modules that used to be listed here did NOT lose their ratchet
    # when the budget rose 1000 -> 1500. They moved to :data:`SHRINK_LOCKED_CEILINGS`
    # below, which preserves ADR-0033's shrink-lock guarantee independently of the
    # global budget. This map is only for modules genuinely OVER budget.
}


# ADR-0033 shrink locks that must survive a budget raise.
#
# ADR-0033 requires every sanctioned ``_auth`` consolidation to stay **shrink-locked at
# its measured pin** from the moment it lands. That guarantee was originally carried by
# :data:`ALLOWLISTED_CEILINGS`, which only works while the pin sits above the budget.
# When the budget rose 1000 -> 1500 all four fell under it, and simply dropping them
# would have handed 275-410 lines of unratcheted growth back to modules that were
# deliberately drained — silently repealing ADR-0033 as a side effect of an ADR-0008
# change. (Caught in review on the budget-raise PR; the first draft did exactly that.)
#
# So the lock moved here instead. Same ratchet semantics as ALLOWLISTED_CEILINGS — may
# not grow past the pin, must tighten when it shrinks — but deliberately NOT subject to
# ``test_budget_is_below_every_allowlisted_ceiling``, because these pins are *below* the
# budget by design. That is the whole point: a module can be under the global budget and
# still be forbidden from re-accreting.
#
# DO NOT raise a pin to make room for new code. Split, or take the ADR-0033 sanctioned
# route (which requires the ``# sanctioned merge (ADR-0033)`` annotation naming donor
# and PR). DO lower a pin when a module shrinks — the gate prints the exact value.
#
# The arcs these pins lock in, for context on why they are worth keeping:
#     _auth/storage.py           3102 -> 1089   (ADR-0034 PR4..PR11B, #2172 B3)
#     _auth/browser_capture.py   1251 -> 1225   (ADR-0033 PR4.1, #2172 B3)
#     _auth/psidts_recovery.py   1222 -> 1214   (ADR-0033 load-composition merge)
#     _auth/refresh.py           1184           (ADR-0033 token-route fold)
# The per-module "sanctioned merge" / "RATCHETED DOWN" annotations are preserved in this
# file's git history at the commit that raised the budget.
#
# Note ``_auth/storage.py`` is *also* pinned at 1089 by
# ``tests/_guardrails/test_auth_profile_document_boundary.py``, for a different reason
# (a consumer boundary). Both are deliberate; a shrink there updates both.
SHRINK_LOCKED_CEILINGS: dict[str, int] = {
    "_auth/browser_capture.py": 1225,
    "_auth/psidts_recovery.py": 1214,
    "_auth/refresh.py": 1184,
    "_auth/storage.py": 1089,
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

    This is the check a consolidation PR hits first. A sanctioned ``_auth``
    consolidation clears it the same way — by adding the merged module to
    :data:`ALLOWLISTED_CEILINGS` at its measured LOC — but under rule 2's
    exception above (ADR-0033), which additionally requires the
    ``# sanctioned merge (ADR-0033)`` annotation naming the absorbed modules.
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


def test_shrink_locked_modules_do_not_exceed_their_pin() -> None:
    """ADR-0033 sanctioned modules must not re-accrete, budget raise or not.

    These pins sit *below* :data:`MODULE_SIZE_BUDGET` on purpose. Without this
    check, raising the global budget would silently repeal ADR-0033's shrink-lock
    guarantee for every already-consolidated ``_auth`` module — which is exactly
    what the first draft of the 1000 -> 1500 raise did.
    """
    grown = _grown_offenders(_measure_all(), SHRINK_LOCKED_CEILINGS)
    assert grown == {}, (
        "ADR-0033 shrink-locked module(s) grew past their pin. These are locked "
        "BELOW the global budget deliberately — being under MODULE_SIZE_BUDGET is "
        "not permission to grow. Split, or take the sanctioned-merge route with its "
        f"annotation {{path: (current, pin)}}: {grown}"
    )


def test_shrink_locked_ceilings_ratchet_down() -> None:
    """A shrunk shrink-locked module must tighten its pin to the new count."""
    slack = _slack_offenders(_measure_all(), SHRINK_LOCKED_CEILINGS)
    assert slack == {}, (
        "ADR-0033 shrink-locked module(s) shrank below their pin — lower each pin in "
        "SHRINK_LOCKED_CEILINGS to the 'current' value to lock the saved ground. Do "
        "NOT delete the entry: unlike ALLOWLISTED_CEILINGS these pins are meant to "
        f"live below the budget: {slack}"
    )


def test_shrink_locked_entries_are_disjoint_and_not_stale() -> None:
    """The two ceiling maps must not overlap, and neither may go stale.

    An overlap would mean one module answers to two pins that can drift apart;
    a stale path can never trip any check, silently weakening the gate.
    """
    overlap = sorted(set(SHRINK_LOCKED_CEILINGS) & set(ALLOWLISTED_CEILINGS))
    assert overlap == [], (
        "Module(s) appear in BOTH ALLOWLISTED_CEILINGS and SHRINK_LOCKED_CEILINGS. "
        "A module is either over budget (allowlist) or shrink-locked under it — "
        f"never both: {overlap}"
    )
    missing = _stale_entries(_measure_all(), SHRINK_LOCKED_CEILINGS)
    assert missing == [], (
        "Shrink-locked path(s) no longer exist under src/notebooklm/ (renamed or "
        f"deleted). Update SHRINK_LOCKED_CEILINGS: {missing}"
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


def test_credential_store_and_migration_modules_use_the_ordinary_budget() -> None:
    leaves = {
        "_auth/credential_io.py",
        "_auth/master_token_file.py",
        "_auth/profile_migration.py",
        "_auth/profile_store.py",
    }
    measured = _measure_all()
    assert leaves.isdisjoint(ALLOWLISTED_CEILINGS)
    assert {path: measured[path] for path in leaves} == {
        "_auth/credential_io.py": 23,
        "_auth/master_token_file.py": 89,
        "_auth/profile_migration.py": 636,
        "_auth/profile_store.py": 876,
    }
    assert (
        measured["_auth/storage.py"]
        + measured["_auth/profile_store.py"]
        + measured["_auth/cookie_filter.py"]
        + measured["_auth/profile_migration.py"]
        == 2697
    )
    synthetic = dict.fromkeys(leaves, MODULE_SIZE_BUDGET + 1)
    assert _over_budget_offenders(synthetic, {}, MODULE_SIZE_BUDGET) == synthetic


def test_phase_11d_bootstrap_extraction_modules_are_measured_exactly() -> None:
    measured = _measure_all()
    assert {
        path: measured[path]
        for path in {
            "_auth/master_token.py",
            "_auth/master_token_bootstrap.py",
            "_auth/storage.py",
        }
    } == {
        "_auth/master_token.py": 469,
        "_auth/master_token_bootstrap.py": 373,
        "_auth/storage.py": 1089,
    }


def test_phase_13_caller_cleanup_modules_are_measured_exactly() -> None:
    measured = _measure_all()
    expected = {
        "_app/login_cookie.py": 537,
        "_app/master_token.py": 223,
        "_app/profile.py": 355,
        "cli/_cookie_import.py": 153,
        "cli/master_token_login.py": 104,
        "cli/playwright_login_io.py": 254,
        "cli/profile_cmd.py": 436,
        "cli/services/auth_refresh.py": 21,
        "cli/services/login/browser_accounts.py": 364,
        "cli/services/login/chromium_accounts.py": 268,
        "cli/services/login/cookie_domains.py": 155,
        "cli/services/login/cookie_jar.py": 244,
        "cli/services/login/master_token.py": 152,
        "cli/services/login/profile_targets.py": 150,
        "cli/services/playwright_login.py": 542,
    }
    assert {path: measured[path] for path in expected} == expected
