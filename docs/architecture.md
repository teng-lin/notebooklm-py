# Architecture (post-v0.5.0)

This document describes the runtime shape of `notebooklm-py` after the
v0.5.0 refactor program closed (Phases 1-4 of the multi-phase refactor
plan; the proposal that drove the work is preserved at
[`docs/refactor.md`](./refactor.md)). It is the canonical post-refactor
map.

## Layered overview

```text
+----------------------------------------------------------+
| CLI Layer (src/notebooklm/cli/*)                         |
|   Top-level commands (login, use, status, list, ask,     |
|   doctor, completion, ...) registered by the session/    |
|   notebook/chat/doctor modules; plus subcommand groups   |
|   (source, artifact, agent, generate, download, note,    |
|   share, skill, research, language, profile). Pure       |
|   adapter — no RPC logic.                                |
+----------------------------------------------------------+
                          ▼
+----------------------------------------------------------+
| Client Layer (client.py + feature APIs)                  |
|   NotebookLMClient + namespaced sub-clients:             |
|     .notebooks  .sources  .artifacts  .chat              |
|     .notes      .research  .settings  .sharing           |
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

ADR-013 ("Composable Session Capabilities") is the design rationale:
feature APIs depend on narrow capability Protocols rather than on the
concrete `Session` class. Six Protocols live in
[`_session_contracts.py`](../src/notebooklm/_session_contracts.py) —
four shared capability Protocols used by ≥2 features, plus `AuthMetadata`
and `Kernel`, whose sole consumer today is `SourceUploadPipeline`. Per
ADR-013 §Decision §2, those two stay in the shared contracts module
(rather than moving into `_source_upload.py`) because they front
Session-owned objects (the authenticated account snapshot and the
transport kernel). ADR-013 explicitly rejects anticipatory promotion —
"No capability is promoted on speculation." Feature-module-local runtime
Protocols live next to their single consumer.

**Module-level Protocols** (defined in
[`_session_contracts.py`](../src/notebooklm/_session_contracts.py)):

| Protocol | Responsibility |
|----------|----------------|
| `RpcCaller` | Exposes `rpc_call(method, params, ...)` — the chokepoint every feature API uses for batchexecute calls. |
| `LoopGuard` | Exposes `assert_bound_loop()` — single-method cross-loop affinity check; consumed by anything that may touch the HTTP client. |
| `OperationScopeProvider` | Exposes `operation_scope(label)` — async context manager that scopes drain admission for graceful shutdown. |
| `AsyncWorkRuntime` | Composes `LoopGuard` + `OperationScopeProvider` for features that own async work. |
| `AuthMetadata` | Selected-account routing metadata — `authuser` + `account_email` properties. Single consumer today: `SourceUploadPipeline`. |
| `Kernel` | Pure transport surface — `post()` method, `cookies` property, `aclose()`. Single consumer today: `SourceUploadPipeline`. |

**Feature-module-local Protocols** (composite runtime unions + the single-consumer
capability slice `DrainHookRegistration`; each lives next to its consumer and is
not exported from `_session_contracts.py`):

| Protocol | Module | Responsibility |
|----------|--------|----------------|
| `ChatRuntime` | [`_chat.py`](../src/notebooklm/_chat.py) | Chat-feature capability union — composes `RpcCaller` + `LoopGuard` and adds chat-specific `transport_post()` + `next_reqid()` methods. (The `ConversationCache` lives on `ChatAPI`, not the Protocol.) |
| `ArtifactsRuntime` | [`_artifacts.py`](../src/notebooklm/_artifacts.py) | Artifact-feature capability union — composes `RpcCaller` + `AsyncWorkRuntime` + `DrainHookRegistration`. No own members; used by `ArtifactsAPI` for RPC dispatch, loop affinity, operation scopes, and close-time drain-hook registration. The `PollRegistry` lives on `ArtifactsAPI`, not the Protocol. |
| `UploadRuntime` | [`_source_upload.py`](../src/notebooklm/_source_upload.py) | Upload-pipeline capability union — composes `RpcCaller` + `OperationScopeProvider`. The upload semaphore is internal to `SourceUploadPipeline`, not the Protocol. |
| `DrainHookRegistration` | [`_artifacts.py`](../src/notebooklm/_artifacts.py) | Exposes `register_drain_hook(name, hook)` for close-time cleanup. Sole `DrainHookRegistration` after the broad-`Session` Protocol was deleted from `_session_contracts.py` (see the `_session_contracts.py` module docstring). |

Production satisfies the shared Protocols via `Session`; tests substitute
[`tests/_fixtures/fake_core.py:FakeSession`](../tests/_fixtures/fake_core.py)
(constructed via `make_fake_core(...)`) — the sanctioned ADR-007 / ADR-013
fixture pattern.

### Narrow ≠ all Protocols are narrow

Not every `Protocol` in the codebase is a thin capability slice. The
executor-facing `RpcOwner` Protocol at
[`_rpc_executor.py:54`](../src/notebooklm/_rpc_executor.py) is
deliberately **wide** — it declares ten members, including the public
`rpc_call` method, four private attrs (`_timeout`, `_refresh_callback`,
`_refresh_retry_delay`, `_http_client`), and five private methods
(`_perform_authed_post`, `_await_refresh`, `_rpc_call_impl`,
`_increment_metrics`, `_emit_rpc_event`) — because `RpcExecutor` was
extracted from `Session` while preserving back-references to auth state,
metrics emission, and the `_perform_authed_post` chain.
Narrowing `RpcOwner` is gated on dismantling the property-bridge zoo
(see [Known debt](#known-architectural-debt--session-is-a-wide-facade)).
Feature APIs depend on capability Protocols that genuinely are narrow;
the executor itself depends on a near-`Session`-shaped Protocol that
hasn't been narrowed yet.

## Post-refactor `Session` collaborator graph

```text
                     +---------------------+
                     |  NotebookLMClient   |
                     +----------+----------+
                                |
                                v
                       +--------+--------+
                       |     Session     |  (facade — see "Known debt" below)
                       +--------+--------+
                                |
   +---------+---------+--------+---------+---------+---------+---------+
   |         |         |        |         |         |         |         |
   v         v         v        v         v         v         v         v
