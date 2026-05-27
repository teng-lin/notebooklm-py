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
+----------------------------------------------------------+
                          ▼
+----------------------------------------------------------+
| Session Layer (Session + collaborators)                  |
|   Session orchestrates a small set of focused            |
|   collaborators such as RpcExecutor, SessionTransport,   |
|   and Kernel (see "Collaborator graph" below).           |
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

`NotebookLMClient` is the composition root. It constructs one shared `Session`,
wires feature APIs to narrow runtime Protocols, and injects stateful services
such as `SourceUploadPipeline`, `NoteService`, `NoteBackedMindMapService`, and
`ArtifactDownloadService`. Feature modules build NotebookLM params and parse
domain rows; Session collaborators own dispatch, transport, auth refresh,
metrics, and lifecycle.

### Typed batchexecute RPCs

Most public methods (`client.notebooks.list()`, `client.sources.rename()`,
`client.settings.get()`, artifact generation, note CRUD, etc.) follow this path:

```text
CLI command or user code
  -> NotebookLMClient.<feature>.<method>()
  -> feature API / service builds params and chooses RPCMethod
  -> RpcCaller.rpc_call(...) (production: RpcExecutor wired directly into the feature
                              per ADR-014 Rule 1; NotebookLMClient.rpc_call still
                              forwards through Session.rpc_call for the public escape hatch)
  -> RpcExecutor.rpc_call(...)
       - pre-open guard via Kernel.get_http_client()
       - logical-RPC request id + rpc_calls_started metric
  -> RpcExecutor._execute_once(...)
       - idempotency policy / client-token injection
       - method-id override resolution, request encoding, URL/body builder
  -> SessionTransport.perform_authed_post(...)
       - loop-affinity guard, auth snapshot, RpcRequest materialization
  -> ADR-009 middleware chain
  -> MiddlewareChainHost._authed_post_chain_terminal(...)   # chain leaf (ADR-014 Rule 4)
  -> SessionTransport.terminal(...)
       - final auth-freshness rebuild immediately before POST
  -> Kernel.post(...) -> _streaming_post -> httpx.AsyncClient
  <- RpcExecutor decodes response with rpc.decode_response(...)
  <- feature API maps decoded payload to a typed/domain result
```

`NotebookLMClient.rpc_call(method, params)` is the public raw-RPC escape hatch.
It skips feature-specific param builders and result parsers, but still enters
the same `Session.rpc_call → RpcExecutor.rpc_call → SessionTransport → Kernel`
pipeline.

### Chat ask path

`ChatAPI.ask()` is the major transport-sharing exception to the pure
`RpcExecutor` shape. Streaming chat has a custom request body and chat-flavored
error mapping, so the first ask POST goes through:

```text
ChatAPI.ask(...)
  -> loop_guard.assert_bound_loop(), source-id lookup, conversation lock/cache, reqid.next_reqid()
  -> chat_aware_authed_post(transport, ...)
  -> SessionTransport.perform_authed_post(...)
  -> ADR-009 middleware chain
  -> SessionTransport.terminal(...) -> Kernel.post
  <- streaming chat parser + citation/reference parser
```

After Wave 8 of the session-decoupling plan (ADR-014 Rule 2 Corollary),
`ChatAPI` holds the four collaborators it needs (`rpc`, `transport`,
`reqid`, `loop_guard`) directly — the legacy `ChatRuntime` Protocol
composite and the indirection through `Session.transport_post` are gone.

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
| Artifact generation | `ArtifactGenerationService` builds `CREATE_ARTIFACT` params and uses the normal `rpc_call` path. `ArtifactPollingService` owns leader/follower polling with `operation_scope(...)` and a feature-local `PollRegistry`; `ArtifactsAPI` registers a close-time drain hook for poll cleanup. |
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

**The classification.** Mutating RPCs are classified into six
retry-safety profiles by the `IdempotencyRegistry` in
[`_idempotency.py`](../src/notebooklm/_idempotency.py):

