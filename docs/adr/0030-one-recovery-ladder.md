# ADR-0030: One recovery ladder

## Status

Accepted (rolling out — refactor (c): c-PR1 lands the pure/off-loop loaders and
the loop-liveness gate; c-PR2 lands `single_flight.py` and folds cold-start
coalescing onto it (deleting the per-loop machinery same-PR); c-PR3 lands
in-process outcome-based re-mint coalescing; c-PR4 lands the opt-in mid-session
refresh-cmd rung (L2.5) plus the refresh-cmd logging/env hardening and the
`refresh_auth` join semantics; c-PR5 is docs + cleanup only).

**Amended 2026-08-07, executed 2026-08-08** — cold start's rung order is now
**aligned** to the documented ladder (L2.5 → L3 → L4); it ran L3 → L4 → L2.5
before. See the amendment note under "One ladder, rung availability as policy".

**Amended 2026-08-10 ([#2161](https://github.com/teng-lin/notebooklm-py/issues/2161))** —
mid-session recovery first performs a local, network- and write-free reload when
a file-backed profile differs from the rejected live jar. The bounded bridge
tries a changed live jar, force-samples disk while preserving one newer
authentication-bearing live candidate, then uses one final disk sample if that
candidate is rejected. The selected sample's cookies and in-band account route
are installed as one generation, and each retry rebuilds its homepage route;
an account-only profile rewrite is therefore retryable too. Cold start already
loads that profile, so the bridge is
mid-session-only and precedes the existing L2.5 → L3 → L4 escalation without
creating another credential tier.

Companion to [ADR-0029](0029-canonical-storage-writer.md) (refactor (b), the
single canonical `storage_state.json` writer). Where ADR-0029 unifies the
**write** side, this ADR unifies the **recovery/refresh** side.

## Context

The cookie/auth audit found the refresh/recovery surface split into **two
parallel universes** that had drifted apart:

- **Mid-session recovery** — middleware → executor → `AuthRefreshCoordinator` +
  `RefreshBudget`. Loop-bound by the ADR-0004 contract, it hands followers a
  shared `asyncio.Task` and has pinned task-identity / slot invariants. This
  composes soundly and is **not** the problem.
- **Cold-start** (`fetch_tokens*`) — implemented its **own** cross-loop
  coalescing (per-loop future maps + a `_REFRESH_GENERATIONS` generation
  counter in `refresh.py`; per-loop in-flight task maps + a settle loop and
  `_COLD_SUCCESS_GENERATIONS` epoch in `recovery.py`), took **no** cross-process
  lock around the `NOTEBOOKLM_REFRESH_CMD` subprocess, and its rung set diverged
  from the mid-session ladder.

Concrete defects this produced:

- [refresh-1 / HIGH#2] The cookie loaders did inline PSIDTS recovery — a
  synchronous ~15 s POST plus file I/O — from inside the load path, stalling the
  event loop of any async caller.
- [refresh-2] Refresh-cmd stampede: per-loop coalescing only, so N processes (or
  N loops with no shared registry) each spawned the subprocess.
- [refresh-4] `NOTEBOOKLM_REFRESH_CMD` was reachable only at cold start, never
  mid-session, so a long-lived server could not re-mint via the operator's
  command.
- [capture-3] Headless re-mint followers coalesced on the file **mtime**
  heuristic, so a follower could observe a stale/pre-wait drive cycle and report
  a false `SUCCESS`.
- [refresh-5] The keepalive poke throttle keyed on a non-canonicalised path, so
  two spellings of one profile throttled independently.

The cold-start coalescing logic was hand-rolled and duplicated across
`refresh.py` and `recovery.py`; every guarantee lived in comments and
whitebox-coupled tests rather than in one auditable core.

## Decision

Unify recovery around three moves: **pure/off-loop loaders**, a **single-flight
core** that formalises (not extracts) the existing cross-loop pattern, and **one
rung ladder** whose rung set is a per-entry-point policy flag. The
`AuthRefreshCoordinator` and the mid-session middleware are **deliberately left
untouched** (see the boundary below).

### Two loader layers, named distinctly (c-PR1)

```text
_load_cookies_pure(path)                 # inner: file I/O + validation ONLY,
                                         #        no network, EVER
build_httpx_cookies_from_storage(path)   # public wrapper: pure load +
                                         #        explicit recovery composition
```

The inline PSIDTS recovery is **extracted from the load path into the wrapper
bodies** (`load_auth_from_storage` in `_auth/tokens.py` and the `cookies.py`
wrapper). The pure loader raises `RequiredCookieValidationError` carrying a
closed-enum reason (`psidts_unroutable` | `missing_cookie`); the wrapper decides
whether to invoke recovery. Sync callers (CLI) keep the inline recovery
behaviour verbatim; async callers offload the wrapper via `asyncio.to_thread`,
so a slow recovery POST never runs on the event loop. The #2061
decline→retry-name-only fallback survives byte-for-byte (pinned by the 87 PSIDTS
tests, which pass unmodified). A loop-liveness gate test (< 50 ms stall with a
mocked slow recovery POST) locks the fix in.

### The single-flight core (`_auth/single_flight.py`, c-PR2)

`single_flight.py` is the **formalisation of the existing
`refresh.py` / `recovery.py` cross-loop pattern**, not an extraction from
`AuthRefreshCoordinator`. Two process-global facilities:

- **Flight registry** — one in-flight operation shared per **`(canonical
  storage path, rung policy)`** flight key (matching the shape of the registries
  it replaces: recovery keyed on `(path, allow_headless)`; refresh keyed on the
  path plus its refresh-cmd policy). A *leader* drives the work as an
  `asyncio.Task` on its own loop (held in a strong-ref set so the asyncio GC
  cannot collect it) and mirrors completion into a
  `concurrent.futures.Future`. *Followers* on **any** loop bridge to that future
  via `asyncio.shield(asyncio.wrap_future(f))` with a settle-before-propagate
  loop — plain `wrap_future` would let one cancelled follower detonate its
  siblings, and `run_coroutine_threadsafe` is deliberately ruled out (a follower
  never runs the leader's coroutine).
- **Success epoch** — one process-global counter **per canonical storage PATH**
  (path only, **not** per policy: a late refresh-cmd waiter must observe a
  completed flight's success regardless of which policy key that flight ran
  under; per-policy epochs would send it to a redundant subprocess). It
  relocates the `_REFRESH_GENERATIONS` semantics from `refresh.py` and preserves
  all **four pinned guarantees**: (1) late-waiter skip
  (capture-epoch-before-wait, and — via `claim_if_epoch_current` — the epoch
  compare and the flight claim happen under a **single** lock hold, so a sibling
  that bumps and prompt-pops in between cannot trick a waiter into a second
  subprocess); (2) bump on subprocess **success only** (a failure leaves waiters
  retrying); (3) a settled future stays inspectable for the cancel/settle race
  (#621); (4) no phantom bump on cancel-before-register, including the
  warm-registry variant (#816).

One `threading.Lock` guards **only** the brief synchronous claim / registry /
epoch mutations; it is never held across an `await` or a subprocess. Retention
is **value-free**: the registry stores only flight bookkeeping and integer
epochs — a flight's jar-bearing result (e.g. `ColdRecoveryResult`) rides the
per-flight future and is prompt-popped the moment the flight settles, so no new
credential-lifetime surface appears (the same value-free contract ADR-0029 pins
for write outcomes).

Consumer-side policy stays with the consumers: `recovery.py` keeps its per-loop
revalidate-on-bump epoch (`_COLD_SUCCESS_GENERATIONS`) and per-loop mutation
lock — deliberately **not** promoted to cross-loop, because that would change
the fresh-loop-runs-full-ladder behaviour.

### One ladder, rung availability as policy

```text
L1 homepage refresh
  → L2 RotateCookies / PSIDTS rotation
  → persisted-profile reload    (mid-session only; no network or write)
  → L2.5 refresh-cmd            (NEW rung; promoted from cold-only [refresh-4])
  → L3 headless re-mint
  → L4 master-token re-mint
```

Rung numbering follows the code's existing convention (L2 is already
RotateCookies). Rung availability is a **policy flag per entry point**:
`fetch_tokens_passive` gets none, CLI login gets none, client cold-start /
mid-session get the rungs as configured.

- **Cross-process exclusion (c-PR2):** the refresh-cmd rung (L2.5) takes a
  **per-path flock** before spawning the subprocess (the keepalive-rotation
  pattern), closing [refresh-2]. The re-mint rungs (L3/L4) deliberately do
  **not** — cross-process re-mints stay each-own-browser by design.
- **In-process re-mint coalescing (c-PR3):** the leader publishes a **typed,
  closed-enum outcome** (`SUCCESS` / `FAILED_<cause>` / `LOGIN_REQUIRED`, never
  free-text reason strings) through the per-path drive lock, so followers stop
  coalescing on the mtime heuristic and the false-`SUCCESS` [capture-3] becomes
  unrepresentable. No sidecar / on-disk artifact is introduced.
- **Canonical poke key (c-PR2):** one `canonical_storage_key(path)` helper backs
  the throttle map, the poke lock, and the flock derivation, closing
  [refresh-5].

*Amended (cold-start rung-order alignment, `_auth` consolidation); **executed
2026-08-08**, so the divergence described next is history:* the order above is
the one **mid-session** follows; **cold start did not**.
`refresh._fetch_tokens_with_refresh` ran L3/L4 first — via
`recovery.coalesced_cold_recovery`, whose `_run_cold_recovery` sequences headless
then master-token — and reached the refresh-cmd rung only afterwards, further
down the *same* `except ValueError` arm. The effective cold order was therefore
**L3 → L4 → L2.5**: the reverse of `session.refresh_auth_session`'s explicit
L2.5 → L3 → L4 if-chain, and the reverse of this ADR's own ladder.

The history says the divergence was never chosen. The refresh-cmd arm is the
older code: it has been the sole `except ValueError` fallback of the cold token
fetch since #336 (2026-05-09) introduced `NOTEBOOKLM_REFRESH_CMD` — before the
current ladder numbering existed. (#336's own `docs/troubleshooting.md` did
number it, as *layer 2* under an older scheme in which the heavier recovery came
after it; the current scheme's first numbered rung is #1525's "layer-3",
2026-06-10.) #2071 (2026-08-04) added cold-start re-mint by inserting
the redirect-only recovery block at the **top of that existing arm** — which
fixed the reported defect and fixed the order as a side effect of where the new
code landed. c-PR4 (#2091, 2026-08-05) then named the rungs and promoted
refresh-cmd into the mid-session ladder *first*, per the diagram above, but left
`_fetch_tokens_with_refresh` alone: its diff adds L2.5 to `session.py` and
re-sequences nothing in `refresh.py`. So cold start has never matched this ADR,
and nothing recorded an intent for it not to.

**Decision (maintainer, 2026-08-07): align cold start to the documented order.**
The cold redirect-with-a-real-storage-path case becomes L2.5 → L3 → L4. This is a
**behavior change**, not a refactor: with `NOTEBOOKLM_REFRESH_CMD` configured,
the operator's command now runs *before* the headless / master-token re-mints on
a dead-cookie cold start rather than only as their backstop (a
`_LoginRedirectError`'s message matches `_should_try_refresh`'s signals today —
the rung is reachable on that path either way; only its position moves). It ships
as its own PR, after the behavior-frozen step that colocates the cold fallback
sequence into one function in `refresh.py` so the reorder is one small diff in
one visible place, and it is verified across the live auth-matrix rotation paths.

Position-first also changes the rung's **failure contract**, which a position-only
swap would get wrong. The cold arm is terminal today: a non-zero exit raises
`RuntimeError` and a still-redirecting retry raises `_LoginRedirectError`, and
both propagate straight out of `_fetch_tokens_with_refresh` — harmless as the
*last* rung. The ladder it is being aligned to does the opposite:
`session._try_refresh_cmd_reauth` catches `(RuntimeError, OSError, ValueError)`,
logs, and returns `False` so the chain falls through. The executing PR must
therefore convert an L2.5 failure into a fall-through to L3/L4, matching the
mid-session bool-per-rung shape — otherwise a broken or timing-out
`NOTEBOOKLM_REFRESH_CMD` would mask the two re-mint rungs an operator recovers by
today.

*As executed (2026-08-08):* the fall-through **stashes** the L2.5 exception and
re-raises it only when the re-mint rungs do not recover either. Mid-session's
`False` lets the caller's own dead-cookie error stand, which is right there
because the mid-session ladder has a caller holding that error; cold start has no
such holder, and a bare fall-through would have replaced today's actionable
`NOTEBOOKLM_REFRESH_CMD exited N (executable: …)` with the generic
"Authentication expired" on every fully-exhausted ladder. Stashing keeps the
exhausted-ladder error **byte-identical to the pre-alignment order** — where the
rung ran last and raised. The alignment's observable deltas are therefore the
rung order itself, the fact that an L2.5 failure no longer ends the ladder, and
the two consequences recorded below: an L2.5-first success returns without
entering `_run_cold_recovery`, so the cold generation never advances and that
fast path stays disarmed on hosts where the command works; and the rung's
warning now prints the original error where the pre-alignment rung printed the
rebound retry error. The
`raise` happens inside the caller's `except`, so the original `ValueError`
remains the `__context__`.

Where no command is configured the new first rung costs a predicate miss and
nothing else: `_should_try_refresh` reads one `ContextVar` and two environment
variables, then substring-matches the error text. The per-path flock, the
single-flight claim, and the subprocess all sit *past* that gate — no network
call, subprocess, lock acquisition, or sleep precedes it — so the default path
gains no stall.

Where a command *is* configured the cost is not only rung order. L2.5 now also
precedes `_run_cold_recovery`'s generation-bump revalidate — a single GET that,
on a loop which already recovered once, can heal a repeat cold redirect with no
rung at all. Alignment pays the subprocess (and its per-path flock) before that
free check is reachable; and because an L2.5-first success returns without
entering `_run_cold_recovery`, the cold generation never advances, so that fast
path stays disarmed on hosts where the command works.

**Non-goal — what does not change.** L2.5's entry surface in `refresh.py` is
strictly *wider* than a ladder rung, and the alignment reorders rung **position
within the redirect-with-a-real-path case only**. Two roles survive untouched:
(1) the arm still fires for any refresh-eligible `ValueError`, not just a
`_LoginRedirectError` — the recovery block is guarded by an `isinstance` check
that other `ValueError`s skip straight past; (2) it still runs when
`storage_path is None` and the path is resolved from the profile/default, a case
the same guard excludes and `_run_cold_recovery`'s `Path`-typed contract cannot
accept. Moving the arm into `recovery.py`, or injecting it inside the cold
flight's coalescing boundary, would change both; the alignment does not.

*Phase 12A ownership extraction (2026-08-09):* the internal, one-shot
`recovery.ColdRecoveryCoordinator` now owns the L2.5 decision/attempt and delegation into the
combined cold flow. `refresh._cold_fallbacks` remains the sole production adapter and supplies
late-bound closures, so the exact DEBUG environment-auth skip and WARNING start/failure messages,
route lookup timing, and wider plain-`ValueError` entry surface remain in `refresh.py`. L2.5 still
runs outside the shared cold flight. The existing `recovery._run_cold_recovery` continues to own
the L3 → L4 sequence, per-loop lock/generation state, and same-sample replacement baseline; its
coalesced wrapper and `single_flight` retain flight settlement until Phase 12C. Cancellation before
caller-jar replacement leaves that jar
untouched; after either arm synchronously replaces it, a later cancelled route/fetch does not roll
the mutation back. A failed L2.5 exception is still retained by identity, later cold success still
wins, and an exhausted ladder still selects that retained failure over the original redirect.

A third role does **not** survive, and the executing PR must not try to preserve
it. Today the arm is also the post-ladder backstop, reached from two rebind
sites: the ladder-exhausted path (`coalesced_cold_recovery` re-raises
`last_redirect`) and the successful-re-mint-with-failed-revalidation path.
Alignment *consumes* that role — L2.5 runs once, before the ladder — and keeping
a second post-ladder invocation would spawn a **second subprocess**, because
`_REFRESH_ATTEMPTED_CONTEXT` is reset in the `finally` so the gate passes again,
and the per-path success epoch does not deduplicate a caller's own re-entry
(`_coalesced_run_refresh_cmd` re-captures the epoch per call, and the claim skips
only on a *strictly greater* one).

### Mid-session refresh-cmd rung (L2.5), opt-in for one release (c-PR4)

`refresh_auth_session`'s ladder gains L2.5, gated **opt-in for one release**
behind `NOTEBOOKLM_REFRESH_CMD_MIDSESSION=1` (de-risks operators whose commands
assume cold-start-only invocation; flips to default-on the following release).
Because promoting the rung into long-lived servers widens exposure, c-PR4
bundles security hardening:

- The refresh-cmd DEBUG log drops raw `stdout`/`stderr` `%r` dumps → basename +
  exit code + byte counts by default; full captured output only behind the
  explicit `NOTEBOOKLM_REFRESH_CMD_LOG_OUTPUT=1` opt-in.
- The subprocess environment pops **all** first-party secret / credential vars
  before spawn (`NOTEBOOKLM_AUTH_JSON`, `NOTEBOOKLM_SERVER_TOKEN`,
  `NOTEBOOKLM_SERVER_TOKEN_FILE`, `NOTEBOOKLM_MCP_TOKEN`,
  `NOTEBOOKLM_MCP_OAUTH_PASSWORD`, `NOTEBOOKLM_MCP_OAUTH_STATE_PATH`), not just
  `NOTEBOOKLM_AUTH_JSON`, so a
  long-lived server's own auth secret cannot leak into every refresh subprocess
  and its grandchildren (`/proc/<pid>/environ`).

Explicit `client.refresh_auth()` routes through the coordinator's
`await_refresh` with **defined join semantics** — **join-then-rerun, implemented
caller-side**: a wider-policy caller (`allow_headless=True`) joins whatever
flight is in progress and, if that flight fails, re-runs its own flight with its
full rung policy, so it never silently loses its L3 rung. The coordinator keeps
its single unkeyed task slot; its internals are untouched.

### Env-var, logging, and read consolidation (c-PR5)

- `NOTEBOOKLM_AUTH_JSON` is read through **one** helper,
  `notebooklm._auth.paths.resolve_auth_json_env()`. Before it, ~7 call sites
  spelled the check by hand and disagreed on presence-vs-truthiness (the #2057 /
  #2083 drift class). The helper is presence-based (unset ⇒ `None`; set ⇒ the
  value, so a set-but-empty value counts as *selected* and never falls through
  to a profile file); the one consumer that parses the payload
  (`_load_storage_state`) raises the "set but empty" configuration error. The
  presence-only callers (path resolvers, PSIDTS recovery, header routing, the
  cookie-save skip) route through the same helper and stay behaviour-identical.
- `_auth/_browser_cookie_filter.py` logs to `notebooklm.auth`
  (not `__name__`) so its dropped-cookie / malformed-row warnings reach the
  documented auth logger (core-F10 / ADR-0016).
  **Amended (ADR-0033 PR 4.2):** the filter now lives in `_auth/storage.py`
  (`_auth/_browser_cookie_filter.py` is a re-export shim). The contract is
  unchanged and still holds — `storage.py` binds its logger by the same
  `notebooklm.auth` *name*, not `__name__`, so no warning changed namespace in
  the move. Any future relocation of this code must preserve that; a module
  whose logger follows `__name__` would silently reroute these warnings to a
  private child no operator subscribes to.

### Boundary — what this ADR does NOT touch

- **`AuthRefreshCoordinator` internals** — loop-bound by ADR-0004, shared-Task
  followers, pinned slot invariants. Any future composition with
  `single_flight.py` is its own PR with its own test-migration plan (plan §c.7).
- **The mid-session middleware chain** — unchanged; only the ladder's rung set
  (L2.5) and the refresh-cmd hardening are added.
- **Ladder rung internals** (capture, minting) — unchanged.
- **No public API signature changes** to `fetch_tokens*` / `refresh_auth`; the
  only new env vars are the two documented additions
  (`NOTEBOOKLM_REFRESH_CMD_MIDSESSION`, `NOTEBOOKLM_REFRESH_CMD_LOG_OUTPUT`).
- **No cross-process re-mint coalescing** and **no new on-disk artifacts**
  (plan §c.5); **no legacy-shape retirement** (candidate refactor (d)).

## Consequences

- The load path never blocks the event loop on recovery; async callers offload a
  network-free-by-construction pure loader plus an explicit recovery wrapper.
- Cross-loop coalescing lives in **one** auditable core with its four guarantees
  expressed in code, not comments. The per-loop future maps and generation dicts
  in `refresh.py` / `recovery.py` are deleted in the same PR that adds the core,
  so a revert restores them cleanly.
- The refresh-cmd subprocess is serialised across processes (per-path flock),
  killing the stampede; re-mint coalescing is outcome-based, killing the
  false-`SUCCESS`.
- The refresh-cmd rung is reachable mid-session (opt-in for one release), with
  tighter default logging and full secret-env scrubbing for server contexts.
- Cold start and mid-session now walk the **same** rung order (L2.5 → L3 → L4),
  so "the ladder" is one sequence rather than two. With a command configured, a
  dead-cookie cold start pays the operator's command (and its per-path flock)
  before the re-mint rungs and before `_run_cold_recovery`'s generation-bump
  revalidate; with none, the extra rung costs one predicate miss.
- Two whitebox pinning suites (`test_refresh_lock_registry.py`,
  `test_refresh_cmd_race.py`) were **migrated** in c-PR2 — each pinned guarantee
  (the four epochs above, plus lock identity) is re-expressed against
  `single_flight.py`'s surface, and the five underscore-private facade
  test-bindings for the deleted machinery are removed (not part of the supported
  facade surface).

## Alternatives considered

- **Extract the core from `AuthRefreshCoordinator`.** Rejected: the coordinator
  is loop-bound (ADR-0004) and hands followers a shared `asyncio.Task` with
  pinned task-identity invariants; a cross-loop core cannot inherit those
  without breaking the mid-session contract. The core instead formalises the
  *other* (cold-start) pattern and leaves the coordinator alone.
- **Per-policy success epoch.** Rejected: a late waiter under one policy key
  must observe a completed flight's success under a different policy key, or it
  spawns a redundant subprocess. The epoch is per canonical PATH; the flight
  registry is per `(path, policy)`.
- **Cross-process re-mint coalescing (outcome sidecar).** Deferred (design
  archived in the plan's Appendix A): re-mints each own their browser by design,
  and a shared outcome record adds a credential-lifetime surface and a
  crash-consistency protocol not justified by the current failure modes.
- **Land the mid-session rung default-on.** Rejected for the first release:
  operators whose refresh commands assume cold-start-only invocation would be
  surprised inside a long-lived server. Opt-in for one release, then default-on.
- **A raising `resolve_auth_json_env()` helper.** Rejected: the deliberate
  #2057 / #2083 contract is that path resolvers return `None` (env-mode) on a
  set-but-empty value while the loader raises "set but empty". A raising helper
  would move the error off the loader and break that tested contract, so the
  helper is presence-based and the single consumer raises.

## Related references

- [Architecture](../architecture.md) — layered design and the `_auth/` file
  index.
- [ADR-0029](0029-canonical-storage-writer.md) — the write-side companion
  (single canonical `storage_state.json` writer).
- [ADR-0004](0004-loop-affinity-contract.md) — the loop-affinity contract that
  keeps `AuthRefreshCoordinator` out of scope here.
- [ADR-0016](0016-auth-identity-and-core-logger-compatibility.md) — the
  `notebooklm.auth` logger namespace the browser-cookie filter now targets.
- [ADR-0023](0023-master-token-headless-auth.md) — the L4 master-token re-mint
  rung.
- [ADR-0033](0033-auth-consolidation-policy.md) — the `_auth` consolidation
  policy governing the effort that carries the rung-order alignment (and the
  colocation step preceding it).