RpcExec-  AuthRefresh- Client-  Middleware Transport ClientMetrics Reqid- CookiePers-
utor      Coordinator  Lifecycle ChainBuilder DrainTracker         Counter istence
   |         |         |        |         |
   |         |         |        v         |
   |         |         |   builds         |
   |         |         |   chain via      |
   |         |         |   ADR-009 order  |
   |         |         |   into Drain/    |
   |         |         |   Metrics/Sema/  |
   |         |         |   Retry/AuthRef/ |
   |         |         |   ErrInj/Tracing |
   |         |         |                  |
   |         |         |                  +--- counters touched by MetricsMiddleware
   |         |         |
   |         |         +--- HTTP open/close + keepalive task
   |         |
   |         +--- refresh task + auth-snapshot lock
   |
   +--- single RPC dispatch path (RpcExecutor.execute → chain → AuthedTransport → httpx)
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
| `IdempotencyRegistry` | [`_idempotency.py`](../src/notebooklm/_idempotency.py) | Policy/classification registry keyed by `(RPCMethod, operation_variant)`. `RpcExecutor.execute()` consults it to resolve `effective_disable_internal_retries` and to inject client tokens for `CLIENT_TOKEN_DEDUPE` methods (most entries are currently `UNCLASSIFIED`, a behaviour-neutral default). Not Session-owned, but part of the RPC dispatch path. Side-effect probing (`idempotent_create(...)`) is a separate mechanism not owned by this registry. |

## Middleware chain (ADR-009)

The runtime chain order is pinned by
[`tests/unit/test_chain_wiring.py`](../tests/unit/test_chain_wiring.py)
(facade-level) and
[`tests/unit/test_middleware_chain_builder.py`](../tests/unit/test_middleware_chain_builder.py)
(builder-level). The order is load-bearing: changing it without
simultaneously updating the pin tests
(`test_chain_seeded_with_final_adr_009_ordering`) is a bug.

The chain list in [`MiddlewareChainBuilder.build()`](../src/notebooklm/_middleware_chain.py)
reads outermost-first (index 0 wraps everything below it):

```text
DrainMiddleware              outermost — admits and tracks for shutdown drain
   ↓
MetricsMiddleware            starts timing here (latency includes queue wait)
   ↓
SemaphoreMiddleware          max_concurrent_rpcs slot acquired AFTER Drain/Metrics,
                             BEFORE Retry can re-enter (one slot per logical RPC)
   ↓
RetryMiddleware              429 / 5xx with Retry-After honor
   ↓
AuthRefreshMiddleware        refresh-on-auth-error; capped retries
   ↓
ErrorInjectionMiddleware     synthetic-error harness; no-op in prod
   ↓
TracingMiddleware            innermost — structured-logging boundary
                             (OpenTelemetry export is future work)
   ↓
RPC dispatch leaf            (RpcExecutor → AuthedTransport → httpx)
```

