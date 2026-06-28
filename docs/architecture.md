# Architecture

This document is the canonical map of `notebooklm-py`'s current runtime shape.
The historical refactor narrative (including the program that first established
this layering) lives in [`docs/refactor-history.md`](./refactor-history.md).

## Layered overview

```text
            three thin, transport-specific adapters
+----------------+  +----------------+  +----------------+
| CLI    (cli/)  |  | MCP    (mcp/)  |  | REST (server/) |
| Click commands |  | FastMCP tools  |  | FastAPI routes |
+----------------+  +----------------+  +----------------+
         \                  |                  /
          \                 |                 /
           +----------------+----------------+
                            ▼
+----------------------------------------------------------+
| Application Layer  (src/notebooklm/_app/*)               |
|   Transport-neutral business logic shared by all three   |
|   adapters: id validation/resolution, plan-building,     |
|   status projection, retry/wait orchestration,           |
|   errors.classify (the single failure-category source),  |
|   diagnostics. Imports no click / rich / fastmcp /       |
|   fastapi (boundary lint-enforced; ADR-0021).            |
+----------------------------------------------------------+
                            ▼
+----------------------------------------------------------+
| Client Layer (client.py + feature APIs)                  |
|   NotebookLMClient + namespaced sub-clients:             |
|     .notebooks  .sources  .artifacts  .chat              |
|     .notes      .mind_maps .research   .settings         |
|     .sharing    .labels                                  |
+----------------------------------------------------------+
                            ▼
+----------------------------------------------------------+
| Runtime Layer (client-owned collaborators)               |
|   ClientComposed + RpcExecutor, RuntimeTransport,        |
|   ClientLifecycle, Kernel.                               |
+----------------------------------------------------------+
                            ▼
+----------------------------------------------------------+
| RPC Layer (src/notebooklm/rpc/*)                         |
|   types.py    method IDs + enums (source of truth)       |
|   encoder.py  request encoding                           |
|   decoder.py  response parsing                           |
+----------------------------------------------------------+
```

Three thin **transport adapters** fan into that one shared core; everything below
`_app/` is then identical regardless of which adapter drove the call — there is
exactly one client runtime and one RPC stack:

| Adapter | Package | Transport | Console script | Install | Failures render as |
| --- | --- | --- | --- | --- | --- |
| **CLI** | `cli/` | terminal (Click) | `notebooklm` | base | exit codes + the byte-stable `--json` error envelope (ADR-0015) |
| **MCP** | `mcp/` | Model Context Protocol (FastMCP) | `notebooklm-mcp` | `mcp` extra · experimental | MCP tool error content (`CODE: message`) |
| **REST** | `server/` | HTTP (FastAPI) | `notebooklm-server` | `server` extra · experimental | HTTP status + `{"error": {"category": "...", "message": "..."}}` |

### Transport-neutral application layer (`_app/`)

