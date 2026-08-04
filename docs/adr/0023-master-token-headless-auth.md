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
