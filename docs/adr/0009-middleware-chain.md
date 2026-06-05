# ADR-0009: Middleware chain for cross-cutting transport concerns

## Status

Accepted (Tier 12 PR 12.1; closed by PR 12.9); context refined by [ADR-0013](0013-composable-session-capabilities.md) (#866).
Amended (arc-1, closes the `[arc-1-formalized-as-deferred-permanent]`
item on the v0.6 architecture-deepening backlog, §6.1): the
`RpcRequest.context: dict[str, Any]` shape is the **long-term**
chain-metadata carrier — see §"Decision: `RpcRequest.context: dict[str,
Any]` is the long-term shape" below for the rationale and the policy
governing additions to the vocabulary table.

This ADR shipped in PR 12.1 of the Tier-12/13 greenfield migration as
type-only scaffolding: the Protocol, dataclasses, and `build_chain` helper
landed without production wiring. PR 12.2 wired an empty chain into
`Session`. PRs 12.3 through 12.8 each extracted one cross-cutting
concern into a dedicated middleware. **PR 12.9 closes the tier** — the
seven-middleware chain `[Drain, Metrics, Semaphore, Retry, AuthRefresh,
ErrorInjection, Tracing]` is fully wired, the leaf
is a pure POST, and the underscore-prefixed compatibility aliases were
removed. A later architecture cleanup retired the interim authed-transport
Adapter; the current terminal is `Session._authed_post_chain_terminal →
Kernel.post`. The chain ordering, the `RpcRequest.context` key
vocabulary, and the Protocol shape pinned below are the load-bearing
contract.

Two implementation realities diverged from the original PR-12.1 pin and
are documented in the "PR 12.9 close-out notes" section at the bottom of
this ADR:
1. The `AuthRefreshMiddleware` constructor shipped with a simpler shape
   that defers request-rebuilding to the leaf (`rebuild_headers` /
   `build_request_factory` closures are NOT yet wired at chain level).
2. The RPC concurrency semaphore wraps the chain dispatch (not the leaf),
   restoring pre-Tier-12 "one slot per logical RPC" semantics.

The `Kernel.post` terminal revisit for (1) has landed; closure callbacks
remain pinned as the target shape for future chain-terminal rewrites.

The signatures pinned in this ADR (especially the `AuthRefreshMiddleware`
constructor, §"AuthRefreshMiddleware constructor signature") are
load-bearing: PR 12.8's implementation has zero degrees of freedom on
shape. PRs 12.2–12.7 also depend on the chain ordering and the
`RpcRequest.context` keys defined below.

ADR-0002 ("Capability Protocol pattern, `SessionCapabilities` fat
union") was superseded by the `arch-d2-cutover` PR (D2 PR-2), per
ADR-0002's own Status line. ADR-0010 was the original Tier-13 supersession plan but was itself superseded by [ADR-0013](0013-composable-session-capabilities.md) ("Composable Session Capabilities") in v0.5.0. See [`docs/architecture.md`](../architecture.md)
for the post-supersession capability-protocol model.

## Context

The post-remediation `Session` orchestrates six cross-cutting concerns
across every authenticated POST. The "Today" column below describes the
pre-Tier-12 state (when ADR-0009 was written, before any chain extraction
landed); the "Post-Tier-12" column describes where each concern lives
after PR 12.9 closed the tier. `_SyntheticErrorTransport` was deleted by
PR 12.9; the chain-layer `ErrorInjectionMiddleware` is the only
substitution path going forward.

| Concern | Pre-Tier-12 | Post-Tier-12 (PR 12.9 → today) |
|---|---|---|
| In-flight drain tracking | `TransportDrainTracker.begin/end` around the call (`_transport_drain.py`) | `DrainMiddleware` (chain pos 0) |
| Metrics emission | `ClientMetrics.on_rpc_event` callbacks woven through the legacy transport POST loop (`_client_metrics.py`) | `MetricsMiddleware` (chain pos 1) |
| RPC concurrency gate | `asyncio.Semaphore` inside the legacy transport POST loop | `SemaphoreMiddleware` (chain pos 2) |
| Retry on 5xx / 429 | inline loops inside the legacy transport POST loop | `RetryMiddleware` (chain pos 3) |
| Auth refresh on 401 | inline branch inside the legacy transport POST loop (`_session_auth.py`) | `AuthRefreshMiddleware` (chain pos 4) |
| Synthetic error injection (tests) | `_SyntheticErrorTransport` wraps the httpx client (`_error_injection.py`) — DELETED PR 12.9 | `ErrorInjectionMiddleware` (chain pos 5) |
| Per-attempt tracing/logging | scattered `logger.debug` calls inside the retry loop | `TracingMiddleware` (chain pos 6) |

Before the chain extraction, adding another concern (e.g. an
idempotency-routing wrapper for retry safety, ADR-0005) required touching
the transport POST loop directly. Each concern's state holder
(`TransportDrainTracker` / `ClientMetrics` / etc.) was also threaded into
that leaf, which meant: (a) a new concern grew the host Interface, and
(b) every change to one concern risked regressing the others because they
shared a function body.

The greenfield design in `docs/architecture-evolution.md` §3.4 proposes
lifting each concern into a composable middleware, leaving `Kernel.post`
as a pure-transport function. The chain is the composition substrate.

Five details that shaped this ADR:

1. **HTTP-level, not RPC-level.** The chain wraps the transport, not the
   encoder. Middlewares see already-encoded bytes; encoding and decoding
   live in `RpcExecutor.rpc_call` (Tier 13). This keeps every middleware
   agnostic of `batchexecute` framing and makes test fixtures small.
2. **Around-style, not before/after pairs.** Each middleware receives a
   `next_call` and decides whether (and how) to invoke it. The
   `AuthRefreshMiddleware` needs to *transform the request and retry once*
   on 401 — that idiom is awkward to express as separate before/after
   hooks and natural as an around handler.
3. **One global chain at Session init, not per-call.** The chain is
   stateless; per-call metadata travels through `RpcRequest.context`.
   Tests override the middleware list via constructor injection
   (`Session(kernel, middlewares=[FakeMetrics(), real_drain])`) — never
   by monkeypatching (ADR-0007).
4. **Idempotency resolution happens *above* the chain.**
   `RpcExecutor.rpc_call` calls
   `_idempotency.resolve_effective_disable_internal_retries(...)` and
   stuffs the resolved bool into `RpcRequest.context["disable_internal_retries"]`
   before chain entry. The `RetryMiddleware` (PR 12.7) reads the bool; it
   does not see the `IdempotencyPolicy` enum or know about
   `operation_variant` routing. Keeps the chain ignorant of the
   mutating-RPC idempotency registry while preserving its semantics.
5. **`AuthRefreshMiddleware` callbacks are pinned here, not in PR 12.8.**
   The constructor's `rebuild_headers` and `build_request_factory`
   callable signatures (§"AuthRefreshMiddleware constructor signature")
   are part of *this* ADR so PR 12.8's implementation has zero degrees of
   freedom on shape. PRs 12.2–12.7 also depend on those signatures
   because they shape `RpcRequest.context` and the chain's interaction
   with `AuthSnapshot`.

## Decision

A single around-style middleware chain wraps the authenticated POST
transport. The chain is built once at Session init and invoked per
request.

### Middleware Protocol

```python
class Middleware(Protocol):
    async def __call__(
        self,
        request: RpcRequest,
        next_call: NextCall,
    ) -> RpcResponse: ...