The CLI, the MCP server (`mcp/`), and the REST server (`server/`) are each thin
adapters over `src/notebooklm/_app/` — transport-neutral business logic (id
validation/resolution, plan-building, status projection, retry/wait
orchestration, error classification, diagnostics) shared by all three
front-ends. Each adapter parses its transport's inputs into typed
`Request`/`Plan`/`Result` dataclasses, calls the neutral core (which receives the
live client), and renders the typed result into its own envelope vocabulary;
simple reads/mutations call the `client.*` namespaces directly, while multi-step
flows go through the `_app/` cores. The package imports no transport framework —
`click` / `rich` / `fastmcp` / `fastapi`, nor the `cli` / `server` / `rpc`
sibling packages — with the boundary lint-enforced
(`tests/_guardrails/test_app_boundary.py`). It raises only the public
`notebooklm.exceptions` hierarchy, with `_app.errors.classify` as the single
neutral source of the failure-category decision each adapter projects onto its
own codes (CLI exit codes, MCP error shapes, REST HTTP statuses). See ADR-0021.
The per-module index and the full tree are in [File map](#file-map) below.

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
| CLI command / MCP tool / REST route / library call             |
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
| ADR-0009 middleware chain                                       |
|   Drain -> Metrics -> Sema -> Retry -> AuthRefresh             |
|   -> ErrInj -> Tracing                                         |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| MiddlewareChainHost._authed_post_chain_terminal(...)           |
|   chain leaf — ADR-0014 Rule 4                                  |
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
`RpcCaller` per ADR-0014 Rule 1; `NotebookLMClient.rpc_call` dispatches
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
| ADR-0009 middleware chain                                       |
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
| Artifact generation | `ArtifactsAPI` delegates the `generate_*` / `revise_slide` / `retry_failed` kickoff paths to `ArtifactGenerationService` (`_artifact/generation.py`), which builds `CREATE_ARTIFACT` params (via the `_artifact/payloads.py` builders) and uses the normal `rpc_call` path; the facade keeps thin signature-preserving delegators. `ArtifactPollingService` owns leader/follower polling with `operation_scope(...)` and a feature-local `PollRegistry`; `ArtifactsAPI` registers a close-time drain hook for poll cleanup. |
| Artifact download | `ArtifactDownloadService` lists/selects artifacts through `RpcCaller`, but media downloads use a separate streaming `httpx.AsyncClient` with storage cookies, trusted-host checks, and a producer/writer split. They do not go through `RpcExecutor` or `Kernel.post`. |
| Notes and mind maps | `NoteService` owns note-row CRUD/classification through `RpcCaller`. `NoteBackedMindMapService` adapts those note rows for artifact-facing mind-map behavior so notes and artifacts do not import each other. |

## Cross-cutting policies

Three policies thread through the layers above and are easy to violate by
accident. Each is pinned by an ADR.

### Loop affinity (ADR-0004)

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

See [ADR-0004](./adr/0004-loop-affinity-contract.md) and the consumer
notes in [`docs/python-api.md`](./python-api.md#concurrency-contract).

### Idempotency (ADR-0005)

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

See [ADR-0005](./adr/0005-idempotency-taxonomy.md). Side-effect probing
(`idempotent_create(...)`) is a separate mechanism not owned by the
registry; see the upload/source-add row in the "Uploads, downloads, and
polling" table above.

### Schema validation (ADR-0011)

Batchexecute responses are undocumented and Google reshapes them without
notice. Decoders walk nested positional lists; a single index shift
either crashes with raw `IndexError` from inside a feature module or
silently degrades.

The single helper that decoders use to navigate row shapes is
`notebooklm.rpc.safe_index` in
[`rpc/_safe_index.py`](../src/notebooklm/rpc/_safe_index.py). It always
raises a typed shape-drift error: strict decoding is the only mode (the
legacy soft-mode opt-out was retired in v0.7.0). The
`RpcExecutor` decode path narrowly wraps
`json.JSONDecodeError`, `KeyError`, `IndexError`, and `TypeError` into
`RPCError`; other exception types (e.g. `AttributeError`) intentionally
propagate as code bugs rather than being conflated with shape drift.

See [ADR-0011](./adr/0011-schema-validation-policy.md).

## Per-capability protocol model

ADR-0013 ("Composable Session Capabilities") is the design rationale:
feature APIs depend on narrow capability Protocols rather than on the
deleted concrete `Session` class.
[ADR-0014](./adr/0014-feature-local-runtime-adapters.md) extends that
intent at runtime: each feature receives the specific collaborator it
needs, never a broad runtime facade. `NotebookLMClient.__init__` is the
composition root that wires each feature with the satisfier it needs.

Three shared Protocols live in
[`_runtime/contracts.py`](../src/notebooklm/_runtime/contracts.py):
`Kernel`, `RpcCaller`, and `LoopGuard`. `RpcCaller` and `LoopGuard`
meet ADR-0013's "shared by at least two features" promotion bar.
`Kernel` remains shared because it is the typed transport surface
implemented by the concrete client-owned kernel and consumed by the
upload pipeline. Single-consumer capabilities stay beside their owner:
`AuthMetadata` lives in `_source/upload.py`, and
`OperationScopeProvider` lives in `_artifact/polling.py`. The unused
`AsyncWorkRuntime` composite and the feature-local composite runtime
Protocols (`ChatRuntime`, `ArtifactsRuntime`, `UploadRuntime`) were
deleted once they no longer represented independently varying
production dependencies.

**Module-level Protocols** (defined in
[`_runtime/contracts.py`](../src/notebooklm/_runtime/contracts.py)):

| Protocol | Responsibility |
|----------|----------------|
| `RpcCaller` | Exposes `rpc_call(method, params, ...)` — the chokepoint every feature API uses for batchexecute calls. |
| `LoopGuard` | Exposes `assert_bound_loop()` — single-method cross-loop affinity check; consumed by anything that may touch the HTTP client. |
| `Kernel` | Pure transport surface — `post()` method, `cookies` property, `aclose()`. Single consumer today: `SourceUploadPipeline`. |

**Feature-module-local Protocols.** Single-consumer capability shapes live
next to their owner (`AuthMetadata` in `_source/upload.py`,
`OperationScopeProvider` in `_artifact/polling.py`). No feature-local
composite-runtime unions or adapter dataclasses exist in production. Every
multi-capability feature takes its collaborators by keyword-only
constructor argument:

- `ArtifactsAPI` and `SourceUploadPipeline` take `rpc: RpcCaller`,
  `drain: TransportDrainTracker`, `lifecycle: ClientLifecycle`.
- `ChatAPI` takes `rpc: RpcCaller`, `transport: RuntimeTransport`,
  `reqid: ReqidCounter`, `loop_guard: LoopGuard`.

Production satisfies shared Protocols via the underlying collaborators
(ADR-0014 Rule 1: `RpcExecutor` satisfies `RpcCaller`,
`ClientLifecycle` satisfies `LoopGuard`, and the concrete `Kernel`
satisfies the `Kernel` Protocol). There is no production `Session`
class in the runtime graph.
Tests substitute
[`tests/_fixtures/fake_core.py:FakeSession`](../tests/_fixtures/fake_core.py)
(constructed via `make_fake_core(...)`) — the sanctioned ADR-0007 / ADR-0013
fixture pattern. `FakeSession` is a backward-compatible test-fixture name,
not a production runtime class. Tests that inject narrow fakes into a
single feature (e.g. `MagicMock(spec=RpcCaller,
rpc_call=AsyncMock(...))`) construct the feature directly under ADR-0014.

### Executor takes its collaborators directly

Per ADR-0014 Rule 5, `RpcExecutor` takes its kernel, transport,
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
tunables (ADR-0014 Rule 4 chain-ownership carve-out). Request types,
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
      → ADR-0009 chain → RuntimeTransport.terminal → Kernel.post → httpx
```

| Collaborator | Module | Responsibility |
|--------------|--------|----------------|
| `NotebookLMClient` | [`client.py`](../src/notebooklm/client.py) | Public surface and composition root. Owns `_auth`, `_seams`, `_composed`, `_collaborators`, `_rpc_executor`, and the ten feature API attributes (`notebooks`, `sources`, `artifacts`, `chat`, `notes`, `mind_maps`, `research`, `settings`, `sharing`, `labels`). `__aenter__`, `close`, `drain`, `is_connected`, `metrics_snapshot`, and `rpc_call` route directly to the owning collaborator. |
| `ClientSeams` | [`_client_seams.py`](../src/notebooklm/_client_seams.py) | Mutable holder for runtime callables that closures re-read after construction: `decode_response`, `sleep`, and `is_auth_error`. Construction-only seams such as `async_client_factory` stay on `compose_client_internals(...)` and the client-shell test helper, not on the public constructor. |
| `ClientComposed` | [`_client_composed.py`](../src/notebooklm/_client_composed.py) | Write-once holder for composition state: `transport`, `executor`, `chain_host`, `chain_builder`, `middlewares`, lazy RPC semaphore, and `runtime_collaborators`. Pre-binding access raises a clear `RuntimeError`; the holder deliberately does not expose a broad `.collaborators` alias. |
| `RpcExecutor` | [`_rpc_executor.py`](../src/notebooklm/_rpc_executor.py) | Single logical batchexecute RPC dispatch path. Owns request-id/started-metric bracketing, idempotency policy lookup, method-ID resolution, request encoding, response decode, RPC error mapping, and decode-time auth refresh retry. Takes its `Kernel`, `RuntimeTransport`, `AuthRefreshCoordinator`, and `ClientMetrics` collaborators directly via keyword-only constructor parameters (ADR-0014 Rule 5). Enters transport through `RuntimeTransport.perform_authed_post`. |
| `RuntimeTransport` | [`_runtime/transport.py`](../src/notebooklm/_runtime/transport.py) | Authed POST collaborator. Owns `perform_authed_post()` (loop guard, auth snapshot, request materialization, chain dispatch, queue-wait recording), `refresh_request_for_current_auth()`, and `terminal()` (freshness rebuild + `Kernel.post`). Called directly by `RpcExecutor` and by `chat_aware_authed_post` (ChatAPI's chat-flavoured transport call); the middleware chain leaf at `MiddlewareChainHost._authed_post_chain_terminal` continues to dispatch through `RuntimeTransport.terminal` per ADR-0014 Rule 4. |
| `MiddlewareChainHost` | [`_middleware/chain_host.py`](../src/notebooklm/_middleware/chain_host.py) | Owns the wired middleware chain (`_authed_post_chain`), the chain leaf (`_authed_post_chain_terminal`), the three retry-budget tunables (`_rate_limit_max_retries`, `_server_error_max_retries`, `_refresh_retry_delay`), and the dynamic `await_refresh` delegate that the auth-refresh middleware captures. The chain's provider lambdas and the transport's `chain_provider` closure read the host's attributes live, so post-construction mutation (e.g. tests setting `client._composed.chain_host._rate_limit_max_retries = 0`) still steers the live chain. |
| `AuthRefreshCoordinator` | [`_runtime/auth.py`](../src/notebooklm/_runtime/auth.py) | Owns the auth-snapshot lock and refresh task. Canonical implementation for `AuthRefreshCoordinator.snapshot(auth=...)`, `update_auth_tokens(auth=..., csrf=..., session_id=...)`, and `update_auth_headers(auth=..., kernel=...)`; callers pass explicit collaborators rather than a host object. |
| `ClientLifecycle` | [`_runtime/lifecycle.py`](../src/notebooklm/_runtime/lifecycle.py) | HTTP-client open/close, keepalive task, cookie save coordination. Holds `_timeout`, `_bound_loop`, `_http_client`, `_keepalive_*`. |
| `MiddlewareChainBuilder` | [`_middleware/chain.py`](../src/notebooklm/_middleware/chain.py) | Constructs the middleware chain in the canonical ADR-0009 order. |
| `TransportDrainTracker` | [`_transport_drain.py`](../src/notebooklm/_transport_drain.py) | Tracks in-flight transport operations + the drain condition variable. Gates graceful shutdown. |
| `ClientMetrics` | [`_client_metrics.py`](../src/notebooklm/_client_metrics.py) | Per-instance counters (`ClientMetricsSnapshot`) + the `on_rpc_event` user callback. |
| `ReqidCounter` | [`_reqid_counter.py`](../src/notebooklm/_reqid_counter.py) | Monotonic `_reqid` for the chat backend; lock-protected `next_reqid(...)`. |
| `CookiePersistence` | [`_cookie_persistence.py`](../src/notebooklm/_cookie_persistence.py) | Cookie-jar persistence + `__Secure-1PSIDTS` rotation. |
| `IdempotencyRegistry` | [`_idempotency.py`](../src/notebooklm/_idempotency.py) | Policy/classification registry keyed by `(RPCMethod, operation_variant)`. The production registry explicitly covers every active `RPCMethod`; `UNCLASSIFIED` is retained only as a placeholder for hand-built test/future registries. `RpcExecutor._execute_once()` consults it to resolve `effective_disable_internal_retries`. It is part of the RPC dispatch path, not lifecycle state. Side-effect probing (`idempotent_create(...)`) is a separate mechanism not owned by this registry. |
| `_request_types` | [`_request_types.py`](../src/notebooklm/_request_types.py) | Owns `AuthSnapshot`, `BuildRequest`, and request materialization shapes shared by RPC, chat, auth refresh, and the chain terminal. |
| `_transport_errors` | [`_transport_errors.py`](../src/notebooklm/_transport_errors.py) | Owns transport-level exceptions, `Retry-After` parsing, and raw `Kernel.post` error mapping consumed by `RetryMiddleware` and `AuthRefreshMiddleware`. |
| `_streaming_post` | [`_streaming_post.py`](../src/notebooklm/_streaming_post.py) | Low-level streaming POST helper with the response-size cap used by `Kernel.post`. |
| `Kernel` | [`_kernel.py`](../src/notebooklm/_kernel.py) | Pure transport core. Owns the `httpx.AsyncClient` and cookie jar; exposes `post()`, the `cookies` property, and `aclose()` (the close path wraps it in `asyncio.shield` from `ClientLifecycle.close()`). Concrete class behind the `Kernel` Protocol in `_runtime/contracts.py`; constructed by `build_collaborators(...)` and called from the middleware leaf via `RuntimeTransport.terminal → Kernel.post`. |
| `_runtime/init` | [`_runtime/init.py`](../src/notebooklm/_runtime/init.py) | Construction-time helpers for `NotebookLMClient`: `validate_constructor_args` (kwarg validation/normalization), `build_collaborators` (the seven collaborators in dependency order: `metrics`, `drain_tracker`, `reqid`, `auth_coord`, `kernel`, `lifecycle`, `cookie_persistence`), `build_runtime_transport`, `wire_middleware_chain`, and `compose_client_internals`. It binds the runtime graph into `ClientComposed` and returns `ClientInternals(collaborators, executor)`. |
| `_loop_affinity` | [`_loop_affinity.py`](../src/notebooklm/_loop_affinity.py) | Tiny free-function `assert_bound_loop(bound_loop)` shared by every helper that captures a loop reference at `open()` time (`TransportDrainTracker`, `ReqidCounter`, `AuthRefreshCoordinator`, `ArtifactPollingService`, `ChatAPI`). Enforces ADR-0004 without coupling those helpers to the public client. |

### Shipped runtime invariants

[ADR-0016](./adr/0016-auth-identity-and-core-logger-compatibility.md)
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
| `ArtifactDownloadService` | [`_artifact/downloads.py`](../src/notebooklm/_artifact/downloads.py) | Asynchronous download coordinator for finished artifacts. |
| `ArtifactGenerationService` | [`_artifact/generation.py`](../src/notebooklm/_artifact/generation.py) | Generation kickoff service (`generate_*`, `revise_slide`, `retry_failed`) extracted from `ArtifactsAPI`. |
| `_artifact_formatters` | [`_artifact/formatters.py`](../src/notebooklm/_artifact/formatters.py) | Markdown, HTML, and plain text formatters for artifacts. |
| `_artifact/listing` | [`_artifact/listing.py`](../src/notebooklm/_artifact/listing.py) | Listing and filtering operations for notebook artifacts. |
| `_row_adapters*` | [`_row_adapters/artifacts.py`](../src/notebooklm/_row_adapters/artifacts.py), [`_row_adapters/chat.py`](../src/notebooklm/_row_adapters/chat.py), [`_row_adapters/labels.py`](../src/notebooklm/_row_adapters/labels.py), [`_row_adapters/notebooks.py`](../src/notebooklm/_row_adapters/notebooks.py), [`_row_adapters/notes.py`](../src/notebooklm/_row_adapters/notes.py), [`_row_adapters/research.py`](../src/notebooklm/_row_adapters/research.py), [`_row_adapters/sources.py`](../src/notebooklm/_row_adapters/sources.py) | Wire-shape adapters that wrap raw batchexecute rows (`ArtifactRow`, `LabelRow`, `NoteRow`, `SourceRow`, the `POLL_RESEARCH` rows, the `SUGGEST_PROMPTS` suggestion rows) and the streamed-chat rows (`AnswerRow`/`CitationRow`/…) behind named accessors so downloads, polling, listing, labels, research, and the chat parser don't open-code positional indices. Strict decode behavior is pinned in `tests/unit/test_row_adapters.py`, `tests/unit/test_chat_row_adapter.py`, `tests/unit/test_notebooks_row_adapter.py`, and `tests/unit/test_research_row_adapter.py`. |
| `_research_task_parser` | [`_research_task_parser.py`](../src/notebooklm/_research_task_parser.py) | Parses deep-research task results from raw rows. Returns dict-shaped output today; a typed-model migration is not yet complete. |
| `_types/` | [`_types/`](../src/notebooklm/_types) | Private package holding the dataclass and `Protocol` implementations behind the public `types.py` / per-feature public schemas. Split per domain (`artifacts.py`, `chat.py`, `labels.py`, `mind_maps.py`, `notebooks.py`, `notes.py`, `research.py`, `sharing.py`, `sources.py`, plus `common.py` for shared shapes like `ConnectionLimits`). |

## Authentication subpackage

[`auth.py`](../src/notebooklm/auth.py) is a thin public facade that
re-exports the canonical implementations under
[`_auth/`](../src/notebooklm/_auth). ADR-0014 closed ADR-0003's deferred
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
| [`_auth/cookie_policy.py`](../src/notebooklm/_auth/cookie_policy.py) | Domain allowlist, cookie-domain builder (`build_cookie_domain_allowlist`), and cookie policy decisions. |
| [`_auth/browser_capture.py`](../src/notebooklm/_auth/browser_capture.py) | Transport-neutral browser launch→navigate→capture→filter→persist core (lazy `playwright`); shared by the interactive CLI login adapter and the layer-3 headless re-auth layer (ADR-0021). The headless arm classifies the landing URL (authenticated→capture, redirected-to-login→`HeadlessLoginRequiredError`). `run_cdp_capture` is an alternative credential source: attach to an operator-pointed already-running Chrome over CDP (`connect_over_cdp`, disconnect-only teardown) using the SAME landing classification + cookie-domain allowlist. |
| [`_auth/headless_reauth.py`](../src/notebooklm/_auth/headless_reauth.py) | Layer-3 (deepest) auth recovery: when first-party cookies are dead, drive a headless browser against the persistent profile to silently re-mint cookies. Typed honest outcomes (`HeadlessReauthStatus` UNAVAILABLE/FAILED/SUCCESS — never silent `None`). Opt-in only (`refresh_auth(allow_headless=True)` or `NOTEBOOKLM_HEADLESS_REAUTH=1`); local-unattended-only, never the remote/MCP auth path. Alternative credential source: `NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL` (or `attempt_headless_reauth(cdp_url=...)`) attaches to an operator-pointed running Chrome instead of the dedicated profile (freshness mitigation). Also exposes `headless_reauth_readiness()` — a credential-free, browser-free probe (profile present + playwright installed) surfaced by `doctor`. |
| [`_auth/account.py`](../src/notebooklm/_auth/account.py) | Account profile + multi-account switching. |
| [`_auth/session.py`](../src/notebooklm/_auth/session.py) | `refresh_auth_session(auth=..., kernel=..., auth_coord=..., lifecycle=..., cookie_persistence=...)` implementation called by `AuthRefreshCoordinator`. Takes five explicit keyword-only collaborators instead of a Session-shaped owner Protocol; the previous `RefreshAuthCore` Protocol and the `update_auth_tokens` / `update_auth_headers` Session-level forwards have been removed. |
| [`_auth/refresh.py`](../src/notebooklm/_auth/refresh.py) | Token refresh driver (external login command, coalesced runs, secret redaction). |
| [`_auth/keepalive.py`](../src/notebooklm/_auth/keepalive.py) | Cookie keepalive + `__Secure-1PSIDTS` rotation. |
| [`_auth/psidts_recovery.py`](../src/notebooklm/_auth/psidts_recovery.py) | Inline PSIDTS recovery for cold-start (see issue #865). |
| [`_auth/master_token.py`](../src/notebooklm/_auth/master_token.py) | Headless master-token auth: mint/persist web cookies from a durable Google master token; layer-4 re-mint recovery (ADR-0023). |

The cookie lifecycle — what gets written, who rotates, what the
keepalive contract is — is documented separately in
[`docs/auth-cookie-lifecycle.md`](./auth-cookie-lifecycle.md).

## CLI layer (ADR-0008)

The CLI is intentionally a thin adapter over the public Python client.
It does not build raw batchexecute payloads, import the RPC layer, or
reach into private `notebooklm._*` implementation modules. Click
commands in
[`src/notebooklm/cli/*_cmd.py`](../src/notebooklm/cli) own argument
parsing, user-visible rendering, JSON envelopes, and exit codes;
workflow logic lives in
[`src/notebooklm/cli/services/`](../src/notebooklm/cli/services). This
separation is the [ADR-0008](./adr/0008-cli-services-extraction-pattern.md)
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
invariant is pinned by `tests/_guardrails/test_no_module_shadowing.py`.

CLI services are organised by feature family; notable examples include
`cli/services/login/` (browser-profile enumeration split across Chromium
and Firefox cookie jars), `cli/services/source_*` (URL/file/research
source flows), and `cli/services/generate.py`. The CLI
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
| [`cli/master_token_login.py`](../src/notebooklm/cli/master_token_login.py) | Command driver for `notebooklm login --master-token[-refresh]`, rendering over the master-token login service [`cli/services/login/master_token.py`](../src/notebooklm/cli/services/login/master_token.py) (mint/persist/refresh + browser `oauth_token` capture; ADR-0023). |
| [`cli/services/auth_source.py`](../src/notebooklm/cli/services/auth_source.py) | Single resolver for CLI auth-source precedence (`--storage`, `NOTEBOOKLM_AUTH_JSON`, active profile). |
| [`cli/context.py`](../src/notebooklm/cli/context.py) | Profile/storage-scoped `context.json` persistence for active notebook, conversation, and account metadata. |
| [`cli/resolve.py`](../src/notebooklm/cli/resolve.py) | Notebook/source/artifact/note ID resolution, including partial-ID matching against public client list calls. |
| [`cli/options.py`](../src/notebooklm/cli/options.py) + [`cli/completion.py`](../src/notebooklm/cli/completion.py) | Shared Click option decorators and best-effort shell completion. Completion providers may load auth and list public client resources, but swallow all failures so shells never print diagnostics during TAB completion. |
| [`cli/rendering.py`](../src/notebooklm/cli/rendering.py) | Rich/text/JSON rendering helpers. Status lines in JSON mode go to stderr so stdout remains parseable JSON. |
| [`cli/error_handler.py`](../src/notebooklm/cli/error_handler.py) | Canonical CLI error-to-exit mapping. Under `--json`, command-body failures use the typed error envelope from [ADR-0015](./adr/0015-json-envelope-contract-for-post-parse-click-exceptions.md). Parse-time Click parser errors remain Click-owned. |
| [`cli/helpers.py`](../src/notebooklm/cli/helpers.py) | Backward-compatible facade for historical imports and test patch targets. New production code should import from the owning helper module instead. |

The boundary is enforced statically by
[`tests/_guardrails/test_cli_boundary.py`](../tests/_guardrails/test_cli_boundary.py):
CLI modules may import public `notebooklm` modules and their own
intra-CLI private helpers, but not `notebooklm._*`, `notebooklm.rpc.*`,
or private names from public modules. **Two sanctioned exceptions** to the
`notebooklm._*` rule are whitelisted in that gate: `notebooklm._app`
(the transport-neutral business-logic layer every adapter consumes) and the
single module `notebooklm._auth.browser_capture` (the transport-neutral
browser launch→capture→filter→persist core that the Playwright login adapter
[`cli/services/playwright_login.py`](../src/notebooklm/cli/services/playwright_login.py)
sits over, per ADR-0021 — interactive presentation stays in `cli/` while the
neutral core moves down to `_auth`, reachable by the client runtime and the
future headless re-auth layer). No other `_auth.*` module may be imported by
the CLI — the rest stays behind the `auth.py` facade. The same test keeps
low-level helpers (`runtime`, `context`, `resolve`, `rendering`,
`auth_runtime`, `options`) from growing upward dependencies on command modules
or the `cli.helpers` compatibility facade.

## MCP adapter (`mcp/`)

The MCP server is a second thin adapter beside `cli/`, opt-in behind the `mcp`
extra and **experimental** (preview). `create_server()` builds a FastMCP server
that exposes the `_app/` cores as MCP tools driving a single long-lived
`NotebookLMClient`; run it with the `notebooklm-mcp` console script (stdio or
loopback HTTP). It imports no `click` / `rich` / `cli` — like the CLI, it is built
on the `_app/` cores only (enforced by `tests/_guardrails/test_mcp_boundary.py`).
Failures surface as `CODE: message` strings projected from `_app.errors.classify`,
and mutating tools are confirmation-gated (they return a `needs_confirmation`
preview unless called with `confirm=true`). `notebooklm mcp install <client>`
wires it into Claude Desktop/Code, Cursor, or Windsurf, and `desktop-extension/`
packages a one-click `.mcpb` bundle. Full guide:
[`docs/mcp-guide.md`](./mcp-guide.md).

## REST server (`server/`)

The single-tenant REST server is the third adapter (ADR-0021), opt-in behind the
`server` extra and **experimental**. A FastAPI app maps `/v1` routes onto the
`_app/` cores and the public client namespaces, with one `NotebookLMClient` opened
once at the ASGI lifespan inside the server loop (honoring the ADR-0004 loop-
affinity contract). Every `/v1` request requires a static bearer token
(constant-time compare) plus a loopback `Host` literal (a DNS-rebinding guard);
`/healthz` is the one public route, and the `/docs` / `/openapi.json` schema
surface is disabled. Long-running work (source ingest, artifact generation) uses
the **poll-the-resource** model — the create call returns immediately and the
matching `GET` reports `pending` / `200` / `404` / `409` / `410`. Failures project
from `_app.errors.classify` onto an HTTP status plus the
`{"error": {"category": "...", "message": "..."}}` envelope. It imports no `click` / `rich` /
`cli` (enforced by `tests/_guardrails/test_server_boundary.py`). Launch and
configuration: [`docs/installation.md`](./installation.md#rest-api-server).

## Middleware chain (ADR-0009)

The runtime chain order is pinned by
[`tests/unit/test_chain_wiring.py`](../tests/unit/test_chain_wiring.py)
(facade-level) and
[`tests/unit/test_middleware_chain_builder.py`](../tests/unit/test_middleware_chain_builder.py)
(builder-level). The order is load-bearing: changing it without
simultaneously updating the pin tests
(`test_chain_seeded_with_final_adr_009_ordering`) is a bug.

The chain list in [`MiddlewareChainBuilder.build()`](../src/notebooklm/_middleware/chain.py)
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
   [`tests/_guardrails/test_client_composition.py`](../tests/_guardrails/test_client_composition.py)
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
the shared `_client_assembly.py::_assemble_client(...)` construction path
from `compose_client_internals(...)` and shared with every feature API.

Feature APIs receive the collaborator they need (`RpcExecutor` for
`RpcCaller`, `ClientLifecycle` for `LoopGuard`, the concrete `Kernel`
for upload cookies/posting, and `TransportDrainTracker` for local
operation scopes / close hooks) per ADR-0014 Rules 1 + 3. Features that
need more than one capability — `ChatAPI`, `ArtifactsAPI`, and
`SourceUploadPipeline` — take each collaborator by keyword-only
constructor argument. The composition wiring is centralized in
[`_client_assembly.py`](../src/notebooklm/_client_assembly.py), which is
called by both `NotebookLMClient.__init__` and the canonical test
factory.

## Testing patterns

Two policies define how tests interact with the architecture above.

### Constructor-injection fixtures (ADR-0007)

The forbidden patterns are `monkeypatch.setattr("notebooklm.…")` against
module-level seams and direct attribute assignment like
`target.rpc_call = AsyncMock(...)`. The sanctioned substitute is
[`tests/_fixtures/fake_core.py:make_fake_core(...)`](../tests/_fixtures/fake_core.py),
which returns a `FakeSession` configured to satisfy the narrow
shared protocols plus the upload/polling local protocols used by legacy
feature tests. The name is backward-compatible test vocabulary; it is
not a production `Session` replacement. Multi-capability features
(`ChatAPI`, `ArtifactsAPI`, `SourceUploadPipeline`) take their direct
collaborators by keyword-only constructor argument, so unit tests can inject narrow
`MagicMock(spec=RpcCaller, rpc_call=AsyncMock(...))`-style fakes
directly via those constructors.

The meta-lint at `tests/_guardrails/test_no_forbidden_monkeypatches.py`
enforces the policy; the file-level allowlist shrinks as legacy tests
migrate. See [ADR-0007](./adr/0007-test-monkeypatch-policy.md).

### Test suite taxonomy

- **Unit tests** (`tests/unit/`): No network, decode/encode only.
- **Integration tests** (`tests/integration/`): Mock HTTP responses or
  use VCR cassettes scrubbed per
  [ADR-0006](./adr/0006-vcr-scrubber-strategy.md).
- **E2E tests** (`tests/e2e/`): Real API; require auth; marked
  `@pytest.mark.e2e` and excluded from the default run.

Pin tests that lock architectural invariants (chain ordering, narrow
Protocol membership, no forbidden monkeypatch) live in `tests/unit/`
and `tests/_guardrails/` — changing the underlying invariant without updating
the pin is a bug.

A fuller taxonomy can be generated with
[`scripts/test_taxonomy_inventory.py`](../scripts/test_taxonomy_inventory.py).

## Implementation surface convention (ADR-0012)

`notebooklm-py` keeps a small set of public-named modules (`artifacts.py`,
`auth.py`, `client.py`, `config.py`, `exceptions.py`, `io.py`, `log.py`,
`migration.py`, `notebooklm_cli.py`, `paths.py`, `research.py`,
`types.py`, `urls.py`, `utils.py`) and routes everything else through
underscore-prefixed seam modules. Anything underscored is *not* a
supported import surface; it can be moved, renamed, or deleted without a
deprecation cycle. See [ADR-0012](./adr/0012-implementation-surface-convention.md).

The corollary for contributors: if you find yourself reaching into
`notebooklm._foo`, prefer a capability Protocol or a public function in
one of the named modules.

## Boundary moratorium

New architectural carve-outs are expensive: every ADR amendment and
`tests/_guardrails/` pin becomes load-bearing for contributors who have
to read the docs before touching the relevant seam. To keep that
surface from drifting upward without bound, the following discipline
applies to any future change that would *expand* the documented
boundary set:

- **Justify by failure mode.** A new ADR amendment or `tests/_guardrails/` pin
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
| Capability Protocol | A narrow structural `Protocol` (e.g. `RpcCaller`, `LoopGuard`) a feature depends on instead of taking the deleted concrete `Session` class or a broad runtime facade. See [ADR-0013](./adr/0013-composable-session-capabilities.md). |
| Chain / leaf / terminal | The middleware chain's ordering vocabulary. The chain wraps outermost-first; the **leaf** is the innermost middleware (`TracingMiddleware`); the **terminal** is the authed-POST function (`RuntimeTransport.terminal → Kernel.post`) that ends the chain. |
| Drain | Graceful-shutdown waiting on in-flight transport operations to complete. Owned by `TransportDrainTracker` and admitted by `DrainMiddleware`. |
| `idempotent_create(...)` | Caller-owned probe-then-create wrapper used by source-add / Drive-add flows. Distinct from the `IdempotencyRegistry` (which only classifies retry safety inside the executor). |
| `operation_variant` | Optional kwarg on `rpc_call(...)` that selects a method-variant-specific idempotency policy from the registry (e.g. `ADD_SOURCE` `"url"` vs `"drive"`). Unknown variants raise `IdempotencyVariantError`. |
| RPC method id | A short obfuscated identifier (`rpcids=`) Google uses to route batchexecute calls. Source of truth: `RPCMethod` enum in `rpc/types.py`. |
| Snapshot | An `AuthSnapshot` (see [`_request_types.py`](../src/notebooklm/_request_types.py)) — an immutable, point-in-time view of session id, CSRF token, authuser, and account email. Taken inside the auth-snapshot lock so a refresh racing with a transport build cannot tear. |

## File map

Per-file index plus the full `src/notebooklm` + `tests` repository tree. The tree is the hand-maintained module map that [`scripts/check_claude_md_freshness.py`](../scripts/check_claude_md_freshness.py) checks in both directions (documented paths exist; every module is documented or intentionally omitted).

### Key Files

| File | Purpose |
|------|---------|
| `client.py` | Main `NotebookLMClient` class |
| `_client_assembly.py` | Single private assembly seam (`_assemble_client`) that wires every constructor-set attribute; shared by `NotebookLMClient.__init__` and the canonical test factory (`tests/_helpers/client_factory.py`) so the two construction paths cannot drift. |
| `_client_composed.py` | Client-owned composition holder for transport, executor, chain host, middleware metadata, and runtime collaborator bundle. |
| `_client_seams.py` | Constructor-only injectable seams used by tests and collaborator construction. |
| `_runtime/init.py` | Constructor helpers that validate client runtime kwargs, build collaborators (returning a `RuntimeCollaborators` bundle), wire middleware, and bind `ClientComposed`. |
| `_kernel.py` | Concrete `Kernel` transport core (owns `httpx.AsyncClient` + cookie jar) |
| `_runtime/config.py` | `DEFAULT_*` knobs and module-level constants. `CORE_LOGGER_NAME = "notebooklm._core"` is intentionally preserved as a compatibility logging contract even though the `_core` module was deleted; renaming it silently breaks downstream `caplog`/logger filters. |
| `_env.py`, `config.py` | Runtime environment defaults and the public config re-export surface |
| `_logging.py`, `log.py` | Redaction/correlation logging internals and the public logging helper surface |
| `_secrets.py` | Canonical runtime registry of must-scrub bare session-cookie names (`RUNTIME_SESSION_COOKIES`), `__Secure-*` / `__Host-*` prefix umbrellas (`SECURE_HOST_UMBRELLA_PATTERNS`, fail-closed for future names), and carrier-agnostic Google credential shapes (`AUTH_TOKEN_SHAPE_PATTERNS` — `g.a000-` / `sidts-` / `ya29.` tokens + the `AIza…` API key) that `_logging.py` redaction and `exceptions.py` scrubbing DERIVE from. Runtime code cannot import from `tests/`, so this restates the cassette sanitizer's must-scrub shapes; `tests/_guardrails/test_runtime_secret_registry_parity.py` asserts lockstep with `tests/cassette_patterns.py` on every axis — bare-cookie superset, umbrella coverage, and regex-string shape equality (issues #1517/#1518). |
| `_callbacks.py` | Sync-or-async callback invocation helper used by telemetry/retry hooks |
| `_lookup.py` | `unwrap_or_raise(obj, exc)` — the shared single-row-lookup helper backing the public `get`/`get_or_none` pair (ADR-0019 Enforcement tier-2). The four `sources`/`artifacts`/`notes`/`mind_maps` `get()` methods call it directly to raise their `*NotFoundError` on a miss (the v0.8.0 flip, issue #1247); `notebooks.get()` already raised on its own path and does not route through it. |
| `_loop_bound.py` | `LoopBoundPrimitive` — template-method base for the loop-affinity `set_bound_loop` protocol. Owns the `_bound_loop` field + a `set_bound_loop` that always stores the binding and fires the `_on_loop_rebind(old, new)` hook only on a real loop change (hook before store). Trivial owners (`TransportDrainTracker`/`ReqidCounter`/`AuthRefreshCoordinator`) use the default no-op hook; clear-on-rebind owners (`ClientComposed`/`SourceUploadPipeline`/`ChatAPI`) override it to discard their cached loop-bound primitive/locks. Owns only the binding + rebind hook — the cross-loop *assert* stays in `_loop_affinity`, and each owner keeps its own `reset_after_open`. |
| `_deprecation.py` | Deprecation helper, gated by `NOTEBOOKLM_QUIET_DEPRECATIONS`: `warn_deprecated` — generic gated primitive for one-off deprecations (e.g. awaiting `from_storage(...)`); pass `removal=None` when no removal version is pinned yet. `deprecations_quiet` / `_deprecations_quiet` / `_QUIET_ENV_VAR` back the suppression gate. ADR-0018 forbids inline `warnings.warn(..., DeprecationWarning)` outside this module — `tests/_guardrails/test_no_inline_deprecation_warnings.py` enforces it (governs the `DeprecationWarning` category only; an inline `RuntimeWarning`/`UserWarning` is allowed). Note `save_cookies_to_storage(original_snapshot=None)` is NOT a deprecation — it's a permanent back-compat shim emitting an inline `RuntimeWarning` race advisory, outside ADR-0018 scope and ungated (issue #1369). The v0.7.0 error-contract machinery (`NOTEBOOKLM_FUTURE_ERRORS`/`future_errors_enabled`, `warn_get_returns_none`, `deprecated_kwarg`, `MappingCompatMixin`) was **removed in v0.8.0** once every break it staged became the default (issue #1365). See `docs/deprecations.md`. |
| `_runtime/helpers.py` | `is_auth_error`, `AUTH_ERROR_PATTERNS`, `_resolve_keepalive_interval` |
| `_error_injection.py` | Synthetic-error env-var resolver + startup guard |
| `_client_metrics.py` | `ClientMetrics` — `ClientMetricsSnapshot` counters + `on_rpc_event` callback |
| `_transport_drain.py` | `TransportDrainTracker` — in-flight transport counters + `_TransportOperationToken` |
| `_deadline.py` | `RuntimeDeadline` helper shared by retry and polling loops so aggregate timeouts clamp sleep consistently |
| `_backoff.py` | Shared capped exponential-backoff calculation with deterministic test injection |
| `_reqid_counter.py` | `ReqidCounter` — monotonic `_reqid` for the chat backend |
| `_runtime/auth.py` | `AuthRefreshCoordinator` — refresh task + auth-snapshot lock |
| `_auth_refresh_retry.py` | Shared auth refresh-and-retry core for the two retry layers (HTTP-status `AuthRefreshMiddleware` + decoded-RPC `RpcExecutor`): the once-per-logical-call `RefreshBudget` token and the common `refresh_and_count` body (log/refresh/sleep/`rpc_auth_retries` metric). Unifies the previously-divergent copies per issue #1205; the two layers keep their distinct triggers and refresh-failure exception shapes. |
| `_runtime/lifecycle.py` | `ClientLifecycle` — loop-affinity guard + keepalive task |
| `_runtime/transport.py` | `RuntimeTransport` — authed-POST transport wrapper that drives the middleware chain and typed transport response handling |
| `_rpc_executor.py` | RPC dispatch executor. Takes its `Kernel`, `RuntimeTransport`, `AuthRefreshCoordinator`, and `ClientMetrics` collaborators directly via keyword-only constructor parameters (ADR-0014 Rule 5). Defines a single local `DecodeResponse` Protocol. |
| `_request_types.py` | Shared authed POST request construction types: `AuthSnapshot`, `BuildRequest`, `PostBody`, and materialization helpers. |
| `_transport_errors.py` | Transport exceptions, `Retry-After` parsing, and terminal `Kernel.post` error mapping for retry/auth middleware. |
| `_streaming_post.py` | Size-capped streaming POST helper used by `Kernel.post`. |
| `_middleware/core.py` | HTTP-shaped middleware request/response envelope, chain composition, and middleware Protocol |
| `_middleware/context.py` | Canonical per-request context-key vocabulary for middleware |
| `_middleware/chain_host.py` | Mutable owner for the live middleware chain slots and retry-budget tunables |
| `_conversation_cache.py` | Per-instance true-LRU conversation cache for `ChatAPI` (caps conversation count via `MAX_CONVERSATION_CACHE_SIZE` and per-conversation turns via `MAX_TURNS_PER_CONVERSATION`) |
| `_polling_registry.py` | Pending-poll registry for long-running artifact generations |
| `_cookie_persistence.py` | Cookie-jar persistence + `__Secure-1PSIDTS` rotation |
| `_runtime/contracts.py` | Shared runtime Protocols consumed by sub-clients |
| `_idempotency.py` | Mutating-RPC idempotency policy registry and probe-then-retry wrapper; ADR-0005 is the taxonomy source |
| `_idempotency_policy.py` | Declarative per-RPC idempotency classification data, applied to `IDEMPOTENCY_REGISTRY` via `register_default_policies` at `_idempotency` import time (#1331). Holds the load-bearing two-pass seeding order (pre-seed `register()` → `_seed_defaults()` → post-seed `register()` + the read/set-op loop). |
| `_atomic_io.py`, `io.py` | Atomic JSON write/update internals and public I/O re-export surface for CLI boundary compliance |
| `exceptions.py` | Public exception hierarchy plus safe diagnostic preview/redaction helpers |
| `paths.py`, `migration.py` | Profile-aware path resolution and locked migration from the legacy flat layout |
| `_types/`, `types.py` | Dataclass implementation package and public type/re-export facade |
| `_types/labels.py` | `Label` pure-value type (source-label topic grouping; `source_ids` only, no artifact members) re-exported by `types.py` |
| `_row_adapters/artifacts.py` | `ArtifactRow` typed view over raw positional artifact RPC rows, plus `ReportSuggestionRow` over `GET_SUGGESTED_REPORTS` rows |
| `_row_adapters/chat.py` | Streamed-chat row adapters (`AnswerRow` / `CitationRow` / `CitationDetail` / `PassageRow` / `StreamFrameRow` / `ErrorPayloadRow` / `TextLeafRow`) that centralise the chat wire positions `_chat/wire.py` used to open-code (#1491) |
| `_row_adapters/labels.py` | `LabelRow` strict typed view over the raw positional label tuple `[name, sources, id, emoji]` (fails loud on schema drift) |
| `_row_adapters/notebooks.py` | `SUGGEST_PROMPTS` (`otmP3b`) suggestion-row view (`PromptSuggestionRow` / `unwrap_prompt_suggestions`) backing `NotebooksAPI.suggest_prompts` |
| `_row_adapters/notes.py` | `NoteRow` typed view over raw positional note and mind-map RPC rows |
| `_row_adapters/research.py` | `ResearchTaskRow` / `ResearchTaskInfoRow` / `ResearchResultRow` typed views over raw positional `POLL_RESEARCH` rows that centralise the single-level positions `_research_task_parser.py` used to open-code (#1501) |
| `_row_adapters/sources.py` | `SourceRow` / `SourceRowShape` typed views over raw positional source RPC rows |
| `artifacts.py`, `research.py`, `utils.py` | Public helper modules for artifact retry, research citation/report utilities, and common async helpers |
| `_research_task_parser.py` | Internal parser for research task result-type selection |
| `_notebooks.py` | `client.notebooks` API + source-id resolver |
| `_notebook_payloads.py` | Stable `batchexecute` notebook RPC request payload builders (currently `SUGGEST_PROMPTS`) |
| `_sources.py` | `client.sources` API |
| `_artifacts.py` | `client.artifacts` API — owns artifact generation orchestration directly (see ADR-0012) |
| `_chat/api.py` | `client.chat` API |
| `_research.py` | `client.research` API |
| `_notes.py` | `client.notes` API |
| `_sharing.py` | `client.sharing` API |
| `_labels.py` | `client.labels` API — source labels (topic groupings); pure-RPC like `SharingAPI`, plus a narrow `list_sources` callable for the membership→`Source` join in `sources()` |
| `_settings.py` | `client.settings` API |
| `_note_service.py` | Service layer managing note CRUD, note-backed content generation, and sync |
| `_mind_map.py` | Specific adapter service representing mind-maps, backed by standard notes |
| `_mind_maps_api.py` | `client.mind_maps` API — unified surface over both mind-map backends (note-backed JSON + interactive studio-artifact), dispatching each op to the correct RPC family (#1256) |
| `_artifact/downloads.py` | Asynchronous download coordinator for finished artifacts |
| `_artifact/_redirect_guard.py` | Per-redirect-hop host/scheme revalidation for downloads — rejects off-allowlist / non-HTTPS redirect targets before the request is sent (#1521) |
| `_artifact/_download_client.py` | Download trusted-host allowlist + transport-aware client factory — wires the #1521 redirect guard for httpx (event hook) or the opt-in curl_cffi (`get_guarded` manual loop) |
| `_artifact/formatters.py` | Markdown, HTML, and plain text formatters for artifacts |
| `_artifact/payloads.py` | Stable CREATE_ARTIFACT / GENERATE_MIND_MAP request payload builders |
| `_artifact/generation.py` | Generation kickoff service (`generate_*`, `revise_slide`, `retry_failed`) extracted from `ArtifactsAPI`; the facade keeps thin delegators |
| `_artifact/listing.py` | Listing and filtering operations for notebook artifacts |
| `_artifact/polling.py` | Poll coordination service for artifact generation tasks |
| `_source/add.py` | Core service layer for adding text, URL, or Google Drive sources |
| `_source/content.py` | Core service layer for fetching source HTML/markdown content |
| `_source/listing.py` | Core service layer for listing notebook sources |
| `_source/polling.py` | Poll coordination service for active source conversions |
| `_source/upload.py` | Concurrency-gated upload pipeline for source files |
| `_source/_upload_decode.py` | Pure decode/validation helpers for the upload pipeline (URL redaction, ADD_SOURCE_FILE source-id extraction, content-type policy), extracted from `upload.py` |
| `_source/upload_payloads.py` | Stable source upload registration, rename, and resumable-upload request builders |
| `_label/params.py` | Stable CREATE_LABEL / LIST_LABELS / UPDATE_LABEL / DELETE_LABEL request payload builders (with the shared `_opts()` request-options wrapper) |
| `_notebook_metadata.py` | Metadata protocol schemas for sub-clients |
| `_url_utils.py`, `urls.py` | URL parsing/validation internals and the public URL helper facade |
| `_sharing_manager.py` | Direct sharing management logic |
| `_version_check.py` | Dynamic client-side version deprecation guard |
| `_chat/notes.py` | Chat-adjacent note saving workflow adapter |
| `_chat/wire.py` | Streamed-chat wire request construction + response parsing for the chat client |
| `_chat/transport.py` | Chat-specific error mapping over the shared transport pipeline |
| `_middleware/chain.py` | Constructs the middleware chain in the canonical ADR-0009 order |
| `_middleware/*.py` | Modular middleware implementations (drain, metrics, semaphore, retry, auth, error injection, tracing) |
| `rpc/types.py` | RPC method IDs (source of truth) |
| `auth.py` | Authentication facade — **almost pure re-exports** (the only remaining function body is `async def enumerate_accounts`, which binds `_poke_session` as a default dependency; ADR-0003 records the optional-`async` audit command). Every other top-level name forwards from the relevant `_auth/*` module: `auth._validate_required_cookies` is identity-equal to `_auth.cookie_policy._validate_required_cookies`, and `load_auth_from_storage` / `AuthTokens` live in `_auth/tokens.py`. **ADR-0003's flat-re-export goal was closed by ADR-0014.** Tests that need to rebind policy names patch `_auth.cookie_policy.X` directly. |
| `_auth/paths.py` | Storage paths and filesystem helpers |
| `_auth/extraction.py` | Cookie/token extraction from browser sessions |
| `_auth/headers.py` | HTTP header construction |
| `_auth/cookies.py` | Cookie map manipulation + `_update_cookie_input` |
| `_auth/cookie_policy.py` | Cookie-domain allowlist, `build_cookie_domain_allowlist` builder, and policy decisions |
| `_auth/browser_capture.py` | Transport-neutral browser launch→capture→filter→persist core (lazy `playwright`); shared by the interactive CLI login adapter (`cli/services/playwright_login.py`) and the layer-3 headless re-auth layer (ADR-0021) |
| `_auth/headless_reauth.py` | Layer-3 headless re-auth decision layer: opt-in/profile-gated, typed honest outcomes (`HeadlessReauthStatus`); drives `run_browser_capture(headless=True, interactive=False)`. Local-unattended-only |
| `cli/label_cmd.py` | `label` command group (list/sources/generate/create/rename/emoji/add/remove/delete); thin Click shells over `client.labels`, `_app.labels`, and the label-listing service (ADR-0008/0021) |
| `cli/services/label_listing.py` | `label` CLI service: the `label list` members→source-titles join (`execute_label_list`/`LabelListPlan`). Re-exports `resolve_label_id` + `LabelResolutionError` from `_app/labels.py` (the composite `<id\|name>` resolver moved to the neutral layer; the re-export keeps `from .services.label_listing import resolve_label_id` resolving for the command layer + tests) |

### Repository Structure

```text
src/notebooklm/
├── __init__.py                  # Public exports
├── __main__.py                  # `python -m notebooklm` entry point
├── client.py                    # NotebookLMClient
├── auth.py                      # Authentication facade — almost pure re-exports (`enumerate_accounts` exception; ADR-0003 flat-re-export goal closed by ADR-0014; see file table above)
├── types.py                     # Dataclasses
├── artifacts.py                 # Public artifact-generation retry helpers
├── config.py                    # Public config facade over _env
├── exceptions.py                # Public exception hierarchy
├── io.py                        # Public atomic-I/O facade for CLI boundary compliance
├── log.py                       # Public logging helper facade
├── migration.py                 # Legacy flat-layout to profile migration
├── paths.py                     # Profile-aware path resolution
├── research.py                  # Public research citation/report helpers
├── urls.py                      # Public URL helper facade
├── utils.py                     # Public async utility helpers
├── _atomic_io.py                # Atomic JSON write/update helpers
├── _auth_refresh_retry.py       # Shared auth refresh-and-retry core (RefreshBudget + refresh_and_count) for both retry layers
├── _backoff.py                  # Shared retry backoff calculation
├── _callbacks.py                # Sync/async callback invocation helper
├── _client_assembly.py          # Shared client-assembly seam (constructor + test factory)
├── _client_composed.py          # Client-owned composition holder
├── _client_seams.py             # Constructor-only injectable seams
├── _deadline.py                 # RuntimeDeadline helper for aggregate timeouts
├── _deprecation.py              # Deprecation helper (warn_deprecated) gated by NOTEBOOKLM_QUIET_DEPRECATIONS
├── _env.py                      # Runtime environment/default endpoint helpers
├── _idempotency.py              # Mutating-RPC idempotency registry + wrappers
├── _idempotency_policy.py       # Declarative per-RPC idempotency classification data (register_default_policies)
├── _kernel.py                   # Concrete Kernel transport core
├── _logging.py                  # Redaction + correlation logging internals
├── _secrets.py                  # Canonical runtime secret registry (cookie names + secure/host umbrellas + token/API-key shapes) the redaction patterns derive from
├── _lookup.py                   # unwrap_or_raise — shared single-row-lookup helper for get/get_or_none
├── _loop_affinity.py            # Event-loop affinity guard helper (assert_bound_loop free function)
├── _loop_bound.py               # LoopBoundPrimitive mixin — template-method set_bound_loop + _on_loop_rebind hook for the loop-bound collaborators
├── _error_injection.py          # Synthetic-error env-var resolver + startup guard
├── _request_types.py            # AuthSnapshot, BuildRequest, PostBody, request materialization helpers
├── _transport_errors.py         # Transport exceptions, Retry-After parsing, Kernel.post error mapping
├── _streaming_post.py           # Size-capped streaming POST helper
├── _curl_cffi_transport.py      # Opt-in curl_cffi browser-impersonation transport (NOTEBOOKLM_TRANSPORT=curl_cffi)
├── _rpc_executor.py             # RPC dispatch executor
├── _client_metrics.py           # Telemetry / metrics seam
├── _transport_drain.py          # In-flight transport drain coordinator
├── _reqid_counter.py            # Request-counter / request-id helpers
├── _conversation_cache.py       # Per-instance true-LRU conversation cache (bounded conversation count + per-conversation turns)
├── _polling_registry.py         # Artifact polling helpers
├── _cookie_persistence.py       # Cookie-jar persistence + __Secure-1PSIDTS rotation
├── _note_service.py             # NoteService
├── _mind_map.py                 # NoteBackedMindMapService
├── _mind_maps_api.py            # MindMapsAPI — unified mind-map surface over both backends (#1256)
├── _notebook_metadata.py        # Metadata protocols
├── _url_utils.py                # URL validation helpers
├── _sharing_manager.py          # Sharing management logic
├── _version_check.py            # Deprecation version guard
├── _research_task_parser.py     # Research task result-type parser
├── _app/                        # Transport-neutral business-logic layer (CLI/MCP/HTTP adapters share it)
│   ├── __init__.py              # Re-exports the neutral primitives
│   ├── artifacts.py             # Click-free artifact core: get/rename/delete/export + poll/wait/retry; kind-aware mind-map dispatch (mind_maps.list for rename, notes.list_mind_maps for delete), get_artifact raises ArtifactNotFoundError, typed Rename/Export results + ArtifactStatusView/status_view neutral status DTO (CLI builds every --json envelope from the typed fields)
│   ├── auth_check.py            # Click-free `auth check` diagnostics core: run_auth_check(plan, read_env_auth_json=…) -> AuthCheckResult (storage-exists/json-valid/cookies-present/SID + optional token-fetch); AuthCheckPlan carries pre-resolved values + the auth_source display label; inline-auth read injected (CLI owns the AuthSource plan-build + Rich table + exit code)
│   ├── chat.py                  # Click-free chat core: conversation-id selection ladder + configure mode/goal/length dispatch + history fetch/format-as-data + ask save-as-note workflow (raises public ValidationError; status emitted into injected ProgressSink)
│   ├── doctor.py                # Click-free doctor core: run_checks(*, fix, paths) -> DoctorReport (five checks incl. headless-reauth readiness + fixes + has_failures; DoctorPaths injects the path helpers; CLI owns rendering/exit codes)
│   ├── download.py              # Click-free download core: DownloadPlan/Result/TypeSpec + build_download_plan/execute_download (injected resolvers; CLI builds the --json envelope from the typed DownloadResult)
│   ├── errors.py                # classify(exc) -> ClassifiedError (category + retriable); class-sensitive
│   ├── events.py                # ProgressEvent + ProgressSink Protocol (neutral progress seam)
│   ├── generate.py              # Click-free `generate` executor: execute_generation (injected notebook/source resolvers preserve the RPC fast paths) + GenerationExecutionResult; re-exports the plan/retry surface so `_app.generate` is the single import point
│   ├── generate_plans.py        # Click-free `generate` plan-building: enum/format maps, GenerationPlan/GenerationKind/GenerationPlanValidationError, build_generation_plan + per-kind builders (parameter_explicit/language_resolver injected)
│   ├── generate_retry.py        # Click-free `generate` retry/wait: GenerationOutcome, generate_with_retry, handle_generation_result, status extractors, spinner status-line formatter (wait_context/wait_start_sink neutral seams)
│   ├── labels.py                # Click-free label core: create/sources/generate/rename/emoji/add/remove/delete + the composite resolve_label_id (<id|name>) resolver + LabelResolutionError (injected notebook/source resolvers; members→titles JOIN render stays in cli/services/label_listing.py)
│   ├── language.py              # Click-free language core: SUPPORTED_LANGUAGES catalog + is_supported_language + LanguageConfigStore (injected config-path/home/atomic-update; get/save/get_language/set_language)
│   ├── mcp_install.py           # Click-free `mcp install <client>` core: supported-client catalog (claude-desktop/claude-code/cursor/windsurf) + per-OS resolve_config_path + uvx build_server_block + merge_server_config read-modify-merge into mcpServers (created/updated/unchanged; never clobbers unrelated keys); UnsupportedClientError. CLI owns the atomic write (cli/mcp_cmd.py)
│   ├── notebooks.py             # Click-free notebook core: create/delete/rename/describe(summary)/metadata fetch+compute (injected resolve_notebook_id; summary/metadata serializers stay in cli/notebook_cmd.py)
│   ├── notes.py                 # Click-free note core: create/get/save/rename/delete (typed-facade only — notes.create returns a Note) + content-preserving rename (resolve_note_content); found-flag results map to the CLI NOT_FOUND/exit-1 path (injected notebook/note resolvers)
│   ├── profile.py               # Click-free profile core: gather_profile_list -> ProfileEntry rows (injected list_profiles/resolve_profile/get_storage_path/read_account_metadata), is_protected_profile delete-guard decision, set_default/retarget_default config.json mutators (CLI keeps the locked _atomic_write_config + click.confirm + Rich render)
│   ├── research.py              # Click-free `research` status/wait core: poll_and_classify -> ResearchStatusResult, ResearchWaitPlan/Result + execute_research_wait (resolver/importer/wait-context injected), validate_research_wait_flags (-> ValidationError); returns typed results only (CLI owns the --json envelope)
│   ├── resolve.py               # Click-free validate_id + resolve_ref (AmbiguousIdError/Resolution)
│   ├── serialize.py             # to_jsonable(obj) recursive JSON-able conversion (enum-before-primitive)
│   ├── session.py               # Click-free session-context core: `use` verify_and_set_notebook (injected resolve_notebook_id) + `status` read_status(StatusInputs) read+project -> StatusReport + `auth logout` execute_logout(LogoutInputs) filesystem-teardown -> typed LogoutOutcome (path/context/clear_context helpers injected via bundles; CLI owns Rich render + exit codes)
│   ├── sharing.py               # Click-free sharing core: status/set_public/set_view_level/add_user/update_user/remove_user (injected resolve_notebook_id; permission/view-level display + str→enum parse stay in cli/share_cmd.py)
│   ├── skill.py                 # Click-free skill-install core: TARGETS/SCOPES catalog + path/version helpers + classify_target (create/up_to_date/overwrite) + report_mixed_no_clobber_up_to_date (CLI owns the atomic write + packaged-source loader)
│   ├── source_add.py            # Click-free `source add` core: input detection + URL SSRF/upload-path validation + add workflow (SourceAddPlan/Result; CLI builds the --json source-summary from the typed result via the neutral serialize.source_summary helper)
│   ├── source_clean.py          # Click-free `source clean` core: junk-source classification + batched-deletion orchestration (SourceCleanResult; injected list/delete/confirm callables)
│   ├── source_content.py        # Click-free read-only source-content fetchers for get/fulltext/guide/stale (typed plan/result pairs)
│   ├── source_listing.py        # Click-free `source list` fetch core: fetch_sources (label_filter resolution; label_resolver injected)
│   ├── source_mutations.py      # Click-free source delete/delete-by-title/rename/refresh/add-drive core: resolvers + SourceMutationError + typed results (validate_id/resolve_source_id injected; confirmer injected)
│   ├── source_research.py       # Click-free `source add-research` start/wait/import workflow + validate_add_research_flags (importer injected; SourceAddResearchPlan/Result)
│   └── source_wait.py           # Click-free `source wait` readiness-poll core: execute_source_wait + typed SourceWaitOutcome (wait_context injected)
├── _runtime/                    # Client-runtime subpackage (promoted from flat _runtime_*.py, #1328)
│   ├── __init__.py              # Re-exports the cluster's public names
│   ├── auth.py                  # AuthRefreshCoordinator (refresh task + auth-snapshot lock)
│   ├── config.py                # DEFAULT_* knobs + module-level constants
│   ├── contracts.py             # Shared runtime Protocols consumed by feature APIs
│   ├── helpers.py               # is_auth_error / AUTH_ERROR_PATTERNS / keepalive helpers
│   ├── init.py                  # Runtime collaborator construction + validation
│   ├── lifecycle.py             # Open/close lifecycle seam (loop affinity + keepalive task)
│   └── transport.py             # Middleware-chain transport wrapper
├── _middleware/                 # Middleware subpackage (promoted from flat _middleware*.py, #1328)
│   ├── __init__.py              # Re-exports the cluster's public names
│   ├── core.py                  # Middleware envelope + Protocol + chain composition primitive (was _middleware.py)
│   ├── context.py               # Middleware context-key vocabulary
│   ├── chain.py                 # Middleware chain builder
│   ├── chain_host.py            # Live middleware chain slots and retry tunables
│   ├── tracing.py               # Tracing middleware
│   ├── metrics.py               # Metrics middleware
│   ├── drain.py                 # Drain middleware
│   ├── error_injection.py       # Error injection middleware
│   ├── retry.py                 # Retry middleware
│   ├── auth_refresh.py          # Auth refresh middleware
│   └── semaphore.py             # Concurrency semaphore middleware
├── _source/                     # Source-feature subpackage (promoted from flat _source_*.py, #1328)
│   ├── __init__.py              # Re-exports the cluster's public service classes
│   ├── _upload_decode.py        # Pure URL/source-id/content-type decode + validation helpers (extracted from upload.py)
│   ├── add.py                   # Source addition coordinator
│   ├── content.py               # Source content fetcher
│   ├── listing.py               # Source listing helper
│   ├── polling.py               # Source polling coordinator
│   ├── upload.py                # Gated source upload service
│   └── upload_payloads.py       # Source upload request payload builders
├── _artifact/                   # Artifact-feature subpackage (promoted from flat _artifact_*.py, #1328)
│   ├── __init__.py              # Re-exports the cluster's public service classes/builders
│   ├── _download_client.py      # Download trusted-host allowlist + transport-aware client factory (httpx event hook / curl_cffi get_guarded)
│   ├── _redirect_guard.py       # Per-redirect-hop host/scheme revalidation for downloads (#1521)
│   ├── downloads.py             # Artifact download coordinator
│   ├── formatters.py            # Artifact formatting helpers
│   ├── generation.py            # Artifact generation kickoff service (generate_*, revise_slide, retry_failed)
│   ├── payloads.py              # Stable artifact request payload builders
│   ├── listing.py               # Artifact listing helper
│   └── polling.py               # Artifact polling coordinator
├── _label/                      # Source-label feature subpackage: stable RPC payload builders
│   ├── __init__.py              # Re-exports the label param builders
│   └── params.py                # Source-label RPC payload builders (CREATE/LIST/UPDATE/DELETE_LABEL)
├── _row_adapters/               # Positional-RPC-row adapters subpackage (promoted from flat _row_adapters_*.py, #1328)
│   ├── __init__.py              # Re-exports the typed row views
│   ├── artifacts.py             # Artifact + GET_SUGGESTED_REPORTS row adapters (ArtifactRow / ReportSuggestionRow)
│   ├── chat.py                  # Streamed-chat row adapters (AnswerRow / CitationRow / CitationDetail / PassageRow / StreamFrameRow / ErrorPayloadRow / TextLeafRow) — closes the chat positional-decode perimeter (#1491)
│   ├── labels.py                # Source-label row adapter
│   ├── notebooks.py             # SUGGEST_PROMPTS suggestion-row adapter (PromptSuggestionRow / unwrap_prompt_suggestions)
│   ├── notes.py                 # Note and mind-map row adapter
│   ├── research.py              # POLL_RESEARCH row adapters (ResearchTaskRow / ResearchTaskInfoRow / ResearchResultRow) — drains the research parser's single-level positional reads (#1501)
│   └── sources.py               # Source row adapter
├── _chat/                       # Chat-feature subpackage — facade + helpers unified (#1328)
│   ├── __init__.py              # Re-exports ChatAPI so `from ._chat import ChatAPI` keeps resolving
│   ├── api.py                   # ChatAPI facade (was _chat.py)
│   ├── notes.py                 # Note saving workflow adapter
│   ├── wire.py                  # Streamed-chat wire request/response parser
│   └── transport.py             # Chat error mapping
├── _auth/                       # Auth subpackage (forwarded through auth.py facade)
│   ├── __init__.py
│   ├── paths.py                 # Storage paths and filesystem helpers
│   ├── extraction.py            # Cookie/token extraction from browser sessions
│   ├── headers.py               # HTTP header construction
│   ├── cookies.py               # Cookie maps + _update_cookie_input
│   ├── cookie_policy.py         # Domain allowlist + cookie-domain builder and policy
│   ├── browser_capture.py       # Transport-neutral browser launch→capture→filter→persist core (lazy playwright)
│   ├── headless_reauth.py       # Layer-3 headless re-auth (opt-in; typed outcomes; local-unattended-only)
│   ├── account.py               # Account profile + multi-account switching
│   ├── session.py               # Auth-session refresh implementation via `refresh_auth_session()` and explicit collaborators
│   ├── storage.py               # Profile/state persistence on disk
│   ├── keepalive.py             # Cookie keepalive + __Secure-1PSIDTS rotation
│   ├── psidts_recovery.py       # Inline PSIDTS recovery for cold-start (issue #865)
│   ├── master_token.py          # Headless master-token auth: mint cookies + layer-4 re-mint (ADR-0023)
│   ├── refresh.py               # Token refresh driver (external login cmd, coalesced runs, redaction)
│   └── tokens.py                # AuthTokens container + load_auth_from_storage loader
├── _types/                      # Dataclass implementation package re-exported by types.py
│   ├── __init__.py
│   ├── artifacts.py
│   ├── chat.py
│   ├── common.py
│   ├── labels.py                # Label pure-value type (source membership; no kind/artifact_ids)
│   ├── mind_maps.py             # MindMap + MindMapKind pure-value types (#1256)
│   ├── notebooks.py
│   ├── notes.py
│   ├── research.py              # ResearchStatus enum + ResearchTask/ResearchSource/ResearchStart/MindMapResult/SourceGuide typed returns (#1209)
│   ├── sharing.py
│   └── sources.py
├── _notebooks.py                # NotebooksAPI
├── _notebook_payloads.py        # batchexecute notebook RPC payload builders (SUGGEST_PROMPTS)
├── _sources.py                  # SourcesAPI
├── _artifacts.py                # ArtifactsAPI
├── _research.py                 # ResearchAPI
├── _notes.py                    # NotesAPI
├── _sharing.py                  # SharingAPI
├── _settings.py                 # SettingsAPI
├── _labels.py                   # LabelsAPI — client.labels (source labels: generate/create/list/…)
├── notebooklm_cli.py            # Entry-point assembler — imports + registers cli/ groups
├── mcp/                         # MCP server (opt-in `mcp` extra) — transport-neutral adapter over _app/, sibling to cli/
│   ├── __init__.py              # Re-exports create_server / SERVER_NAME / SERVER_INSTRUCTIONS
│   ├── __main__.py              # `notebooklm-mcp` entrypoint: argparse (--profile/--transport/--host/--port/--log-level), stderr logging, loopback HTTP bind guard + fail-closed token guard (non-loopback bind requires NOTEBOOKLM_MCP_TOKEN); builds the auth provider and passes it to create_server on the http path
│   ├── server.py                # create_server(profile, client_factory, auth): FastMCP server; lifespan binds one NotebookLMClient; register_all tool-registration seam; auth passed explicitly (never reads the token env)
│   ├── _auth.py                 # Remote-transport bearer auth: McpBearerAuthProvider(TokenVerifier) with constant-time hmac.compare_digest over NOTEBOOKLM_MCP_TOKEN (env-only, never logged/repr'd); build_auth_provider/get_configured_token — mirrors server/_auth.py, NOT fastmcp StaticTokenVerifier
│   ├── _context.py              # AppState dataclass + get_client(ctx) — the lifespan-bound client
│   ├── _errors.py               # Structured tool-error projection (CATEGORY_TABLE/ERROR_CODES/mcp_errors/to_tool_error/tool_error_payload) over _app.errors.classify
│   ├── _resolve.py              # resolve_notebook/resolve_source/resolve_note — name + partial-id resolution over _app.resolve plus exact-title matching
│   ├── _confirm.py              # needs_confirmation() both-mode envelope + READ_ONLY/DESTRUCTIVE ToolAnnotations
│   └── tools/                   # Per-domain tool modules; each exposes register(mcp) wired by server.register_all
│       ├── __init__.py          # Tools package marker (no click/rich/cli)
│       ├── _passthrough.py      # Shared pass-through resolvers (passthrough_notebook_id/passthrough_child_id) for the CLI-shaped _app executors
│       ├── _preview.py          # title_for_id() — shared id→title lookup for the delete tools' needs_confirmation previews
│       ├── notebooks.py         # notebook_list/create/describe/rename/delete over _app.notebooks
│       ├── sources.py           # source_list/get_content/rename/delete/wait/add over _app.source_* (add: url/text/file/youtube via source_add, drive via source_mutations)
│       ├── chat.py              # chat_ask (client.chat.ask) + chat_configure (_app.chat.execute_configure)
│       ├── notes.py             # note_create/list/update/delete over _app.notes
│       ├── artifacts.py         # artifact_list/generate/status/download (enum dispatch over _app.generate + _app.download; stateless poll via _app.artifacts.poll_artifact)
│       ├── research.py          # research_start (client.research.start) + research_status (_app.research.poll_and_classify) + research_import
│       └── meta.py              # server_info — package version + auth-health over _app.auth_check (no notebook arg)
├── rpc/                         # RPC protocol layer
│   ├── types.py                 # Method IDs and enums
│   ├── encoder.py               # Request encoding
│   ├── decoder.py               # Response parsing
│   ├── _safe_index.py           # Strict bounds-checked positional access for decoded RPC payloads
│   └── overrides.py             # Runtime RPC ID override policy (env-driven)
├── cli/                         # CLI implementation
    ├── __init__.py              # Re-exports click groups under historical names from *_cmd modules
    ├── _chromium_profiles.py    # Multi-user-data-profile cookie extraction for Chromium browsers
    ├── _cookie_import.py        # `auth import-cookies` helpers: parse/normalize/validate cookie JSON + backup-then-atomic-write storage_state
    ├── _download_specs.py       # Registry data for `download <type>` leaf commands
    ├── _encoding.py             # Encoding-safe CLI output helpers
    ├── _firefox_containers.py   # Container-aware Firefox cookie extraction
    ├── _session_render.py       # Session-command render helpers (status/auth tables)
    ├── _source_render.py        # Source CLI render/validation helpers (extracted from source_cmd.py)
    ├── agent_cmd.py             # agent show commands
    ├── agent_templates.py       # agent prompts and configurations
    ├── artifact_cmd.py          # artifact commands
    ├── auth_runtime.py          # CLI authentication + command runtime helpers
    ├── chat_cmd.py              # ask, configure, history
    ├── completion.py            # Best-effort shell-completion providers for live IDs
    ├── context.py              # CLI context persistence helpers
    ├── doctor_cmd.py            # diagnostic/repair tool
    ├── download_cmd.py          # download commands
    ├── download_helpers.py      # Helper functions for download commands
    ├── error_handler.py         # Centralized CLI error handling
    ├── generate_cmd.py          # generate audio, video, etc.
    ├── grouped.py               # Custom Click group with sectioned help output
    ├── helpers.py               # Shared Click utilities
    ├── input.py                 # CLI prompt and stdin input helpers
    ├── label_cmd.py             # label list/sources/generate/create/rename/emoji/add/remove/delete
    ├── language_cmd.py          # Language configuration CLI commands
    ├── master_token_login.py    # Command driver for `login --master-token[-refresh]` (ADR-0023)
    ├── mcp_cmd.py               # `mcp install <client>` command — thin Click adapter over `_app/mcp_install.py`; resolves the client config path (`--config-path` override) and applies the merge inside `notebooklm.io.atomic_update_json` (locked, crash-safe, merge-not-clobber)
    ├── notebook_cmd.py          # list, create, delete, rename
    ├── note_cmd.py              # note commands
    ├── options.py               # Shared CLI option decorators
    ├── playwright_login_io.py   # Command-side LoginIO sink + wrappers for the Playwright login service (#1391)
    ├── polling_ui.py            # Command-layer UI helpers for long-running polling
    ├── profile_cmd.py           # Profile management CLI commands
    ├── rendering.py             # CLI rendering helpers
    ├── research_cmd.py          # Research management CLI commands
    ├── research_import.py       # Research import helpers shared by CLI commands
    ├── resolve.py               # CLI notebook/entity ID resolution helpers
    ├── runtime.py               # CLI runtime primitives
    ├── session_cmd.py           # login, use, status, clear
    ├── share_cmd.py             # Sharing management CLI commands
    ├── skill_cmd.py             # Skill management commands
    ├── source_cmd.py            # source add, list, delete
    └── services/                # CLI-specific service layer (ADR-0008 Click-to-service extraction)
        ├── __init__.py
        ├── auth_diagnostics.py  # `auth check` CLI adapter over `_app/auth_check.py` — re-exports AuthCheckPlan/Result; builds the plan from the AuthSource Click-context precedence (plan_from_click_context + the auth_source display label) and injects read_env_auth_json into the neutral run_auth_check
        ├── auth_source.py       # Single source of truth for the active CLI auth source (Click-context precedence resolver; stays in cli/ — reads ctx.obj + NOTEBOOKLM_AUTH_JSON)
        ├── confirming_mutation.py # Shared confirmed-mutation pipeline for CLI resources
        ├── download.py          # CLI adapter over _app/download.py: re-exports plan types, injects cli.resolve resolvers (keeps resolve_notebook_id patch seam), projects DownloadResult → envelope dict
        ├── generate.py          # `generate` CLI adapter over `_app/generate.py` — re-exports plan/result/error + build_generation_plan; injects cli.resolve resolve_notebook_id/resolve_source_ids (read at call time, preserving the resolve_module monkeypatch seam) into the neutral execute_generation; re-exports _INFOGRAPHIC_STYLE_MAP from `_app/generate_plans.py` for generate_cmd
        ├── label_listing.py     # `label list` members→titles join service; re-exports resolve_label_id + LabelResolutionError from _app/labels.py
        ├── listing.py           # Shared list-command pipeline for CLI resources
        ├── login/               # Browser-cookie login helper package
        │   ├── __init__.py      # re-export-only patch surface
        │   ├── browser_accounts.py
        │   ├── chromium_accounts.py
        │   ├── cookie_domains.py
        │   ├── cookie_jar.py
        │   ├── cookie_writes.py
        │   ├── exceptions.py
        │   ├── firefox_accounts.py
        │   ├── io_seam.py        # Caller-injected LoginIO Protocol + resolver (#1393)
        │   ├── master_token.py   # Headless master-token bootstrap/refresh + browser oauth_token capture (ADR-0023)
        │   ├── outcomes.py
        │   ├── profile_targets.py
        │   ├── refresh.py
        │   └── rookiepy_errors.py
        ├── playwright_login.py  # Playwright-driven Google login service
        ├── playwright_redaction.py # Subprocess-output redaction helpers for the Playwright login service
        ├── polling.py           # Shared polling helpers for CLI wait commands
        ├── research.py          # `research wait` CLI adapter over `_app/research.py` — re-exports plan/result/outcome; injects cli.resolve.resolve_notebook_id + cli.research_import.import_research_sources defaults (preserves their patch seams)
        ├── session_context.py   # Notebook-context CLI adapter over `_app/session.py` for `use`/`status`/`auth logout` — re-exports the typed result classes; builds the injected StatusInputs/LogoutInputs bundles from its own session_context-namespace path helpers (read at call time, preserving the get_context_path/get_storage_path/clear_context patch seams)
        ├── source_listing.py    # `source list` CLI adapter over `_app/source_listing.py` — owns the ListSpec/prepare_list presentation half; injects resolve_label_id into the neutral fetch_sources
        ├── source_mutations.py  # Source-mutation CLI adapter over `_app/source_mutations.py` — re-exports plan/result/error/helpers; injects cli.resolve validate_id + resolve_source_id (preserves the resolve_source_id monkeypatch seam) and the click.confirm confirmer
        ├── source_research.py   # `source add-research` CLI adapter — thin wrapper over `_app/source_research.py` (injects the rich-coupled importer; re-exports plan/result + validate_add_research_flags; preserves the import_research_sources monkeypatch seam)
        └── source_serializers.py # Shared JSON serializers for source CLI output
└── server/                      # Single-tenant REST API adapter (the third _app adapter, after cli/ and mcp/; behind the optional `server` extra). EXPERIMENTAL: /v1 surface may change, excluded from the api-compat gate. Imports no click/rich/cli.
    ├── __init__.py              # Re-exports create_app + SERVER_NAME; importing it without the `server` extra fails on the fastapi import
    ├── __main__.py              # `notebooklm-server` entry: argparse + NOTEBOOKLM_SERVER_* env defaults + loopback-bind guard + fail-closed token check
    ├── app.py                   # create_app(*, client_factory=None) -> FastAPI; ASGI lifespan binds one client; public /healthz; auth-gated /v1 mount (docs/redoc/openapi disabled)
    ├── _context.py              # AppState (lifespan-bound client + pending registry) + get_client / get_pending FastAPI dependencies
    ├── _auth.py                 # Bearer-token (constant-time, 401) + loopback-Host (DNS-rebinding guard, 403) dependency for /v1
    ├── _errors.py               # ErrorCategory -> HTTP status table + _redact + the classify-once exception handler emitting {error:{category,message}}
    ├── _pending.py              # In-process pending-id registry (per-notebook provenance for poll -> 200-pending vs 404)
    └── routes/                  # Per-resource FastAPI routers; handlers call _app.serialize.to_jsonable directly
        ├── __init__.py          # Aggregates the resource routers for the app factory
        ├── _passthrough.py      # Pass-through resolvers handed to the _app cores (REST works in full ids)
        ├── notebooks.py         # /v1/notebooks list/get/create/delete
        ├── sources.py           # /v1/notebooks/{id}/sources list/get/add(url·text·file)/delete + poll-the-resource status
        ├── notes.py             # /v1/notebooks/{id}/notes list/get/create/update(PUT)/delete — thin adapter over client.notes
        ├── chat.py              # POST /v1/notebooks/{id}/chat — blocking ask (no SSE)
        ├── artifacts.py         # /v1/notebooks/{id}/artifacts list/generate/poll/download (registry-projected poll; server-generated temp download path)
        └── share.py             # /v1/notebooks/{id}/share status/public/users/view-level over _app.sharing
```

## ADR cross-references

- [ADR-0001](./adr/0001-layered-core-seams-and-property-bridge-policy.md) — Layered seams + property-bridge policy (superseded; shims retired).
- [ADR-0002](./adr/0002-capability-protocol-pattern.md) — Capability Protocol pattern (Superseded by ADR-0013).
- [ADR-0003](./adr/0003-auth-facade-write-through.md) — `auth.py` write-through facade (Superseded — closed by [ADR-0014](./adr/0014-feature-local-runtime-adapters.md); `auth.py` is now almost pure re-exports with `enumerate_accounts` as the sole function-body exception).
- [ADR-0004](./adr/0004-loop-affinity-contract.md) — Loop-affinity contract (Accepted; enforced by `_loop_affinity.assert_bound_loop`).
- [ADR-0005](./adr/0005-idempotency-taxonomy.md) — Mutating-RPC idempotency taxonomy (Accepted; enforced by `_idempotency.IdempotencyRegistry`).
- [ADR-0006](./adr/0006-vcr-scrubber-strategy.md) — VCR cassette scrubber strategy (Accepted).
- [ADR-0007](./adr/0007-test-monkeypatch-policy.md) — Constructor-injection test pattern via `tests/_fixtures/` (Accepted; enforced by `tests/_guardrails/test_no_forbidden_monkeypatches.py`).
- [ADR-0008](./adr/0008-cli-services-extraction-pattern.md) — `cli/services/` extraction pattern (Accepted).
- [ADR-0009](./adr/0009-middleware-chain.md) — Middleware chain ordering (Accepted; load-bearing).
- [ADR-0010](./adr/0010-session-kernel-split.md) — Session/Kernel split (Superseded by ADR-0013).
- [ADR-0011](./adr/0011-schema-validation-policy.md) — Schema validation policy (Accepted; `safe_index` is the canonical decode helper).
- [ADR-0012](./adr/0012-implementation-surface-convention.md) — Implementation surface convention (Accepted; underscore-prefix = unsupported import surface).
- [ADR-0013](./adr/0013-composable-session-capabilities.md) — Composable Session Capabilities (the composable session-capability model).
- [ADR-0014](./adr/0014-feature-local-runtime-adapters.md) — Feature-local runtime adapters (Accepted; features receive direct collaborators instead of `Session`).
- [ADR-0015](./adr/0015-json-envelope-contract-for-post-parse-click-exceptions.md) — Typed JSON error envelope for post-parse CLI failures (Accepted).
- [ADR-0021](./adr/0021-transport-neutral-app-layer.md) — Transport-neutral application layer (`_app/`) (Accepted; boundary enforced by `tests/_guardrails/test_app_boundary.py`, classify↔error_handler agreement by `tests/_guardrails/test_classify_error_handler_consistency.py`).

## See also

- [`CLAUDE.md`](../CLAUDE.md) — quick-start commands, common pitfalls, and the PR workflow for AI agents working in this repo (the per-file index + repository tree now live in [File map](#file-map) above).
- [`docs/development.md`](./development.md) — how to add a new feature API.
- [`docs/refactor-history.md`](./refactor-history.md) — historical narrative of the multi-phase refactor + downstream migration tables.
- [`docs/python-api.md`](./python-api.md) — public Python API surface.
- [`docs/auth-cookie-lifecycle.md`](./auth-cookie-lifecycle.md) — cookie keepalive, rotation, and PSIDTS recovery.
- [`docs/rpc-development.md`](./rpc-development.md) — capturing and debugging new RPCs.
- [`docs/rpc-reference.md`](./rpc-reference.md) — RPC payload structures.
