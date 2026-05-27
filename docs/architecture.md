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
  -> RpcCaller.rpc_call(...) (production: Session.rpc_call)
  -> RpcExecutor.rpc_call(...)
       - pre-open guard via Kernel.get_http_client()
       - logical-RPC request id + rpc_calls_started metric
  -> RpcExecutor._execute_once(...)
       - idempotency policy / client-token injection
       - method-id override resolution, request encoding, URL/body builder
  -> Session._perform_authed_post(...)
  -> SessionTransport.perform_authed_post(...)
       - loop-affinity guard, auth snapshot, RpcRequest materialization
  -> ADR-009 middleware chain
  -> Session._authed_post_chain_terminal(...)
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
| `ArtifactsRuntime` | [`_artifacts.py`](../src/notebooklm/_artifacts.py) | Artifact-feature capability union — composes `RpcCaller` + `AsyncWorkRuntime` + `DrainHookRegistration`. No own members; used by `ArtifactsAPI` for RPC dispatch, loop affinity, operation scopes, and close-time drain-hook registration. The `PollRegistry` lives on `ArtifactsAPI`, not the Protocol. |
| `UploadRuntime` | [`_source_upload.py`](../src/notebooklm/_source_upload.py) | Upload-pipeline capability union — composes `RpcCaller` + `OperationScopeProvider` + `LoopGuard`. The upload semaphore is internal to `SourceUploadPipeline`, not the Protocol. |
| `DrainHookRegistration` | [`_artifacts.py`](../src/notebooklm/_artifacts.py) | Exposes `register_drain_hook(name, hook)` for close-time cleanup. Sole `DrainHookRegistration` after the broad-`Session` Protocol was deleted from `_session_contracts.py` (see the `_session_contracts.py` module docstring). |

`ChatRuntime` was deleted in Wave 8 of the session-decoupling plan
(ADR-014 Rule 2 Corollary). `ChatAPI` now takes its four direct
collaborators (`rpc: RpcCaller`, `transport: SessionTransport`,
`reqid: ReqidCounter`, `loop_guard: LoopGuard`) by keyword-only
constructor argument rather than reaching them through a feature-local
runtime composite.

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
providers. The executor enters transport through
`Session._perform_authed_post`, which forwards to
`SessionTransport.perform_authed_post`; the middleware terminal is
`Session._authed_post_chain_terminal → SessionTransport.terminal → Kernel.post`.
Request types, transport errors, and streaming helpers live in separate owning
modules instead of one catch-all transport helper. This keeps feature APIs on
narrow capability Protocols while avoiding a near-`Session` structural contract
inside the RPC stack.

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
| `RpcExecutor` | [`_rpc_executor.py`](../src/notebooklm/_rpc_executor.py) | Single logical batchexecute RPC dispatch path. Owns request-id/started-metric bracketing, idempotency policy lookup, method-ID resolution, request encoding, response decode, RPC error mapping, and decode-time auth refresh retry. Consumes the `RpcOwner` Protocol declared at module top and enters transport through `Session._perform_authed_post`. |
| `SessionTransport` | [`_session_transport.py`](../src/notebooklm/_session_transport.py) | Authed POST collaborator. Owns `perform_authed_post()` (loop guard, auth snapshot, request materialization, chain dispatch, queue-wait recording), `refresh_request_for_current_auth()`, and `terminal()` (freshness rebuild + `Kernel.post`). Reached through the Session forwards `_perform_authed_post`, `transport_post`, and `_authed_post_chain_terminal`. |
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
[`_auth/`](../src/notebooklm/_auth). The facade still hosts the public
`AuthTokens` name (re-exported from `_auth.tokens`), owns
`load_auth_from_storage()`, and owns the
`_validate_required_cookies()` write-through that propagates
`auth.py`-level policy rebindings into `_auth.cookie_policy` (the flat
re-export goal in ADR-003 is **deferred** — see CLAUDE.md's `auth.py`
row for the current status).

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

## Session as facade

`Session` is large (~780 lines) because it is the historical orchestration
class plus a compatibility facade. The post-v0.5.0 collaborator graph above
shows what `Session` actually delegates today; almost everything it exposes
is a one-line forward to a focused collaborator (`AuthRefreshCoordinator`,
`SessionTransport`, `RpcExecutor`, `ClientLifecycle`, `ClientMetrics`,
`TransportDrainTracker`, `ReqidCounter`, `CookiePersistence`).

The facade survives for three reasons:

1. **Public API stability.** `NotebookLMClient.rpc_call(method, params)`
   forwards to `Session.rpc_call`, and feature APIs satisfy `RpcCaller` via
   `Session`. Removing the facade is a public-surface change.
2. **Compatibility shims.** A handful of `Session` methods
   (`_perform_authed_post`, `transport_post`,
   `_authed_post_chain_terminal`, `update_auth_tokens`) are the
   structural Protocol surface other collaborators or the
   `RefreshAuthCore` Protocol depend on.
3. **Test seams.** Module-level binding paths (e.g.
   `notebooklm._session.asyncio.sleep`, `notebooklm._session.httpx.AsyncClient`)
   are documented monkeypatch targets steered from `Session.__init__`.
   See [ADR-007](./adr/0007-test-monkeypatch-policy.md).

Known follow-up work is to narrow the executor/session callback cycle
further and continue retiring facade-only compatibility surfaces as their
callers move to collaborator-owned contracts.

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
- [ADR-003](./adr/0003-auth-facade-write-through.md) — `auth.py` write-through facade (Superseded by [arch-d1-auth-side](https://github.com/teng-lin/notebooklm-py/pull/834) (#834); flat re-export goal is deferred).
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
