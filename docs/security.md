# Security model

NotebookLM authentication material is account-equivalent. `storage_state.json` contains live
cookies; `master_token.json` can mint new sessions. Treat both as secrets: keep them out of source
control, logs, bug reports, shared artifact stores, and untrusted process environments. Prefer a
dedicated account for unattended use. The operational cookie and recovery details live in
[auth-cookie-lifecycle.md](auth-cookie-lifecycle.md).

## Credential boundaries

- `ProfileStore` owns one caller-spelled profile path, its derived token path, typed reads, and
  profile commits. `MasterTokenFile` is the legacy arbitrary-path token owner. A production
  operation must not independently pair a profile path with an unrelated token path.
- `credential_io.py` is the only unchecked atomic-write capability. Profile and arbitrary-token
  writes use separate private wrappers so an aggregate cannot be committed through the wrong
  authority.
- Profile directories/files are created with restrictive POSIX permissions (`0700`/`0600`). On
  Windows, access follows inherited ACLs rather than emulated POSIX mode bits.
- `ProfileDocument` preserves unknown JSON and raw cookie rows, but pure typed views never serialize
  themselves back over that raw document. Filtering and corruption policy stay at operation
  boundaries.
- `CookieJar` and `MasterToken` redact secret values from repr. Auth diagnostics log bounded types,
  counts, paths, and statuses rather than cookie/token values.

## Recovery and concurrency

Three explicit owners replace free-floating process state:

- `SingleFlight` owns cross-loop flights, strong leader-task references, and per-canonical-path
  success epochs. Flight claim plus stale-epoch comparison is atomic. Settled slots prompt-pop, so
  terminal cookie-bearing results are not retained in the registry.
- `ColdRecoveryState` owns weak-loop path locks and success generations. Its one threading lock is
  held only for synchronous map access, never across an await.
- `RotationState` owns weak-loop keepalive locks and per-canonical-path monotonic attempt stamps.
  The claim is stamped before the RotateCookies POST, so failure or cancellation consumes the same
  60-second throttle slot as success.

`ColdRecoveryCoordinator` is a one-shot operation. It claims synchronously before its first await,
runs L2.5 then the coalesced L3/L4 ladder, and deletes all eleven injected callbacks on every exit.
`AccountRepairService` applies the same rule to its six collaborators. This bounds references to
cookie jars, paths, browser functions, and persistence capabilities to one operation.

Cancellation does not mean rollback:

- a cancelled single-flight follower settles without cancelling the shared worker or siblings;
- a cancelled leader is mirrored as `CancelledError`, not rewritten as another exception;
- cancellation before caller-jar replacement leaves that jar untouched, while cancellation after
  replacement does not undo it;
- cancellation of an `asyncio.to_thread` persistence call reaches the caller immediately, but an
  already-dispatched worker may still finish and commit;
- reset helpers are test-only and reject live flights or locked paths instead of clearing active
  work.

## PSIDTS recovery

Inline PSIDTS recovery keeps raw and typed responsibilities separate. `cookies.py` owns pure
network-free loaders. `psidts_recovery.py` receives a loader explicitly, reads raw rows through
`ProfileStore`/`ProfileDocument`, records a value-only observation before the network POST,
converts live jars through `CookieJar`, and persists through `ProfileStore`. It does not import the
cookie module or the `storage.py` facade.

The sentinel, contention, full-reread, observation, POST, typed CAS, and disk-reread order is
security-relevant. A write result is not proof that the desired cookie landed; the post-save disk
state is authoritative and may show that a sibling process won. Catches are deliberately narrow:
Unicode failures, cancellation, and unlisted errors retain their identity rather than being
silently treated as a declined heal.

## Master-token and account compatibility

`MasterTokenError` is defined in the dependency-bottom types leaf but preserves its historical
`notebooklm._auth.master_token` module and pickle identity. The same is true for `Account` and
`PlaywrightAccountRepairResult` with `notebooklm._auth.account`. Facade, storage, and compatibility
imports resolve to those exact objects; adapters do not wrap or translate unexpected exceptions.

Account repair extracts the active email before its handled-error region, offloads only cookie
loading, and performs typed write/clear operations synchronously. Only `OSError`, `ValueError`,
`RuntimeError`, and `httpx.HTTPError` become the historical result value. Cancellation and unlisted
exceptions escape. Cleanup failure is warning-only and cannot replace the original handled error.

## Compatibility surface

Phase 12C changes ownership, not the v0.x public contract or disk schema. Public auth/storage saver
identities, loader signatures, late-bound adapters, client cookie-saver injection, keepalive raw
state identity views, result objects, messages, causes, and traceback projections remain pinned.
The raw keepalive names are non-owning views into `RotationState`; rebinding them or introducing a
second registry is unsupported.

The measured auth graph is 40 modules / 15,237 lines / 128 unique edges (117 module + 11
function-local). Module-only and all-scope SCC sets are empty; the former
`cookies/master_token/psidts_recovery/storage` cycle has been removed. Exact touched production LOC
is pinned in [development.md](development.md); those values are evidence and not spare capacity.

## Operator checklist

1. Restrict profile directories to the service account that runs NotebookLM.
2. Do not place unrelated secrets in the environment inherited by `NOTEBOOKLM_REFRESH_CMD`; only
   the documented NotebookLM secret variables are scrubbed before the child starts.
3. Keep CDP endpoints loopback-only and trusted. A CDP connection is account-equivalent.
4. Disable `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` only when the network policy requires it; doing so
   removes proactive rotation, not the need for fresh cookies.
5. Rotate or revoke a master token immediately if its file may have been exposed.
6. Preserve the canonical lock files and atomic writers; custom writers must retain the documented
   locking, permissions, and exact-path behavior.
