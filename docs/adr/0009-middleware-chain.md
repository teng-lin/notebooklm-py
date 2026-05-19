# ADR-009: Middleware chain for cross-cutting transport concerns

## Status

Accepted (Tier 12 PR 12.1; closed by PR 12.9).

This ADR shipped in PR 12.1 of the Tier-12/13 greenfield migration as
type-only scaffolding: the Protocol, dataclasses, and `build_chain` helper
landed without production wiring. PR 12.2 wired an empty chain into
`ClientCore`. PRs 12.3 through 12.8 each extracted one cross-cutting
concern into a dedicated middleware. **PR 12.9 closes the tier** — the
seven-middleware chain `[Drain, Metrics, Semaphore, Retry, AuthRefresh,
ErrorInjection, Tracing]` is fully wired, the leaf
(`AuthedTransport.perform_authed_post`) is a pure POST, and the
underscore-prefixed compatibility aliases were removed. The chain ordering, the `RpcRequest.context` key vocabulary, and
the Protocol shape pinned below are the load-bearing contract going
forward into Tier 13.

Two implementation realities diverged from the original PR-12.1 pin and
are documented in the "PR 12.9 close-out notes" section at the bottom of
this ADR:
1. The `AuthRefreshMiddleware` constructor shipped with a simpler shape
   that defers request-rebuilding to the leaf (`rebuild_headers` /
   `build_request_factory` closures are NOT yet wired at chain level).
2. The RPC concurrency semaphore wraps the chain dispatch (not the leaf),
   restoring pre-Tier-12 "one slot per logical RPC" semantics.

Tier 13 (`Kernel.post` terminal) will revisit (1) — the closure callbacks
remain pinned as the target shape for the chain-leaf rewrite.

The signatures pinned in this ADR (especially the `AuthRefreshMiddleware`
constructor, §"AuthRefreshMiddleware constructor signature") are
load-bearing: PR 12.8's implementation has zero degrees of freedom on
shape. PRs 12.2–12.7 also depend on the chain ordering and the
`RpcRequest.context` keys defined below.