| Policy | Meaning | Effect on the inner retry loop |
|--------|---------|--------------------------------|
| `UNCLASSIFIED` | Placeholder; never classified | Silent, retries enabled (preserves pre-taxonomy behavior) |
| `PROBE_THEN_CREATE` | Caller owns a probe loop; transport must not blind-retry | Force-disable inner retries |
| `IDEMPOTENT_SET_OP` | Server applies set semantics (delete / rename) | Retries are safe; left enabled |
| `CLIENT_TOKEN_DEDUPE` | Server dedupes on an injected token slot | Retries are safe; client-token injected before encoding |
| `AT_LEAST_ONCE_ACCEPTED` | Caller has explicitly accepted duplicate side-effect cost (emails / billing / notifications) | Retries enabled; rate-limited WARN emitted so operators can see the trade-off |
| `NON_IDEMPOTENT_NO_RETRY` | No dedupe key and no probe; first failure must surface | Force-disable inner retries |

The axis is *closed*. A seventh policy would need an ADR update and an
executor change in lockstep — the six-policy cap is intentional so a
reviewer can hold the whole taxonomy in mind during a code review.

`RpcExecutor._execute_once` consults the registry once per call to
resolve the effective `disable_internal_retries` and to inject client
tokens. The caller's explicit `disable_internal_retries=True` always
wins over the registry default.

The audit inventory in
[`_mutating_operations.py`](../src/notebooklm/_mutating_operations.py)
pairs each `PROBE_THEN_CREATE` entry with a `RecoveryKind` —
`EXECUTABLE` (a probe/recovery wrapper exists) or `DISABLE_ONLY` (with a
documented reason). A registry-audit test fails if a new
`PROBE_THEN_CREATE` policy is added without one of those.

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
[`rpc/_safe_index.py`](../src/notebooklm/rpc/_safe_index.py). It
raises a typed shape-drift error by default. Explicit
`NOTEBOOKLM_STRICT_DECODE=0` opts into the temporary legacy soft mode,
where missing indices warn and return `None`. The `RpcExecutor` decode
path narrowly wraps
`json.JSONDecodeError`, `KeyError`, `IndexError`, and `TypeError` into
`RPCError`; other exception types (e.g. `AttributeError`) intentionally
propagate as code bugs rather than being conflated with shape drift.

See [ADR-011](./adr/0011-schema-validation-policy.md).

## Per-capability protocol model

ADR-013 ("Composable Session Capabilities") is the design rationale:
feature APIs depend on narrow capability Protocols rather than on the
concrete `Session` class.
[ADR-014](./adr/0014-feature-local-runtime-adapters.md) extends that
intent at runtime: each feature receives the *collaborator* (for
single-capability Protocols) or a *feature-local frozen-dataclass
adapter* (for composite Protocols) that satisfies its Protocol —
never `Session` itself. `NotebookLMClient.__init__` is the composition
root that wires each feature with the satisfier it needs.

Six Protocols live in
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
| `AsyncWorkRuntime` | Composes `LoopGuard` + `OperationScopeProvider` for features that own async work. No production consumer at present (the artifact polling service now takes the two underlying Protocols directly); retained because the composition rule it pins is still useful documentation. |
| `AuthMetadata` | Selected-account routing metadata — `authuser` + `account_email` properties. Single consumer today: `SourceUploadPipeline`. |
| `Kernel` | Pure transport surface — `post()` method, `cookies` property, `aclose()`. Single consumer today: `SourceUploadPipeline`. |

**Feature-module-local Protocols.** The feature-local composite runtime
unions (`ArtifactsRuntime` in `_artifacts.py`, `UploadRuntime` in
`_source_upload.py`) and the single-consumer capability slice
`DrainHookRegistration` previously listed here were retired together
with their adapter dataclasses (`ArtifactsRuntimeAdapter`,
`UploadRuntimeAdapter`) once it was clear each only hid three stable
collaborators with exactly one production satisfier. `ArtifactsAPI` and
`SourceUploadPipeline` now take their three runtime collaborators
(`rpc: RpcCaller`, `drain: TransportDrainTracker`, `lifecycle:
ClientLifecycle`) by keyword-only constructor argument — mirroring the
`ChatAPI` pattern below.

`ChatRuntime` was deleted earlier in Wave 8 of the session-decoupling
plan (ADR-014 Rule 2 Corollary) on the same grounds. `ChatAPI` takes
its four direct collaborators (`rpc: RpcCaller`, `transport:
SessionTransport`, `reqid: ReqidCounter`, `loop_guard: LoopGuard`) by
keyword-only constructor argument rather than reaching them through a
feature-local runtime composite.

