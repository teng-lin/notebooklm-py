# ADR-0023: Master-token headless auth

## Status

Accepted.

## Context

The client authenticates to consumer NotebookLM with browser-captured Google
cookies (`storage_state.json`). Those cookies are short-lived: `__Secure-1PSIDTS`
rotates, `__Secure-1PSID` is eventually culled, and there is no unattended way to
re-acquire them — a human must re-run `notebooklm login` in a browser. This makes
true headless / long-lived / CI usage fragile (see persona D in
[installation.md](../installation.md)).

The #1638 spike evaluated an Android gRPC backend as a cookie-free alternative
and, in doing so, proved something smaller and more useful: a durable Google
**master token**
(`aas_et/…`, obtained once from `accounts.google.com/EmbeddedSetup`) can **mint
fresh NotebookLM web cookies on demand** off-device, no browser per session
(`OAuthLogin → uberauth → MergeSession`). The minted cookies authorize the entire
existing web surface (verified: `batchexecute LIST_NOTEBOOKS` → 200, 61
notebooks; `from_storage` lists live).

Two options followed: **A** — mint cookies from the master token and reuse the
existing web client; **B** — build a second Android gRPC backend. B is a large,
separately-maintained RPC surface with open problems (upload endpoint, media
download). A solves the original headless-auth problem with a small diff.

## Decision

Implement **Option A**; defer Option B.

- A new `[headless]` extra (`gpsoauth`) and `_auth/master_token.py` mint cookies
  from the master token. `notebooklm login --master-token` bootstraps (one
  browser sign-in to capture the single-use `oauth_token`, then durable),
  and the legacy `--master-token-refresh` route forcibly re-mints. Conditional
  operator recovery uses `notebooklm auth refresh`; when storage is absent it
  mints from the sibling token and passively validates once. The token is stored `0600` at
  `master_token.json` beside the profile's `storage_state.json`.
- Minted cookies are written into the normal `storage_state.json`; the existing
  loader, inline `__Secure-1PSIDTS` recovery, keepalive, and persistence run
  **unchanged** (the minted jar carries `SID`+`APISID`+`SAPISID`, so recovery
  mints PSIDTS on first load).
- **Recovery:** when a `master_token.json` is present, an expired file-backed
  session re-mints in-process as layer 4 after the homepage / `RotateCookies` /
  headless-browser ladder is exhausted. A neutral adapter is shared by cold
  token construction and `refresh_auth_session`; equivalent same-loop cold
  callers coalesce, while live RPCs retain `AuthRefreshCoordinator`
  single-flight.
- The CI env-var path (`NOTEBOOKLM_MASTER_TOKEN`) is **deferred** — shipping the
  `master_token.json` file (like `storage_state.json`) covers CI today, and an
  inline token would still need cookies written to disk for recovery to work.

## Consequences

- **Headless auth is solved for the web client** with no per-session browser and
  automatic recovery — independent of ever building the Android backend.
- **Security:** the master token is full-account, durable, and survives password
  changes until explicitly revoked — a materially larger blast radius than an
  expiring `storage_state.json`. Mitigations: dedicated/throwaway account only,
  `0600`, strict redaction (no token/`oauth_token`/`ya29`/cookie in logs or
  errors; third-party urllib3/requests DEBUG bodies suppressed around gpsoauth),
  prominent doc warnings. The flow uses Google's unofficial Android auth path
  (`gpsoauth`) and is ToS-grey like the rest of the client.
- **Single-consumer per account:** each re-mint creates a new session, so N
  concurrent workers re-minting the same account can invalidate each other's
  `SID`. In-process re-mint is coalesced; cross-process callers should treat one
  account as single-consumer.