## ADR cross-references

- [ADR-001](./adr/0001-layered-core-seams-and-property-bridge-policy.md) — Layered seams + property-bridge policy.
- [ADR-002](./adr/0002-capability-protocol-pattern.md) — Capability Protocol pattern (Superseded by the arch-d2-cutover PR).
- [ADR-009](./adr/0009-middleware-chain.md) — Middleware chain ordering (Accepted; load-bearing).
- [ADR-013](./adr/0013-composable-session-capabilities.md) — Composable Session Capabilities (the post-v0.5.0 capability model).

## Known architectural debt — `Session` is a wide facade

**`Session` remains a wide facade (~1454 lines) post-v0.5.0.** ADR-013's
capability-protocol refactor decomposed Session's *implementation* into
focused collaborators (`RpcExecutor`, `AuthRefreshCoordinator`,
`ClientLifecycle`, `MiddlewareChainBuilder`, `TransportDrainTracker`,
`ClientMetrics`, `ReqidCounter`, `CookiePersistence`) but did not shrink
the facade's *surface*. Two pieces of scaffolding in
[`_session.py`](../src/notebooklm/_session.py) exist solely to keep the
legacy `Session.__new__(Session)` test-fixture pattern working:

- ~two dozen compatibility-bridge properties, starting at
  [`_session.py:442`](../src/notebooklm/_session.py) and running through
  the chat-conversation properties further down.
- Four `_ensure_*()` lazy-init methods:
  [`_ensure_auth_coord` (:514)](../src/notebooklm/_session.py),
  [`_ensure_lifecycle` (:584)](../src/notebooklm/_session.py),
  [`_ensure_observability_state` (:751)](../src/notebooklm/_session.py),
  [`_ensure_authed_post_chain` (:785)](../src/notebooklm/_session.py).

**The unblock is bounded.** 13 test sites across 6 files still construct
fixtures via `Session.__new__(Session)`:

- [`tests/unit/test_chain_wiring.py:350`](../tests/unit/test_chain_wiring.py)
- [`tests/unit/test_client_metrics.py`](../tests/unit/test_client_metrics.py) (lines `328`, `346`, `361`, `370`, `391`)
- [`tests/unit/test_logging_correlation.py:262`](../tests/unit/test_logging_correlation.py)
- [`tests/unit/test_middleware_chain_builder.py:55`](../tests/unit/test_middleware_chain_builder.py)
- [`tests/unit/test_swallow_observability.py`](../tests/unit/test_swallow_observability.py) (lines `26`, `49`, `67`)
- [`tests/unit/test_transport_drain.py`](../tests/unit/test_transport_drain.py) (lines `319`, `355`)

Once those 13 sites migrate to the ADR-007 / ADR-013-sanctioned
`make_fake_core(...)` fixture, the `_ensure_*()` backfill and the
compatibility-property bridges can be deleted, shrinking `_session.py`
by an estimated 200–400 lines.

**The bridge tax is not just ergonomic.** The `_ensure_*()` methods have
ordering dependencies — `_ensure_auth_coord` primes
`_ensure_observability_state` first because every coordinator method
reaches into `host._metrics_obj`. Every new collaborator extraction grows
the scaffold non-linearly: a fresh `_ensure_*()` plus a tranche of
property bridges per seam, plus its position in the call chain. This is
future-refactor friction, not first-read inconvenience.

**Status: deferred until those tests migrate**, not deferred indefinitely.
Effort estimate: Medium (1–2 days) — migrate the 13 fixture sites to
`make_fake_core(...)`, then delete the bridges and `_ensure_*()` methods.
Open an issue when picking this up so the migration can be tracked.

## See also

- [`CLAUDE.md`](../CLAUDE.md) — high-level navigation map for AI agents working in this repo.
- [`docs/development.md`](./development.md) — how to add a new feature API.
- [`docs/refactor.md`](./refactor.md) — historical narrative of the multi-phase refactor.
- [`docs/python-api.md`](./python-api.md) — public Python API surface.