Production satisfies the shared Protocols via the underlying
collaborators (ADR-014 Rule 1: `RpcExecutor` satisfies `RpcCaller`,
`ClientLifecycle` satisfies `LoopGuard`, `TransportDrainTracker`
satisfies `OperationScopeProvider`).
`Session` no longer claims to satisfy the shared Protocols itself.
Tests substitute
[`tests/_fixtures/fake_core.py:FakeSession`](../tests/_fixtures/fake_core.py)
(constructed via `make_fake_core(...)`) — the sanctioned ADR-007 / ADR-013
fixture pattern; tests that inject narrow fakes into a single feature
(e.g. `MagicMock(spec=RpcCaller, rpc_call=AsyncMock(...))`) construct
the feature directly under ADR-014.

### Executor takes its collaborators directly

Per ADR-014 Rule 5, `RpcExecutor` no longer reaches its kernel,
transport, auth-refresh coordinator, or metrics tracker through a
Session-shaped owner Protocol. The earlier `RpcOwner` Protocol — which
re-declared the four private Session attributes the executor needed —
was deleted in Wave 4 of the session-decoupling plan; the executor's
constructor now takes
`kernel: Kernel`, `transport: SessionTransport`,
`auth_refresh: AuthRefreshCoordinator`, and `metrics: ClientMetrics`
as keyword-only parameters, plus the previously-existing
constructor-injected providers for timeout, refresh-callback enablement,
and retry-delay values. The executor enters transport through
`SessionTransport.perform_authed_post` directly; the middleware
terminal remains `MiddlewareChainHost._authed_post_chain_terminal →
SessionTransport.terminal → Kernel.post`. The chain leaf moved off
`Session` onto `MiddlewareChainHost` so the chain owns its own
terminal and retry tunables (ADR-014 Rule 4 chain-ownership
carve-out). Request types, transport errors, and streaming helpers
live in separate owning modules instead of one catch-all transport
helper. This keeps feature APIs on narrow capability Protocols and
the executor on direct collaborator dependencies.

## Post-refactor `Session` collaborator graph

```text
                     +---------------------+
                     |  NotebookLMClient   |
                     +----------+----------+
                                |
                                v
                       +--------+--------+
                       |     Session     |
                       +--------+--------+
                                |
   +-----+-----+-----+-----+-----+----+-----+-----+-----+-----+
   |     |     |     |     |     |    |     |     |     |
   v     v     v     v     v     v    v     v     v     v
Rpc-  Auth-  Client- Mid-  Sess- Trans- Metrics Reqid Cookie- Kernel
Exec  Ref    Life    Chain Trans Drain  Tracker Coun  Pers
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
   +--- single logical RPC dispatch path (RpcExecutor.rpc_call → _execute_once
   |    → SessionTransport.perform_authed_post → chain
   |    → SessionTransport.terminal → Kernel → httpx)
   |
   +--- Kernel (transport core; owns httpx.AsyncClient + cookie jar)
```