NextCall = Callable[[RpcRequest], Awaitable[RpcResponse]]
```

`RpcRequest` and `RpcResponse` are HTTP-shape dataclasses (`url: str`,
`headers: dict[str, str]`, `body: bytes`, `context: dict[str, Any]`). The
chain operates on already-encoded HTTP requests; encoding/decoding lives
*above* the chain in `RpcExecutor.rpc_call`.

### Chain ordering (load-bearing)

The chain is composed in this exact order (outermost → innermost):

```text
Drain → Metrics → Semaphore → Retry → AuthRefresh → ErrorInjection → Tracing → terminal
```

Where `terminal` is `Session._authed_post_chain_terminal → Kernel.post`.

The leftmost middleware in the sequence becomes the outermost wrapper.
`build_chain` enforces this ordering by composing in reverse (last
middleware is wrapped first around `terminal`).

`SemaphoreMiddleware` was inserted at chain position 2 in PR 12.9 (see
"PR 12.9 close-out notes" below) after the first cut of the audit-find
moved the `max_concurrent_rpcs` slot to `Session._perform_authed_post`
(outside the chain) and codex caught the resulting Drain-admission
regression. PR 12.1 originally pinned six middlewares; the chain is seven
post-PR-12.9.

Per-position rationale:

- **Drain outermost.** Every in-flight call — including ones that haven't
  reached the transport yet because Semaphore / Retry / AuthRefresh /
  ErrorInjection haven't released them — must count toward shutdown
  drain. Putting Drain inside any of those would let a stuck retry (or a
  queued call waiting for the semaphore) escape the drain accounting.
- **Metrics outside Semaphore.** Metrics measure end-to-end timing
  *including* the time a call spent waiting for the `max_concurrent_rpcs`
  slot. (`ClientMetrics` also tracks `rpc_queue_wait_seconds_total`
  separately via the `RPC_QUEUE_WAIT_CONTEXT_KEY` plumbing — that's just
  queue time, while Metrics latency covers queue + work.) Placing Metrics
  inside Semaphore would exclude queue wait from latency, breaking
  pre-Tier-12 telemetry semantics.
- **Semaphore outside Retry.** The `asyncio.Semaphore` is non-reentrant.
  Placing it inside Retry would let each retry attempt try to acquire a
  fresh slot, deadlocking under sustained 429s when every slot is held by
  a retrying call waiting to retry into a slot. Placing it outside Retry
  bounds the whole retry-and-refresh cohort to one slot per logical RPC
  (matching the pre-Tier-12 contract).
- **Retry outside AuthRefresh.** These are orthogonal failure modes — 5xx
  / 429 / network errors trigger `RetryMiddleware`; 401 triggers
  `AuthRefreshMiddleware`. Nesting prevents infinite-loop duplication
  (each layer has its own guard). Putting them in the other order would
  let an auth-refresh-then-success-then-5xx sequence cause a retry that
  re-triggers the refresh, which the legacy transport loop also guarded
  against with a per-attempt flag. Both layers honor the same
  `disable_internal_retries` post-resolution bool: a non-idempotent /
  probe-then-create write is neither retried on 5xx/429 nor replayed
  after an auth refresh, because a mid-flight 401/403 can land *after*
  the server committed the write (issue #1157).
- **AuthRefresh outside ErrorInjection.** Test-injected 401s exercise the
  refresh path realistically — a test that injects a 401 expects the
  refresh middleware to run, not for the injection to short-circuit
  before refresh sees it. Putting AuthRefresh inside ErrorInjection would
  invert that.
- **ErrorInjection inside Retry.** Synthetic transient failures should
  look like network errors to `RetryMiddleware`. Putting ErrorInjection
  outside Retry would make the retry path invisible to the test,
  defeating the purpose. Pre-PR-12.6 this was a transport-layer wrapper
  (`_SyntheticErrorTransport`); PR 12.6 lifted it into the chain and PR
  12.9 deleted the transport class — substitution is now exclusively a
  chain-layer concern.
- **Tracing innermost.** Tracing logs every actual HTTP attempt, including
  retried ones. Putting Tracing outside Retry would log only one entry
  per logical call regardless of retries, losing the per-attempt
  visibility the original transport debug logging provided.

### `RpcRequest.context` keys (the chain's metadata vocabulary)

| Key | Type | Set by | Read by |
|---|---|---|---|
| `rpc_method` | `str \| None` | `Session._perform_authed_post` (receives the resolved method-name string from `RpcExecutor._execute_once`, which passes `method.name` — never the `RPCMethod` enum itself; chat-side callers pass `None`) | `MetricsMiddleware`, `TracingMiddleware` |
| `disable_internal_retries` | `bool` | `Session._perform_authed_post` (receives the post-resolution boolean from `RpcExecutor._execute_once`, which calls `_idempotency.resolve_effective_disable_internal_retries(...)` before invoking the chain) | `RetryMiddleware`, `AuthRefreshMiddleware` (when set, skips the auth-refresh-and-retry replay so a non-idempotent / probe-then-create write is not re-issued after a mid-flight 401/403 — issue #1157) |
| `build_request` | `BuildRequest` | `Session._perform_authed_post` (stashed before chain entry as the rebuild recipe) | `AuthRefreshMiddleware._rebuild_request_after_refresh`, `Session._authed_post_chain_terminal` (via `_refresh_request_for_current_auth`) |
| `log_label` | `str` | `Session._perform_authed_post` | `DrainMiddleware`, `RetryMiddleware`, `ErrorInjectionMiddleware`, `AuthRefreshMiddleware`, `TracingMiddleware`, `Session._authed_post_chain_terminal` |
| `auth_snapshot` | `AuthSnapshot` | `Session._perform_authed_post` (initial snapshot before chain entry); refreshed by `AuthRefreshMiddleware._rebuild_request_after_refresh` after a successful refresh, and replaced by `Session._refresh_request_for_current_auth` at the chain leaf when a freshness check detects auth moved while the request was queued | `Session._refresh_request_for_current_auth` (chain-terminal pre-POST freshness check); pair-mutated with the materialized envelope so middlewares never observe a torn `(snapshot, request)` pair |
| `auth_refreshed` | `bool` | `AuthRefreshMiddleware` (sets to `True` after a successful refresh, **before** the retry leg) | `AuthRefreshMiddleware` (skip-on-replay guard so a `RetryMiddleware` retry on the post-refresh leg cannot drive a second refresh on a fresh 401) |
| `rpc_queue_wait_seconds` | `float` | `SemaphoreMiddleware` (writes queue-wait duration on slot acquire — also exported as `RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS` from `_middleware_context.py`; `RPC_QUEUE_WAIT_CONTEXT_KEY` remains a compatibility alias in `_middleware_semaphore.py`) | `Session._perform_authed_post` (forwards to `ClientMetrics.record_rpc_queue_wait` after the chain returns) |

Middlewares are forbidden from inventing new keys without an ADR update.
The dict is mutable by reference (deliberately) but read-mostly in
practice. See
§"Decision: `RpcRequest.context: dict[str, Any]` is the long-term shape"
below for the rationale and the policy that governs additions.

> **Note on `operation_variant`.** Idempotency policy is resolved
> **before chain entry** in `RpcExecutor._execute_once()` via
> `_idempotency.resolve_effective_disable_internal_retries(...)`; the
> resolved boolean is what flows through the chain as
> `disable_internal_retries`. The chain itself never needs the
> per-request `operation_variant` selector, so it is intentionally
> absent from this vocabulary.

### Decision: `RpcRequest.context: dict[str, Any]` is the long-term shape

The stringly-keyed `dict[str, Any]` on `RpcRequest.context` is the
**permanent** chain-metadata carrier. It will **not** be replaced with a
typed dataclass, `TypedDict`, or per-key dataclass field. The
bounded-vocabulary table above is the contract; the policy below bounds
drift. The corresponding consequence summary lives under the "Unwanted"
list in §"Consequences" below; this section is the authoritative
rationale.

Rationale (decision crystallized in the arc-1 architecture-deepening
review and flagged on the demotion list as
`[arc-1-formalized-as-deferred-permanent]` — formalized here rather
than deferred to a future arc):

- The typed-envelope migration that arrived with PR #1018 (`b856e01`)
  promoted `RpcRequest.url`, `RpcRequest.headers`, and `RpcRequest.body`
  to typed fields and introduced `BuildRequestResult` for the rebuild
  path. The HTTP-shape envelope is now typed end-to-end; `context` is the
  only remaining `dict[str, Any]` surface on the chain envelope, and that
  is by design.
- Typing the per-key shape (`TypedDict`, `dataclass`, or a discriminated
  union of metadata records) would force a mechanical refactor of every
  read site (middlewares, terminal, host helpers) for ~400 LOC of churn
  whose only payoff is replacing one shape of "the key isn't there at
  runtime" failure with another: a `KeyError` becomes an
  `AttributeError`, but neither is caught at write time, and `mypy
  --strict` already cannot prove anything stronger about a `TypedDict`
  whose keys are populated across module boundaries by middlewares the
  type checker does not know are in the chain.
- The drift protection a typed dict would buy comes — at a fraction of
  the cost — from the **bounded-vocabulary table + lint enforcement**.
  Reviewer attention is the actual gate today; the planned meta-lint
  (see "Follow-up" below) makes that gate enforceable without per-call
  type machinery.
- This is the same trade ADR-0013's composable-capabilities split makes
  in the other direction: keep the typed surfaces typed, keep the
  metadata-bag surface stringly when its consumers are a closed set
  governed by ADR review.

**Policy: vocabulary additions require an ADR update.**

- Any new `context` key — read OR written — by a middleware, the
  terminal, or a host helper requires an ADR-0009 amendment that adds
  the key to the vocabulary table above with `Set by` / `Read by`
  columns populated.
- Reuse before invention: if an existing key carries the same semantic
  (e.g. `disable_internal_retries` already encodes "skip my own retry
  budget"), reuse it; do not coin a near-synonym.
- The ADR-update requirement is per-key, not per-PR — a single PR that
  adds two related keys still amends the table for both.
- Removing a key also requires an amendment (mark the row with a
  retirement note and the closing PR, e.g. `(retired in #NNNN)`); the
  table is the audit log.
- Local-only ephemera that a single middleware writes and reads inside
  one `await next_call(request)` boundary, never observed by another
  middleware or by the terminal, is NOT a context key — keep that state
  on the middleware instance or in a `contextvars.ContextVar` scoped to
  the call. Context keys are for cross-middleware contract.

The vocabulary is also centralized in
`_middleware_context.ALLOWED_RPC_CONTEXT_KEYS`, and
`tests/_guardrails/test_middleware_context_contract.py` scans production
middleware and transport code for non-approved literal context keys.
Adding a key must update this table, `_middleware_context.py`, and the
guard test in the same PR.

### AuthRefreshMiddleware constructor signature (Tier-13 target, NOT shipped in Tier-12)

The signature pinned in this section is the **target** shape for the
post-`Kernel.post` rewrite (Tier-13 row 13.2). PR 12.8 SHIPPED a simpler
interim shape that defers request-rebuilding to the leaf — see "PR 12.9
close-out notes" §"AuthRefreshMiddleware shipped without rebuild
closures" for the details and rationale. Until Tier 13 makes the chain
leaf a pure POST, the closure-callback pair below remains aspirational:

```python
class AuthRefreshMiddleware:
    def __init__(
        self,
        coordinator: AuthRefreshCoordinator,
        rebuild_headers: Callable[[AuthSnapshot], Mapping[str, str]],
        build_request_factory: Callable[[AuthSnapshot], BuildRequestResult],
    ) -> None: ...

    async def __call__(
        self,
        request: RpcRequest,
        next_call: NextCall,
    ) -> RpcResponse:
        try:
            return await next_call(request)
        except (HTTP401, _TransportAuthExpired):
            # Refresh tokens (coalesced, may raise).
            await self.coordinator.refresh()
            # Re-snapshot auth state, rebuild headers + url + body for the retry.
            # ``build_request_factory`` is called exactly ONCE so the rebuilt
            # url and body stay consistent (a side-effecting factory must not
            # produce a torn pair).
            snapshot: AuthSnapshot = await self.coordinator.snapshot()
            rebuilt = self.build_request_factory(snapshot)
            new_headers = dict(self.rebuild_headers(snapshot))
            if rebuilt.headers is not None:
                # Per the headers-merge policy below, ``BuildRequestResult.headers``
                # overlays ``rebuild_headers`` so per-request extras (e.g. an
                # explicit ``Content-Type`` for an upload variant) win over the
                # snapshot-derived auth headers.
                new_headers.update(rebuilt.headers)
            new_request = dataclasses.replace(
                request,
                headers=new_headers,
                url=rebuilt.url,
                body=rebuilt.body,
            )
            return await next_call(new_request)
```

Pinned details:

- `coordinator: AuthRefreshCoordinator` — the existing seam from
  `_session_auth.py:53` (`AuthRefreshCoordinator`). PR 12.8 reuses it.
- `rebuild_headers: Callable[[AuthSnapshot], Mapping[str, str]]` — **sync**
  (no I/O; pure header construction from snapshot). Returns the *full*
  base header dict for the retry, not a delta. The middleware copies the
  result into a fresh `dict` via `dict(self.rebuild_headers(snapshot))` so
  the callback's return is not shared with `RpcRequest.headers`.
- `build_request_factory: Callable[[AuthSnapshot], BuildRequestResult]` —
  **sync**. Returns a `BuildRequestResult` dataclass (`url: str`,
  `body: bytes`, `headers: Mapping[str, str] | None`) — equivalent to
  today's `_BuildRequest` tuple return, but as a named dataclass for the
  new code path. Called **exactly once per retry attempt**: a single
  invocation produces the rebuilt `url`, `body`, and per-request headers
  overlay so a side-effecting factory cannot emit a torn `url`/`body`
  pair.
- Headers-merge policy: `rebuild_headers` provides the *base* headers
  (snapshot-derived auth: CSRF token, session id, X-Goog-AuthUser, etc.).
  `BuildRequestResult.headers` is an *overlay*: when non-`None`, the
  middleware merges it on top of the base via `dict.update`. This mirrors
  the current `materialize_rpc_request(...)` semantics where the `headers`
  slot in the `BuildRequest` tuple represents per-request extras (e.g. an
  explicit `Content-Type` for an upload variant) that win over the
  snapshot defaults. Most call sites today pass `None` here, in which
  case the base headers from `rebuild_headers` are used unchanged.
- Retry semantics: **exactly one** retry per `next_call` invocation. If
  the retry also raises 401, the exception propagates — no second retry,
  no recursion. `RetryMiddleware` (outside `AuthRefresh` per the chain
  ordering) handles non-auth retries.
- `AuthSnapshot` and `BuildRequestResult` are promoted from private to
  public-ish in `_request_types.py` (PR 12.1, alongside `BuildRequest`).
  The current `_AuthSnapshot` and the tuple-return shape become these
  named types; the underscore-prefixed originals remain as
  `__all__`-excluded re-exports for one cycle, then delete in PR 12.9.

PR 12.8 writes the implementation; everything else (the `Session` wiring
of the two callbacks, the retry semantics, the types) is fixed here.

## Consequences

**Wanted:**

- The transport POST path shrinks to a pure `Kernel.post` terminal after
  PRs 12.4 (metrics out), 12.5 (drain out), 12.7 (retry out), 12.8 (auth
  refresh out), and the later Adapter retirement. The terminal has no
  middleware concerns left.
- Each cross-cutting concern becomes independently testable: build a
  chain with just `[FakeRetry()]` around a terminal stub, drive a failing
  request, assert the retry happened. No more "the metrics callback fires
  on the third nested call inside the transport loop if the 429 branch …"
  tests.
- Adding a new concern is a new middleware class plus an entry in the
  chain ordering — no transport-leaf surgery, no growth of a host
  Protocol.
- The chain ordering becomes a single line of code (`[Drain, Metrics,
  Semaphore, Retry, AuthRefresh, ErrorInjection, Tracing]`) instead of an
  implicit invariant scattered across one transport function.

**Unwanted:**

- A typed dataclass per request is more allocation than the existing
  three-tuple from `_BuildRequest`. The cost is in microseconds per RPC;
  the chain itself does not run in a hot loop (one chain dispatch per
  authed POST, of which there are at most a few hundred per session).
  Benchmarks in PR 12.2 will quantify; expected overhead is <1% of
  per-RPC wall time and dominated by the existing `httpx` POST.
- The chain's `context: dict[str, Any]` is dynamically typed by
  design — see §"Decision: `RpcRequest.context: dict[str, Any]` is the
  long-term shape" above for the rationale and the bounded-vocabulary
  policy that holds drift back. The meta-lint originally scoped for PR
  12.9 was not delivered in that PR; until the follow-up lint noted in
  the Decision section lands, the bounded-vocabulary table is enforced
  by reviewer attention on every `request.context["…"]` literal in a
  diff.
- Around-style middlewares can short-circuit (return without calling
  `next_call`). No production middleware in the Tier-12 set does this,
  but the protocol allows it; test middlewares that need to (e.g. a
  "deny all requests" canary) get the capability for free. The lint
  policy is to grep the production middleware modules for "without
  calling `next_call`" patterns at review time.
- Six small middleware files replace six branches in one large transport
  function. Total LOC roughly equal; navigation improves (one concern per
  file) but the call path becomes longer in the stack trace.

## Alternatives considered

**Before/after hook pairs (the Flask / Express middleware shape).** Rejected.
The `AuthRefreshMiddleware` transforms the request and *retries once*; a
before-hook returns the transformed request and an after-hook can observe
the response, but neither can re-invoke the chain mid-call. Modelling
auth refresh as before/after would require a separate "retry once on
401" mechanism inside the transport, defeating the extraction.

**Single `transport_wrapper: Callable[[NextCall], NextCall]` factory list.**
Rejected. The factory shape works for around-style behavior but obscures
the request/response types at every wrap site, since each factory has to
re-declare its own `async def wrapper(request, /): …` body. The
`Middleware: Protocol` shape with `RpcRequest` and `RpcResponse`
dataclasses gives type checkers full visibility into the chain at every
wrap point.

**`contextvars.ContextVar` for per-request metadata instead of
`RpcRequest.context: dict`.** Rejected. The `dict` is shared by reference
across the chain (intentional), is trivially mockable in tests, and shows
up plainly in `repr(request)`. `contextvars` would: (a) require every
middleware to import the var; (b) be invisible in the request repr; (c)
leak state across tests if not cleared. The audit work that goes into
`tests/_guardrails/` to enforce `RpcRequest.context` key discipline is cheaper
than the audit work to enforce `ContextVar` reset discipline.

**Build the chain per-call instead of once at Session init.** Rejected.
Per-call chain construction would allow per-call middleware lists, which
breaks the "the chain is the transport contract" invariant — every call
must traverse the same chain, otherwise the chain isn't doing the
cross-cutting work it's supposed to. Tests that want different middleware
lists construct different `Session` instances.

**Inline the dataclass definitions in `_middleware.py` and skip
`_request_types.py`.** Initially rejected while the transport leaf was still
being extracted. The later architecture cleanup accepted the promotion:
`AuthSnapshot` and `BuildRequest` are not chain-specific, and owning them in
`_request_types.py` keeps `_middleware.py` focused on the chain envelope shape
while giving non-chain callers a stable import path.

**Use `httpx.Request` / `httpx.Response` directly instead of
`RpcRequest` / `RpcResponse` dataclasses.** Rejected for the request side;
accepted for the response side. The request dataclass needs to carry
`context: dict[str, Any]` for chain metadata, and `httpx.Request` has no
extension point for that. The response side just carries
`httpx.Response` (the field name `RpcResponse.response`) plus context, so
the dataclass is a thin wrapper there.

## PR 12.9 close-out notes

Two implementation details landed differently than the PR-12.1 pin and
are documented here so Tier-13 callers have an authoritative reference.

### `SemaphoreMiddleware` inserted at chain position 2

The `max_concurrent_rpcs` slot is acquired by `SemaphoreMiddleware`,
which sits between `MetricsMiddleware` and `RetryMiddleware` in the
chain. The middleware writes the per-call queue-wait duration to
`RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS` and
`Session._perform_authed_post` forwards that value to
`ClientMetrics.record_rpc_queue_wait` after the chain returns.

The placement is constrained by three simultaneous invariants the
shipped chain must preserve (codex caught the violations in the first
cut of PR 12.9):

1. **Drain admission scope.** `DrainMiddleware` (chain pos 0) increments
   `_in_flight_posts` for every call that enters the chain, INCLUDING
   ones still waiting for the `max_concurrent_rpcs` slot. If the
   semaphore wait happened OUTSIDE the chain (e.g. wrapping the chain
   dispatch in `_perform_authed_post`), `client.close()` mid-flight
   would reject queued tasks instead of waiting for them — a regression
   vs. the PR-12.5-onwards contract.
2. **Metrics latency includes queue wait.** `MetricsMiddleware`
   (chain pos 1) starts its `perf_counter` BEFORE `next_call` reaches
   `SemaphoreMiddleware`. Latency emitted on `rpc_latency_seconds_total`
   and `RpcTelemetryEvent.elapsed_seconds` covers queue wait + work,
   matching the pre-PR-12.9 (PR 12.8) telemetry shape where Metrics
   wrapped the leaf-side semaphore.
3. **`asyncio.Semaphore` is non-reentrant.** `RetryMiddleware`
   (chain pos 3) re-invokes its `next_call` on retry attempts. Placing
   `SemaphoreMiddleware` INSIDE `RetryMiddleware` would have each retry
   attempt try to acquire a fresh slot, deadlocking under sustained
   429s when every slot is held by a retrying call waiting to retry
   into a slot. Placing it OUTSIDE `RetryMiddleware` (chain pos 2)
   bounds the whole retry-and-refresh cohort to one slot per logical
   RPC.

The middleware takes a zero-arg async-context-manager factory rather
than a raw `asyncio.Semaphore`, so production wires
`ClientComposed.get_rpc_semaphore`. The holder returns a
`contextlib.nullcontext` when `max_concurrent_rpcs is None` (unbounded
opt-out) — the `async with` collapses to a no-op for that case.

History: the first cut of PR 12.9 audit-find #1 wrapped the semaphore
around `Session._perform_authed_post` directly (outside the chain).
Codex caught the Drain-admission regression with a reproducible
`max_concurrent_rpcs=1` test case — queued tasks raised `RuntimeError`
during shutdown instead of being awaited. `SemaphoreMiddleware`
restored the contract while keeping the retry-multi-acquisition guard
the original audit-find existed to provide.

### `AuthRefreshMiddleware` shipped without rebuild closures

The original §"AuthRefreshMiddleware constructor signature" pinned a
shape with two callbacks:

```python
rebuild_headers: Callable[[AuthSnapshot], Mapping[str, str]]
build_request_factory: Callable[[AuthSnapshot], BuildRequestResult]
```

PR 12.8 shipped a simpler `AuthRefreshMiddleware` that catches
`httpx.HTTPStatusError`, drives the coalesced refresh via
`AuthRefreshCoordinator.await_refresh`, marks
`RPC_CONTEXT_AUTH_REFRESHED`, and re-invokes `next_call`
**with the same `RpcRequest`** — the terminal re-reads the now-refreshed
`AuthSnapshot` from the coordinator and rebuilds headers/url/body before
calling `Kernel.post`, preserving the pre-Tier-12 semantics.

Why deferred: lifting `rebuild_headers` and `build_request_factory` into
chain-level closures requires the leaf to become a pure POST that accepts
already-built bytes/headers (i.e. `Kernel.post` from Tier-13 row 13.2).
Doing it before the `Kernel.post` rewrite would create a third
request-construction path (chain-side closure + leaf-side rebuild +
`_chat_transport.send_authed_post` direct path) that all have to stay in
sync — strictly worse than leaving the leaf authoritative for one more
tier.

The `AuthSnapshot` and `BuildRequestResult` named dataclasses landed in
PR 12.1 and live in `_request_types.py`. They are unused by
`AuthRefreshMiddleware` today but are the target shape for Tier 13.

Tier-13 follow-up: rewrite
`AuthRefreshMiddleware` against the pinned closure-callback signature
once `Kernel.post` is the chain leaf. The signature pinned in
§"AuthRefreshMiddleware constructor signature" above is the target.

ADR-0010 (the original target of this forward reference) was itself
superseded by ADR-0013 ("Composable Session Capabilities") in v0.5.0.
ADR-0009's middleware-chain ordering remains load-bearing; chain
construction now lives in `MiddlewareChainBuilder`
(`_middleware_chain.py`) — an extraction performed inside this ADR's
domain, not a supersession — and the order is preserved by
`tests/unit/test_chain_wiring.py`. Status: Accepted (chain order
load-bearing).
