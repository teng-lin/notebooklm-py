# Architecture (post-v0.5.0)

This document describes the runtime shape of `notebooklm-py` after the
v0.5.0 refactor program closed (Phases 1-4 of
[`refactor-completion-plan`](../.sisyphus/plans/refactor-completion-plan.md)).
It is the canonical post-refactor map; the historical narrative lives in
[`docs/refactor.md`](./refactor.md).

## Layered overview

```
+----------------------------------------------------------+
| CLI Layer (src/notebooklm/cli/*)                         |
|   Click groups: login / use / list / source / generate / |
|   download / chat / note. Pure adapter — no RPC logic.   |
+----------------------------------------------------------+
                          ▼
+----------------------------------------------------------+
| Client Layer (client.py + feature APIs)                  |
|   NotebookLMClient + namespaced sub-clients:             |
|     .notebooks  .sources  .artifacts  .chat              |
|     .notes      .research                                |
|   Each feature API depends on a NARROW capability        |
|   protocol — not on the broad ``Session`` class.         |
+----------------------------------------------------------+
                          ▼
+----------------------------------------------------------+
| Session Layer (Session + collaborators)                  |
|   Session orchestrates a small set of focused            |
|   collaborators (see "Collaborator graph" below).        |
|   Session itself stays a wide facade because the         |
|   ``Session.__new__(Session)`` test-fixture pattern      |
|   needs the property bridges + ``_ensure_*()`` lazy-init |
|   surface intact.                                        |
+----------------------------------------------------------+
                          ▼
+----------------------------------------------------------+
| RPC Layer (src/notebooklm/rpc/*)                         |
|   types.py    method IDs + enums (source of truth)       |
|   encoder.py  request encoding                           |
|   decoder.py  response parsing                           |
+----------------------------------------------------------+
```

## Per-capability protocol model

ADR-013 ("Composable Session Capabilities") is the design rationale: feature
APIs depend on narrow capability protocols defined in
[`_session_contracts.py`](../src/notebooklm/_session_contracts.py), not on
the concrete `Session` class. Production satisfies the union via `Session`;
tests satisfy it via [`tests/_fixtures/fake_core.py:FakeSession`](../tests/_fixtures/fake_core.py).

| Protocol | Responsibility |
|----------|----------------|
| `RpcCaller` | Exposes `rpc_call(method, params, ...)` — the chokepoint every feature API uses for batchexecute calls. |
| `LoopGuard` | Exposes `bound_loop` and the cross-loop affinity check; consumed by anything that may touch the HTTP client. |
| `OperationScopeProvider` | Exposes `operation_scope(label)` — the async context manager that scopes drain admission for graceful shutdown. |
| `AsyncWorkRuntime` | Exposes `kernel.create_task(...)` for spawning task children that drain on close. |
| `DrainHookRegistration` | Exposes `register_drain_hook(name, hook)` so feature APIs can wire close-time cleanup. |
| `ChatRuntime` | Chat-specific surface (cache, next_reqid, transport_post). Local to `_chat.py`. |
| `ArtifactsRuntime` | Artifact-specific surface (poll_registry, RPC dispatch). Local to `_artifacts.py`. |
| `UploadRuntime` | Upload-specific surface (semaphore-gated upload pipeline). Local to `_source_upload.py`. |

Design rationale (ADR-013): feature APIs depend on a narrow capability
protocol that names only what they need. Tests can substitute the broad
`FakeSession` (which satisfies the union) without paying the cost of
constructing a real `Session`.

## Post-refactor `Session` collaborator graph

```
        +---------------------+
        |  NotebookLMClient   |
        +----------+----------+
                   |
                   v
          +--------+--------+
          |     Session     |  (facade — see "Known debt" below)
          +--------+--------+
                   |
   +---------------+----------------+------------------+
   |               |                |                  |
   v               v                v                  v
RpcExecutor   AuthRefresh-   ClientLifecycle   MiddlewareChain-
              Coordinator                       Builder
   |               |                |                  |
   |               |                |                  v
   |               |                |          (chain construction
   |               |                |           ordered per ADR-009)
   |               |                v
   |               |        TransportDrainTracker
   |               v
   |       (refresh task + auth-snapshot lock)
   v
ClientMetrics + ReqidCounter + CookiePersistence
       (per-instance state)
```

