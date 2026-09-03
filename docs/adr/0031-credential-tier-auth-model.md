# ADR-0031: Credential-tier domain model for `_auth`

## Status

Proposed — Stage 0 (the single RotateCookies wire contract) landed with this
ADR and its raw wire now lives in `_auth/mint_service.py` per ADR-0034 Phase 11C;
Stages 1–5 are sequenced follow-up work, each independently shippable.

Companion to [ADR-0029](0029-canonical-storage-writer.md) (write side) and
[ADR-0030](0030-one-recovery-ladder.md) (recovery/refresh side). Where those
unified one *flow* each, this ADR names the domain model both flows already
implement implicitly, so future auth work has types and named operations to
land on instead of free functions to scatter.

## Context

The auth cross-boundary ledger audit (#2139 and its follow-up review) traced
every name `cli/`/`_app/` import from the `notebooklm.auth` facade and found
that the hard-to-classify cases were all symptoms of one gap: `_auth` is
organized by *topic* (`cookies.py`, `account.py`, `cookie_policy.py`,
`storage_writer.py`) rather than by the domain model its own docstrings
describe. Concretely:

- **Noun sprawl.** A cookie exists in six shapes: the raw Playwright
  storage-state dict, the rookiepy dict, `DomainCookieMap`
  (`(name, domain, path) → value`), `FlatCookieMap` (`name → value`),
  `LegacyDomainCookieMap`, and the "sanitized entries" list
  (`cookies._sanitized_auth_entries`). `AuthTokens` carries cookies **twice**
  (`cookies: DomainCookieMap` + `cookie_jar: httpx.Cookies`) and reconciles
  the pair in `__post_init__`.
- **Verb sprawl.** Ten-plus verbs (mint / bootstrap / exchange / remint /
  rotate / poke / recover / heal / reauth / refresh / validate) name about six
  concepts. Worst case: the POST to `accounts.google.com/RotateCookies` was
  independently assembled at **four** sites (`keepalive._rotate_cookies`,
  `psidts_recovery._attempt_rotation`, `psidts_recovery.recover_psidts_in_memory`,
  and the mint leg in `master_token.mint_cookies`), one of which
  silently omitted `raise_for_status` — protocol drift in the
  credential-rotation path with nothing keeping the copies in sync.
- **Coupling with no owner.** Validation helpers
  (`_validate_required_cookies`, `_has_valid_secondary_binding`,
  `_validate_routable_entries`) are shared between the core recovery ladder
  and the CLI browser-cookie import — not because those flows are related,
  but because "is this cookie set usable" has no type to be a method of, so
  every flow reaches for the same scattered private functions. This is what
  made several "move it across the boundary" proposals in the audit unsafe:
  three names that looked adapter-local turned out core-coupled through
  hidden private-helper fan-out.

The model the docstrings already describe is a **credential tier system**:

```text
Tier 0: DURABLE   master_token.json | persistent browser profile | refresh_cmd
        (~years)        │ mint
                        ▼
Tier 1: SESSION   cookie jar (SID, PSIDTS, bindings…)
        (~days)         │ rotate (PSIDTS) · validate/heal · persist/load
                        ▼ fetch
Tier 2: REQUEST   csrf_token + session_id            (~minutes, per RPC)
```

Every operation in the subsystem is one of five things: **mint** (tier N →
N+1), **rotate/heal** (refresh a component within a tier), **validate**
(check a tier is usable), **persist/load** (move a tier to/from the profile),
**identify** (bind credentials to `{email, authuser}`). The ADR-0030 ladder
is "walk down the tiers until something works, then mint back up": L1
re-fetches Tier 2, L2 rotates within Tier 1, L3/L4 re-mint Tier 1 from two
different Tier-0 sources.

## Decision

Adopt the credential-tier model as the organizing principle for `_auth`, and
introduce its objects and operations in independently shippable stages:

- **Stage 0, with the Phase 11C owner update: one RotateCookies wire contract.**
  The raw URL/body/headers/kwargs and async/sync POST functions live only in
  `_auth/mint_service.py`; `_auth/keepalive.py` imports/re-exports them and owns
  throttle/recovery policy plus `_rotation_http_client`. The four former sites
  remain thin policy/adapter callers. Enforced by
  `tests/_guardrails/test_rotate_wire_contract.py`.
- **Stage 1: `CookieJar`** — one canonical cookie type
  (`_auth/cookie_types.py`, completing the `cookie_semantics` /
  `cookie_policy` family) with constructors (`from_storage_state` /
  `from_rookiepy` / `from_domain_map`), converters (`to_httpx` /
  `to_storage_state` / `to_domain_map`), and the policy questions as methods
  (`names` / `validate_required` / `has_secondary_binding` / `is_rotatable` /
  `missing_hint`). A non-breaking wrapper: every decision still delegates to
  the free function that owns it, pinned by equivalence tests. A shrink-only
  ratchet (`tests/_guardrails/test_cookie_conversion_ratchet.py`) blocks *new*
  bespoke conversion call sites; the five existing ones are grandfathered with
  the stage that retires each, pinned at their measured call counts so a module
  already on the list cannot grow a second one for free, and the allowlist may
  only shrink.

  **The flat `name -> value` shape is deliberately not on the type.** It
  collapses the path component (#369) and picks an arbitrary winner among
  same-tier domains, so the survivor changes when `storage_state` is reordered
  (#2054) — `AuthTokens.flat_cookies` documents itself as "lossy, and not
  correct for building a request". Every remaining caller is back-compat (the
  public property, and `_update_cookie_input`'s write-back into a
  legacy-shaped caller dict). Carrying it onto the canonical type would import
  the footgun into the model meant to retire it, so it stays reachable only
  through the legacy free function, and `to_httpx()` is the path- and
  domain-correct route for cookies on the wire.
- **Stage 2: `validate` / `heal` split** —
  `validate(rows) → tuple[dict[str, Any], ValidationResult]` is pure (no
  network, no mutation; wraps the closed-enum
  `RequiredCookieValidationError` #2061 introduced rather than replacing it).
  It returns the **converted storage state alongside** the result, not the
  result alone, so a caller that needs the converted form — as every caller of
  the wrapper does — does not pay for a second conversion. `heal(rows) → bool`
  is the named seam whose only strategy today is the Stage-0
  rotate. `validate_with_recovery` survives unchanged as the compatibility
  wrapper — it has four first-party callers, a `RefreshDeps` injection seam,
  an entry in the cross-boundary ledger, and an in-place mutation contract
  `cli/services/login/refresh.py` depends on.

  Splitting it surfaced an asymmetry the fused control flow hid: the post-heal
  re-check runs the Tier-1 **presence** check only, never the RFC 6265 routing
  preflight. That is intentional (the heal exists to mint a PSIDTS that routes,
  so a successful rotation already established what the preflight would
  re-litigate) but it was previously invisible. It is now documented and pinned
  by a test that fails on the naive "just re-run `validate()`" refactor.

  **Stage 2 does not adopt the Stage-1 `CookieJar` at this seam.** `validate`
  and `heal` still take the raw rookiepy `list[dict[str, Any]]` rows. The
  routing preflight deliberately reads those rows rather than the converted
  state — the two shapes spell the http-only flag differently (`http_only` vs
  `httpOnly`) — and the in-place mutation contract
  `cli/services/login/refresh.py` depends on *is* a mutation of the caller's
  row list. Moving this seam onto `CookieJar` is deferred to a later stage
  rather than folded in here, so that the "identical behavior" claim this
  stage rests on stays checkable.
- **Stage 3: the storage-write transaction template** — COMPLETE, and both
  modules named below are now re-export shims. *Amended 2026-08-08 (`_auth`
  consolidation, ADR-0033):* `storage_writer.py` and `storage_transaction.py`
  were absorbed into `_auth/storage.py`, and the last three writers
  (`replace_from_remint`, `replace_from_login`, `persist_minted_jar`) were
  converted, so no writer hand-rolls the preamble and the ratchet's
  `_UNCONVERTED` list is empty and pinned empty. The original text follows.
  — the writers in
  `storage_writer.py` each hand-roll the same preamble (secure the parent dir,
  derive the lock path, take the bounded lock, branch on held).
  `storage_transaction.in_storage_transaction` owns it; method names and
  `persist_minted_jar`'s #2108 ownership-guard and write-ordering are preserved
  bit-for-bit.

  **Investigation corrected this stage's premise.** The writers are not uniform,
  and the plan above ("one object") would have flattened a real distinction.
  On lock-unavailable there are **two intents**:

  - *must-know* — the write mattered and a caller proceeding as though it
    happened is wrong (five writers);
  - *tolerable* — the write was cleanup and a miss degrades gracefully (one).

  Must-know has **two mechanisms**, and the split is forced by the writers'
  return channels rather than by intent: `-> None` has nowhere to report, and
  `update_account_metadata`'s `-> bool` already spends `False` on "deliberately
  skipped (`only_if_absent`)", so both raise; the two full-replace writers have
  rich outcome enums with room for a distinct `LOCK_UNAVAILABLE`, so they
  report. Each choice is locally forced; the inconsistency is one level up, in
  writers doing morally identical things having different return types.

  `merge_cookie_delta` is exempt by design, not oversight — it takes the
  *blocking* `_file_lock_exclusive` and skips the parent-dir prep (it only
  updates a file that already exists). Different operation, not a variant.

  Two follow-ups this surfaced, deliberately **not** taken here:

  1. *Unify the must-know channel.* Giving every must-know writer a rich outcome
     type would collapse raise-vs-report to one mechanism, but it is a breaking
     change for callers that today catch `OSError`/`TimeoutError` around
     `persist_minted_jar` and `update_account_metadata` — it needs the
     deprecation runway, not a refactor stage.
  2. *Revisit the tolerable case.* `clear_in_band_account`'s swallow is
     justified functionally (the legacy reader still resolves the record), but
     the operation is **privacy**-motivated — "a stale key must not leave the
     account email at rest". A swallowed failure leaves exactly that email on
     disk. The swallow is defended on a different axis than the one that
     matters most; promoting it to must-know would let a best-effort cleanup
     fail a caller, so it wants its own decision rather than a silent change.
- **Stage 4: `AuthTokens.cookies: CookieJar`** — the dual
  `cookies`/`cookie_jar` fields collapse. The only stage touching documented
  public API; requires a `Mapping`-compatible shim or a deprecation runway
  per the breaking-change policy.
- **Stage 5: mode packages** — the acquisition modes (master-token,
  browser-cookie import, Playwright login) become self-contained packages
  composing the named operations, mirroring what `browser_capture.py`
  already is for the Playwright capture half.

**Boundary principle** (resolves the recurring "does this belong in
`_auth`/`_app`/`cli`" question): a mint strategy that runs **unattended**
(master-token remint, headless capture, refresh_cmd, rotation) belongs to the
library and may sit on the automatic ladder; a strategy that needs a
**human** (interactive Playwright login, browser-cookie extraction, OAuth
capture) is driven by the CLI. Both produce the same session jar and persist
through the same writer. What crosses the facade boundary is the shared
vocabulary — types and named tier operations — never a mode's internals.

**Amended 2026-09-02:** ADR-0036 applies that principle without reviving the
deferred Stage-5 mode-package rewrite inside `_auth`. Browser acquisition is a
separate `_browser` implementation package; interactive ordering is
`_app/login_browser.py`; and first-party consumers receive only five coarse
lazy operations plus two canonical policy aliases through `auth.py`. Browser
plan/result/protocol types remain private to their owners and never cross the
facade.

## Consequences

- A protocol change to Google's rotation contract now lands in one place;
  the guardrail makes a fifth bespoke POST a test failure, not a review
  catch. The mint leg additionally gains the `raise_for_status` the other
  sites already had (a silent 4xx/5xx becomes a logged, skipped rotation —
  same control flow, better observability).
- Each later stage retires a class of scattered helpers by giving them an
  owner, which is what makes future boundary questions answerable
  structurally ("does the type cross?") instead of by hand-tracing call
  graphs.
- **Costs, honestly:** every stage churns ADR-0007 patch seams (tests patch
  owning modules at bare-name call sites, so relocating a body moves its
  seam — this bit #2139's Phase 4 directly); the guardrail ledgers
  (`AUTH_CROSS_BOUNDARY_NAMES`, de-blessed lists, the public-import
  manifest) need same-PR bookkeeping; Stage 4 is a real public-API change
  and must not ship as a side effect of an internal stage.
- Stages are ordered by risk-to-value: each is independently green,
  independently reversible, and none blocks the others except 4-after-1.

## Alternatives considered

- **Keep shrinking the cross-boundary ledger case-by-case.** Rejected: the
  #2139 audit ended with every remaining entry either core-critical, a
  documented feature, or irreducible without exactly the type/operation
  consolidation this ADR names. The ledger is a symptom meter, not the
  disease.
- **Move adapter-local names into `cli/`/`_app/` wholesale.** Rejected on
  evidence: three candidates that looked CLI-only (`build_cookie_jar`,
  `validate_with_recovery`, `extract_cookies_from_storage`) turned out to be
  load-bearing in the client's own token-construction and recovery chains
  through internal `_auth` call paths the facade-level audit cannot see.
  Moving code before the model exists just relocates hidden coupling.
- **One big rewrite.** Rejected: the same audit produced three separate
  "obviously safe" changes that were wrong on first pass. Staged, per-stage
  verification is the only approach that has actually held up in this
  subsystem.