| Collaborator | Module | Responsibility |
|--------------|--------|----------------|
| `RpcExecutor` | [`_rpc_executor.py`](../src/notebooklm/_rpc_executor.py) | Single logical batchexecute RPC dispatch path. Owns request-id/started-metric bracketing, idempotency policy lookup, method-ID resolution, request encoding, response decode, RPC error mapping, and decode-time auth refresh retry. Takes its `Kernel`, `SessionTransport`, `AuthRefreshCoordinator`, and `ClientMetrics` collaborators directly via keyword-only constructor parameters (ADR-014 Rule 5; the historical `RpcOwner` Protocol was deleted in Wave 4 of session-decoupling). Enters transport through `SessionTransport.perform_authed_post`. |
| `SessionTransport` | [`_session_transport.py`](../src/notebooklm/_session_transport.py) | Authed POST collaborator. Owns `perform_authed_post()` (loop guard, auth snapshot, request materialization, chain dispatch, queue-wait recording), `refresh_request_for_current_auth()`, and `terminal()` (freshness rebuild + `Kernel.post`). Called directly by `RpcExecutor` and by `chat_aware_authed_post` (ChatAPI's chat-flavoured transport call); the middleware chain leaf at `MiddlewareChainHost._authed_post_chain_terminal` continues to dispatch through `SessionTransport.terminal` per ADR-014 Rule 4. |
| `MiddlewareChainHost` | [`_middleware_chain_host.py`](../src/notebooklm/_middleware_chain_host.py) | Owns the wired middleware chain (`_authed_post_chain`), the chain leaf (`_authed_post_chain_terminal`), the three retry-budget tunables (`_rate_limit_max_retries`, `_server_error_max_retries`, `_refresh_retry_delay`), and the dynamic `await_refresh` delegate that the auth-refresh middleware captures. The chain's provider lambdas and the transport's `chain_provider` closure read the host's attributes live, so post-construction mutation (e.g. tests setting `core._chain_host._rate_limit_max_retries = 0`) still steers the live chain. `Session` references the host as `self._chain_host` but exposes no Session-side aliases. |
| `AuthRefreshCoordinator` | [`_session_auth.py`](../src/notebooklm/_session_auth.py) | Owns the auth-snapshot lock and the refresh task. Canonical implementation for `AuthRefreshCoordinator.snapshot(host)` and token updates. `Session.update_auth_tokens()` remains a one-line delegate for the `RefreshAuthCore` Protocol; the old `Session._snapshot` delegate was inlined. |
| `ClientLifecycle` | [`_session_lifecycle.py`](../src/notebooklm/_session_lifecycle.py) | HTTP-client open/close, keepalive task, cookie save coordination. Holds `_timeout`, `_bound_loop`, `_http_client`, `_keepalive_*`. |
| `MiddlewareChainBuilder` | [`_middleware_chain.py`](../src/notebooklm/_middleware_chain.py) | Constructs the middleware chain in the canonical ADR-009 order. Extracted in Phase 3 PR 7. |
| `TransportDrainTracker` | [`_transport_drain.py`](../src/notebooklm/_transport_drain.py) | Tracks in-flight transport operations + the drain condition variable. Gates graceful shutdown. |
| `ClientMetrics` | [`_client_metrics.py`](../src/notebooklm/_client_metrics.py) | Per-instance counters (`ClientMetricsSnapshot`) + the `on_rpc_event` user callback. |
| `ReqidCounter` | [`_reqid_counter.py`](../src/notebooklm/_reqid_counter.py) | Monotonic `_reqid` for the chat backend; lock-protected `await core.next_reqid()`. |
| `CookiePersistence` | [`_cookie_persistence.py`](../src/notebooklm/_cookie_persistence.py) | Cookie-jar persistence + `__Secure-1PSIDTS` rotation. |
| `IdempotencyRegistry` | [`_idempotency.py`](../src/notebooklm/_idempotency.py) | Policy/classification registry keyed by `(RPCMethod, operation_variant)`. `RpcExecutor._execute_once()` consults it to resolve `effective_disable_internal_retries` and to inject client tokens for `CLIENT_TOKEN_DEDUPE` methods (most entries are currently `UNCLASSIFIED`, a behaviour-neutral default). Not Session-owned, but part of the RPC dispatch path. Side-effect probing (`idempotent_create(...)`) is a separate mechanism not owned by this registry. |
| `_request_types` | [`_request_types.py`](../src/notebooklm/_request_types.py) | Owns `AuthSnapshot`, `BuildRequest`, and request materialization shapes shared by RPC, chat, auth refresh, and the chain terminal. |
| `_transport_errors` | [`_transport_errors.py`](../src/notebooklm/_transport_errors.py) | Owns transport-level exceptions, `Retry-After` parsing, and raw `Kernel.post` error mapping consumed by `RetryMiddleware` and `AuthRefreshMiddleware`. |
| `_streaming_post` | [`_streaming_post.py`](../src/notebooklm/_streaming_post.py) | Low-level streaming POST helper with the response-size cap used by `Kernel.post`. |
| `Kernel` | [`_kernel.py`](../src/notebooklm/_kernel.py) | Pure transport core. Owns the `httpx.AsyncClient` and cookie jar; exposes `post()`, the `cookies` property, and `aclose()` (the close path wraps it in `asyncio.shield` from `ClientLifecycle.close()`). Concrete class behind the `Kernel` Protocol in `_session_contracts.py`; constructed by `Session.__init__()` and called from the middleware leaf via `SessionTransport.terminal → Kernel.post`. |
| `_session_init` | [`_session_init.py`](../src/notebooklm/_session_init.py) | Construction-time helpers extracted from `Session.__init__`: `validate_constructor_args` (kwarg validation/normalization), `build_collaborators` (the 8 collaborators in dependency order), `build_session_transport`, and `wire_middleware_chain`. Lets `Session.__init__` stay short while keeping the seam-resolution boundary documented (`None`-default resolution for `sleep` / `async_client_factory` stays in `_session.py` so the documented monkeypatch paths still steer construction). |
| `_loop_affinity` | [`_loop_affinity.py`](../src/notebooklm/_loop_affinity.py) | Tiny free-function `assert_bound_loop(bound_loop)` shared by every helper that captures a loop reference at `open()` time (`TransportDrainTracker`, `ReqidCounter`, `AuthRefreshCoordinator`, `ArtifactPollingService`, `ChatAPI`). Module-private on purpose so those helpers can guard without importing `Session`. Enforces ADR-004. |

## Domain-service collaborators

Beyond the Session-orchestration graph, several feature APIs are implemented via dedicated domain services and helper modules:

| Service / Module | Module | Responsibility |
|-------------------|--------|----------------|
| `NoteService` | [`_note_service.py`](../src/notebooklm/_note_service.py) | Service layer managing note CRUD, note-backed content generation, and sync. |
| `NoteBackedMindMapService` | [`_mind_map.py`](../src/notebooklm/_mind_map.py) | Specific adapter service representing mind-maps, backed by standard notebook notes. |
| `ArtifactDownloadService` | [`_artifact_downloads.py`](../src/notebooklm/_artifact_downloads.py) | Asynchronous download coordinator for finished artifacts. |
| `_artifact_formatters` | [`_artifact_formatters.py`](../src/notebooklm/_artifact_formatters.py) | Markdown, HTML, and plain text formatters for artifacts. |
| `_artifact_listing` | [`_artifact_listing.py`](../src/notebooklm/_artifact_listing.py) | Listing and filtering operations for notebook artifacts. |
| `_row_adapters` | [`_row_adapters.py`](../src/notebooklm/_row_adapters.py) | Wire-shape adapters that wrap raw batchexecute rows (`ArtifactRow`, etc.) behind named accessors so downloads, polling, and listing don't open-code positional indices. Soft-degrade and strict-mode behavior is pinned in `tests/unit/test_row_adapters.py`. |
| `_research_task_parser` | [`_research_task_parser.py`](../src/notebooklm/_research_task_parser.py) | Parses deep-research task results from raw rows. Returns dict-shaped output today; a typed-model migration is not yet complete. |
| `_mutating_operations` | [`_mutating_operations.py`](../src/notebooklm/_mutating_operations.py) | Audit inventory binding each `PROBE_THEN_CREATE` registry entry to a `RecoveryKind` (`EXECUTABLE` or `DISABLE_ONLY`) with a reason. Cross-checked by the registry-audit unit test so a new `PROBE_THEN_CREATE` policy cannot land without either a recovery wrapper or a documented disable-only justification. |
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
| [`_auth/session.py`](../src/notebooklm/_auth/session.py) | `RefreshAuthCore` Protocol + `refresh_auth_session()` implementation called by `AuthRefreshCoordinator`. |
| [`_auth/refresh.py`](../src/notebooklm/_auth/refresh.py) | Token refresh driver (external login command, coalesced runs, secret redaction). |
| [`_auth/keepalive.py`](../src/notebooklm/_auth/keepalive.py) | Cookie keepalive + `__Secure-1PSIDTS` rotation. |
| [`_auth/psidts_recovery.py`](../src/notebooklm/_auth/psidts_recovery.py) | Inline PSIDTS recovery for cold-start (see issue #865). |

The cookie lifecycle — what gets written, who rotates, what the
keepalive contract is — is documented separately in
[`docs/auth-cookie-lifecycle.md`](./auth-cookie-lifecycle.md).

## CLI layer (ADR-008)

The CLI is intentionally a thin adapter. Click commands in
[`src/notebooklm/cli/*_cmd.py`](../src/notebooklm/cli) own argument
parsing, user-visible rendering, JSON envelopes, and exit codes;
business logic lives in
[`src/notebooklm/cli/services/`](../src/notebooklm/cli/services). This
separation is the [ADR-008](./adr/0008-cli-services-extraction-pattern.md)
extraction pattern.

| Layer | Owns | Does NOT own |
|-------|------|--------------|
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
assembler entry point is
[`notebooklm_cli.py`](../src/notebooklm/notebooklm_cli.py), which
imports each command group and registers it on the root Click group.

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
Authed POST leaf             (SessionTransport.terminal → Kernel → httpx)
```

## Session as lifecycle root

`Session` is no longer a compatibility facade. Waves 5 + 11 of the
session-decoupling plan
([ADR-014](./adr/0014-feature-local-runtime-adapters.md)) deleted the
historical drain/metrics/operation_scope/kernel/authuser/save_cookies
forwards that existed only because feature APIs used to reach through
`Session`. What remains is a narrow lifecycle root: `Session` constructs
the collaborator graph at `__init__` time, owns the open/close lifecycle
(loop-affinity binding, keepalive task), and exposes the few surfaces
that remain load-bearing for the public API or the middleware chain. The
exact retention list is checked-in at
[`docs/session-method-retention.md`](./session-method-retention.md) and
enforced by [`tests/_lint/test_session_retention.py`](../tests/_lint/test_session_retention.py)
— a new method on `Session` cannot land without a documented
disposition.

Concretely, `Session` retains:

1. **Public-API forward.** `Session.rpc_call(method, params)` is
   pinned by `tests/unit/test_public_shims.py` because
   `NotebookLMClient.rpc_call` (the documented raw-RPC escape hatch)
   forwards through it. Internally it delegates to
   `self.rpc_executor.rpc_call(...)`.
2. **Stage-A collaborator accessors** (ADR-014 Rule 3 Stage A —
   transitional). `Session.collaborators`, `Session.session_transport`,
   and `Session.rpc_executor` are the three typed accessors
   `NotebookLMClient.__init__` reads while wiring features. They are
   lint-guarded to the composition root + tests by
   [`tests/_lint/test_client_composition.py`](../tests/_lint/test_client_composition.py)
   so they cannot become a discoverability hub. Stage B (Wave 7
   follow-up) moves `build_collaborators` ownership to `NotebookLMClient`
   and deletes all three accessors.
3. **Middleware-chain seams.** The chain leaf
   (`_authed_post_chain_terminal`), the chain slot (`_authed_post_chain`),
   the dynamic refresh delegate (`await_refresh`), and the three
   retry-budget tunables (`_rate_limit_max_retries`,
   `_server_error_max_retries`, `_refresh_retry_delay`) all live on
   `MiddlewareChainHost` after the chain-ownership carve-out (ADR-014
   Rule 4). `wire_middleware_chain` and `build_session_transport` take
   `chain_host: MiddlewareChainHost` directly and read the host
   attributes live; tests rebind through `core._chain_host._<attr>`.
   The only middleware-chain capture target that remains on `Session`
   is `assert_bound_loop`, which is reached as `host.assert_bound_loop`
   from `build_session_transport`'s `bound_loop_check` lambda.
4. **Lifecycle methods.** `open`, `close`, `is_open`, `_keepalive_loop`,
   and `assert_bound_loop` (now a one-line forward to
   `ClientLifecycle.assert_bound_loop` since
   `ClientLifecycle` satisfies the `LoopGuard` Protocol directly).
5. **AST-guarded auth surface.** `update_auth_tokens` is asserted by
   `tests/unit/test_concurrency_refresh_race.py`.

Feature APIs do **not** receive `Session`. They receive the
collaborator (`RpcExecutor` for `RpcCaller`, `ClientLifecycle` for
`LoopGuard`, `TransportDrainTracker` for `OperationScopeProvider` /
`register_drain_hook`) per ADR-014 Rules 1 + 3. Features that need
more than one capability — `ChatAPI`, `ArtifactsAPI`, and
`SourceUploadPipeline` — take each collaborator by keyword-only
constructor argument; the feature-local composite-runtime adapter
dataclasses that previously bundled three collaborators apiece were
retired once it was clear they only hid three stable collaborators
with one production satisfier. The composition wiring is in
[`client.py`](../src/notebooklm/client.py).

## Testing patterns

Two policies define how tests interact with the architecture above.

### Constructor-injection fixtures (ADR-007)

The forbidden patterns are `monkeypatch.setattr("notebooklm.…")` against
module-level seams and direct attribute assignment like
`core.rpc_call = AsyncMock(...)`. The sanctioned substitute is
[`tests/_fixtures/fake_core.py:make_fake_core(...)`](../tests/_fixtures/fake_core.py),
which returns a `FakeSession` configured to satisfy the narrow capability
Protocols a feature actually consumes (`RpcCaller`, `LoopGuard`,
`OperationScopeProvider`, `AuthMetadata`, `Kernel`, plus feature-local
runtimes like `ArtifactsRuntime` / `UploadRuntime`). `ChatAPI` no
longer uses a feature-local runtime (Wave 8 of session-decoupling,
ADR-014 Rule 2 Corollary) — chat unit tests inject narrow
`MagicMock(spec=RpcCaller, rpc_call=AsyncMock(...))`-style fakes
directly via the keyword-only constructor.

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

A fuller taxonomy is in
[`docs/test-suite-taxonomy-inventory.md`](./test-suite-taxonomy-inventory.md).

## Implementation surface convention (ADR-012)

`notebooklm-py` keeps a small set of public-named modules (`auth.py`,
`client.py`, `config.py`, `exceptions.py`, `io.py`, `log.py`,
`migration.py`, `notebooklm_cli.py`, `paths.py`, `research.py`,
`types.py`, `urls.py`, `utils.py`) and routes everything else through
underscore-prefixed seam modules. Anything underscored is *not* a
supported import surface; it can be moved, renamed, or deleted without a
deprecation cycle. See [ADR-012](./adr/0012-implementation-surface-convention.md).

The corollary for contributors: if you find yourself reaching into
`notebooklm._foo`, prefer a capability Protocol or a public function in
one of the named modules.

## Boundary moratorium

New architectural carve-outs are expensive: every ADR amendment,
[`session-method-retention.md`](./session-method-retention.md) entry,
and `tests/_lint/` pin becomes load-bearing for contributors who have
to read the docs before touching the relevant seam. To keep that
surface from drifting upward without bound, the following discipline
applies to any future change that would *expand* the documented
boundary set:

- **Justify by failure mode.** A new ADR amendment,
  [`session-method-retention.md`](./session-method-retention.md) row,
  or `tests/_lint/` pin must cite a concrete user-visible failure mode
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
| Capability Protocol | A narrow structural `Protocol` (e.g. `RpcCaller`, `LoopGuard`) a feature depends on instead of taking a concrete `Session`. See [ADR-013](./adr/0013-composable-session-capabilities.md). |
| Chain / leaf / terminal | The middleware chain's ordering vocabulary. The chain wraps outermost-first; the **leaf** is the innermost middleware (`TracingMiddleware`); the **terminal** is the authed-POST function (`SessionTransport.terminal → Kernel.post`) that ends the chain. |
| Drain | Graceful-shutdown waiting on in-flight transport operations to complete. Owned by `TransportDrainTracker` and admitted by `DrainMiddleware`. |
| `idempotent_create(...)` | Caller-owned probe-then-create wrapper used by source-add / Drive-add flows. Distinct from the `IdempotencyRegistry` (which only classifies retry safety inside the executor). |
| `operation_variant` | Optional kwarg on `rpc_call(...)` that selects a method-variant-specific idempotency policy from the registry (e.g. `ADD_SOURCE` `"url"` vs `"drive"`). Unknown variants raise `IdempotencyVariantError`. |
| RPC method id | A short obfuscated identifier (`rpcids=`) Google uses to route batchexecute calls. Source of truth: `RPCMethod` enum in `rpc/types.py`. |
| Snapshot | An `AuthSnapshot` (see [`_request_types.py`](../src/notebooklm/_request_types.py)) — an immutable, point-in-time view of session id, CSRF token, authuser, and account email. Taken inside the auth-snapshot lock so a refresh racing with a transport build cannot tear. |

## ADR cross-references

- [ADR-001](./adr/0001-layered-core-seams-and-property-bridge-policy.md) — Layered seams + property-bridge policy (superseded; shims retired).
- [ADR-002](./adr/0002-capability-protocol-pattern.md) — Capability Protocol pattern (Superseded by [arch-d2-cutover](https://github.com/teng-lin/notebooklm-py/pull/835) (#835)).
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

## See also

- [`CLAUDE.md`](../CLAUDE.md) — high-level navigation map for AI agents working in this repo, including the full file index.
- [`docs/development.md`](./development.md) — how to add a new feature API.
- [`docs/refactor-history.md`](./refactor-history.md) — historical narrative of the multi-phase refactor + downstream migration tables.
- [`docs/python-api.md`](./python-api.md) — public Python API surface.
- [`docs/auth-cookie-lifecycle.md`](./auth-cookie-lifecycle.md) — cookie keepalive, rotation, and PSIDTS recovery.
- [`docs/rpc-development.md`](./rpc-development.md) — capturing and debugging new RPCs.
- [`docs/rpc-reference.md`](./rpc-reference.md) — RPC payload structures.
