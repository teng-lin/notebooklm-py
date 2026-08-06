# Implementation plan v2: move the master-token transaction below the CLI

Status: REVISED after 3-model momus round (agy REVISE · Codex REVISE · Claude REVISE; no BLOCK)
Base: main @ 2ce9394b (#2104 + #2105 merged) · Tracking: #2103 structural follow-up
v1 → v2: PR-0 redesigned migrate-then-drop; ledger made same-PR-atomic; chokepoint re-sited
to public paths; D6 moved under the storage-write lock; D7 four-state outcome; inventories
completed. Finding IDs below: A#=agy, C#=Codex, F#=Claude-momus.

## Goal

Relocate the headless master-token transaction (bootstrap / re-mint / ownership guard) from
`cli/services/login/master_token.py` into `_auth`, so that: (1) the re-mint sequence exists
once (today duplicated vs the L4 rung in `_auth/recovery.py:187-248`); (2) the CLI invokes
whole audited transactions, never assembles minting primitives; (3) library-owned invariants
(sibling path, `android_id` continuity, account ownership) are enforced by construction at
the mutation boundary.

## Non-goals

- No change to L4 rung locking (ADR-0030:139-142 stands; bootstrap is an entry point, not a
  rung — its internal flock is not a rung-policy change).
- No relocation of interactive login (`login/refresh.py`, `playwright_login.py`,
  `browser_accounts.py`, cookie extraction/writes/domains stay in `cli/` per ADR-0021).
- No new `auth.__all__` entries; no sidecar renaming (owner check closes P1's hazard).

## Design decisions (v2)

- **D1 — Strict kernel, policy wrappers.** One raising `_auth` primitive
  `remint_from_stored_token(storage_path)` (read record → mint → persist → reload). L4 wraps
  with swallow-to-`None` + single-flight; operator refresh wraps and lets `MasterTokenError`
  propagate (typed `master_token_refresh_failed` at `session_cmd.py:988` unchanged).
  **[F11]** The kernel reloads via the strict side-effect-free loader
  (`_build_httpx_cookies_from_storage_strict`), NOT `build_httpx_cookies_from_storage`
  (which triggers inline PSIDTS recovery: network POST + storage write). L4's wrapper keeps
  its existing recovery-loader reload semantics unchanged.
- **D2 — One-path signatures.** `master_token_path` ceases to exist as a parameter above the
  chokepoint.
- **D3 — Chokepoint in PUBLIC `notebooklm/paths.py`** **[F2/C5]**:
  `master_token_path_for(storage_path)` beside `get_master_token_path` (which becomes a thin
  profile→storage→sibling wrapper). Public siting means `_app`, `cli/`, and `_auth` all
  reach it with ZERO ledger entries — both boundary lints bar `_auth.paths` imports from
  `_app`/`cli`, so the v1 siting was illegal for 3 of 4 readers. Canonicalization decided
  here, once. PR-1 must verify no import cycle (`notebooklm/paths.py` must not import
  `_auth`; move/duplicate the `expanduser().resolve()` canonicalization locally). Fallback
  if a cycle bites: facade re-export + one ledger entry.
- **D4 — Minting denylist, honestly scoped** **[F13/C7]**: no `cli/` module may import
  `exchange_master_token` / `mint_cookies` / `persist_minted_jar` / `write_master_token` by
  name (any import shape: direct, aliased, `from .. import auth` + name binding), PLUS an
  attribute-access clause (`auth.<name>`) copied from the `test_storage_writer_boundary.py`
  clause-(v) pattern — an import-only lint is blind to `auth.mint_cookies(...)` and
  `cli/helpers.py:21` / `cli/_cookie_import.py:20` already establish the module-alias shape.
  Facade attributes stay importable everywhere else (de-blessed freeze).
- **D5 — `android_id` internalized, probe preserved** **[F12]**: `bootstrap(android_id=None)`
  resolves explicit → stored → generated inside the library (`--force` keeps the stored id —
  named test). The CLI driver KEEPS its cheap pre-capture `read_master_token` probe so a
  malformed `master_token.json` still fails BEFORE the ~300s interactive Google sign-in, not
  after (`read_master_token` stays on the ledger regardless — `_app/auth_check.py` importer).
- **D6 — Owner check UNDER the storage-write lock** **[C4]**: the comparison (token record
  `email` vs storage's persisted account) happens inside the persist intent
  (`storage_writer`), where the canonical lock already lives — closing both the TOCTOU of a
  check-before-mint and the documented-recipe bypass (`docs/python-api.md` calls
  `mint_cookies` + `persist_minted_jar` directly; a kernel-side-only check would never see
  that path). Policy: existing storage with a DIFFERENT owner → typed refusal; existing
  storage with UNKNOWN owner (no in-band metadata) → refusal unless `force` (rare after
  PR-0's promotion); no existing storage → proceed. L4 converts refusal to `False` (ladder
  continues); operator paths surface it.
  **Public commitment:** #2104 thread `discussion_r3731673393` binds PR-2 to closing the L4
  read-side cross-account re-mint — this is that closure.
- **D7 — Bootstrap machinery moves down, four-state outcome** **[F14/C2]**: the `_auth`
  bootstrap entry point internally owns the bootstrap flock (distinct from the storage-write
  lock — self-deadlock constraint moves with it), non-blocking acquire, cancellation
  settlement (hold lock until the off-thread persist settles), recheck-after-wait, and
  returns `BootstrapOutcome` ∈ {MINTED, PRESENT_AFTER_WAIT, PRESENT_ON_ENTRY, NO_TOKEN}.
  Explicit CLI mapping (the v1 boolean hid a corruption hazard at
  `playwright_login_io.py:190/:215`): `bootstrap_ready = outcome in {MINTED,
  PRESENT_AFTER_WAIT}` → mandatory passive validation (ADR-0023:38-40);
  PRESENT_ON_ENTRY and NO_TOKEN → `False` → ordinary recovery, exactly today's semantics.
  Not an ADR-0030 conflict (entry point, not rung) — state in the cross-reference.
  `cli/services/auth_refresh.py` reduces to a thin call. `filelock` is already a core dep in
  `_auth` (`account.py:15`). The real-subprocess serialization test retargets the `_auth` op.
- **D8 — ADRs amended in the same PRs as their code** — ADR-0021 (placement paragraph),
  ADR-0023 (owner check + outcome contract), ADR-0029 (`drop_legacy_account_key` note,
  Codex#3), ADR-0030 (entry-point-vs-rung cross-reference). `docs/development.md` lock table
  gains the missing `.storage_state.json.lock.bootstrap` row and fixes the already-false
  "only rotation and refresh-cmd locks canonicalize" note **[F15]**.

## PR-0 — legacy `context.json` ACCOUNT support: MIGRATE, then drop

v1's bare deletion was wrong twice **[F3, F5]**: (a) blast radius is NOT soft — deleting the
fallback makes a pre-v0.5.0 profile whose `authuser=3` lives only in the legacy sibling
resolve to `authuser=0` (`account.py:307-309`) and **silently query a different signed-in
Google account** (`_auth/headers.py` feeds it into the per-request route); (b) the home-root
population v1 "protected" is exactly the population whose migrated `context.json`
(`migration.py:48` copies it wholesale) carries the legacy account key — the two "legacy
axes" are the same users, so drop-one-keep-other was incoherent.

1. **Promotion (one-shot, crash-safe #2085 discipline):** extend the startup migration —
   when profile `storage_state.json` lacks in-band account AND the sibling `context.json`
   holds an `account` key → embed in-band via the canonical writer's account intent + strip
   the legacy key. Home-root users flow through layout-migration → promotion, so the
   home-root fallback (kept, deprecation-routed via `_deprecation.warn_deprecated(...,
   removal="0.9")` — inline `warnings.warn` is guardrail-banned, **[F7]**) stays coherent.
2. **Then delete the standing read path:** `_read_legacy_account` + fallback branch
   (`account.py:252,293`); `_drop_legacy_account_key` post-write call sites
   (`cookie_writes.py:276`, `refresh.py:481`) + facade export (auth.py) + ledger entry.
   **KEEP** an unconditional legacy-key scrub inside `clear_account_metadata` **[F6]** —
   deleting it would silently downgrade logout from full scrub to partial, leaving the
   user's email at rest forever; `_account_context_path` survives as the scrub's single
   private site (facade alias `auth.py:224` deleted).
3. **Complete inventory** **[C3, F4]**: tests — `test_auth_account_coverage.py` (~10 sites),
   `test_swallow_observability.py:208` (premise rewrite, not mechanical),
   `test_profile_atomic_write.py:100-163,211-228,393-402` (acceptance-critical fixtures
   become MIGRATION tests — the promotion must satisfy their intent: no user loses account
   binding), `test_storage_writer_boundary.py` frozen inventories if `atomic_write_json`
   leaves `account.py`; docs/comments — `docs/development.md:258-265`,
   ADR-0029:154-160, `_auth/tokens.py:39-42`, `_auth/storage.py:330-332`,
   `storage_writer.py:430-435`.
4. **Ledger/freeze bookkeeping (same PR)** **[F1, F9]**: remove `drop_legacy_account_key`
   from `AUTH_CROSS_BOUNDARY_NAMES`; add to `_AUTH_DEBLESSED_KEEP_IMPORTABLE` (25 → 26,
   update the count assertion); refresh the api-compat allowlist reason string that cites
   the ledger freeze. No new api-compat allowance needed (audit collects `__all__`+extras
   only; none of these are in either — C3).
5. CHANGELOG under **Removed** + `docs/deprecations.md` record (fallback removed now with
   migration; home-root layout removal filed for 0.9).

## PR-1 — chokepoint + reader convergence (intentional path-semantics change) **[A3/C6/F10]**

NOT "pure refactor": readers become resolve-aware, and routing `get_master_token_path`
through storage-path derivation changes legacy-root selection. Pin outcomes as named tests
BEFORE the change: ordinary profile, legacy home-root, relative path, `~`, symlinked dir.
- `paths.py::master_token_path_for(storage_path)` (public, D3) + cycle check.
- Converge the four sites (`recovery.py:223`, `auth_check.py:231`, `auth_refresh.py:62`,
  `master_token_login.py`).
- Literal-string guardrail: `"master_token.json"` in exactly one production module.
- No ledger change (public siting).

## PR-2 — the move (kernel + wrappers + seams + SAME-PR bookkeeping)

- `_auth/master_token.py` gains kernel + `bootstrap_from_oauth_token` + owner check +
  `BootstrapOutcome` (≈480 lines total — comfortably under the 1000 cap, F17; the v1
  sibling-module contingency is dropped). `session_cmd.py` stays net-zero (AT the cap).
- L4 (`_run_master_token_reauth`) delegates to the kernel, keeping single-flight, swallow
  policy, and its existing reload semantics.
- CLI keeps `capture_oauth_token` + the D5 pre-capture probe; driver + `auth_refresh.py`
  call the facade ops.
- **Ledger, same PR** **[F1/C1, F8]**: −5 (`exchange_master_token`, `mint_cookies`,
  `persist_minted_jar`, `write_master_token`, `generate_android_id`) +3
  (`master_token_bootstrap`, `master_token_remint`, `assert_account_writable` — the guard is
  called pre-capture by the Click driver, pinned by `test_login_master_token.py:273-295`, so
  it must stay CLI-callable). `_AUTH_DEBLESSED_KEEP_IMPORTABLE` 26 → 31; count assertion +
  allowlist reason strings refreshed **[F9]**.
- Tests: 10 `patch.object(mt_service, …)` seams in `test_login_master_token.py` (not ~6,
  **[F16]**) + `test_auth_refresh_cold_start.py:153`; retargeted seams must respect
  `test_no_forbidden_monkeypatches.py` patterns (d)/(e) — patch via facade/public homes.
- Docs: rewrite the `docs/python-api.md` headless recipe onto the coarse ops **[A2]** (it
  currently demonstrates the #2103 two-independent-getters pattern);
  `architecture.md:548,652,1198,1296,1333` **[F15]**.
- Live-verify before merge **[C8]**: isolated `NOTEBOOKLM_HOME` with a copied throwaway
  profile — `login --master-token-refresh` (kernel path) then `auth refresh` with storage
  deleted (bootstrap path), notebook-count as evidence; alternatively a green
  `verify-artifacts.yml` dispatch (it exercises `login --master-token-refresh`).

## PR-3 — denylist + ADR text + residual docs

- D4 guardrail (with attribute-access clause + self-tests for all import shapes).
- ADR-0021 amendment paragraph; deprecations.md; any docs not consumed by PR-0/PR-2.
- PR-3 carries NO ledger changes (all were same-PR'd away — F1).

## Ledger accounting (corrected, F8)

30 → **29** (PR-0: −`drop_legacy_account_key`) → **29** (PR-1: public siting, no entry) →
**27** (PR-2: −5 +3). De-blessed freeze: 25 → 26 (PR-0) → 31 (PR-2). The CLI ends with zero
minting-primitive imports; the denylist locks that.

## Momus disposition (all 3 reviewers)

Accepted and folded: A1/A2/A3, C1–C8 (C-NIT base SHA fixed in header), F1–F16, F18.
F17 accepted as relief (line-budget worry dropped). No finding rejected; agy's "will break
python-api.md imports" corrected to "must be rewritten" (imports keep resolving via the
de-blessed freeze — verified).

## Risks (v2)

| Risk | Mitigation |
|---|---|
| Promotion misses an edge → user loses account binding (F3 class) | acceptance-critical fixtures become migration tests; promotion runs before any deletion lands |
| `paths.py` ↔ `_auth` import cycle at D3 | checked first in PR-1; facade-re-export fallback documented |
| Seam rewrites (10 + 1 sites) regress coverage | behavior tests stay CliRunner-level; concurrency tests move to the `_auth` seam (subprocess test retargets) |
| Owner check refuses a legitimate fresh mint | no-storage → proceed; unknown-owner refusal is force-overridable and rare post-promotion |
| `master_token_refresh_failed` contract drift | pinned test untouched |
| Live-verify needs real creds | throwaway-profile procedure above; verify-artifacts.yml as CI-side alternative |
