# Architecture (post-v0.5.0)

This document describes the runtime shape of `notebooklm-py` after the
v0.5.0 refactor program closed. It is the canonical post-refactor map;
the historical narrative lives in
[`docs/refactor-history.md`](./refactor-history.md).

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
+----------------------------------------------------------+
                          ▼
+----------------------------------------------------------+
| Runtime Layer (client-owned collaborators)               |
|   NotebookLMClient owns ClientComposed plus focused       |
|   collaborators such as RpcExecutor, RuntimeTransport,   |
|   ClientLifecycle, and Kernel.                           |
+----------------------------------------------------------+
                          ▼
+----------------------------------------------------------+
| RPC Layer (src/notebooklm/rpc/*)                         |
|   types.py    method IDs + enums (source of truth)       |
|   encoder.py  request encoding                           |
|   decoder.py  response parsing                           |
+----------------------------------------------------------+
```

## Library call flows

`NotebookLMClient` is the composition root. It constructs the shared runtime
collaborator graph, wires feature APIs to narrow runtime Protocols, and
injects stateful services such as `SourceUploadPipeline`, `NoteService`,
`NoteBackedMindMapService`, and `ArtifactDownloadService`. Feature modules
build NotebookLM params and parse domain rows; client-owned collaborators own
dispatch, transport, auth refresh, metrics, and lifecycle.

### Typed batchexecute RPCs

Most public methods (`client.notebooks.list()`, `client.sources.rename()`,
`client.settings.get()`, artifact generation, note CRUD, etc.) follow this path:

```text
+----------------------------------------------------------------+
| CLI command or user code                                       |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| NotebookLMClient.<feature>.<method>()                          |
|   feature API / service builds params and chooses RPCMethod   |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| RpcExecutor.rpc_call(...)                 satisfies RpcCaller  |
|   - pre-open guard via Kernel.get_http_client()                |
|   - logical-RPC request id + rpc_calls_started metric          |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| RpcExecutor._execute_once(...)                                 |
|   - idempotency policy resolution                              |
|   - method-id resolution, request encoding, URL/body builder   |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| RuntimeTransport.perform_authed_post(...)                      |
|   - loop-affinity guard, auth snapshot                         |
|   - RpcRequest materialization                                 |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| ADR-009 middleware chain                                       |
|   Drain -> Metrics -> Sema -> Retry -> AuthRefresh             |
|   -> ErrInj -> Tracing                                         |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| MiddlewareChainHost._authed_post_chain_terminal(...)           |
|   chain leaf — ADR-014 Rule 4                                  |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| RuntimeTransport.terminal(...)                                 |
|   - final auth-freshness rebuild immediately before POST       |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| Kernel.post(...) -> _streaming_post -> httpx.AsyncClient       |
+----------------------------------------------------------------+
                                 |
                                 v  response unwinds back up
+----------------------------------------------------------------+
| RpcExecutor decodes via rpc.decode_response(...)               |
| Feature API maps decoded payload -> typed/domain result        |
+----------------------------------------------------------------+
```

Production wires `RpcExecutor` directly into each feature as its
`RpcCaller` per ADR-014 Rule 1; `NotebookLMClient.rpc_call` dispatches
through the same `RpcExecutor` stored as `NotebookLMClient._rpc_executor`
for the public raw-RPC escape hatch.

`NotebookLMClient.rpc_call(method, params)` is the public raw-RPC escape hatch.
It skips feature-specific param builders and result parsers, but still enters
the same `RpcExecutor.rpc_call → RuntimeTransport → Kernel`
pipeline.

### Chat ask path

`ChatAPI.ask()` is the major transport-sharing exception to the pure
`RpcExecutor` shape. Streaming chat has a custom request body and chat-flavored
error mapping, so the first ask POST goes through:

```text
+----------------------------------------------------------------+
| ChatAPI.ask(...)                                               |
|   - loop_guard.assert_bound_loop()                             |
|   - source-id lookup                                           |
|   - conversation lock / cache                                  |
|   - reqid.next_reqid()                                         |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| chat_aware_authed_post(transport, ...)                         |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| RuntimeTransport.perform_authed_post(...)                      |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| ADR-009 middleware chain                                       |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| RuntimeTransport.terminal(...) -> Kernel.post                  |
+----------------------------------------------------------------+
                                 |
                                 v  streaming response
+----------------------------------------------------------------+
| streaming chat parser + citation/reference parser              |
+----------------------------------------------------------------+
```

`ChatAPI` holds the four collaborators it needs (`rpc`, `transport`,
`reqid`, `loop_guard`) directly — there is no `ChatRuntime` composite
or broad runtime transport indirection.

For a new conversation, `ChatAPI.ask()` then calls `GET_LAST_CONVERSATION_ID`
through the normal `RpcExecutor` path. Other chat methods such as
`get_conversation_turns()` and `delete_conversation()` also use normal
`rpc_call`.

### Uploads, downloads, and polling

Some feature workflows intentionally combine RPC with non-RPC HTTP work:

| Flow | Runtime shape |
|------|---------------|
| Source file upload | `SourcesAPI.add_file()` delegates to `SourceUploadPipeline.add_file()`. The pipeline opens an `operation_scope`, takes its own upload semaphore, registers the file source through `runtime.rpc_call(ADD_SOURCE_FILE)`, then uses a dedicated `httpx.AsyncClient` and live Kernel cookies for the Scotty resumable-upload start/finalize calls. Optional wait/rename steps return to `rpc_call`. |
| Source URL/text/Drive add | `SourceAddService` wraps URL and Drive mutating RPCs in `idempotent_create(...)` because those flows have stable probes. Text-source adds are intentionally non-idempotent unless the caller handles dedupe externally. |
| Artifact generation | `ArtifactsAPI` builds `CREATE_ARTIFACT` params (via the `_artifact_payloads.build_*` helpers) and uses the normal `rpc_call` path directly — the former `ArtifactGenerationService` was folded into the facade (#1205). `ArtifactPollingService` owns leader/follower polling with `operation_scope(...)` and a feature-local `PollRegistry`; `ArtifactsAPI` registers a close-time drain hook for poll cleanup. |
| Artifact download | `ArtifactDownloadService` lists/selects artifacts through `RpcCaller`, but media downloads use a separate streaming `httpx.AsyncClient` with storage cookies, trusted-host checks, and a producer/writer split. They do not go through `RpcExecutor` or `Kernel.post`. |
| Notes and mind maps | `NoteService` owns note-row CRUD/classification through `RpcCaller`. `NoteBackedMindMapService` adapts those note rows for artifact-facing mind-map behavior so notes and artifacts do not import each other. |

## Cross-cutting policies

Three policies thread through the layers above and are easy to violate by
accident. Each is pinned by an ADR.

### Loop affinity (ADR-004)

**Why we need it.** The client is built on `httpx.AsyncClient` plus a
network of `asyncio` primitives — locks, semaphores, condition variables,
queues, and a keepalive `Task`. Every one of those binds to the event
loop on which it is first awaited. Re-using a client across loops either
*deadlocks* (the wake-up is scheduled on a loop that will never run
again) or raises a confusing `RuntimeError` from deep inside the
primitive — both fail far away from the actual cause. The contract is
the simplest mitigation that makes the failure mode visible: bind to one
loop and fail loudly on the first violating call instead of hanging ten
minutes later. The cost of cross-loop safety is paid once at the
lifecycle layer instead of in every seam, so individual collaborators
can use plain `asyncio.Lock` / `asyncio.Semaphore` without defensive
re-binding logic.

**The contract.** One `NotebookLMClient` instance is bound to its
`open()`-time event loop. Cross-loop reuse (a different `asyncio.run`,
a different thread's loop) is unsupported and raises `RuntimeError` at
the first authed POST. Cross-thread reuse is unsupported for the same
reason — every thread has its own default loop. Cross-tenant reuse is
unsupported because a live client owns per-instance chat state and auth
state. `ChatAPI._cache` keys on `conversation_id` without an
`account_email` dimension, so tenant-switching a client risks mixing
local chat history if a conversation id is reused across accounts.

The contract is enforced by the free function `assert_bound_loop(...)` in
[`_loop_affinity.py`](../src/notebooklm/_loop_affinity.py), which is
called from every helper that captured a loop reference at `open()` time
(transport drain, reqid counter, auth refresh, artifact polling, chat).
The `LoopGuard` capability Protocol (`assert_bound_loop()`) is how
feature APIs surface the same check without taking a `Session` dependency.

See [ADR-004](./adr/0004-loop-affinity-contract.md) and the consumer
notes in [`docs/python-api.md`](./python-api.md#concurrency-contract).

### Idempotency (ADR-005)

**Why we need it.** `batchexecute` runs over HTTPS, so every mutating
call (create, delete, refresh, share, generate, …) is exposed to a
*commit-lost* failure: the server commits the write, then the response
is lost in transit. A naive retry on top of a commit-lost failure
produces a duplicate write — a duplicate notebook, a duplicate source,
an extra LLM inference, a re-sent invite email — depending on the RPC.
The transport's inner retry loop is *correct* for read-only RPCs and
*dangerous* for mutating ones. Before the taxonomy existed, the only
mitigation was a per-call-site `disable_internal_retries=True` flag that
didn't document *why* a given RPC was retry-unsafe, so the decision was
easy to lose during refactors. The taxonomy makes retry safety a
**property of the RPC** (declared once in the registry) instead of a
**property of the call site** (re-derived every time someone touches
the code).

**The classification.** Every active RPC is classified into one of five
retry-safety profiles by the `IdempotencyRegistry` in
[`_idempotency.py`](../src/notebooklm/_idempotency.py):

| Policy | Meaning | Effect on the inner retry loop |
|--------|---------|--------------------------------|
| `UNCLASSIFIED` | Placeholder for hand-built test/future registries; not used by the production registry for active RPCs | Silent, retries enabled (preserves pre-taxonomy behavior) |
| `PROBE_THEN_CREATE` | Caller owns a probe loop; transport must not blind-retry | Force-disable inner retries |
| `IDEMPOTENT_SET_OP` | Replay-safe read-only, delete, rename, or set-state RPC | Retries are safe; left enabled |
| `AT_LEAST_ONCE_ACCEPTED` | Caller has explicitly accepted duplicate side-effect cost (emails / billing / notifications) | Retries enabled; rate-limited WARN emitted so operators can see the trade-off |
| `NON_IDEMPOTENT_NO_RETRY` | No dedupe key and no probe; first failure must surface | Force-disable inner retries |

The axis is *closed*. A sixth policy would need an ADR update and an
executor change in lockstep — the five-policy cap is intentional so a
reviewer can hold the whole taxonomy in mind during a code review.

`RpcExecutor._execute_once` consults the registry once per call to
resolve the effective `disable_internal_retries`. The caller's explicit
`disable_internal_retries=True` always wins over the registry default.

Every `PROBE_THEN_CREATE` entry must carry a documented `notes`
rationale describing how that mutation recovers (a probe/recovery wrapper
exists) or why inner retries stay disabled. The registry-audit test
`test_retry_disabled_entries_are_intentional_and_documented` fails if a
new `PROBE_THEN_CREATE` policy is added without one.

The production registry has explicit coverage for every active
`RPCMethod`, including read-only RPCs. Read-only entries are registered
as replay-safe `IDEMPOTENT_SET_OP` rows rather than left as
production-`UNCLASSIFIED`; `UNCLASSIFIED` is retained only as a
placeholder for tests and future development.

See [ADR-005](./adr/0005-idempotency-taxonomy.md). Side-effect probing
(`idempotent_create(...)`) is a separate mechanism not owned by the
registry; see the upload/source-add row in the "Uploads, downloads, and
polling" table above.

### Schema validation (ADR-011)

Batchexecute responses are undocumented and Google reshapes them without
notice. Decoders walk nested positional lists; a single index shift
either crashes with raw `IndexError` from inside a feature module or
silently degrades.

The single helper that decoders use to navigate row shapes is
`notebooklm.rpc.safe_index` in
[`rpc/_safe_index.py`](../src/notebooklm/rpc/_safe_index.py). It always
raises a typed shape-drift error: strict decoding is the only mode (the
`NOTEBOOKLM_STRICT_DECODE=0` soft-mode opt-out was retired in v0.7.0). The
`RpcExecutor` decode path narrowly wraps
`json.JSONDecodeError`, `KeyError`, `IndexError`, and `TypeError` into
`RPCError`; other exception types (e.g. `AttributeError`) intentionally
propagate as code bugs rather than being conflated with shape drift.

See [ADR-011](./adr/0011-schema-validation-policy.md).

## Per-capability protocol model

ADR-013 ("Composable Session Capabilities") is the design rationale:
feature APIs depend on narrow capability Protocols rather than on the
deleted concrete `Session` class.
[ADR-014](./adr/0014-feature-local-runtime-adapters.md) extends that
intent at runtime: each feature receives the specific collaborator it
needs, never a broad runtime facade. `NotebookLMClient.__init__` is the
composition root that wires each feature with the satisfier it needs.

Six Protocols live in
[`_runtime_contracts.py`](../src/notebooklm/_runtime_contracts.py) —
four shared capability Protocols used by ≥2 features, plus `AuthMetadata`
and `Kernel`, whose sole consumer today is `SourceUploadPipeline`. Per
ADR-013 §Decision §2, those two stay in the shared contracts module
(rather than moving into `_source_upload.py`) because they front
client-owned objects (the authenticated account snapshot and the
transport kernel). ADR-013 explicitly rejects anticipatory promotion —
"No capability is promoted on speculation." Feature-module-local runtime
Protocols live next to their single consumer.

**Module-level Protocols** (defined in
[`_runtime_contracts.py`](../src/notebooklm/_runtime_contracts.py)):

| Protocol | Responsibility |
|----------|----------------|
| `RpcCaller` | Exposes `rpc_call(method, params, ...)` — the chokepoint every feature API uses for batchexecute calls. |
| `LoopGuard` | Exposes `assert_bound_loop()` — single-method cross-loop affinity check; consumed by anything that may touch the HTTP client. |
| `OperationScopeProvider` | Exposes `operation_scope(label)` — async context manager that scopes drain admission for graceful shutdown. |
| `AsyncWorkRuntime` | Composes `LoopGuard` + `OperationScopeProvider` for features that own async work. No production consumer at present (the artifact polling service now takes the two underlying Protocols directly); retained because the composition rule it pins is still useful documentation. |
| `AuthMetadata` | Selected-account routing metadata — `authuser` + `account_email` properties. Single consumer today: `SourceUploadPipeline`. |
| `Kernel` | Pure transport surface — `post()` method, `cookies` property, `aclose()`. Single consumer today: `SourceUploadPipeline`. |

**Feature-module-local Protocols.** No feature-local composite-runtime
unions or adapter dataclasses exist in production. Every
multi-capability feature takes its collaborators by keyword-only
constructor argument:

- `ArtifactsAPI` and `SourceUploadPipeline` take `rpc: RpcCaller`,
  `drain: TransportDrainTracker`, `lifecycle: ClientLifecycle`.
- `ChatAPI` takes `rpc: RpcCaller`, `transport: RuntimeTransport`,
  `reqid: ReqidCounter`, `loop_guard: LoopGuard`.

Production satisfies the shared Protocols via the underlying
collaborators (ADR-014 Rule 1: `RpcExecutor` satisfies `RpcCaller`,
`ClientLifecycle` satisfies `LoopGuard`, `TransportDrainTracker`
satisfies `OperationScopeProvider`). There is no production `Session`
class in the runtime graph.
Tests substitute
[`tests/_fixtures/fake_core.py:FakeSession`](../tests/_fixtures/fake_core.py)
(constructed via `make_fake_core(...)`) — the sanctioned ADR-007 / ADR-013
fixture pattern. `FakeSession` is a backward-compatible test-fixture name,
not a production runtime class. Tests that inject narrow fakes into a single feature
(e.g. `MagicMock(spec=RpcCaller, rpc_call=AsyncMock(...))`) construct
the feature directly under ADR-014.

### Executor takes its collaborators directly

Per ADR-014 Rule 5, `RpcExecutor` takes its kernel, transport,
auth-refresh coordinator, and metrics tracker directly — there is no
Session-shaped owner Protocol. The constructor takes
`kernel: Kernel`, `transport: RuntimeTransport`,
`auth_refresh: AuthRefreshCoordinator`, and `metrics: ClientMetrics`
as keyword-only parameters, plus constructor-injected providers for
timeout, refresh-callback enablement, and retry-delay values. The
executor enters transport through
`RuntimeTransport.perform_authed_post` directly; the middleware
terminal is `MiddlewareChainHost._authed_post_chain_terminal →
RuntimeTransport.terminal → Kernel.post`. The chain leaf lives on
`MiddlewareChainHost` so the chain owns its own terminal and retry
tunables (ADR-014 Rule 4 chain-ownership carve-out). Request types,
transport errors, and streaming helpers live in separate owning
modules. This keeps feature APIs on narrow capability Protocols and
the executor on direct collaborator dependencies.

## Client-owned runtime collaborator graph

```text
                        +---------------------+
                        |  NotebookLMClient   |
                        +----------+----------+
                                   |
        +--------------------------+--------------------------+
        |                          |                          |
        v                          v                          v
  _auth: AuthTokens        _seams: ClientSeams        feature API objects
  one mutable instance     decode/sleep/auth-error     notebooks/sources/
                           runtime callables           artifacts/chat/...
        |
        v
  _collaborators: RuntimeCollaborators
  metrics | drain_tracker | reqid | auth_coord | kernel | lifecycle | cookie_persistence
        |
        v
  Kernel owns httpx.AsyncClient + cookie jar; ClientLifecycle opens/closes it

        +--------------------------+
        | _composed: ClientComposed|
        +--------------------------+
        | transport: RuntimeTransport
        | executor: RpcExecutor
        | chain_host: MiddlewareChainHost
        | chain_builder + middlewares
        | get_rpc_semaphore()
        +--------------------------+
             |
             v
  RpcExecutor.rpc_call → RuntimeTransport.perform_authed_post
      → ADR-009 chain → RuntimeTransport.terminal → Kernel.post → httpx
```

| Collaborator | Module | Responsibility |
|--------------|--------|----------------|
| `NotebookLMClient` | [`client.py`](../src/notebooklm/client.py) | Public surface and composition root. Owns `_auth`, `_seams`, `_composed`, `_collaborators`, `_rpc_executor`, and the eight feature API attributes. `__aenter__`, `close`, `drain`, `is_connected`, `metrics_snapshot`, and `rpc_call` route directly to the owning collaborator. |
| `ClientSeams` | [`_client_seams.py`](../src/notebooklm/_client_seams.py) | Mutable holder for runtime callables that closures re-read after construction: `decode_response`, `sleep`, and `is_auth_error`. Construction-only seams such as `async_client_factory` stay on `compose_client_internals(...)` and the client-shell test helper, not on the public constructor. |
| `ClientComposed` | [`_client_composed.py`](../src/notebooklm/_client_composed.py) | Write-once holder for composition state: `transport`, `executor`, `chain_host`, `chain_builder`, `middlewares`, lazy RPC semaphore, and `runtime_collaborators`. Pre-binding access raises a clear `RuntimeError`; the holder deliberately does not expose a broad `.collaborators` alias. |
| `RpcExecutor` | [`_rpc_executor.py`](../src/notebooklm/_rpc_executor.py) | Single logical batchexecute RPC dispatch path. Owns request-id/started-metric bracketing, idempotency policy lookup, method-ID resolution, request encoding, response decode, RPC error mapping, and decode-time auth refresh retry. Takes its `Kernel`, `RuntimeTransport`, `AuthRefreshCoordinator`, and `ClientMetrics` collaborators directly via keyword-only constructor parameters (ADR-014 Rule 5). Enters transport through `RuntimeTransport.perform_authed_post`. |
| `RuntimeTransport` | [`_runtime_transport.py`](../src/notebooklm/_runtime_transport.py) | Authed POST collaborator. Owns `perform_authed_post()` (loop guard, auth snapshot, request materialization, chain dispatch, queue-wait recording), `refresh_request_for_current_auth()`, and `terminal()` (freshness rebuild + `Kernel.post`). Called directly by `RpcExecutor` and by `chat_aware_authed_post` (ChatAPI's chat-flavoured transport call); the middleware chain leaf at `MiddlewareChainHost._authed_post_chain_terminal` continues to dispatch through `RuntimeTransport.terminal` per ADR-014 Rule 4. |
| `MiddlewareChainHost` | [`_middleware_chain_host.py`](../src/notebooklm/_middleware_chain_host.py) | Owns the wired middleware chain (`_authed_post_chain`), the chain leaf (`_authed_post_chain_terminal`), the three retry-budget tunables (`_rate_limit_max_retries`, `_server_error_max_retries`, `_refresh_retry_delay`), and the dynamic `await_refresh` delegate that the auth-refresh middleware captures. The chain's provider lambdas and the transport's `chain_provider` closure read the host's attributes live, so post-construction mutation (e.g. tests setting `client._composed.chain_host._rate_limit_max_retries = 0`) still steers the live chain. |
| `AuthRefreshCoordinator` | [`_runtime_auth.py`](../src/notebooklm/_runtime_auth.py) | Owns the auth-snapshot lock and refresh task. Canonical implementation for `AuthRefreshCoordinator.snapshot(auth=...)`, `update_auth_tokens(auth=..., csrf=..., session_id=...)`, and `update_auth_headers(auth=..., kernel=...)`; callers pass explicit collaborators rather than a host object. |
| `ClientLifecycle` | [`_runtime_lifecycle.py`](../src/notebooklm/_runtime_lifecycle.py) | HTTP-client open/close, keepalive task, cookie save coordination. Holds `_timeout`, `_bound_loop`, `_http_client`, `_keepalive_*`. |
| `MiddlewareChainBuilder` | [`_middleware_chain.py`](../src/notebooklm/_middleware_chain.py) | Constructs the middleware chain in the canonical ADR-009 order. |
| `TransportDrainTracker` | [`_transport_drain.py`](../src/notebooklm/_transport_drain.py) | Tracks in-flight transport operations + the drain condition variable. Gates graceful shutdown. |
| `ClientMetrics` | [`_client_metrics.py`](../src/notebooklm/_client_metrics.py) | Per-instance counters (`ClientMetricsSnapshot`) + the `on_rpc_event` user callback. |
| `ReqidCounter` | [`_reqid_counter.py`](../src/notebooklm/_reqid_counter.py) | Monotonic `_reqid` for the chat backend; lock-protected `next_reqid(...)`. |
| `CookiePersistence` | [`_cookie_persistence.py`](../src/notebooklm/_cookie_persistence.py) | Cookie-jar persistence + `__Secure-1PSIDTS` rotation. |
| `IdempotencyRegistry` | [`_idempotency.py`](../src/notebooklm/_idempotency.py) | Policy/classification registry keyed by `(RPCMethod, operation_variant)`. The production registry explicitly covers every active `RPCMethod`; `UNCLASSIFIED` is retained only as a placeholder for hand-built test/future registries. `RpcExecutor._execute_once()` consults it to resolve `effective_disable_internal_retries`. It is part of the RPC dispatch path, not lifecycle state. Side-effect probing (`idempotent_create(...)`) is a separate mechanism not owned by this registry. |
| `_request_types` | [`_request_types.py`](../src/notebooklm/_request_types.py) | Owns `AuthSnapshot`, `BuildRequest`, and request materialization shapes shared by RPC, chat, auth refresh, and the chain terminal. |
| `_transport_errors` | [`_transport_errors.py`](../src/notebooklm/_transport_errors.py) | Owns transport-level exceptions, `Retry-After` parsing, and raw `Kernel.post` error mapping consumed by `RetryMiddleware` and `AuthRefreshMiddleware`. |
| `_streaming_post` | [`_streaming_post.py`](../src/notebooklm/_streaming_post.py) | Low-level streaming POST helper with the response-size cap used by `Kernel.post`. |
| `Kernel` | [`_kernel.py`](../src/notebooklm/_kernel.py) | Pure transport core. Owns the `httpx.AsyncClient` and cookie jar; exposes `post()`, the `cookies` property, and `aclose()` (the close path wraps it in `asyncio.shield` from `ClientLifecycle.close()`). Concrete class behind the `Kernel` Protocol in `_runtime_contracts.py`; constructed by `build_collaborators(...)` and called from the middleware leaf via `RuntimeTransport.terminal → Kernel.post`. |
| `_runtime_init` | [`_runtime_init.py`](../src/notebooklm/_runtime_init.py) | Construction-time helpers for `NotebookLMClient`: `validate_constructor_args` (kwarg validation/normalization), `build_collaborators` (the seven collaborators in dependency order: `metrics`, `drain_tracker`, `reqid`, `auth_coord`, `kernel`, `lifecycle`, `cookie_persistence`), `build_runtime_transport`, `wire_middleware_chain`, and `compose_client_internals`. It binds the runtime graph into `ClientComposed` and returns `ClientInternals(collaborators, executor)`. |
| `_loop_affinity` | [`_loop_affinity.py`](../src/notebooklm/_loop_affinity.py) | Tiny free-function `assert_bound_loop(bound_loop)` shared by every helper that captures a loop reference at `open()` time (`TransportDrainTracker`, `ReqidCounter`, `AuthRefreshCoordinator`, `ArtifactPollingService`, `ChatAPI`). Enforces ADR-004 without coupling those helpers to the public client. |

### Shipped runtime invariants

[ADR-016](./adr/0016-auth-identity-and-core-logger-compatibility.md)
pins two compatibility-sensitive details that survive the session-elimination
work:

- `NotebookLMClient._auth` is the authoritative mutable `AuthTokens`
  instance. Refresh paths mutate that object in place, and collaborators that
  observe auth must alias it rather than holding detached copies.
- `CORE_LOGGER_NAME` intentionally remains the literal
  `"notebooklm._core"` even though the `_core.py` compatibility module was
  deleted. Runtime code keeps using this logger key through
  `CORE_LOGGER_NAME` for downstream log filters and `caplog` selectors.
  Treat it as a logging compatibility contract, not evidence that
  `notebooklm._core` is an active module or that a concrete `Session` owner
  remains in the runtime graph.

## Domain-service collaborators

Beyond the client-owned runtime graph, several feature APIs are implemented via dedicated domain services and helper modules:

| Service / Module | Module | Responsibility |
|-------------------|--------|----------------|
| `NoteService` | [`_note_service.py`](../src/notebooklm/_note_service.py) | Service layer managing note CRUD, note-backed content generation, and sync. |
| `NoteBackedMindMapService` | [`_mind_map.py`](../src/notebooklm/_mind_map.py) | Specific adapter service representing mind-maps, backed by standard notebook notes. |
| `ArtifactDownloadService` | [`_artifact_downloads.py`](../src/notebooklm/_artifact_downloads.py) | Asynchronous download coordinator for finished artifacts. |
| `_artifact_formatters` | [`_artifact_formatters.py`](../src/notebooklm/_artifact_formatters.py) | Markdown, HTML, and plain text formatters for artifacts. |
| `_artifact_listing` | [`_artifact_listing.py`](../src/notebooklm/_artifact_listing.py) | Listing and filtering operations for notebook artifacts. |
| `_row_adapters*` | [`_row_adapters_artifacts.py`](../src/notebooklm/_row_adapters_artifacts.py), [`_row_adapters_notes.py`](../src/notebooklm/_row_adapters_notes.py), [`_row_adapters_sources.py`](../src/notebooklm/_row_adapters_sources.py) | Wire-shape adapters that wrap raw batchexecute rows (`ArtifactRow`, `NoteRow`, `SourceRow`) behind named accessors so downloads, polling, and listing don't open-code positional indices. Soft-degrade and strict-mode behavior is pinned in `tests/unit/test_row_adapters.py`. |
| `_research_task_parser` | [`_research_task_parser.py`](../src/notebooklm/_research_task_parser.py) | Parses deep-research task results from raw rows. Returns dict-shaped output today; a typed-model migration is not yet complete. |
| `_types/` | [`_types/`](../src/notebooklm/_types) | Private package holding the dataclass and `Protocol` implementations behind the public `types.py` / per-feature public schemas. Split per domain (`artifacts.py`, `chat.py`, `notebooks.py`, `notes.py`, `sharing.py`, `sources.py`, plus `common.py` for shared shapes like `ConnectionLimits`). |

## Authentication subpackage

[`auth.py`](../src/notebooklm/auth.py) is a thin public facade that
re-exports the canonical implementations under
[`_auth/`](../src/notebooklm/_auth). ADR-014 closed ADR-003's deferred
flat-re-export goal: `AuthTokens` and `load_auth_from_storage()` now live
in `_auth.tokens`, `_validate_required_cookies` is a direct
`_auth.cookie_policy` re-export, and `async def enumerate_accounts` is the
only remaining `auth.py` function body because it binds `_poke_session` as
the default dependency.

| Module | Responsibility |
|--------|----------------|
| [`_auth/tokens.py`](../src/notebooklm/_auth/tokens.py) | Token dataclass + storage-loading helpers. |
| [`_auth/paths.py`](../src/notebooklm/_auth/paths.py) | Storage paths and filesystem helpers. |
| [`_auth/storage.py`](../src/notebooklm/_auth/storage.py) | Profile/state persistence on disk. |
| [`_auth/extraction.py`](../src/notebooklm/_auth/extraction.py) | Cookie/token extraction from browser sessions. |
| [`_auth/headers.py`](../src/notebooklm/_auth/headers.py) | HTTP header construction. |
| [`_auth/cookies.py`](../src/notebooklm/_auth/cookies.py) | Cookie maps + `_update_cookie_input` helper. |
| [`_auth/cookie_policy.py`](../src/notebooklm/_auth/cookie_policy.py) | Domain allowlist and cookie policy decisions. |
| [`_auth/account.py`](../src/notebooklm/_auth/account.py) | Account profile + multi-account switching. |
| [`_auth/session.py`](../src/notebooklm/_auth/session.py) | `refresh_auth_session(auth=..., kernel=..., auth_coord=..., lifecycle=..., cookie_persistence=...)` implementation called by `AuthRefreshCoordinator`. Takes five explicit keyword-only collaborators instead of a Session-shaped owner Protocol; the previous `RefreshAuthCore` Protocol and the `update_auth_tokens` / `update_auth_headers` Session-level forwards have been removed. |
| [`_auth/refresh.py`](../src/notebooklm/_auth/refresh.py) | Token refresh driver (external login command, coalesced runs, secret redaction). |
| [`_auth/keepalive.py`](../src/notebooklm/_auth/keepalive.py) | Cookie keepalive + `__Secure-1PSIDTS` rotation. |
| [`_auth/psidts_recovery.py`](../src/notebooklm/_auth/psidts_recovery.py) | Inline PSIDTS recovery for cold-start (see issue #865). |

The cookie lifecycle — what gets written, who rotates, what the
keepalive contract is — is documented separately in
[`docs/auth-cookie-lifecycle.md`](./auth-cookie-lifecycle.md).

## CLI layer (ADR-008)

The CLI is intentionally a thin adapter over the public Python client.
It does not build raw batchexecute payloads, import the RPC layer, or
reach into private `notebooklm._*` implementation modules. Click
commands in
[`src/notebooklm/cli/*_cmd.py`](../src/notebooklm/cli) own argument
parsing, user-visible rendering, JSON envelopes, and exit codes;
workflow logic lives in
[`src/notebooklm/cli/services/`](../src/notebooklm/cli/services). This
separation is the [ADR-008](./adr/0008-cli-services-extraction-pattern.md)
extraction pattern.

The console-script entry point is
[`notebooklm_cli.py`](../src/notebooklm/notebooklm_cli.py). It declares
the root `notebooklm` Click group with
[`SectionedGroup`](../src/notebooklm/cli/grouped.py), owns process-wide
options (`--storage`, `--profile`, `--verbose`, `--quiet`),
canonicalizes the storage path into `ctx.obj`, stores the selected
profile/quiet values there, and registers the top-level commands plus
command groups. `SectionedGroup` is a presentation concern only: it
bins commands in help output, and
[`tests/unit/cli/test_grouped.py`](../tests/unit/cli/test_grouped.py)
rejects new unbinned commands.

A typical authenticated command follows this path:

```text
+----------------------------------------------------------------+
| notebooklm_cli.cli root group                                  |
|   - SectionedGroup                                             |
|   - process-wide options:                                      |
|     --storage / --profile / --verbose / --quiet                |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| cli/<domain>_cmd.py Click command                              |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| cli.auth_runtime.with_auth_and_errors(...)                     |
|   or run_client_workflow(...)                                  |
|   - handle_errors(...) wraps command-body failures             |
|   - AuthSource resolves precedence:                            |
|     --storage > NOTEBOOKLM_AUTH_JSON > active profile storage  |
|   - get_auth_tokens(...) builds AuthTokens                     |
|   - cli.runtime.run_async(...) -> one top-level asyncio.run    |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| async with NotebookLMClient(auth) as client                    |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| cli/services/<domain>.py plan/executor                         |
|   or direct public client call                                 |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| command module:                                                |
|   - renders text / JSON                                        |
|   - applies exit-code policy                                   |
+----------------------------------------------------------------+
```

| Layer | Owns | Does NOT own |
|-------|------|--------------|
| `notebooklm_cli.py` | Root Click group, global options, profile/storage setup, command registration | Per-command workflows, rendering of command results |
| `cli/*_cmd.py` | Click decorators, option parsing, stdout/stderr rendering, JSON output, exit codes | Business logic, RPC dispatch, retry loops |
| `cli/services/*.py` | Workflow orchestration, plan dataclasses, result types, retry/wait policy | Click context, `console.print`, `SystemExit` (target end-state; some modules are still mid-migration) |

Command modules are named `*_cmd.py` (e.g. `source_cmd.py`,
`notebook_cmd.py`) to avoid Python's package-attribute shadowing — the
historical short names (`source`, `notebook`, …) are re-exported from
`cli/__init__.py` so existing imports keep working. The shadowing
invariant is pinned by `tests/_lint/test_no_module_shadowing.py`.

CLI services are organised by feature family; notable examples include
`cli/services/login/` (browser-profile enumeration split across Chromium
and Firefox cookie jars), `cli/services/source_*` (URL/file/research
source flows), and `cli/services/artifact_generation.py`. The CLI
service-layer boundary is guarded by
[`tests/unit/cli/test_services_boundary.py`](../tests/unit/cli/test_services_boundary.py):
new service modules must either be fully cleaned of Click/rendering/exit
ownership or be added to the explicit transitional inventory with the
current violations and rationale.

The cross-command helpers form a small internal CLI stack:

| Module | Role |
|--------|------|
| [`cli/runtime.py`](../src/notebooklm/cli/runtime.py) | Leaf runtime helpers: root `--quiet` lookup and the single `asyncio.run(...)` bridge for sync Click handlers. |
| [`cli/auth_runtime.py`](../src/notebooklm/cli/auth_runtime.py) | Shared auth bootstrap, command-body error wrapping, and optional opened-client workflow helper. |
| [`cli/services/auth_source.py`](../src/notebooklm/cli/services/auth_source.py) | Single resolver for CLI auth-source precedence (`--storage`, `NOTEBOOKLM_AUTH_JSON`, active profile). |
| [`cli/context.py`](../src/notebooklm/cli/context.py) | Profile/storage-scoped `context.json` persistence for active notebook, conversation, and account metadata. |
| [`cli/resolve.py`](../src/notebooklm/cli/resolve.py) | Notebook/source/artifact/note ID resolution, including partial-ID matching against public client list calls. |
| [`cli/options.py`](../src/notebooklm/cli/options.py) + [`cli/completion.py`](../src/notebooklm/cli/completion.py) | Shared Click option decorators and best-effort shell completion. Completion providers may load auth and list public client resources, but swallow all failures so shells never print diagnostics during TAB completion. |
| [`cli/rendering.py`](../src/notebooklm/cli/rendering.py) | Rich/text/JSON rendering helpers. Status lines in JSON mode go to stderr so stdout remains parseable JSON. |
| [`cli/error_handler.py`](../src/notebooklm/cli/error_handler.py) | Canonical CLI error-to-exit mapping. Under `--json`, command-body failures use the typed error envelope from [ADR-015](./adr/0015-json-envelope-contract-for-post-parse-click-exceptions.md). Parse-time Click parser errors remain Click-owned. |
| [`cli/helpers.py`](../src/notebooklm/cli/helpers.py) | Backward-compatible facade for historical imports and test patch targets. New production code should import from the owning helper module instead. |

The boundary is enforced statically by
[`tests/unit/test_cli_boundary.py`](../tests/unit/test_cli_boundary.py):
CLI modules may import public `notebooklm` modules and their own
intra-CLI private helpers, but not `notebooklm._*`, `notebooklm.rpc.*`,
or private names from public modules. The same test keeps low-level
helpers (`runtime`, `context`, `resolve`, `rendering`, `auth_runtime`,
`options`) from growing upward dependencies on command modules or the
`cli.helpers` compatibility facade.

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
Authed POST leaf             (RuntimeTransport.terminal → Kernel → httpx)
```

## Client as composition root

`NotebookLMClient` is both the public surface and the composition root. It owns
`ClientComposed`, the collaborator bundle, the RPC executor, and the feature API
instances. `ClientLifecycle` owns open/close behavior (loop-affinity binding,
keepalive task, cookie persistence, and transport teardown);
`TransportDrainTracker` owns drain semantics.

Concretely, the client-owned runtime retains:

1. **Late-bound composition slots.** `ClientComposed.transport`, the chain
   metadata slots (`chain_builder` / `middlewares`), and
   `ClientComposed.executor` are bound exactly once by
   `compose_client_internals(...)` through write-once binders. Pre-binding
   access trips the `ClientComposed` guard. `ClientComposed` exposes
   `runtime_collaborators`, not a broad `collaborators` alias.
   [`tests/_lint/test_client_composition.py`](../tests/_lint/test_client_composition.py)
   guards against inlining holder state back onto `NotebookLMClient`.
2. **Middleware-chain seams.** The chain leaf
   (`_authed_post_chain_terminal`), the chain slot (`_authed_post_chain`),
   the dynamic refresh delegate (`await_refresh`), and the three
   retry-budget tunables (`_rate_limit_max_retries`,
   `_server_error_max_retries`, `_refresh_retry_delay`) live on
   `MiddlewareChainHost`. `wire_middleware_chain` and
   `build_runtime_transport` take that host directly and read its
   attributes live.
3. **Lifecycle methods.** Public client `__aenter__`, `__aexit__`,
   `close`, `drain`, and `is_connected` call `ClientLifecycle` and
   `TransportDrainTracker` directly.

`NotebookLMClient.rpc_call(method, params)` dispatches directly through
`self._rpc_executor.rpc_call(...)` — the `RpcExecutor` captured during
`NotebookLMClient.__init__` from `compose_client_internals(...)` and
shared with every feature API.

Feature APIs receive the collaborator they need (`RpcExecutor` for
`RpcCaller`, `ClientLifecycle` for `LoopGuard`, `TransportDrainTracker`
for `OperationScopeProvider` / `register_drain_hook`) per ADR-014 Rules
1 + 3. Features that need more than one capability — `ChatAPI`,
`ArtifactsAPI`, and `SourceUploadPipeline` — take each collaborator by
keyword-only constructor argument. The composition wiring is in
[`client.py`](../src/notebooklm/client.py).

## Testing patterns

Two policies define how tests interact with the architecture above.

### Constructor-injection fixtures (ADR-007)

The forbidden patterns are `monkeypatch.setattr("notebooklm.…")` against
module-level seams and direct attribute assignment like
`target.rpc_call = AsyncMock(...)`. The sanctioned substitute is
[`tests/_fixtures/fake_core.py:make_fake_core(...)`](../tests/_fixtures/fake_core.py),
which returns a `FakeSession` configured to satisfy the narrow
capability Protocols features consume (`RpcCaller`, `LoopGuard`,
`OperationScopeProvider`, `AuthMetadata`, `Kernel`). The name is
backward-compatible test vocabulary; it is not a production `Session`
replacement. Multi-capability features (`ChatAPI`, `ArtifactsAPI`,
`SourceUploadPipeline`) take their direct collaborators by keyword-only
constructor argument, so unit tests can inject narrow
`MagicMock(spec=RpcCaller, rpc_call=AsyncMock(...))`-style fakes
directly via those constructors.

The meta-lint at `tests/_lint/test_no_forbidden_monkeypatches.py`
enforces the policy; the file-level allowlist shrinks as legacy tests
migrate. See [ADR-007](./adr/0007-test-monkeypatch-policy.md).

### Test suite taxonomy

- **Unit tests** (`tests/unit/`): No network, decode/encode only.
- **Integration tests** (`tests/integration/`): Mock HTTP responses or
  use VCR cassettes scrubbed per
  [ADR-006](./adr/0006-vcr-scrubber-strategy.md).
- **E2E tests** (`tests/e2e/`): Real API; require auth; marked
  `@pytest.mark.e2e` and excluded from the default run.

Pin tests that lock architectural invariants (chain ordering, narrow
Protocol membership, no forbidden monkeypatch) live in `tests/unit/`
and `tests/_lint/` — changing the underlying invariant without updating
the pin is a bug.

A fuller taxonomy can be generated with
[`scripts/test_taxonomy_inventory.py`](../scripts/test_taxonomy_inventory.py).

## Implementation surface convention (ADR-012)

`notebooklm-py` keeps a small set of public-named modules (`artifacts.py`,
`auth.py`, `client.py`, `config.py`, `exceptions.py`, `io.py`, `log.py`,
`migration.py`, `notebooklm_cli.py`, `paths.py`, `research.py`,
`types.py`, `urls.py`, `utils.py`) and routes everything else through
underscore-prefixed seam modules. Anything underscored is *not* a
supported import surface; it can be moved, renamed, or deleted without a
deprecation cycle. See [ADR-012](./adr/0012-implementation-surface-convention.md).

The corollary for contributors: if you find yourself reaching into
`notebooklm._foo`, prefer a capability Protocol or a public function in
one of the named modules.

## Boundary moratorium

New architectural carve-outs are expensive: every ADR amendment and
`tests/_lint/` pin becomes load-bearing for contributors who have
to read the docs before touching the relevant seam. To keep that
surface from drifting upward without bound, the following discipline
applies to any future change that would *expand* the documented
boundary set:

- **Justify by failure mode.** A new ADR amendment or `tests/_lint/` pin
  must cite a concrete user-visible failure mode
  it prevents (loop-affinity break, auth-snapshot tear, transport drain
  regression, public-API breakage, etc.). "Future-proofing" or "in case
  someone refactors X" is not sufficient.
- **Prefer deletion over carve-out.** When a compatibility seam can be
  removed instead of documented, remove it. Carve-outs are the fallback
  when removal is genuinely infeasible, not the default.
- **One owner per rule.** A pin without a corresponding ADR clause (and
  vice versa) is a smell — it means the rule is enforced but not
  explained, or explained but not enforced.

The intent is architectural: shrink the boundary set whenever the
underlying code allows it, and resist growing it on speculative grounds.

## Glossary

Vocabulary that recurs in this document and the surrounding code.

| Term | Meaning |
|------|---------|
| `batchexecute` | Google's internal RPC protocol over HTTPS. The wire is positional lists keyed by an obfuscated method id; see [`rpc/types.py`](../src/notebooklm/rpc/types.py). |
| Capability Protocol | A narrow structural `Protocol` (e.g. `RpcCaller`, `LoopGuard`) a feature depends on instead of taking the deleted concrete `Session` class or a broad runtime facade. See [ADR-013](./adr/0013-composable-session-capabilities.md). |
| Chain / leaf / terminal | The middleware chain's ordering vocabulary. The chain wraps outermost-first; the **leaf** is the innermost middleware (`TracingMiddleware`); the **terminal** is the authed-POST function (`RuntimeTransport.terminal → Kernel.post`) that ends the chain. |
| Drain | Graceful-shutdown waiting on in-flight transport operations to complete. Owned by `TransportDrainTracker` and admitted by `DrainMiddleware`. |
| `idempotent_create(...)` | Caller-owned probe-then-create wrapper used by source-add / Drive-add flows. Distinct from the `IdempotencyRegistry` (which only classifies retry safety inside the executor). |
| `operation_variant` | Optional kwarg on `rpc_call(...)` that selects a method-variant-specific idempotency policy from the registry (e.g. `ADD_SOURCE` `"url"` vs `"drive"`). Unknown variants raise `IdempotencyVariantError`. |
| RPC method id | A short obfuscated identifier (`rpcids=`) Google uses to route batchexecute calls. Source of truth: `RPCMethod` enum in `rpc/types.py`. |
| Snapshot | An `AuthSnapshot` (see [`_request_types.py`](../src/notebooklm/_request_types.py)) — an immutable, point-in-time view of session id, CSRF token, authuser, and account email. Taken inside the auth-snapshot lock so a refresh racing with a transport build cannot tear. |

## ADR cross-references

- [ADR-001](./adr/0001-layered-core-seams-and-property-bridge-policy.md) — Layered seams + property-bridge policy (superseded; shims retired).
- [ADR-002](./adr/0002-capability-protocol-pattern.md) — Capability Protocol pattern (Superseded by ADR-013).
- [ADR-003](./adr/0003-auth-facade-write-through.md) — `auth.py` write-through facade (Superseded — closed by [ADR-014](./adr/0014-feature-local-runtime-adapters.md); `auth.py` is now almost pure re-exports with `enumerate_accounts` as the sole function-body exception).
- [ADR-004](./adr/0004-loop-affinity-contract.md) — Loop-affinity contract (Accepted; enforced by `_loop_affinity.assert_bound_loop`).
- [ADR-005](./adr/0005-idempotency-taxonomy.md) — Mutating-RPC idempotency taxonomy (Accepted; enforced by `_idempotency.IdempotencyRegistry`).
- [ADR-006](./adr/0006-vcr-scrubber-strategy.md) — VCR cassette scrubber strategy (Accepted).
- [ADR-007](./adr/0007-test-monkeypatch-policy.md) — Constructor-injection test pattern via `tests/_fixtures/` (Accepted; enforced by `tests/_lint/test_no_forbidden_monkeypatches.py`).
- [ADR-008](./adr/0008-cli-services-extraction-pattern.md) — `cli/services/` extraction pattern (Accepted).
- [ADR-009](./adr/0009-middleware-chain.md) — Middleware chain ordering (Accepted; load-bearing).
- [ADR-010](./adr/0010-session-kernel-split.md) — Session/Kernel split (Superseded by ADR-013).
- [ADR-011](./adr/0011-schema-validation-policy.md) — Schema validation policy (Accepted; `safe_index` is the canonical decode helper).
- [ADR-012](./adr/0012-implementation-surface-convention.md) — Implementation surface convention (Accepted; underscore-prefix = unsupported import surface).
- [ADR-013](./adr/0013-composable-session-capabilities.md) — Composable Session Capabilities (the post-v0.5.0 capability model).
- [ADR-014](./adr/0014-feature-local-runtime-adapters.md) — Feature-local runtime adapters (Accepted; features receive direct collaborators instead of `Session`).
- [ADR-015](./adr/0015-json-envelope-contract-for-post-parse-click-exceptions.md) — Typed JSON error envelope for post-parse CLI failures (Accepted).

## See also

- [`CLAUDE.md`](../CLAUDE.md) — high-level navigation map for AI agents working in this repo, including the full file index.
- [`docs/development.md`](./development.md) — how to add a new feature API.
- [`docs/refactor-history.md`](./refactor-history.md) — historical narrative of the multi-phase refactor + downstream migration tables.
- [`docs/python-api.md`](./python-api.md) — public Python API surface.
- [`docs/auth-cookie-lifecycle.md`](./auth-cookie-lifecycle.md) — cookie keepalive, rotation, and PSIDTS recovery.
- [`docs/rpc-development.md`](./rpc-development.md) — capturing and debugging new RPCs.
- [`docs/rpc-reference.md`](./rpc-reference.md) — RPC payload structures.