- **Risks / open items:** DBSC could one day reject server-minted cookies
  (re-mint is the mitigation while it isn't enforced); `gpsoauth.exchange_token`
  is the fragile call (pinned `>=1.1.0`, no `<2` cap so the 2.0.0 `ServiceDisabled`
  fix installs); master-token durability over weeks is unverified (a durability
  cron is the follow-up). Cold-dead file-backed sessions now recover through the
  normal token loader; the explicit login flag remains an unconditional manual
  route.
- **Option B (a full Android gRPC backend) is deferred** as a DBSC hedge; the
  master token already solves headless auth for the web client, so building a
  second RPC surface is not justified now.

## Amendment (#2103 structural follow-up, PR-2): transaction relocated to `_auth`; owner check + bootstrap outcome

The whole master-token *transaction* (bootstrap / re-mint / ownership guard) —
previously assembled from minting primitives inside
`cli/services/login/master_token.py` and `cli/services/auth_refresh.py` — moved
into `_auth/master_token.py`. The CLI now invokes whole audited transactions
(`notebooklm.auth.master_token_bootstrap` / `master_token_remint` /
`bootstrap_missing_storage_from_master_token` / `assert_account_writable`) and
never assembles minting primitives itself; only the inherently-interactive
browser `oauth_token` capture (Directive B) stays CLI-side.

- **One re-mint kernel.** `remint_from_stored_token(storage_path)` (read the
  stored token → mint → persist → reload) replaced two independent
  assemblies of the same sequence — the L4 recovery rung and the CLI's
  operator-refresh path — that disagreed on error handling and reload. L4
  wraps it with single-flight + swallow-to-`None`, keeping its own existing
  reload afterward (`build_httpx_cookies_from_storage`, with inline-PSIDTS
  semantics) rather than trusting the kernel's own reload (which uses the
  strict, side-effect-free loader specifically to avoid a redundant network
  POST). The operator path lets `MasterTokenError` propagate unchanged.
- **Account-ownership guard, enforced under the storage-write lock.**
  `storage_writer.persist_minted_jar` now refuses to overwrite existing
  storage recorded under a *different* account unless `force` — closing both
  the check-before-mint TOCTOU a pre-check alone cannot close, and the
  documented low-level recipe's bypass (calling `mint_cookies` +
  `persist_minted_jar` directly skips any CLI-side pre-check entirely). This
  is the closure of the #2104 PR review thread
  (`discussion_r3731673393`) that bound this ADR to fixing the L4 read-side
  cross-account re-mint. A caller re-minting from a token already paired
  with that exact `storage_path` (L4, the operator re-mint) passes
  `refuse_unknown_owner=False`: requiring pre-existing in-band account
  metadata as a precondition would break mid-session self-recovery for a
  profile that was never bound to an explicit `--account` (empirically the
  common case, not the rare one this amendment originally assumed) — the
  "different recorded owner" refusal still applies unconditionally in every
  case. A caller *selecting* an account for the first time
  (`bootstrap_from_oauth_token`) keeps the stricter default.
- **`BootstrapOutcome`, a four-state result.** The missing-storage bootstrap
  (mint fresh storage from a stored token when the client cold-starts with no
  `storage_state.json`) moved into `_auth/master_token.py` as
  `bootstrap_storage_from_master_token`, returning `BootstrapOutcome ∈
  {MINTED, PRESENT_AFTER_WAIT, PRESENT_ON_ENTRY, NO_TOKEN}` instead of a plain
  bool that conflated "this call minted it" with "a concurrent leader already
  had", and "nothing to do because storage already existed" with "nothing to
  do because there is no token" — each outcome is logged at DEBUG, closing
  the observability gap the enum exists to fix. The CLI never needs the
  fine-grained type: `bootstrap_missing_storage_from_master_token` (also in
  `_auth/master_token.py`, the only one of the two that crosses the CLI
  boundary) does the `{MINTED, PRESENT_AFTER_WAIT} → True` /
  `{PRESENT_ON_ENTRY, NO_TOKEN} → False` collapse internally — identical
  external behavior to before this amendment, now backed by an explicit
  state machine that stays fully inside `_auth`. The bootstrap flock
  (distinct from the storage-write lock — holding that lock here would
  self-deadlock against the kernel's own persist) is an ADR-0030 cold-start
  ENTRY POINT, not a recovery rung.
- **`android_id` resolution internalized.** `bootstrap_from_oauth_token`
  resolves `android_id` explicit → stored → generated inside the library; the
  CLI driver keeps only its cheap pre-capture `read_master_token` probe (fail
  fast on a malformed `master_token.json` before the ~300s interactive sign-in,
  not after).

## Amendment (ADR-0034 Phase 11D): one path-owned bootstrap coordinator

`_auth/master_token_bootstrap.py` now owns bootstrap, re-mint, and
missing-storage coordination in one concrete `MasterTokenBootstrapper`. Each
instance retains one stateless `MintService`, one authoritative `ProfileStore`,
one bootstrap `FileLock`, and one verifier. It cannot receive an independently
pairable token file or storage writer; every token read/write derives through
the retained store.

The coordinator preserves the two-owner advisory check, authoritative
under-lock session-owner gate, session-before-token durability order, strict
post-persist reload, four-state recheck-after-wait result, and
shield-to-settlement cancellation rule. `_auth/master_token.py` remains the
v0.x boundary: public signatures and facade identities are unchanged, and its
call-time bridges retain legacy account lookup, strict-loader, Android-ID, and
default-verifier monkeypatch timing without importing runtime/client code into
the coordinator.