| Collaborator | Module | Responsibility |
|--------------|--------|----------------|
| `RpcExecutor` | [`_rpc_executor.py`](../src/notebooklm/_rpc_executor.py) | Single RPC dispatch path. Encodes the request, runs the middleware chain, decodes the response. Consumes the `RpcOwner` Protocol declared at module top. |
| `AuthRefreshCoordinator` | [`_session_auth.py`](../src/notebooklm/_session_auth.py) | Owns the auth-snapshot lock and the refresh task. Canonical implementation for `Session._snapshot` / `Session.update_auth_tokens` (which are now one-line delegates per Phase 3 PR 8). |
| `ClientLifecycle` | [`_session_lifecycle.py`](../src/notebooklm/_session_lifecycle.py) | HTTP-client open/close, keepalive task, cookie save coordination. Holds `_timeout`, `_bound_loop`, `_http_client`, `_keepalive_*`. |
| `MiddlewareChainBuilder` | [`_middleware_chain.py`](../src/notebooklm/_middleware_chain.py) | Constructs the middleware chain in the canonical ADR-009 order. Extracted in Phase 3 PR 7. |
| `TransportDrainTracker` | [`_transport_drain.py`](../src/notebooklm/_transport_drain.py) | Tracks in-flight transport operations + the drain condition variable. Gates graceful shutdown. |
| `ClientMetrics` | [`_client_metrics.py`](../src/notebooklm/_client_metrics.py) | Per-instance counters (`ClientMetricsSnapshot`) + the `on_rpc_event` user callback. |
| `ReqidCounter` | [`_reqid_counter.py`](../src/notebooklm/_reqid_counter.py) | Monotonic `_reqid` for the chat backend; lock-protected `await core.next_reqid()`. |
| `CookiePersistence` | [`_cookie_persistence.py`](../src/notebooklm/_cookie_persistence.py) | Cookie-jar persistence + `__Secure-1PSIDTS` rotation. |

## Middleware chain (ADR-009)

The runtime chain order is pinned by
[`tests/unit/test_chain_wiring.py`](../tests/unit/test_chain_wiring.py). The
order is load-bearing: changing it without simultaneously updating the
pin tests (`test_chain_seeded_with_final_adr_009_ordering`) is a bug.

```
TracingMiddleware
   ↓
SemaphoreMiddleware
   ↓
DrainMiddleware
   ↓
ErrorInjectionMiddleware     (synthetic-error harness; no-op in prod)
   ↓
RetryMiddleware              (429 / 5xx with Retry-After honor)
   ↓
AuthRefreshMiddleware        (refresh-on-auth-error; capped retries)
   ↓
MetricsMiddleware
   ↓
RPC dispatch leaf            (RpcExecutor → AuthedTransport → httpx)
```

## ADR cross-references

- [ADR-001](./adr/0001-layered-core-seams-and-property-bridge-policy.md) — Layered seams + property-bridge policy.
- [ADR-002](./adr/0002-capability-protocol-pattern.md) — Capability Protocol pattern (Superseded by the arch-d2-cutover PR).
- [ADR-009](./adr/0009-middleware-chain.md) — Middleware chain ordering (Accepted; load-bearing).
- [ADR-013](./adr/0013-composable-session-capabilities.md) — Composable Session Capabilities (the post-v0.5.0 capability model).

## Known architectural debt — `Session` is a wide facade

**`Session` remains a wide facade (~1450 lines) post-v0.5.0.** The
capability-protocol refactor (ADR-013) decomposed Session's *implementation*
into focused collaborators (`RpcExecutor`, `AuthRefreshCoordinator`,
`ClientLifecycle`, `MiddlewareChainBuilder`, `TransportDrainTracker`,
`ClientMetrics`, `ReqidCounter`), but did not shrink the facade's *surface*.
The property bridges + `_ensure_*()` lazy-init backfill are load-bearing
for the `Session.__new__(Session)` test-fixture pattern; deleting them
would require migrating dozens of tests off the fixture pattern first.

Thin-facade work (a hypothetical target Session of ~400-500 lines) is
**deferred indefinitely** pending a concrete feature blocker. The
property bridges have no measurable cost in production; the debt
exists only against future ergonomics for new contributors reading
`Session` for the first time. Open a placeholder issue if you hit a
specific friction point worth tracking — do not pre-plan absent a
named pain point.

## See also

- [`CLAUDE.md`](../CLAUDE.md) — high-level navigation map for AI agents working in this repo.
- [`docs/development.md`](./development.md) — how to add a new feature API.
- [`docs/refactor.md`](./refactor.md) — historical narrative of the multi-phase refactor.
- [`docs/python-api.md`](./python-api.md) — public Python API surface.