Forward reference: ADR-002 ("Capability Protocol pattern,
`ClientCoreCapabilities` fat union") will be superseded by ADR-010 in
Tier 13. That supersession is *not* performed by this ADR; ADR-002 remains
`Accepted (Sunset = D2 cutover)` until ADR-010 lands.

## Context

The post-remediation `ClientCore` orchestrates six cross-cutting concerns
across every authenticated POST. The "Today" column below describes the
pre-Tier-12 state (when ADR-009 was written, before any chain extraction
landed); the "Post-Tier-12" column describes where each concern lives
after PR 12.9 closed the tier. `_SyntheticErrorTransport` was deleted by
PR 12.9; the chain-layer `ErrorInjectionMiddleware` is the only
substitution path going forward.

| Concern | Pre-Tier-12 | Post-Tier-12 (PR 12.9 → today) |
|---|---|---|
| In-flight drain tracking | `TransportDrainTracker.begin/end` around the call (`_core_drain.py`) | `DrainMiddleware` (chain pos 0) |
| Metrics emission | `ClientMetrics.on_rpc_event` callbacks woven through `AuthedTransport` (`_core_metrics.py`) | `MetricsMiddleware` (chain pos 1) |
| RPC concurrency gate | `asyncio.Semaphore` inside `AuthedTransport.perform_authed_post` | `SemaphoreMiddleware` (chain pos 2) |
| Retry on 5xx / 429 | inline loops inside `AuthedTransport.perform_authed_post` | `RetryMiddleware` (chain pos 3) |
| Auth refresh on 401 | inline branch inside `AuthedTransport.perform_authed_post` (`_core_auth.py`) | `AuthRefreshMiddleware` (chain pos 4) |
| Synthetic error injection (tests) | `_SyntheticErrorTransport` wraps the httpx client (`_core_error_injection.py`) — DELETED PR 12.9 | `ErrorInjectionMiddleware` (chain pos 5) |
| Per-attempt tracing/logging | scattered `logger.debug` calls inside the retry loop | `TracingMiddleware` (chain pos 6) |

Adding a seventh concern (e.g. an idempotency-routing wrapper for retry
safety, ADR-005) requires touching `AuthedTransport.perform_authed_post`
directly. Each concern's state holder
(`TransportDrainTracker` / `ClientMetrics` / etc.) is also threaded into
`AuthedTransport` as constructor arguments or `_AuthedTransportHost`
attributes, which means: (a) a new concern grows the host Protocol, and
(b) every change to one concern risks regressing the others because they
share a function body.

The greenfield design in `docs/architecture-evolution.md` §3.4 proposes
lifting each concern into a composable middleware, leaving
`AuthedTransport.perform_authed_post` (and, post-PR 13.2, `Kernel.post`)
as a pure-transport function. The chain is the composition substrate.

Five details that shaped this ADR (the others are in the master plan):

1. **HTTP-level, not RPC-level.** The chain wraps the transport, not the
   encoder. Middlewares see already-encoded bytes; encoding and decoding
   live in `Session.rpc_call` (Tier 13). This keeps every middleware
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
   by monkeypatching (ADR-007).
4. **Idempotency resolution happens *above* the chain.**
   `Session.rpc_call` calls
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
*above* the chain in `Session.rpc_call`.

### Chain ordering (load-bearing)

The chain is composed in this exact order (outermost → innermost):

```text
Drain → Metrics → Semaphore → Retry → AuthRefresh → ErrorInjection → Tracing → terminal
```

Where `terminal` is `Kernel.post` after PR 13.2, and
`AuthedTransport.perform_authed_post` until then.

The leftmost middleware in the sequence becomes the outermost wrapper.
`build_chain` enforces this ordering by composing in reverse (last
middleware is wrapped first around `terminal`).

`SemaphoreMiddleware` was inserted at chain position 2 in PR 12.9 (see
"PR 12.9 close-out notes" below) after the first cut of the audit-find
moved the `max_concurrent_rpcs` slot to `ClientCore._perform_authed_post`
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
  re-triggers the refresh, which is a bug the current `AuthedTransport`
  also guards against with a per-attempt flag.
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
  visibility the original `AuthedTransport` debug logging provided.

### `RpcRequest.context` keys (the chain's metadata vocabulary)

| Key | Type | Set by | Read by |
|---|---|---|---|
| `rpc_method` | `RPCMethod \| None` | `Session.rpc_call` | metrics, tracing |
| `operation_variant` | `str \| None` | `Session.rpc_call` | `AuthRefreshMiddleware` (passes back to `resolve_effective_disable_internal_retries` on retry) |
| `disable_internal_retries` | `bool` | `Session.rpc_call` (post-resolution from `_idempotency.resolve_effective_disable_internal_retries`) | `RetryMiddleware` |
| `build_request` | `BuildRequest` | `Session.rpc_call` / `Session.transport_post` | chain leaf (adapter into `AuthedTransport.perform_authed_post`) |
| `log_label` | `str` | `Session.rpc_call` / `Session.transport_post` | chain leaf, `DrainMiddleware`, `TracingMiddleware` |
| `auth_refreshed` | `bool` | `AuthRefreshMiddleware` (sets to `True` after a successful refresh) | `AuthRefreshMiddleware` (skip-on-replay guard so a `RetryMiddleware` retry doesn't drive a second refresh on a fresh 401) |
| `rpc_queue_wait_seconds` | `float` | `SemaphoreMiddleware` (writes queue-wait duration on slot acquire) | `ClientCore._perform_authed_post` (forwards to `ClientMetrics.record_rpc_queue_wait`) |

Middlewares are forbidden from inventing new keys without an ADR update.
The dict is mutable by reference (deliberately, per master plan
§"Per-request behavior") but read-mostly in practice.

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
  `_core_auth.py:53` (`AuthRefreshCoordinator`). PR 12.8 reuses it.
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
  today's `_core_transport.py:282` semantics where the `headers` slot in
  the `_BuildRequest` tuple represents per-request extras (e.g. an
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

- `AuthedTransport.perform_authed_post` shrinks to a pure POST after
  PRs 12.4 (metrics out), 12.5 (drain out), 12.7 (retry out), 12.8 (auth
  refresh out). PR 12.9 verifies the post-extraction leaf has no
  middleware concerns left.
- Each cross-cutting concern becomes independently testable: build a
  chain with just `[FakeRetry()]` around a `FakeAuthedPost`, drive a
  failing request, assert the retry happened. No more "the metrics
  callback fires on the third nested call inside `AuthedTransport` if
  the 429 branch …" tests.
- Adding a new concern is a new middleware class plus an entry in the
  chain ordering — no `AuthedTransport` surgery, no growth of
  `_AuthedTransportHost`.
- The chain ordering becomes a single line of code (`[Drain, Metrics,
  Retry, AuthRefresh, ErrorInjection, Tracing]`) instead of an implicit
  invariant scattered across `_core_transport.py:243`-`379`.

**Unwanted:**

- A typed dataclass per request is more allocation than the existing
  three-tuple from `_BuildRequest`. The cost is in microseconds per RPC;
  the chain itself does not run in a hot loop (one chain dispatch per
  authed POST, of which there are at most a few hundred per session).
  Benchmarks in PR 12.2 will quantify; expected overhead is <1% of
  per-RPC wall time and dominated by the existing `httpx` POST.
- The chain's `context: dict[str, Any]` is dynamically typed and
  trades static-type-checking strictness for forward compatibility
  across middleware PRs. The `RpcRequest.context` keys table above is
  the policy that bounds drift; the meta-lint planned for PR 12.9 will
  enforce it.
- Around-style middlewares can short-circuit (return without calling
  `next_call`). No production middleware in the Tier-12 set does this,
  but the protocol allows it; test middlewares that need to (e.g. a
  "deny all requests" canary) get the capability for free. The lint
  policy is to grep the production middleware modules for "without
  calling `next_call`" patterns at review time.
- Six small middleware files replace six branches in one large
  `AuthedTransport` function. Total LOC roughly equal; navigation
  improves (one concern per file) but the call path becomes longer in
  the stack trace.

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
`tests/_lint/` to enforce `RpcRequest.context` key discipline is cheaper
than the audit work to enforce `ContextVar` reset discipline.

**Build the chain per-call instead of once at Session init.** Rejected.
Per-call chain construction would allow per-call middleware lists, which
breaks the "the chain is the transport contract" invariant — every call
must traverse the same chain, otherwise the chain isn't doing the
cross-cutting work it's supposed to. Tests that want different middleware
lists construct different `Session` instances.

**Inline the dataclass definitions in `_middleware.py` and skip
`_request_types.py`.** Rejected. The `AuthSnapshot` and `BuildRequest`
aliases are not chain-specific — they describe types that already live in
`_core_transport.py` and are read by `_chat.py`, `_chat_transport.py`, and
`_core_rpc.py`. Promoting them into a sibling module (`_request_types.py`)
keeps `_middleware.py` focused on the chain envelope shape and gives
non-chain callers a stable import path that survives PR 12.9's underscore
removal.

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
`request.context["rpc_queue_wait_seconds"]` and
`ClientCore._perform_authed_post` forwards that value to
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
`SemaphoreMiddleware(self._get_rpc_semaphore)` and the accessor returns
a `contextlib.nullcontext` when `max_concurrent_rpcs is None` (unbounded
opt-out) — the `async with` collapses to a no-op for that case.

History: the first cut of PR 12.9 audit-find #1 wrapped the semaphore
around `ClientCore._perform_authed_post` directly (outside the chain).
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
`request.context["auth_refreshed"] = True`, and re-invokes `next_call`
**with the same `RpcRequest`** — the leaf
(`AuthedTransport.perform_authed_post`) re-reads the now-refreshed
`AuthSnapshot` from the coordinator and rebuilds headers/url/body inside
the leaf, exactly as it did pre-Tier-12.

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

Tier-13 follow-up (tracked in
`.sisyphus/plans/tier-12-13-greenfield-migration.md` row 13.2): rewrite
`AuthRefreshMiddleware` against the pinned closure-callback signature
once `Kernel.post` is the chain leaf. The signature pinned in
§"AuthRefreshMiddleware constructor signature" above is the target;
update this ADR's Status to "Superseded by ADR-010" when that lands.
