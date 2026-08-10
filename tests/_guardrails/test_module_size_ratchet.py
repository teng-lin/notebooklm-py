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
``> 1000`` filter must track ``MODULE_SIZE_BUDGET`` below)::

    python -c "from pathlib import Path; src=Path('src/notebooklm'); \
        [print(f\"{len(p.read_text(encoding='utf-8').splitlines()):>6}  {p.relative_to(src).as_posix()}\") \
         for p in sorted(src.rglob('*.py')) \
         if len(p.read_text(encoding='utf-8').splitlines()) > 1000]"

Line counting uses ``str.splitlines()`` to match the diagnostic in
``scripts/audit_test_suite.py`` (``big_files``), so the two never disagree.

Modelled after the AST/path lints in ``tests/_guardrails/`` (e.g.
``test_no_inline_deprecation_warnings.py`` / ``test_no_module_shadowing.py``).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"

# Any *new* module is forbidden from exceeding this many lines: a round 1000-line
# cap that new work must come in at/under or split before merge. The allowlist
# below is the *complete* set of modules over budget today, so the gate is green
# on main. (Raised from 900 once the sub-1000 modules — ``_chat/api.py``,
# ``_research.py``, ``cli/source_cmd.py`` — were trimmed below the round cap.)
MODULE_SIZE_BUDGET = 1000

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
# entry lands it is shrink-only like every other, the 1000-line budget is
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
    # ``_source/_upload_decode.py``. ``_artifacts.py`` dropped out once its
    # ``generate_*`` / ``revise_slide`` / ``retry_failed`` kickoff paths moved to
    # the sibling ``_artifact/generation.py`` (``ArtifactGenerationService``).
    #
    # ``exceptions.py`` is the canonical public exception home — ``__all__`` and
    # the public-surface manifest pin every class to ``notebooklm.exceptions``, so
    # the classes cannot move to sibling files without forking that home. Bumped
    # 1512 -> 1524 for ``MissingDependencyError`` (the new DEPENDENCY category's
    # public exception; #1959), then 1524 -> 1577 for ``CollectionError`` +
    # ``CollectionNotFoundError`` (the Collections domain; #2006), then
    # 1577 -> 1599 for ``LockUnavailableError`` (the canonical-storage-writer
    # fail-closed lock exception; ADR-0029 — replaces ``filelock.Timeout`` and
    # must be public so callers catch it) — all irreducible additions to this home.
    "exceptions.py": 1599,
    # sanctioned merge (ADR-0033) — the `_auth` persistence merge: the seam that
    # was spelled as three cap-split files becomes one deep module.
    # ``_auth/storage_writer.py`` (981) and ``_auth/storage_transaction.py``
    # (183) were absorbed in full and reduced to re-export shims in the
    # same change; the merged module carries the lock primitives, the bounded acquire,
    # the transaction template, the snapshot types, the CAS/merge math and the
    # seven intent writers. Pinned at its MEASURED post-merge LOC (a sanctioned
    # entry is a pin, not a budget: shrink-locked from here on). Later sanctioned
    # merges into this same module (the write-time cookie-filter relocation and the
    # account-record relocation) raise it under their own fresh annotations.
    #
    # sanctioned template adoption (ADR-0033 decision 1, third class) — PR 1.2
    # 2149 -> 2183. ADR-0031 Stage 3 finished here: the last three writers still
    # hand-rolling the four-step lock preamble (``replace_from_remint``,
    # ``replace_from_login``, ``persist_minted_jar``) now route through
    # ``in_storage_transaction``. This class has NO donor — the growth is
    # intra-module, because a template call site plus its ``body`` closure header
    # and explicit success return cost more lines than the inline preamble they
    # replace (measured precedent: #2152 grew the same code by 22 lines while
    # converting three writers). The evidence the duplication really went is
    # test_storage_transaction_ratchet's ``_UNCONVERTED``, which this commit takes
    # to EMPTY — so this class is now exhausted for this module.
    #
    # sanctioned merge (ADR-0033) — the write-time cookie-filter relocation, the
    # SECOND sanctioned consolidation into this module (PR 4.2): 2183 -> 2416.
    # DONOR: ``_auth/_browser_cookie_filter.py``, 233 -> 29 (-204), reduced to a
    # re-export shim in the same commit. This is the donor-shrinking relocation
    # class, not the intra-module template class above: the filter
    # (``filter_storage_state_cookies_by_domain_policy`` + its value-free
    # malformed-row diagnostics) is write-time policy that only wore a
    # ``browser_`` name because the capture arms were its first callers — three
    # of its six call sites are the intent writers here, each of which reached
    # it through a function-local import that this commit deletes.
    #
    # The +233 exceeds the donor's -204 by 29 lines, and the gap is deliberate,
    # not slop: this PR's other half is the ADR-0033 D3 comment correction. The
    # old prose framed the writer's filter pass as "idempotent with the call
    # above" — i.e. as redundancy, the exact reading that would get one of the
    # two passes deleted. Both are required (the capture-side pass feeds
    # ``heal_captured_state``; the writer's is ADR-0029's entry-path-independent
    # guarantee), so each now states its own reason, and the merged module gains
    # a labelled section header explaining why write-time policy lives here.
    # No behaviour changed: same function objects, same ``notebooklm.auth``
    # logger (both modules bound it by NAME, so no log-emission point moved).
    # Pinned at its MEASURED post-relocation LOC; shrink-locked from here on.
    # PR 5.2's account-record relocation, annotated below, is the last raise.
    #
    # RATCHETED DOWN 2419 -> 2412 by PR 5.1 (the account-read write-timing
    # move). ``update_account_metadata`` lost its ``deadline_seconds``
    # parameter and the paragraph justifying it: the only caller that ever
    # passed one was ``account.promote_legacy_account``, which shortened the
    # lock deadline to 2s purely because it ran inside a per-RPC READ. It no
    # longer runs there, so the override had no caller and its rationale had
    # become false. This is an ordinary shrink, not a sanctioned class — the
    # ratchet demanded it, and the ground is now locked.
    #
    # sanctioned merge (ADR-0033) — the account-record relocation, the THIRD
    # and LAST planned consolidation into this module (PR 5.2): 2412 -> 3102.
    # DONOR: ``_auth/account.py``, 980 -> 355 (-625). This is the
    # donor-shrinking relocation class (the same one PR 4.2 used), not the
    # intra-module template class: ``account.py`` was two modules wearing one
    # name. The NETWORK identity half — ``enumerate_accounts``,
    # ``_probe_authuser``, ``extract_email_from_html``, the ``authuser`` wire
    # formatters, and ``repair_account_metadata_from_playwright_storage``,
    # which recomposes over both halves — stayed there and is now the whole
    # file. The account RECORD half (readers, ``_sanitize_legacy_account_record``,
    # the detached promotion one-shot with its ``atexit`` drain,
    # ``write_account_metadata`` / ``clear_account_metadata``, and the sibling
    # ``context.json`` scrub) is persistence: same document, same lock, driving
    # the two in-band account writers already here.
    #
    # The evidence the split was structural and not cosmetic: it cost a
    # BIDIRECTIONAL function-local import pair — 3 ``from . import storage``
    # sites in ``account.py``, 5 ``from . import account as _account`` sites
    # here — and this commit deletes all 8, leaving one module-scope edge
    # (``account`` -> ``storage``) in a single direction. (Note for the record:
    # the governing plan said "8 sites, verified exact" and was RIGHT; a
    # mid-flight recount that reported 4 had missed the parenthesised
    # multi-line ``from . import (\\n    account as _account,\\n)`` spelling.)
    #
    # +690 here against the donor's -625 leaves a 65-line gap, and like PR 4.2's
    # 29 it is deliberate: the ``SECTION 7b`` banner and the module-docstring
    # entry that say WHY persistence code lives beside the writers, eight new
    # ``__all__`` entries, the ``atexit``-registration-moved-modules paragraph
    # (the single riskiest thing in the move — see ``_drain_promotions_at_exit``),
    # and the ``_drop_legacy_account_key`` note explaining why the ONE writer
    # here that does not touch ``storage_state.json`` uses the guarded public
    # ``atomic_write_json`` and a ``filelock`` sibling lock. No behaviour
    # changed: same function objects, same ``notebooklm.auth`` logger (both
    # modules bound it by NAME), same facade names at the same identities.
    #
    # RATCHETED DOWN 3102 -> 2829 by ADR-0034 PR4: process/OS lock mechanics,
    # bounded retry, the raw-key thread-lock registry, and warning-once state
    # moved to the dependency-bottom ``_auth/storage_lock.py`` owner. Storage
    # retains only the v0.x wrappers, secure-parent policy, and transaction
    # routing. This is an ordinary shrink pin with zero banked slack.
    #
    # RATCHETED DOWN 2829 -> 2563 by ADR-0034 PR5: the deterministic snapshot/CAS
    # merge and permanent no-baseline overlay moved to the dependency-bottom
    # ``_auth/cookie_merge.py`` leaf. Storage keeps the blocking transaction,
    # corruption and logging policy, compatibility adapters, and sole raw write.
    # This is another ordinary shrink pin with zero banked slack; the new leaf
    # remains under the ordinary 1000-line budget and has no exemption here.
    #
    # RATCHETED DOWN 2563 -> 2329 by ADR-0034 PR6: the sealed typed commit spine,
    # path-owned cookie transactions, document reads, and common bounded-lock
    # template moved to ``credential_io.py`` and ``profile_store.py``. Storage
    # keeps v0.x policy/adapters and five temporarily adapted profile writers.
    # Both new owners remain under the ordinary budget with no exemption.
    #
    # RATCHETED DOWN 2329 -> 2210 by ADR-0034 PR7A: typed in-band account
    # read/update/clear policy moved into ``ProfileStore`` while storage keeps
    # only the raw compatibility adapters and legacy reconciliation/scrub.
    #
    # RATCHETED DOWN 2210 -> 1905 by ADR-0034 PR7B: the raw capture/domain
    # filter moved to ``cookie_filter.py`` and browser/remint destination carry,
    # bounded transaction, and commit moved into ``ProfileStore``. Storage keeps
    # the exact v0.x adapter and the login/minted-session writers.
    #
    # RATCHETED DOWN 1905 -> 1771 by ADR-0034 PR7C: login/import filtering,
    # required-cookie gating, directive-specific namespace construction, backup,
    # and commit moved into ``ProfileStore``. Storage keeps the exact v0.x adapter
    # and post-success legacy reconciliation; minted-session replacement remains.
    #
    # RATCHETED DOWN 1771 -> 1683 by ADR-0034 PR7D: minted-session snapshot and
    # error projection remain here while the owner/filter/document/commit body
    # moved to ``ProfileStore``.
    #
    # RATCHETED DOWN 1683 -> 1150 by ADR-0034 PR8: lossless legacy-account
    # resolution, context I/O, promotion lifecycle, and post-write reconciliation
    # moved to the ordinary-budget ``_auth/profile_migration.py`` owner.
    # RATCHETED DOWN 1150 -> 1131 by ADR-0034 PR11B: token credential
    # encoding, secure-parent preparation, lock ownership, and commit moved to
    # the path-owned ``MasterTokenFile``.
    "_auth/storage.py": 1127,
    # sanctioned merge (ADR-0033) — the `_auth` load-composition merge:
    # ``_auth/browser_cookie_recovery.py`` (142) was absorbed in full and reduced
    # to a re-export shim in the same change. It held the captured-cookie
    # ``validate`` / ``heal`` / ``validate_with_recovery`` seam, which existed in
    # its own file only so this module stayed under the budget — reached through
    # a 4-line pass-through that lazily imported back into the leaf while the
    # leaf imported this module at module scope (a two-node cycle). The merged
    # module also gains the single load -> heal -> retry composition the two
    # public load wrappers used to spell out (net of the copy deleted from
    # ``_auth/tokens.py``). Pinned at its MEASURED post-merge LOC (a sanctioned
    # entry is a pin, not a budget: shrink-locked from here on).
    #
    # DEFERRED, and the deferral is a real constraint on whoever picks it up:
    # the plan wanted this module's deps record to land in the SAME change,
    # because a pin leaves zero headroom. It cannot. The deps record exists to
    # retire the 11 module-scope patch-protocol aliases, and the 87-test PSIDTS
    # suite patches exactly those aliases — so landing it requires editing the
    # suite that pins the #2061 decline->retry contract, which this change was
    # required to leave untouched. The hard constraint wins. Consequence: a
    # later deps-record change must NET-SHRINK this module (a deps dataclass
    # plus threading against ~20 deleted alias lines is roughly neutral), or it
    # needs an ADR-0033 amendment — the template-adoption class does not reach
    # new structure. A call-time ``heal`` seam already exists on
    # ``load_with_recovery`` / ``load_session_jar`` as the first step.
    "_auth/psidts_recovery.py": 1222,
    # sanctioned merge (ADR-0033) — the `_auth` token-route fold: ``_auth/headers.py``
    # (68 lines, one function — ``_resolve_token_route_kwargs`` — whose only three
    # call sites are the token-fetch entry points here) was absorbed in full and
    # DELETED in the same change, so the donor is gone entirely. The same PR also
    # colocated the cold-start fallback sequence (``_cold_fallbacks``) and landed
    # the refresh deps record, which is why the fold and that work had to ship
    # together: a sanctioned entry is a pin, not a budget, so it leaves ZERO
    # headroom and the module may cross the 1000-line budget only ONCE. Pinned at
    # its MEASURED post-PR LOC; shrink-locked from here on.
    #
    # The ladder-alignment change (cold start reordered to ADR-0030's L2.5 → L3
    # → L4) landed net-neutral at the then-current pin. Phase 12A moved its
    # operation-scoped control flow behind ``ColdRecoveryCoordinator`` while
    # retaining this module's late-bound callback composition and exact public
    # adapters. Phase 12B then replaced the legacy cookie-saver adapter with a
    # direct typed ``ProfileStore`` merge, shrinking the measured module by a
    # further five lines; freeze all saved ground immediately.
    "_auth/refresh.py": 1184,
    # sanctioned merge (ADR-0033) — the `_auth` browser-cluster merge (PR 4.1):
    # ``_auth/browser_state_validation.py`` (56) and ``_auth/login_wait_trace.py``
    # (181) were absorbed in full and reduced to re-export shims in the same
    # change. Both existed only to keep this file under the budget — the capture
    # core was already the sole consumer of each (the validation bridge's two
    # callers and the tracing's three call sites are all in this module), which is
    # why the leaves failed the deletion test. ``browser_launch_errors.py`` is NOT
    # part of this merge: ``classify_launch_failure`` has a second, independent
    # consumer in ``cli/services/login/master_token.py``, so it stays a real leaf.
    # Pinned at its MEASURED post-merge LOC (a sanctioned entry is a pin, not a
    # budget: shrink-locked from here on, and the plan schedules no further growth
    # of this module).
    "_auth/browser_capture.py": 1251,
    # ``mcp/tools/sources.py`` was allowlisted at 1020 (over the 1000-line budget after
    # #1871's shared source-policy wiring + the await_upload era). #1890 folded
    # source_add_and_wait + source_upload_bytes BACK into source_add — removing the two
    # tool bodies (and ``_add_source_to_wait_on``) drained it to ~970 LOC, back UNDER the
    # 1000 budget, so its exemption is dropped (one-way ratchet). The module is now gated
    # by MODULE_SIZE_BUDGET like any other; keep it there.
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
        "_auth/profile_migration.py": 375,
        "_auth/profile_store.py": 864,
    }
    assert (
        measured["_auth/storage.py"]
        + measured["_auth/profile_store.py"]
        + measured["_auth/cookie_filter.py"]
        + measured["_auth/profile_migration.py"]
        == 2462
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
        "_auth/master_token.py": 455,
        "_auth/master_token_bootstrap.py": 373,
        "_auth/storage.py": 1127,
    }


def test_phase_13_caller_cleanup_modules_are_measured_exactly() -> None:
    measured = _measure_all()
    expected = {
        "_app/login_cookie.py": 533,
        "_app/master_token.py": 216,
        "_app/profile.py": 355,
        "cli/_cookie_import.py": 153,
        "cli/master_token_login.py": 101,
        "cli/playwright_login_io.py": 254,
        "cli/profile_cmd.py": 436,
        "cli/services/auth_refresh.py": 21,
        "cli/services/login/browser_accounts.py": 365,
        "cli/services/login/chromium_accounts.py": 266,
        "cli/services/login/cookie_domains.py": 155,
        "cli/services/login/cookie_jar.py": 244,
        "cli/services/login/master_token.py": 152,
        "cli/services/login/profile_targets.py": 150,
        "cli/services/playwright_login.py": 539,
    }
    assert {path: measured[path] for path in expected} == expected
