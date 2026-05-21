# Architecture (post-v0.5.0)

This document describes the runtime shape of `notebooklm-py` after the
v0.5.0 refactor program closed (Phases 1-4 of the multi-phase refactor
plan; the proposal that drove the work is preserved at
[`docs/refactor-history.md`](./refactor-history.md)). It is the canonical post-refactor
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
|   The historical test-only property shims and lazy       |
|   backfill methods were retired in the session-shrink    |
|   arc; tests now use collaborators or ``make_fake_core``.|
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

### Executor protocols are narrow too

The executor-facing Protocols are intentionally implementation-local,
but they no longer depend on Session's historical private attribute
surface. `RpcOwner` in
[`_rpc_executor.py`](../src/notebooklm/_rpc_executor.py) declares only
the kernel plus the methods the executor still calls; timeout,
refresh-callback, and retry-delay values are supplied through constructor
providers. `_AuthedTransportHost` in
[`_authed_transport.py`](../src/notebooklm/_authed_transport.py) declares
only the kernel and auth-snapshot method used by the leaf transport.
This keeps feature APIs on narrow capability Protocols while avoiding a
near-`Session` structural contract inside the RPC stack.

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
   +-----+-----+-----+-----+----+----+-----+-----+-----+
   |     |     |     |     |         |     |     |     |
   v     v     v     v     v         v     v     v     v
Rpc-  Auth-  Client- Mid-  Trans-  Metrics Reqid Cookie- Kernel
Exec  Ref    Life    Chain Drain   Tracker Coun  Pers
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
   +--- single RPC dispatch path (RpcExecutor.execute → chain → AuthedTransport → Kernel → httpx)
   |
   +--- Kernel (transport core; owns httpx.AsyncClient + cookie jar)
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
| `AuthedTransport` | [`_authed_transport.py`](../src/notebooklm/_authed_transport.py) | Single-attempt authed-POST seam (today's middleware-chain leaf); post-Tier-12 a pure POST, with all retry decisions (429 / 5xx via `RetryMiddleware`; 401 / 403 / 400-CSRF via `AuthRefreshMiddleware`) owned by the chain. Consumes the `_AuthedTransportHost` Protocol declared at module top. |
| `Kernel` | [`_kernel.py`](../src/notebooklm/_kernel.py) | Pure transport core. Owns the `httpx.AsyncClient` and cookie jar; exposes `post()`, the `cookies` property, and `aclose()` (the close path wraps it in `asyncio.shield` from `ClientLifecycle.close()`). Concrete class behind the `Kernel` Protocol in `_session_contracts.py`; constructed by `Session.__init__()` at `_session.py:393`. The middleware-chain leaf is expected to migrate from `AuthedTransport.perform_authed_post` to `Kernel.post` per the Tier-13 migration plan (row 13.2; chain-leaf contract pinned in [ADR-009](./adr/0009-middleware-chain.md)). |

## Domain-service collaborators

Beyond the Session-orchestration graph, several feature APIs are implemented via dedicated domain services and helper modules:

| Service / Module | Module | Responsibility |
|-------------------|--------|----------------|
| `NoteService` | [`_note_service.py`](../src/notebooklm/_note_service.py) | Service layer managing note CRUD, note-backed content generation, and sync. |
| `NoteBackedMindMapService` | [`_mind_map.py`](../src/notebooklm/_mind_map.py) | Specific adapter service representing mind-maps, backed by standard notebook notes. |
| `ArtifactDownloadService` | [`_artifact_downloads.py`](../src/notebooklm/_artifact_downloads.py) | Asynchronous download coordinator for finished artifacts. |
| `_artifact_formatters` | [`_artifact_formatters.py`](../src/notebooklm/_artifact_formatters.py) | Markdown, HTML, and plain text formatters for artifacts. |
| `_artifact_listing` | [`_artifact_listing.py`](../src/notebooklm/_artifact_listing.py) | Listing and filtering operations for notebook artifacts. |

## Middleware chain (ADR-009)

The runtime chain order is pinned by
[`tests/unit/test_chain_wiring.py`](../tests/unit/test_chain_wiring.py)
(facade-level) and
[`tests/unit/test_middleware_chain_builder.py`](../tests/unit/test_middleware_chain_builder.py)
(builder-level). The order is load-bearing: changing it without
simultaneously updating the pin tests
(`test_chain_seeded_with_final_adr_009_ordering`) is a bug.

The chain list in [`MiddlewareChainBuilder.build()`](../src/notebooklm/_middleware_chain.py) (PR [#883](https://github.com/teng-lin/notebooklm-py/pull/883))
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

- [ADR-001](./adr/0001-layered-core-seams-and-property-bridge-policy.md) — Layered seams + property-bridge policy (superseded; shims retired).
- [ADR-002](./adr/0002-capability-protocol-pattern.md) — Capability Protocol pattern (Superseded by [arch-d2-cutover](https://github.com/teng-lin/notebooklm-py/pull/835) (#835)).
- [ADR-009](./adr/0009-middleware-chain.md) — Middleware chain ordering (Accepted; load-bearing).
- [ADR-013](./adr/0013-composable-session-capabilities.md) — Composable Session Capabilities (the post-v0.5.0 capability model).

## Known architectural debt — `Session` facade size

**Status: closed for the property-shim debt.** The staged 8-PR
session-shrink arc retired the legacy `Session.__new__(Session)` fixture
backfill, the private attribute property shims, and the broad executor
Protocol attributes. [`_session.py`](../src/notebooklm/_session.py) is
now about 1,043 lines and functions as a clean orchestrator around
focused collaborators (`RpcExecutor`, `AuthRefreshCoordinator`,
`ClientLifecycle`, `MiddlewareChainBuilder`, `TransportDrainTracker`,
`ClientMetrics`, `ReqidCounter`, `CookiePersistence`, `AuthedTransport`,
and `Kernel`).

The permanent regression guard is
[`tests/_lint/test_no_session_compat_bridges.py`](../tests/_lint/test_no_session_compat_bridges.py):
its allowlist is empty, and new test reach-ins to the retired Session
private attributes fail in CI. Tests that need lightweight cores should
use the ADR-007 / ADR-013-sanctioned
[`make_fake_core(...)`](../tests/_fixtures/fake_core.py) fixture; tests
that need collaborator internals should target the owning collaborator
directly.

## See also

- [`CLAUDE.md`](../CLAUDE.md) — high-level navigation map for AI agents working in this repo.
- [`docs/development.md`](./development.md) — how to add a new feature API.
- [`docs/refactor-history.md`](./refactor-history.md) — historical narrative of the multi-phase refactor + downstream migration tables.
- [`docs/python-api.md`](./python-api.md) — public Python API surface.
