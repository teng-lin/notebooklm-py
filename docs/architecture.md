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
|     .sharing    .labels    .collections                  |
+----------------------------------------------------------+
                            ▼
+----------------------------------------------------------+
| Web Backend + Runtime Layer                              |
|   WebRpcBackend owns WebExecutionRuntime and the         |
|   auth/lifecycle/metrics/transport leaves.               |
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

### Semantic backend boundary

[ADR-0035](./adr/0035-semantic-backend-boundary.md) established the private semantic boundary
below the client/feature facades. P7 completed the runtime collapse behind the single web backend;
there is no parallel legacy backend:

```text
CLI / MCP / REST
        |
        v
_app workflows                         frontend-neutral
        |
        v
NotebookLMClient + feature facades     compatibility boundary
        |
        v
semantic services                      typed operation input/output
        |
        v
BackendAdapter                         protocol-neutral port
        |
        v
web binding + codec                    RPC IDs, arrays, cookies, web errors
        |
        v
WebExecutionRuntime + web transport
```

The dependency direction is one-way: codecs build private records, projectors build existing
public models, and semantic services never import RPC, HTTP, auth, or adapter vocabulary. The
immutable exported structured-document value graph is the one explicit projection exception:
web codecs may construct it through its validating constructors because it contains no positional
or protocol knowledge and owns the shared UTF-16 offset/rendering invariants.

The migration runs P0 through P8, with the runtime collapse in P7 after feature domains have moved
and the web cookie-provider extraction in P8 after P7. P3's codec/model separation is approved;
it reuses the current strict row-adapter and wire-contract evidence rather than renaming it for its
own sake. P0's catalog and contract evidence are implemented and frozen. P1 also constructs the
private `WebRpcBackend` at the shared client-assembly seam and registers typed handlers for
the P2.1 notebook/source reads, P2.2 notebook mutations, P2.3 URL/YouTube registration, P5.1 Studio
catalog reads, P5.2 Audio, P5.3 Quiz/Flashcards, P5.4 Report/Video, P5.5
Infographic/Slide Deck generation, P5.6 Data Table/Mind Map generation and Drive export,
P5.7 Studio representation retrieval/serialization boundaries, P5.8 artifact management,
lifecycle polling, suggestions, and representation dispatch, P6.1 Chat, P6.2 Research, P6.3
note/mind-map workflows, P6.4 source-label/collection operations, P6.5 Sharing, P6.6
settings/suggestions, and P6.7 source variants. These public paths delegate through
transport-neutral semantic services and that client-owned backend. Audio discovery and download
selection reuse the Studio catalog rather than adding a second listing authority. The URL-source
handler owns ordinary and hidden YouTube dispatch, exact pre-create reconciliation, and best-effort
title mutation. It honors one caller-supplied absolute semantic deadline when present; the public
`wait_timeout` remains the readiness-poll budget and starts after creation. P3 routes the live notebook, source,
artifact, Chat, label, collection, sharing, and Research response grammars through private records and compatibility
projectors; GET_SOURCE structured documents retain their explicit transport-neutral value exemption.
The retained public parsing factories remain callable but have no production callers. P4 converges
the active bindings: whole-workflow `CallPolicy` values, exact native idempotency expectations,
caller-owned absolute deadline identity, and closed public-error projection are audited together
without moving retry authority out of the native registry. Future operation migrations must extend
that same ledger; P5.1 through P5.8, P6.1–P6.7, and the final P7 notebook/share entry slice extend
it to all 82 active handlers. P0–P8 are complete. P8 places an immutable
`WebCookieGeneration`/`WebCookieProvider` port between the web session and the existing auth
owners; public-surface work and a mobile backend require separate decisions. A P9 web-backend
decomposition is proposed in the plan and not started.

The operation-catalog audit classifies only the shared generic web RPC forwarder as inert. The four
notebook/source read handlers, three notebook-mutation handlers, URL-source composite, two Studio
catalog handlers, nine family generation handlers, one Drive-export handler, four Research handlers, five plain-note
handlers, six mind-map handlers, eleven source-label/collection handlers, four Sharing handlers,
five settings/suggestion handlers, ten remaining-source handlers, six Chat handlers, and the final
notebook/share handlers are active catalogued authorities. `MindMapsAPI` delegates to the semantic note and Studio mind-map
services; retained
saved-chat/artifact compatibility seams remain explicitly named legacy RPC consumers and never
borrow NOTE_* authority. This bounded classification is mutation-tested and shrinks as later slices
activate handlers.

P0 adds four ADR-0022 contract baselines before runtime delegation:

| Baseline | Freezes |
| --- | --- |
| `operation_catalog` | 87 operations with 173 exact authority rows (46 multi-authority); 56 native rows (19 multi-site, four honestly `not_recorded` goldens) with variant-specific evidence and per-binding override proof; the exact 82-operation active web policy/native-idempotency ledger; 146 namespace methods (eight local-only), ten root-client members, and 12 divergences (11 authority, one policy) |
| `public_model_contract` | The 86 exported identities (50 dataclasses, 36 enums): construction, field/member order, behavior flags, export paths, structured pickle success/failure, first-party state hooks, and `Notebook` / `ChatReference` legacy-state restore invariants |
| `json_envelope` | Exact sink/view-backed projection modes, keys, causal fields, and conditional variants: CLI 31 model identities/133 projections, MCP 32/123, REST 32/57 (313 unique ids). Its closed-world sink inventory covers 350 terminal/error sites: 225 public-projection, 117 reviewed non-public, eight forwarding infrastructure, and 15 conditional non-public variants across 14 sites. Every live id has a terminal allocation; registrations/direct JSON bypasses fail closed. It also pins 36 private DTO -> public dataclass paths (32 linked; `SourceRefreshResult.result` production-dead; `ValidatedSessionConfig.limits` internal-runtime-only; two `StructuredDocument` paths deliberately dropped by scalar source-content adapters), 16 explicit helper fingerprints, and a compact aggregate digest for the bounded 521-node / 1,245-edge transitive helper graph (522 unique helpers overall). Thirty-seven declarations across 28 literal final-dict sites are AST-derived, while 168 explicit declarations remain manually reviewed. The supplemental 49-dataclass inventory excludes `AuthTokens`; only the exact redacted MCP/REST `server_info` identity contributions are allowed. `authuser` / `account_email` may emit, while storage path/profile generation only select control flow; recursive credentials and any extra projection fail closed. |
| `metrics_contract` | The 14 snapshot and five event fields plus normalized success/transport-error/decode-error observations through composed public `rpc_call()` / `metrics_snapshot()`; direct non-RPC middleware probes are supplemental |

`_app/` remains governed by ADR-0021 and never imports the private backend or deadline type.
The internal use of the exported generation-retry helper moves behind an artifact facade in P4.2
while the public helper remains available and `_app` retains planning, optional wait dispatch,
progress, and result projection. Artifact and source waiting already delegate polling to their
facades. Download selection/conflict and multi-item composition stay in `_app`, with each
list/download call treated as its own semantic operation. Pagination remains a pure returned-list
slice; protocol pagination support belongs to backend capability metadata.

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

`NotebookLMClient` is the public shell; `_client_composition.py` is the composition boundary. It
constructs the backend-owned runtime leaves, wires feature APIs to narrow runtime Protocols, and
injects stateful services such as `SourceUploadPipeline`, `NoteService`,
`MindMapFamilyService`, and the private Studio family/representation services. Feature modules
build semantic inputs and project domain values; `WebRpcBackend` owns
dispatch, transport, auth refresh, metrics, and lifecycle leaves.

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
|   facade validates and invokes a typed semantic service        |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| BackendAdapter.invoke(OperationDef, typed input, deadline)     |
|   WebRpcBackend selects one registered handler + web codec      |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| WebExecutionRuntime.rpc_call(...)                              |
|   - pre-open guard via Kernel.get_http_client()                |
|   - logical-RPC request id + rpc_calls_started metric          |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| WebExecutionRuntime._execute_once(...)                         |
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
| ADR-0009 middleware chain                                      |
|   Drain -> Metrics -> Sema -> Retry -> AuthRefresh             |
|   -> Tracing                                                   |
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
| WebExecutionRuntime decodes via rpc.decode_response(...)       |
| Web codec -> neutral record -> public compatibility projector  |
+----------------------------------------------------------------+
```

Production feature services depend on `BackendAdapter`, never on the web wire or the execution
runtime. `WebRpcBackend` owns the one `WebExecutionRuntime` and delegates registered semantic
handlers through it. `_rpc_executor.py::RpcExecutor` is a behaviorless compatibility subclass; it
does not retain a second encode/dispatch/decode authority.

`NotebookLMClient.rpc_call(method, params)` is the public raw-RPC escape hatch.
It skips feature-specific param builders and result parsers, but still enters
the same `WebRpcBackend.public_rpc_call → WebExecutionRuntime → RuntimeTransport → Kernel`
pipeline. The client does not retain an executor attribute.

### Chat ask path

`ChatAPI.ask()` is the major transport-sharing exception to the unary semantic
runtime shape. Streaming chat has a custom request body and chat-flavored
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
| ADR-0009 middleware chain                                      |
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

For a new conversation, `ChatAPI.ask()` then invokes the typed
`chat.get_conversation_id` operation. Other unary chat methods likewise use the semantic
service/backend path.

### Uploads, downloads, and polling

Some feature workflows intentionally combine RPC with non-RPC HTTP work:

| Flow | Runtime shape |
|------|---------------|
| Source file upload | `SourcesAPI.add_file()` delegates to `SourceUploadPipeline.add_file()`. The pipeline opens an `operation_scope`, takes its own upload semaphore, registers the file source through `runtime.rpc_call(ADD_SOURCE_FILE)`, then uses a dedicated `httpx.AsyncClient` and live Kernel cookies for the Scotty resumable-upload start/finalize calls. Optional wait/rename steps return to `rpc_call`. |
| Source URL/text/Drive add | `SourceAddService` wraps URL and Drive mutating RPCs in `idempotent_create(...)` because those flows have stable probes. Text-source adds are intentionally non-idempotent unless the caller handles dedupe externally. |
| Artifact generation | P5.2–P5.6 route every family kickoff through transport-neutral family services and the typed web backend while preserving established payload builders and public `GenerationStatus`. P5.8 routes revision, retry, rename, delete, suggestions, lifecycle status reads, and representation discovery through typed Studio services; `_artifact/generation.py` and `_artifact/downloads.py` retain import-compatible helper exports only and own no native RPC authority. `ArtifactLifecycleService` composes the existing `ArtifactPollingService`, `operation_scope(...)`, and feature-local `PollRegistry`, so public `wait_for_completion()` remains lifecycle-terminal; family-usable readiness does not alter that wait condition. |
| Artifact download | P5.8 routes every family through `ArtifactRepresentationService` and typed `artifact.download` catalog/content actions. It delegates remote bytes to `StudioDownloadClient` and local report/interactive/table/map formats to `StudioSerializationClient`, preserving storage cookies, trusted-host checks, per-hop redirect validation, exact latest-created selection, and the explicit prefetched no-refetch path. |
| Notes and mind maps | Backend-neutral `NoteService` invokes typed NOTE_* operations for `NotesAPI` and note-backed MIND_MAP_* workflows for `MindMapsAPI`; `MindMapFamilyService` owns its interactive Studio branch. `WebRpcBackend` owns the six typed mind-map bindings and mixed note-row decoding. `LegacyNoteBackedService` remains bounded to deferred saved-chat/artifact compatibility callers and is absent from `MindMapsAPI`. |

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

The axis is *closed*. A sixth policy would need an ADR update and a web-runtime
change in lockstep — the five-policy cap is intentional so a
reviewer can hold the whole taxonomy in mind during a code review.

`WebExecutionRuntime._execute_once` consults the registry once per call to
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
`WebExecutionRuntime` decode path narrowly wraps
`json.JSONDecodeError`, `KeyError`, `IndexError`, and `TypeError` into
`RPCError`; other exception types (e.g. `AttributeError`) intentionally
propagate as code bugs rather than being conflated with shape drift.

See [ADR-0011](./adr/0011-schema-validation-policy.md).

## Per-capability protocol model

ADR-0013 ("Composable Session Capabilities") is the design rationale:
feature APIs depend on narrow capability Protocols rather than on the
deleted concrete `Session` class.
[ADR-0014](./adr/0014-feature-local-runtime-adapters.md) extended that
intent at runtime. P7 superseded its client-owned executor/holder graph:
semantic features now receive `BackendAdapter` or focused collaborators,
and `_client_composition.py` atomically constructs the backend-owned runtime.

Three shared Protocols live in
[`_runtime/contracts.py`](../src/notebooklm/_runtime/contracts.py):
`Kernel`, `RpcCaller`, and `LoopGuard`. `RpcCaller` remains only for the bounded
`LegacyNoteBackedService` compatibility implementation and the behaviorless raw-RPC adapter;
semantic services may not consume it. `LoopGuard` meets ADR-0013's
"shared by at least two features" promotion bar.
`Kernel` remains shared because it is the typed transport surface
implemented by the concrete backend-owned kernel and consumed by the
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
| `RpcCaller` | Compatibility shape for the bounded legacy note-backed implementation and raw web runtime adapter; semantic services must use `BackendAdapter`. |
| `LoopGuard` | Exposes `assert_bound_loop()` — single-method cross-loop affinity check; consumed by anything that may touch the HTTP client. |
| `Kernel` | Pure transport surface — `post()` method, `cookies` property, `aclose()`. Single consumer today: `SourceUploadPipeline`. |

**Feature-module-local Protocols.** Single-consumer capability shapes live
next to their owner (`AuthMetadata` in `_source/upload.py`,
`OperationScopeProvider` in `_artifact/polling.py`). No feature-local
composite-runtime unions or adapter dataclasses exist in production. Every
multi-capability feature takes its collaborators by keyword-only
constructor argument:

- `ArtifactsAPI` takes the client-owned `BackendAdapter` plus
  `drain: TransportDrainTracker` and `lifecycle: ClientLifecycle`; its deprecated
  `rpc=` constructor keyword remains only for source-compatible manual construction.
- `SourceUploadPipeline` takes transport-neutral registration/list/rename callbacks plus its upload collaborators; its deprecated `rpc=` keyword is ignored for source compatibility.
- `ChatAPI` takes `backend: BackendAdapter` plus focused streamed-transport,
  request-id, and loop-guard collaborators.

Production satisfies shared Protocols via the underlying collaborators
(`WebExecutionRuntime` satisfies the retained compatibility `RpcCaller`,
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

### Backend-owned execution runtime takes its collaborators directly

`WebExecutionRuntime` takes its kernel, transport,
auth-refresh coordinator, and metrics tracker directly — there is no
Session-shaped owner Protocol. The constructor takes
`kernel: Kernel`, `transport: RuntimeTransport`,
`auth_refresh: AuthRefreshCoordinator`, and `metrics: ClientMetrics`
as keyword-only parameters, plus constructor-injected providers for
timeout, refresh-callback enablement, and retry-delay values. The
runtime enters transport through
`RuntimeTransport.perform_authed_post` directly. `RuntimePipeline` composes the
fixed behavior order once and ends at
`RuntimeTransport.terminal → Kernel.post`; there is no mutable chain host,
construction-only builder, or retained middleware list. Request types,
transport errors, and streaming helpers live in separate owning
modules. `_rpc_executor.py::RpcExecutor` inherits this runtime only as an import-compatible,
behaviorless name.

## Backend-owned runtime collaborator graph

```text
NotebookLMClient
  | owns feature facades + one _backend + one _provider reference
  |                    |
  |                    -> RuntimeWebCookieProvider
  |                       -> provider Kernel + ClientLifecycle
  |                       -> existing auth storage/refresh/recovery/persistence owners
  |                       -> immutable WebCookieGeneration
  v
WebRpcBackend (receives the provider port, never profile storage)
  | owns WebExecutionRuntime, WebCookieSession, drain, metrics,
  | request-id counter, RPC semaphore, RuntimeTransport,
  | and the immutable RuntimePipeline
  | -> WebBackendSession -> a distinct private backend Kernel
  v
WebExecutionRuntime.rpc_call
  -> RuntimeTransport.perform_authed_post
  -> provider.generation -> backend Kernel.install_generation (private clone)
  -> Drain -> Metrics -> Semaphore -> Retry -> AuthRefresh -> Tracing
  -> RuntimeTransport.terminal -> backend Kernel.post -> httpx
```

`compose_client_internals()` creates every leaf in dependency order and returns a frozen
`ClientInternals` construction receipt. `_client_composition.py` immediately consumes that receipt
while constructing `WebRpcBackend`; neither the client nor the backend retains the receipt or
pipeline internals. The deleted `ClientComposed`/`RuntimeCollaborators` graph has no bind/reset or
post-construction mutation replacement.

| Collaborator | Module | Responsibility |
|--------------|--------|----------------|
| `NotebookLMClient` | [`client.py`](../src/notebooklm/client.py) | Public shell. Owns `_backend`, `_provider`, and the eleven feature facade attributes. Auth, refresh, and account methods delegate to the provider; lifecycle, metrics, and raw RPC delegate to the backend. It retains no credential implementation, test-only construction seam, or protocol-runtime duplicate. |
| `WebCookieGeneration` / `WebCookieProvider` | [`_web_cookie_provider.py`](../src/notebooklm/_web_cookie_provider.py) | Frozen cookie/token/account-route value and the narrow provider port. The value copies its immutable `CookieJar` and redacts every credential field from repr; the port exposes whole generation/refresh/account/lifecycle transactions, never profile paths, lock primitives, or browser drivers. |
| `RuntimeWebCookieProvider` | [`_runtime/web_cookie_provider.py`](../src/notebooklm/_runtime/web_cookie_provider.py) | Concrete compatibility adapter and generation authority. It owns the existing acquisition `Kernel`/`ClientLifecycle`, refresh coordinator, persistence, and typed load-time baseline; serializes whole refresh transactions; publishes immutable success epochs; and reconciles only a generation-matching detached backend jar before persistence. Policy-keyed identity tasks survive an individual waiter cancellation; close cancels live probes before the credential lock while preserving offline post-close lookup. It neither reads profile files nor implements recovery rungs. |
| `WebCookieSession` / `WebBackendSession` | [`_web_cookie_provider.py`](../src/notebooklm/_web_cookie_provider.py), [`_runtime/web_backend_session.py`](../src/notebooklm/_runtime/web_backend_session.py) | Narrow private-session port and concrete backend-session owner. The session clones a provider generation into a second `Kernel`, exposes only a redacted detached value for reconciliation, and closes without acquiring or persisting credentials. Provider and backend jars never alias. |
| `WebRpcBackend` | [`_web/backend.py`](../src/notebooklm/_web/backend.py) | One web semantic backend. Owns typed operation dispatch plus raw-RPC/session/metrics delegates; it receives `WebCookieProvider` and `WebCookieSession` as ports and closes the provider only when construction explicitly transferred ownership. It does not import or retain auth tokens, auth coordination, persistence, acquisition lifecycle, profile storage, recovery, a `ClientInternals` receipt, chain builder, or middleware list. |
| `WebExecutionRuntime` | [`_web/runtime.py`](../src/notebooklm/_web/runtime.py) | Sole logical batchexecute encode/dispatch/decode authority. Owns request-id/started-metric bracketing, idempotency lookup, method-ID resolution, encoding, decode/error mapping, and decoded-auth retry through narrow injected callables. Credential-to-wire materialization is delegated to `_web_request_auth.py`. |
| `RpcExecutor` | [`_rpc_executor.py`](../src/notebooklm/_rpc_executor.py) | Behaviorless compatibility subclass of `WebExecutionRuntime` for retained private/raw construction imports; it owns no method implementation. |
| `RuntimeTransport` | [`_runtime/transport.py`](../src/notebooklm/_runtime/transport.py) | Authed POST collaborator. Owns loop guard, immutable generation materialization, ordered chain dispatch, queue-wait recording, final freshness rebuild, synchronous private-session generation install, and the `Kernel.post` terminal. |
| `RpcCallState` | [`_runtime/rpc_call_state.py`](../src/notebooklm/_runtime/rpc_call_state.py) | Frozen typed call configuration plus a bounded progress record shared by exact identity across retries. Replaces the deleted mutable string-key context protocol. |
| `AuthRefreshCoordinator` | [`_runtime/auth.py`](../src/notebooklm/_runtime/auth.py) | Provider-side owner of the auth-snapshot lock and established refresh task. Token/profile installs serialize against its lock; the provider's transaction lock publishes the resulting cookie/token/route value as one immutable epoch. The refresh-task single-flight remains distinct. |
| `ClientLifecycle` | [`_runtime/lifecycle.py`](../src/notebooklm/_runtime/lifecycle.py) | Provider-side acquisition-session open/close, keepalive, loop binding/reopen reset, and typed-versus-legacy cookie-save routing. It never owns the backend-private session. |
| `RuntimePipeline` | [`_runtime/pipeline.py`](../src/notebooklm/_runtime/pipeline.py) | Immutable owner of the canonical ADR-0009 behavior order. It composes the fixed terminal once and exposes no bind step, mutable retry slots, or replaceable chain reference. |
| `RpcRequest` / `RpcResponse` / `NextCall` | [`_runtime/rpc_call.py`](../src/notebooklm/_runtime/rpc_call.py) | Typed request/response envelopes and the narrow pipeline-call shape shared by runtime behaviors and the transport terminal. |
| `TransportDrainTracker` | [`_transport_drain.py`](../src/notebooklm/_transport_drain.py) | Tracks in-flight transport operations + the drain condition variable. Gates graceful shutdown. |
| `ClientMetrics` | [`_client_metrics.py`](../src/notebooklm/_client_metrics.py) | Per-instance counters (`ClientMetricsSnapshot`) + the `on_rpc_event` user callback. |
| `ReqidCounter` | [`_reqid_counter.py`](../src/notebooklm/_reqid_counter.py) | Monotonic `_reqid` for the chat backend; lock-protected `next_reqid(...)`. |
| `CookiePersistence` | [`_cookie_persistence.py`](../src/notebooklm/_cookie_persistence.py) | Provider-side per-canonical-path typed baseline state, ordered `ProfileStore` cookie merges, `__Secure-1PSIDTS` rotation, and the concrete v0.x snapshot adapter. First-party `_from_store` instances retain no `AuthTokens`; public-constructor instances preserve legacy save compatibility. |
| `IdempotencyRegistry` | [`_idempotency.py`](../src/notebooklm/_idempotency.py) | Policy/classification registry keyed by `(RPCMethod, operation_variant)`. `WebExecutionRuntime._execute_once()` consults it once per call. |
| `_request_types` | [`_request_types.py`](../src/notebooklm/_request_types.py) | Retains `AuthSnapshot` as a compatibility alias of `WebCookieGeneration`, plus `BuildRequest` and request materialization shapes shared by RPC, chat, auth refresh, and the runtime pipeline terminal. |
| `_web_request_auth` | [`_web_request_auth.py`](../src/notebooklm/_web_request_auth.py) | Sole ordinary-RPC credential-to-wire adapter. It formats an already-acquired immutable generation into URL/body values outside `_web`; it has no storage, refresh, recovery, browser, or mutable-session capability. |
| `_transport_errors` | [`_transport_errors.py`](../src/notebooklm/_transport_errors.py) | Owns transport-level exceptions, `Retry-After` parsing, and raw `Kernel.post` error mapping consumed by `RetryBehavior` and `AuthRefreshBehavior`. |
| `_streaming_post` | [`_streaming_post.py`](../src/notebooklm/_streaming_post.py) | Low-level streaming POST helper with the response-size cap used by `Kernel.post`. |
| `Kernel` | [`_kernel.py`](../src/notebooklm/_kernel.py) | Pure transport core used in two non-aliasing instances. The provider kernel owns acquisition/refresh; the backend kernel owns execution. `install_generation()` copies only a newer provider generation and never replays an equal/stale generation over response mutations. |
| `_runtime/init` | [`_runtime/init.py`](../src/notebooklm/_runtime/init.py) | Atomically validates options, constructs the provider kernel/lifecycle and distinct backend kernel/session, wires the provider/session ports and pipeline, and returns the frozen construction-only `ClientInternals` receipt consumed immediately by `_client_composition.py` while constructing `WebRpcBackend`. |
| `_loop_affinity` | [`_loop_affinity.py`](../src/notebooklm/_loop_affinity.py) | Tiny free-function `assert_bound_loop(bound_loop)` shared by every helper that captures a loop reference at `open()` time (`TransportDrainTracker`, `ReqidCounter`, `AuthRefreshCoordinator`, `ArtifactPollingService`, `ChatAPI`). Enforces ADR-0004 without coupling those helpers to the public client. |

### Shipped runtime invariants

[ADR-0016](./adr/0016-auth-identity-and-core-logger-compatibility.md)
pins two compatibility-sensitive details that survive the session-elimination
work:

- `RuntimeWebCookieProvider.auth` is the authoritative mutable `AuthTokens` instance.
  `NotebookLMClient.auth` delegates to it; refresh paths mutate that object in place, and
  collaborators that observe auth alias it rather than holding detached copies.
- `CORE_LOGGER_NAME` intentionally remains the literal
  `"notebooklm._core"` even though the `_core.py` compatibility module was
  deleted. Runtime code keeps using this logger key through
  `CORE_LOGGER_NAME` for downstream log filters and `caplog` selectors.
  Treat it as a logging compatibility contract, not evidence that
  `notebooklm._core` is an active module or that a concrete `Session` owner
  remains in the runtime graph.

## Domain-service collaborators

Beyond the backend-owned runtime graph, feature APIs are implemented via dedicated domain services and helper modules:

| Service / Module | Module | Responsibility |
|-------------------|--------|----------------|
| `NoteService` | [`_note_service.py`](../src/notebooklm/_note_service.py) | Backend-neutral plain-note and note-backed-mind-map workflows, including shielded create finalization and cancellation cleanup. The same module's private `LegacyNoteBackedService` is restricted to deferred saved-chat/artifact compatibility callers. |
| `NoteBackedMindMapService` | [`_mind_map.py`](../src/notebooklm/_mind_map.py) | Deferred note-backed compatibility adapter retained outside the migrated `MindMapsAPI` path. |
| `ArtifactRepresentationService` | [`_studio/representations.py`](../src/notebooklm/_studio/representations.py) | Backend-neutral P5.8 representation discovery, family selection, trusted remote byte dispatch, and local serialization orchestration. |
| `StudioDownloadClient` | [`_studio/downloads.py`](../src/notebooklm/_studio/downloads.py) | Trusted remote byte retrieval with shared factory/allowlist and per-hop redirect validation for both httpx and curl_cffi. |
| `StudioSerializationClient` | [`_studio/serialization.py`](../src/notebooklm/_studio/serialization.py) | RPC-free local text, JSON, and CSV representation serialization. |
| `StudioManagementService` / `ReportSuggestionService` | [`_studio/management.py`](../src/notebooklm/_studio/management.py) | Typed P5.8 revise/retry/rename/delete and report-suggestion operations. |
| `ArtifactLifecycleService` | [`_studio/lifecycle.py`](../src/notebooklm/_studio/lifecycle.py) | Typed lifecycle status observation plus the unchanged lifecycle-terminal public polling contract. |
| `ReportFamilyService` / `VideoFamilyService` | [`_studio/documents.py`](../src/notebooklm/_studio/documents.py) | Backend-neutral P5.4 report/video generation, catalog filtering, and family metadata/availability rules. |
| `VisualFamilyService` | [`_studio/visuals.py`](../src/notebooklm/_studio/visuals.py) | Backend-neutral P5.5 infographic/slide-deck generation, catalog filtering, usable readiness, and accessibility metadata. |
| `DataTableFamilyService` / `NoteBackedMindMapFamilyService` | [`_studio/data_views.py`](../src/notebooklm/_studio/data_views.py) | Backend-neutral P5.6 data-table and artifact note-backed mind-map generation plus complete catalog selection. |
| `MindMapFamilyService` | [`_studio/mind_maps.py`](../src/notebooklm/_studio/mind_maps.py) | Backend-neutral P6.3 interactive Studio mind-map generation, discovery, tree, update, and delete workflow. |
| `DriveExportService` | [`_studio/exports.py`](../src/notebooklm/_studio/exports.py) | Explicit P5.6 Google Drive companion export for report/data-table representations. |
| `_artifact_formatters` | [`_artifact/formatters.py`](../src/notebooklm/_artifact/formatters.py) | Markdown, HTML, and plain text formatters for artifacts. |
| `_artifact/listing` | [`_artifact/listing.py`](../src/notebooklm/_artifact/listing.py) | Listing and filtering operations for notebook artifacts. |
| `_row_adapters*` | [`_row_adapters/artifacts.py`](../src/notebooklm/_row_adapters/artifacts.py), [`_row_adapters/chat.py`](../src/notebooklm/_row_adapters/chat.py), [`_row_adapters/documents.py`](../src/notebooklm/_row_adapters/documents.py), [`_row_adapters/labels.py`](../src/notebooklm/_row_adapters/labels.py), [`_row_adapters/notebooks.py`](../src/notebooklm/_row_adapters/notebooks.py), [`_row_adapters/notes.py`](../src/notebooklm/_row_adapters/notes.py), [`_row_adapters/research.py`](../src/notebooklm/_row_adapters/research.py), [`_row_adapters/sources.py`](../src/notebooklm/_row_adapters/sources.py) | Wire-shape adapters that wrap raw batchexecute rows (`ArtifactRow`, `LabelRow`, `NoteRow`, `SourceRow`, the `POLL_RESEARCH` rows, the `SUGGEST_PROMPTS` suggestion rows) and the streamed-chat rows (`AnswerRow`/`CitationRow`/…) behind named accessors so downloads, polling, listing, labels, research, and the chat parser don't open-code positional indices. Strict decode behavior is pinned in `tests/unit/test_row_adapters.py`, `tests/unit/test_chat_row_adapter.py`, `tests/unit/test_notebooks_row_adapter.py`, `tests/unit/test_research_row_adapter.py`, and `tests/unit/test_citation_alignment.py`. |
| `_research_task_parser` | [`_research_task_parser.py`](../src/notebooklm/_research_task_parser.py) | Parses deep-research task results from raw rows. Returns dict-shaped output today; a typed-model migration is not yet complete. |
| `_types/` | [`_types/`](../src/notebooklm/_types) | Private package holding the dataclass and `Protocol` implementations behind the public `types.py` / per-feature public schemas. Split per domain (`artifacts.py`, `artifact_content.py`, `chat.py`, `documents.py`, `labels.py`, `mind_maps.py`, `notebooks.py`, `notes.py`, `research.py`, `sharing.py`, `sources.py`, plus `common.py` for shared shapes like `ConnectionLimits`). |

### Structured-document value boundary

The exported `StructuredDocument` graph is the one ADR-0035 exception to the P3 rule that web
codecs return private records before public projection. Its frozen public constructors are the
transport-neutral validation boundary for UTF-16 ranges, span and cell normalization, annotation
ordering, clipping, and rendering. Source fulltext and streamed chat therefore share one coordinate
space without a second private document representation. Wire positions remain in
`_row_adapters/documents.py`; they do not enter `_types/documents.py`.

`tests/_guardrails/test_document_value_boundary.py` enforces the exception's exact type/export set,
closed standard-library dependency set, frozen construction, and a rich nested pickle/UTF-16/render
round trip. It complements `tests/unit/test_citation_alignment.py`, the public-model compatibility
baseline, 39 independently schema-checked document wire mappings, and the source/chat decoded
goldens. Any new document dependency or value type therefore requires an explicit boundary review;
ordinary mutable decoded resources still follow `wire -> private record -> public model`.

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
| [`_auth/tokens.py`](../src/notebooklm/_auth/tokens.py) | `AuthTokens` plus the typed stored-auth application boundary. `StoredAuthLoader` keeps inline/file source, paired seed, final-attempt route, acquisition baseline, initial store merge, and closed `InlineLoadedAuth | FileLoadedAuth` result together. `TokenAcquirer` is its sole structural seam; blocking source/account/merge work is offloaded. The v0.x classmethod and provider-storage adapter share the call-time `_load_stored_auth` composition function. |
| [`_auth/web_provider_storage.py`](../src/notebooklm/_auth/web_provider_storage.py) | P8 construction adapter over the whole `_load_stored_auth` transaction. It projects the existing inline/file result into a redacted frozen `WebProviderBootstrap`, preserving the exact `ProfileStore`/persistence-baseline pair without reading a document or recreating paths, locks, CAS, atomic writes, or permissions. |
| [`_auth/web_provider_refresh.py`](../src/notebooklm/_auth/web_provider_refresh.py) | P8 refresh adapter over the whole `refresh_auth_session` transaction. It preserves the base-policy direct path and the wider-policy join-then-rerun rule; only the established exhausted-base `ValueError` starts the headless-enabled rerun. Recovery and master-token rungs remain in their existing owners. |
| [`_auth/paths.py`](../src/notebooklm/_auth/paths.py) | Storage paths and filesystem helpers, including the **single** derivation behind all four credential lock files (`.lock`, `.rotate.lock`, `.refresh.lock`, `.lock.bootstrap` — the last folded in from `master_token.py` by ADR-0033 PR 1.3, which kept every path byte-identical and every lock mechanism untouched). |
| [`_auth/storage_lock.py`](../src/notebooklm/_auth/storage_lock.py) | Dependency-bottom `StorageLockManager`: process-default exact-raw-path thread-lock identity, POSIX/Windows OS gateway, bounded synchronous retry, and manager-lifecycle cookie warning claim. Imports stdlib only; `storage`, `profile_store`, and `keepalive` share its process default. |
| [`_auth/credential_io.py`](../src/notebooklm/_auth/credential_io.py) | Sealed commit capability: the sole unchecked-atomic importer, with distinct private wrappers for complete profile and arbitrary-path master-token documents. |
| [`_auth/master_token_types.py`](../src/notebooklm/_auth/master_token_types.py) | Dependency-bottom `MasterTokenError`, immutable `MasterToken`, and pure permissive version-1 legacy-record codec. The exception keeps historical `notebooklm._auth.master_token` module/pickle identity; the credential secret is redacted from repr. |
| [`_auth/master_token_file.py`](../src/notebooklm/_auth/master_token_file.py) | Path-owned one-sample raw/typed master-token reads and canonical writes under the exact dotted sibling lock. Explicit paths remain valid for v0.x adapters; it owns no network/bootstrap policy or cache. |
| [`_auth/mint_service.py`](../src/notebooklm/_auth/mint_service.py) | Stateless per-attempt master-token exchange and cookie minting, plus the sole raw RotateCookies wire. It depends downward only on `MasterToken` and network libraries; its process-wide mutex only serializes temporary third-party logger levels, and it owns no paths, persistence, storage/domain locks, bootstrap/recovery policy, cache, or retained secrets. |
| [`_auth/master_token_bootstrap.py`](../src/notebooklm/_auth/master_token_bootstrap.py) | Concrete path-owned bootstrap/re-mint coordinator over exactly one `MintService`, one `ProfileStore`, one bootstrap lock, and one verifier. It owns two-owner advisory checks, session-before-token persistence, strict reload, the four-state missing-storage recheck, and shield-to-settlement cancellation; token I/O routes only through its store. It imports no token file, storage facade, runtime/client, CLI, or recovery owner. |
| [`_auth/cookie_filter.py`](../src/notebooklm/_auth/cookie_filter.py) | Dependency-bottom raw capture/domain filter plus value-free malformed-row diagnostics. Pure policy/logging: no paths, files, locks, commits, documents, or lifecycle state. |
| [`_auth/profile_store.py`](../src/notebooklm/_auth/profile_store.py) | Path-owned synchronous document/session reads, derived typed master-token access, blocking cookie transactions, typed in-band account read/update/clear, and typed browser/remint, login/import, and minted-session replacement. Token methods resolve the sibling path at call time and share the store's lock manager. Minted replacement owns the same-lock latest-owner gate, default raw filter, lossless destination preservation/rebind, and one commit. Its repr-hidden request snapshots raw master-token cookie fields (`same_site="None"`) and runtime-permissive email together before path/lock work; it intentionally does not use filtering/SameSite-lossy `CookieJar.from_httpx()`. Owns no cache, baseline, live HTTP jar, legacy scheduler, or network policy. |
| [`_auth/profile_migration.py`](../src/notebooklm/_auth/profile_migration.py) | Concrete legacy-account ownership plus the path-shaped native login replacement operation: primitive keep/clear/set inputs become dependency-bottom directives, one `ProfileStore`/request/writer composition returns `ReplaceResult`, and post-login reconciliation stays outside the store lock. Also owns lossless two-read resolution, `context.json` read/scrub, only-if-absent embed-before-scrub promotion, and canonical retryable single-flight daemon scheduling with bounded exit drain. It depends downward on `ProfileStore` and account values; no loader, network, runtime, CLI, or token orchestration. |
| [`_auth/storage.py`](../src/notebooklm/_auth/storage.py) | Shrinking v0.x persistence policy/compatibility facade. Retains raw signatures/results, lock and cookie adapters, the minted live-jar snapshot adapter, and a thin arbitrary-path token writer over `MasterTokenFile`. Remint/login wrappers use exhaustive maps to project native `ReplaceResult`/`CookieMergeResult` statuses into their legacy return types; first-party capture, app, CLI, runtime, and recovery paths consume native results instead. Filter names and transaction functions remain exact compatibility aliases. |
| [`_auth/storage_writer.py`](../src/notebooklm/_auth/storage_writer.py) | **Shim** — re-exports the writer API from `_auth/storage.py`; defines nothing. Removed at the next major. |
| [`_auth/storage_transaction.py`](../src/notebooklm/_auth/storage_transaction.py) | **Shim** — re-exports `in_storage_transaction` + the lock-failure policies through `_auth/storage.py`; their definitions live in `_auth/profile_store.py`. Removed at the next major. |
| [`_auth/extraction.py`](../src/notebooklm/_auth/extraction.py) | Cookie/token extraction from browser sessions. |
| [`_auth/cookies.py`](../src/notebooklm/_auth/cookies.py) | Cookie maps, `_update_cookie_input`, the network-free pure storage loaders, and thin recovery-composition adapters. Each successfully converted live cookie supplies identity/value/runtime fields while SameSite comes from that same sanitized raw row, preserving first-successful duplicate behavior in the typed baseline. |
| [`_auth/cookie_policy.py`](../src/notebooklm/_auth/cookie_policy.py) | Domain allowlist, cookie-domain builder (`build_cookie_domain_allowlist`), and cookie policy decisions. |
| [`_auth/cookie_semantics.py`](../src/notebooklm/_auth/cookie_semantics.py) | Shared cookie-shape and expiry semantics used by sanitized auth loaders and persistence boundaries. |
| [`_auth/cookie_types.py`](../src/notebooklm/_auth/cookie_types.py) | The canonical `Cookie` / `CookieJar` types (ADR-0031 Stage 1): constructors from every input shape, converters to httpx/storage-state, and the cookie-set policy questions as methods. A delegating wrapper — policy still lives in `cookie_policy`/`cookies`. |
| [`_auth/browser_cookie_recovery.py`](../src/notebooklm/_auth/browser_cookie_recovery.py) | **Shim** — re-exports `validate` / `heal` / `validate_with_recovery` + `ValidationResult` from `_auth/psidts_recovery.py`; defines nothing. Removed at the next major. |
| [`_auth/browser_state_validation.py`](../src/notebooklm/_auth/browser_state_validation.py) | **Shim** — re-exports `heal_captured_state` from `_auth/browser_capture.py`; defines nothing. Removed at the next major. |
| [`_auth/browser_capture.py`](../src/notebooklm/_auth/browser_capture.py) | **One deep module for the browser launch→navigate→capture→filter→heal→persist core** (ADR-0033 sanctioned merge — absorbed `browser_state_validation.py` and `login_wait_trace.py`), lazy `playwright`; shared by the interactive CLI login adapter and the layer-3 headless re-auth layer (ADR-0021). Both capture arms construct `RemintWriteRequest` and consume `ProfileStore.replace_from_remint() -> ReplaceResult` directly. The headless arm classifies the landing URL (authenticated→capture, redirected-to-login→`HeadlessLoginRequiredError`). `run_cdp_capture` is an alternative credential source: attach to an operator-pointed already-running Chrome over CDP (`connect_over_cdp`, disconnect-only teardown) using the SAME landing classification + cookie-domain allowlist. Absorbed sections: (1) **login-wait DEBUG tracing** — `log_observed_navigations` logs each main-frame navigation observed during the five-minute interactive wait at DEBUG so `notebooklm -vv login` is self-diagnosing when a login never lands; inert unless DEBUG is enabled (no listener attached) and it swallows every listener exception, so it can never destabilise the wait. Redaction goes through its own `trace_url`, which keeps **only** scheme + host — deliberately stricter than `extraction._safe_url` (which preserves the path outside a Google-OAuth allowlist), because this traces arbitrary SSO redirects where a federated IdP can carry a one-time assertion in the path. The two redactors are **kept distinct on purpose**. (2) **captured-state heal** — `heal_captured_state`, a best-effort in-memory PSIDTS heal for Playwright-captured state that preserves cookie attributes, returns `(state, error)` and never raises, so a failed heal cannot discard a completed sign-in. `browser_launch_errors.py` stays a separate leaf for cohesion — a channel registry plus a pure classifier, testable without a browser. (It has ONE consumer: the CLI reaches `classify_launch_failure` through this module's re-export, since the CLI-boundary guardrail sanctions only `_auth/browser_capture`.) |
| [`_auth/_browser_cookie_filter.py`](../src/notebooklm/_auth/_browser_cookie_filter.py) | **Shim** — re-exports the `storage.py` compatibility alias of `cookie_filter.filter_storage_state_cookies_by_domain_policy`; defines nothing. Removed at the next major. |
| [`_auth/navigation_errors.py`](../src/notebooklm/_auth/navigation_errors.py) | Transport-neutral leaf for `browser_capture`: classifies a Playwright navigation failure from its message. `navigation_error_code` extracts the `net::ERR_*` token (anchored, so it cannot return arbitrary text after a stray `net::`) — used to LOG a failure without logging the credential-bearing URL Playwright embeds in it. The two predicates deliberately disagree about the same error depending on who issued the navigation: `is_navigation_race` (narrow — `ERR_ABORTED` plus the prose interruptions) guards the sites where *we* navigate, so `ERR_CONNECTION_REFUSED`/`ERR_INVALID_URL` still surface; `is_navigation_failure` (broad — the whole `net::ERR_*` family plus a beforeunload cancellation) guards only the interactive login wait, where we are watching a *human* navigate and a failed hop says nothing about their sign-in (#2257). Pure string-in/bool-out — no Playwright, no I/O, no CLI. |
| [`_auth/browser_launch_errors.py`](../src/notebooklm/_auth/browser_launch_errors.py) | Transport-neutral leaf for `browser_capture`: the `CHANNEL_BROWSERS` channel registry plus `classify_launch_failure`, which maps a Playwright launch failure to actionable help (system browser not installed, bundled Chromium not downloaded, or a Windows `spawn UNKNOWN` execution veto from AppLocker/WDAC/Defender) or to `None` so the original exception propagates. Pure string-in/string-out — no Playwright, no I/O, no CLI. |
| [`_auth/login_wait_trace.py`](../src/notebooklm/_auth/login_wait_trace.py) | **Shim** — re-exports `log_observed_navigations` / `safe_page_url` / `trace_url` from `_auth/browser_capture.py`; defines nothing. Removed at the next major. |
| [`_auth/headless_reauth.py`](../src/notebooklm/_auth/headless_reauth.py) | Layer-3 (deepest) auth recovery: when first-party cookies are dead, drive a headless browser against the persistent profile to silently re-mint cookies. Typed honest outcomes (`HeadlessReauthStatus` UNAVAILABLE/FAILED/SUCCESS — never silent `None`). Opt-in only (`refresh_auth(allow_headless=True)` or `NOTEBOOKLM_HEADLESS_REAUTH=1`); local-unattended-only, never the remote/MCP auth path. Alternative credential source: `NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL` (or `attempt_headless_reauth(cdp_url=...)`) attaches to an operator-pointed running Chrome instead of the dedicated profile (freshness mitigation). Also exposes `headless_reauth_readiness()` — a credential-free, browser-free probe (profile present + playwright installed) surfaced by `doctor`. |
| [`_auth/recovery.py`](../src/notebooklm/_auth/recovery.py) | Client-neutral recovery composition and exact paired replacement provenance. A network- and write-free mid-session adapter can reload changed file-backed cookies before external recovery while preserving a concurrently refreshed live jar. One-shot `ColdRecoveryCoordinator` owns the explicit L2.5 → L3 → L4 ladder and scrubs all eleven injected callbacks on every exit. `ColdRecoveryState` owns synchronized weak-loop path locks and success generations; class-owned `_drive_cold`/`_coalesce_cold` are the sole ladder/flight bodies, while the exact-signature legacy functions are thin process-default adapters. |
| [`_auth/single_flight.py`](../src/notebooklm/_auth/single_flight.py) | `SingleFlight` owns one cross-loop flight registry, strong leader-task set, and per-canonical-path success epochs. A leader task mirrors into a `concurrent.futures.Future`; followers use shielded settle-before-propagate bridging, so waiter cancellation never cancels shared work. Claim plus stale-epoch comparison is atomic, settled slots prompt-pop, and quiescent-only reset refuses live work. Exact module functions remain process-default adapters. |
| [`_auth/account_types.py`](../src/notebooklm/_auth/account_types.py) | Dependency-neutral immutable `Account` and `PlaywrightAccountRepairResult` values. Both retain historical `notebooklm._auth.account` module/pickle/repr identity. |
| [`_auth/account_repair.py`](../src/notebooklm/_auth/account_repair.py) | One-operation `AccountRepairService` over six exact collaborators. It claims synchronously before its first await, offloads only cookie loading, performs typed write/clear synchronously, maps only the frozen handled exception set to the legacy result, and scrubs all collaborator references on success, error, cancellation, or an unlisted exception. |
| [`_auth/account.py`](../src/notebooklm/_auth/account.py) | Account network adapter: probing `?authuser=N`, extracting the active email, formatting the wire value, and composing one `AccountRepairService` with call-time legacy seams. Typed in-band writes live in `ProfileStore`; legacy file policy lives in `_auth/profile_migration.py`; raw compatibility remains in `_auth/storage.py`. |
| [`_auth/account_email.py`](../src/notebooklm/_auth/account_email.py) | Generation-safe account-email resolution: match persisted identity to the authoritative live cookie route, probe through an injected callback when needed, and self-heal with exact-document CAS without crossing profile-session generations. |
| [`_auth/session.py`](../src/notebooklm/_auth/session.py) | `refresh_auth_session(auth=..., kernel=..., auth_coord=..., lifecycle=..., cookie_persistence=...)` implementation called as one whole transaction by `WebProviderRefresh`. Takes five explicit keyword-only collaborators instead of a Session-shaped owner Protocol; the previous `RefreshAuthCore` Protocol and the `update_auth_tokens` / `update_auth_headers` Session-level forwards have been removed. |
| [`_auth/refresh.py`](../src/notebooklm/_auth/refresh.py) | Token refresh driver, sole `ColdRecoveryCoordinator` production adapter, and typed `fetch_tokens_with_domains` persistence boundary. `_cold_fallbacks` supplies late-bound L2.5, cold-delegation, route, final-fetch, and jar-replacement closures while preserving exact logs and raw caller / canonical L2.5 / raw caller route timing. L2.5 remains outside the cold single-flight. The domain fetch consumes one paired live/SameSite-preserving baseline sample, carries the selected initial/L2.5/L3/L4 baseline, captures an immutable final observation, and offloads one concrete `ProfileStore` merge. `HARD_FAILURE`, the sole non-advancing result, retains the exact selected baseline; advancing outcomes return the exact next baseline. Caller cancellation during worker offload propagates immediately, without preventing an already-dispatched merge from finishing. The frozen `RefreshDeps` and v0.x compatibility ladder remain. |
| [`_auth/keepalive.py`](../src/notebooklm/_auth/keepalive.py) | Cookie keepalive and `__Secure-1PSIDTS` rotation policy. `RotationState` owns per-loop/per-canonical-path locks and monotonic attempt stamps behind one short-held threading lock; claims are stamped before POST, so failure and cancellation consume the 60-second slot. Historical raw state names are non-owning identity views into the process-default owner, and the raw RotateCookies wire remains an exact `mint_service.py` re-export. |
| [`_auth/psidts_recovery.py`](../src/notebooklm/_auth/psidts_recovery.py) | Inline PSIDTS recovery plus the generic load→validate→heal→retry composition over injected pure loaders. It owns typed raw-document observation/CAS and `ProfileStore` persistence, not cookie-module or storage-facade policy. Sentinel/contended/acquired paths preserve their distinct rereads and narrow caught-error sets; success means the post-save disk state is live, including a sibling winner. Also owns the captured-cookie `validate`/`heal` compatibility seam. |
| [`_auth/master_token.py`](../src/notebooklm/_auth/master_token.py) | Headless master-token compatibility boundary: exchange/mint remain exact v0.x adapters over `MintService`, the raw reader projects one `MasterTokenFile` sample, the writer preserves the call-time `storage.write_master_token` seam, and coarse operations compose `MasterTokenBootstrapper` with late-bound legacy-owner, Android-ID, strict-loader, and verifier bridges. `MasterTokenError` is an identity re-export from the dependency-bottom types leaf. |

The measured persistence boundary is 1,090 lines in `storage.py`, 602 in
`profile_migration.py`, 876 in `profile_store.py`, 96 in `cookie_filter.py`, and 89 in
`master_token_file.py`: 2,753 lines total.
The migration module is internal composition, not a public `ProfileStore` extension surface.
The Phase 9 loader owners remain in `tokens.py` and `refresh.py`. Phase 10 consumes their closed
`FileLoadedAuth` result by registering its exact `ProfileStore`/baseline pair in runtime
`CookiePersistence`, without rereading disk. Direct clients prepare a one-shot disk baseline before
transport; fileless clients capture only the live compatibility projection. A missing saver always
uses ordered typed merges; only an explicit `cookie_saver=` retains the v0.x callback surface and
its per-key adapter snapshot. `ClientLifecycle` owns the sole
`AuthTokens.cookie_snapshot` mirror. Measured Phase 10 owners are 457 lines in
`_cookie_persistence.py`, 618 in `_runtime/init.py`, 628 in `_runtime/lifecycle.py`, and 992 in
`client.py`.

Phase 12C completes state ownership without changing the public ladder or on-disk schema. The
measured auth graph is **40 modules / 15,237 lines / 128 unique edges (117 module + 11
function-local)**; both the module-only and all-scope SCC sets are empty. The former
`cookies/master_token/psidts_recovery/storage` all-scope cycle is gone. Final touched owner sizes
are pinned by the module ratchet; `storage.py` and `refresh.py` shrink, and no bottom owner imports
the facade. Public storage/auth savers, `Account`/repair-result/`MasterTokenError` identities,
module adapters, keepalive raw-state identity views, and client/runtime injection seams remain
v0.x compatible.

The cookie lifecycle — what gets written, who rotates, what the
keepalive contract is — is documented separately in
[`docs/auth-cookie-lifecycle.md`](./auth-cookie-lifecycle.md). Credential authority, cancellation,
retention, and compatibility threats are summarized in [`docs/security.md`](./security.md).

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
| [`cli/master_token_login.py`](../src/notebooklm/cli/master_token_login.py) | Command driver for `notebooklm login --master-token[-refresh]`: resolves paths and renders the outcome over the public `master_token_bootstrap` / `master_token_remint` / `assert_account_writable` adapters. Coordination lives in `_auth/master_token_bootstrap.py`; only interactive browser `oauth_token` capture ([`cli/services/login/master_token.py`](../src/notebooklm/cli/services/login/master_token.py)) stays CLI-side (ADR-0023/ADR-0034). |
| [`cli/services/auth_refresh.py`](../src/notebooklm/cli/services/auth_refresh.py) | Pure re-export of `notebooklm.auth.bootstrap_missing_storage_from_master_token` for `auth refresh`'s missing-storage preflight. `_auth/master_token_bootstrap.py` owns the four-state machine; `_auth/master_token.py` owns the v0.x enum-to-bool collapse. |
| [`cli/services/auth_source.py`](../src/notebooklm/cli/services/auth_source.py) | Single resolver for CLI auth-source precedence (`--storage`, `NOTEBOOKLM_AUTH_JSON`, active profile). |
| [`cli/context.py`](../src/notebooklm/cli/context.py) | Profile/storage-scoped `context.json` persistence for active notebook and conversation state. Account metadata now lives unified in-band in `storage_state.json` (`_auth/storage.py`); `context.json` is only its pre-v0.5.0 legacy source, promoted in-band on read and no longer written here (#2103 PR-0). |
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
Expensive route groups have lifespan-owned concurrency limiters, tuned by
`NOTEBOOKLM_SERVER_*_CONCURRENCY` env vars, so source mutation/wait, artifact
generation/download, research, and blocking chat work cannot unboundedly starve
cheap reads or `/healthz`.

## Runtime behavior pipeline (ADR-0009)

The runtime behavior order is pinned by
[`tests/unit/test_chain_wiring.py`](../tests/unit/test_chain_wiring.py).
The order is load-bearing: changing it without simultaneously updating the
pin tests is a bug.

The composition in [`RuntimePipeline`](../src/notebooklm/_runtime/pipeline.py)
reads outermost-first (each behavior wraps everything below it):

```text
DrainBehavior                outermost — admits and tracks for shutdown drain
   ↓
MetricsBehavior              starts timing here (latency includes queue wait)
   ↓
SemaphoreBehavior            max_concurrent_rpcs slot acquired AFTER Drain/Metrics,
                             BEFORE Retry can re-enter (one slot per logical RPC)
   ↓
RetryBehavior                429 / 5xx with Retry-After honor
   ↓
AuthRefreshBehavior          refresh-on-auth-error; capped retries
   ↓
TracingBehavior              innermost — structured-logging boundary
                             (OpenTelemetry export is future work)
   ↓
Authed POST leaf             (RuntimeTransport.terminal → Kernel → httpx)
```

## Client shell and atomic backend composition

`NotebookLMClient` is the public shell; `_client_composition.py` is the production-only composition
boundary. Composition validates constructor options, receives one frozen `ClientInternals`
construction record, builds `WebRpcBackend` with those leaves, and then wires feature facades to the
semantic backend or their focused collaborators. No partially bound runtime is published.

The client retains `_backend`, `_provider`, the source uploader, and public feature
facades. It does not retain `_auth`, `_collaborators`, `_composed`, or `_rpc_executor`. Public
`auth`, raw `rpc_call`, refresh/account methods, metrics, connection state, open, drain, and close
delegate to `WebRpcBackend`, which owns their concrete runtime leaves. `ClientLifecycle` still owns
transport open/close and loop-reopen behavior; `TransportDrainTracker` still owns drain admission;
`ClientMetrics` still owns telemetry state.

Feature APIs receive `BackendAdapter` for semantic operations and direct focused collaborators for
local upload/stream/poll work. The bounded `LegacyNoteBackedService` is the only compatibility
consumer of `RpcCaller`; adding another semantic-service consumer fails the P7 guard.

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
not a production `Session` replacement. Multi-capability features (`ChatAPI`,
`ArtifactsAPI`, `SourceUploadPipeline`) take their direct collaborators by
keyword-only constructor argument. Artifact service tests use the typed recording
backend; retained native consumers use narrow
`MagicMock(spec=RpcCaller, rpc_call=AsyncMock(...))`-style fakes.

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
| Pipeline / leaf / terminal | The runtime pipeline's ordering vocabulary. The pipeline wraps outermost-first; the **leaf** is the innermost behavior (`TracingBehavior`); the **terminal** is the authed-POST function (`RuntimeTransport.terminal → Kernel.post`) that ends the pipeline. |
| Drain | Graceful-shutdown waiting on in-flight transport operations to complete. Owned by `TransportDrainTracker` and admitted by `DrainBehavior`. |
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
| `_client_composition.py` | Production-only composition root (`compose_client`) that wires every constructor-set attribute. Tests construct the narrow runtime owner they exercise rather than calling this root. |
| `_runtime/init.py` | Constructor helpers that validate runtime kwargs, build leaves, wire middleware, and return a frozen construction-only `ClientInternals` receipt. |
| `_runtime/pipeline.py` | Immutable `RuntimePipeline` that composes the fixed drain/metrics/semaphore/retry/auth-refresh/tracing order around the transport terminal. |
| `_kernel.py` | Concrete `Kernel` transport core (owns `httpx.AsyncClient` + cookie jar) |
| `_runtime/config.py` | `DEFAULT_*` knobs and module-level constants. `CORE_LOGGER_NAME = "notebooklm._core"` is intentionally preserved as a compatibility logging contract even though the `_core` module was deleted; renaming it silently breaks downstream `caplog`/logger filters. |
| `_env.py`, `config.py` | Runtime environment defaults and the public config re-export surface |
| `_logging.py`, `log.py` | Redaction/correlation logging internals and the public logging helper surface |
| `_secrets.py` | Canonical runtime registry of must-scrub bare session-cookie names (`RUNTIME_SESSION_COOKIES`), `__Secure-*` / `__Host-*` prefix umbrellas (`SECURE_HOST_UMBRELLA_PATTERNS`, fail-closed for future names), and carrier-agnostic Google credential shapes (`AUTH_TOKEN_SHAPE_PATTERNS` — `g.a000-` / `sidts-` / `ya29.` tokens + the `AIza…` API key) that `_logging.py` redaction and `exceptions.py` scrubbing DERIVE from. Runtime code cannot import from `tests/`, so this restates the cassette sanitizer's must-scrub shapes; `tests/_guardrails/test_runtime_secret_registry_parity.py` asserts lockstep with `tests/cassette_patterns.py` on every axis — bare-cookie superset, umbrella coverage, and regex-string shape equality (issues #1517/#1518). |
| `_callbacks.py` | Sync-or-async callback invocation helper used by telemetry/retry hooks |
| `_lookup.py` | `unwrap_or_raise(obj, exc)` — the shared single-row-lookup helper backing the public `get`/`get_or_none` pair (ADR-0019 Enforcement tier-2). The four `sources`/`artifacts`/`notes`/`mind_maps` `get()` methods call it directly to raise their `*NotFoundError` on a miss (the v0.8.0 flip, issue #1247); `notebooks.get()` already raised on its own path and does not route through it. |
| `_loop_bound.py` | `LoopBoundPrimitive` — template-method base for loop-affinity binding. Clear-on-rebind owners (`RpcSemaphore`/`SourceUploadPipeline`/`ChatAPI`) discard cached loop-bound primitives. |
| `_deprecation.py` | Deprecation helper, gated by `NOTEBOOKLM_QUIET_DEPRECATIONS`. The immutable `DEPRECATION_SPECS` table owns the two Phase 13D auth-storage messages, replacements, since/removal versions, categories, and public-boundary stacklevels; `warn_registered_deprecation` emits them through `warn_deprecated`. `scripts/check_deprecation_targets.py` parses the table and callsites without importing application code and fails closed on malformed, missing, stale, lapsed, or structurally unresolved entries. Unrelated one-off deprecations continue to use `warn_deprecated`; `deprecations_quiet` / `_deprecations_quiet` / `_QUIET_ENV_VAR` retain the live suppression gate. ADR-0018 forbids inline `warnings.warn(..., DeprecationWarning)` outside this module — `tests/_guardrails/test_no_inline_deprecation_warnings.py` enforces it (only for `DeprecationWarning`; inline `RuntimeWarning`/`UserWarning` remains allowed). The permanent `save_cookies_to_storage(original_snapshot=None)` race advisory is therefore still an ungated `RuntimeWarning`. See `docs/deprecations.md`. |
| `_runtime/helpers.py` | `is_auth_error`, `AUTH_ERROR_PATTERNS`, `_resolve_keepalive_interval` |
| `_error_injection.py` | Synthetic-error env-var resolver + startup guard |
| `_client_metrics.py` | `ClientMetrics` — `ClientMetricsSnapshot` counters + `on_rpc_event` callback |
| `_rpc_semaphore.py` | `RpcSemaphore` — focused loop-bound owner of the one-per-logical-call concurrency permit and reopen reset. |
| `_transport_drain.py` | `TransportDrainTracker` — in-flight transport counters + `_TransportOperationToken` |
| `_deadline.py` | `RuntimeDeadline` helper shared by retry and polling loops so aggregate timeouts clamp sleep consistently |
| `_backend_compat.py` | Private compatibility projector from closed semantic `BackendErrorReason` + safe diagnostics back to the existing public exception subclasses at migrated facade boundaries. |
| `_backend.py` | Private protocol-neutral semantic port: backend kind/capabilities, typed `BackendAdapter.invoke`, and the minimal scrubbed error/deadline handoff used by the P2 slice. |
| `_binding.py` | Neutral binding vocabulary (P9.0): `OperationDisposition`, `NativeCallSpec`, `CodecPayload`, the `CodecBinding` / `CustomBinding` / `ResolvedHandlerBinding` row kinds, `BindingTable`, the construction-time `audit_bindings`, and the `invoke_binding` dispatch function; imports no `_web/`, `rpc/`, `_auth/`, or `httpx` module. |
| `_records.py` | Compatibility re-export hub for frozen, slotted, protocol-neutral input/output records and `OperationDef` values for P2 notebook/source operations, P5.1–P5.8 Studio families, and P6.1–P6.7 domain workflows, plus P3 decoded values and closed URL-source error evidence. Large domain families live in sibling record modules so this hub remains under the module-size ratchet. |
| `_chat_records.py` | P6.1 neutral Chat records and six typed operation definitions, re-exported from `_records.py`. |
| `_label_records.py` | P6.4 neutral source-label/collection records and eleven typed operation definitions, re-exported from `_records.py`. |
| `_note_records.py` | P6.3 neutral plain-note records and five typed operation definitions, re-exported from `_records.py`. |
| `_notebook_records.py` | P2/P6.6 neutral notebook records and nine typed operation definitions, re-exported from `_records.py`. |
| `_research_records.py` | P6.2 neutral Research records and four typed operation definitions, re-exported from `_records.py`. |
| `_settings_records.py` | P6.6 neutral account-settings records and three typed operation definitions, re-exported from `_records.py`. |
| `_sharing_records.py` | P6.5 neutral Sharing records and four typed operation definitions, re-exported from `_records.py` while keeping the shared record module below the size ratchet. |
| `_source_records.py` | Frozen, slotted, protocol-neutral source read/add/content/refresh/Drive/upload records split from `_records.py` to keep the shared record surface below the module-size ratchet. |
| `_backoff.py` | Shared capped exponential-backoff calculation with deterministic test injection |
| `_reqid_counter.py` | `ReqidCounter` — monotonic `_reqid` for the chat backend |
| `_runtime/auth.py` | `AuthRefreshCoordinator` — refresh task + auth-snapshot lock |
| `_auth_refresh_retry.py` | Shared auth refresh-and-retry core for the HTTP-status middleware and decoded-RPC `WebExecutionRuntime`, including the once-per-logical-call `RefreshBudget`. |
| `_runtime/lifecycle.py` | `ClientLifecycle` — loop-affinity guard + keepalive task |
| `_runtime/transport.py` | `RuntimeTransport` — authed-POST transport wrapper that drives the immutable runtime pipeline and typed transport response handling |
| `_rpc_executor.py` | Behaviorless `RpcExecutor(WebExecutionRuntime)` compatibility name; no execution implementation. |
| `_operations.py` | Closed P0 semantic vocabulary: `Operation` / `CallPolicy` enums and frozen, slotted, typed `OperationDef`, consumed by the private P1 backend port and registries. |
| `_projectors.py` | Shared compatibility projectors from neutral records to the existing public notebook/source/artifact/note/label/collection/sharing/research models, using normal constructors with no positional indices, RPC IDs, or public parsing factories. |
| `_notebook_mutation_service.py` | Private P2.2 transport-neutral notebook create/title-update/delete service; validates semantic input and invokes only typed backend definitions. |
| `_mutation_services.py` | Private live P2.3 transport-neutral URL-source mutation service; carries the ordinary/YouTube request and uncertainty receipt through `BackendAdapter` without wire dependencies. |
| `_read_services.py` | Private P2.1 transport-neutral notebook/source list/get services; invokes only typed operation definitions through `BackendAdapter`, forwards `RuntimeDeadline`, and delegates public-model construction to `_projectors.py`. |
| `_source_service.py` | Private transport-neutral service for source content, refresh, Drive, upload, and remaining source variants over typed operation definitions and records. |
| `_label_service.py` | Private P6.4 transport-neutral source-label/collection service over one discriminated neutral record family and eleven typed operations. |
| `_research_service.py` | Private P6.2 transport-neutral Research start/poll/wait/cancel/import service; wait and verified import remain service compositions over typed backend operations. |
| `_settings_service.py` | Private P6.6 transport-neutral account settings/limits/language service over three typed backend operations. |
| `_chat/service.py` | Private P6.1 transport-neutral Chat ask/history/configuration/save-note service over six typed backend operations. |
| `_chat/stream_request.py` | Credential-aware streamed-Chat request encoder kept outside `_web`; consumes an injected auth snapshot without acquiring or persisting credentials. |
| `_chat/stream_decode.py` | Projects the retained streamed parser result into neutral Chat records outside the web provider boundary. |
| `_sharing_service.py` | Private P6.5 transport-neutral sharing service for status reads, link visibility/view-level mutation, and individual-user grants. |
| `_suggestion_service.py` | Private P6.6 transport-neutral notebook-prompt and report-format suggestion service over two typed backend operations. |
| `_studio/` | Private transport-neutral Studio boundary: the P5.1 heterogeneous catalog/classifier; P5.2–P5.6 family/generation/export services; P5.7 trusted representation retrieval plus local serialization clients; P5.8 management, lifecycle, suggestions, and representation orchestration; and the P6.3 interactive mind-map family. |
| `_studio/interactive.py` | Private P5.3 Quiz/Flashcards family service: typed generation dispatch, catalog-backed discovery, and family-usable readiness/user-state metadata without wire vocabulary. |
| `_studio/mind_maps.py` | Private P6.3 interactive mind-map family service: catalog-backed discovery plus typed generation/tree/update/delete dispatch. |
| `_web/backend.py` | Single web semantic backend, owner of the runtime/lifecycle/auth/metrics leaves and all 82 active semantic handlers. |
| `_web/runtime.py` | Sole batchexecute encode/dispatch/decode implementation (`WebExecutionRuntime`). |
| `_web/transport.py` | `WebTransport` (P9.1): the web backend's two transport verbs — `call` (one deadline-bound `batchexecute` call over `WebExecutionRuntime`, tagging escaped native errors `dispatched`) and `stream` (the chat-aware authed POST) — plus the frozen `WebRequest`/`WebStreamRequest` values and `assemble` for codec rows. Lifecycle stays on `WebRpcBackend`. |
| `_web/deadline_rpc.py` | Deadline/operation-bound compatibility caller used only inside legacy note-backed web composites. |
| `_web/chat.py` | P6.1 Chat web workflow mixin; owns ask/history/configuration/save-note handlers while keeping the composed backend below the module-size ratchet. |
| `_web/error_policy.py` | Closed native-to-semantic error classification and safe-diagnostic allowlist shared by the composed web backend. |
| `_web/failure_projection.py` | Bounded, serializable projection of public exception graphs into transport-neutral source failure records. |
| `_web/labels.py` | P6.4 source-label/collection web workflow mixin; since P9.3 only the four create/update composites and the shared set read they preflight through remain here (the list/get/delete/generate leaves are `_web/bindings/labels.py` rows). |
| `_web/research.py` | P6.2 Research web workflow mixin; owns fast/deep start, poll, cancel, and ordered import handlers while keeping the composed backend below the module-size ratchet. |
| `_web/sharing.py` | P6.5 Sharing web workflow mixin; owns the status/readback and mutation request dialects while keeping the composed backend below the module-size ratchet. |
| `_web/bindings/__init__.py` | `WEB_BINDING_ROWS` (P9.3): the union of every domain's binding rows, checked for one row per operation and canonical definitions; `_web/registry.py` partitions the supported set between these rows and the remaining handler names. |
| `_web/bindings/labels.py` | P9.3 labels/collections codec rows: `LABEL_LIST`, `LABEL_GET`, `LABEL_GENERATE`, `LABEL_DELETE`, `COLLECTION_LIST`, `COLLECTION_GET`, `COLLECTION_DELETE` — one `LIST_LABELS`/`CREATE_LABEL`/`DELETE_LABEL` call per row; the get rows select by exact id inside `decode`. |
| `_web/bindings/settings.py` | P9.3 settings/suggestions codec rows: `SETTINGS_GET`, `SETTINGS_GET_LIMITS`, `SETTINGS_SET_LANGUAGE`, `ARTIFACT_SUGGEST_REPORTS` — `encode → one native call → decode` with the `NativeCallSpec` as the sole method authority; the walker derives their catalog authorities from these module-level assignments. |
| `_web/settings_suggestions.py` | P6.6 prompt-suggestion web workflow mixin; since P9.3 only the input-defaulting `NOTEBOOK_SUGGEST_PROMPTS` composite remains here (the settings and report-suggestion leaves are `_web/bindings/settings.py` rows). |
| `_web/source_variants.py` | Web workflow mixin for URL add plus source content, freshness, refresh, Drive, upload, and remaining source variants; owns request dialects and composite reconciliation while keeping the composed backend below the module-size ratchet. |
| `_web/policy.py` | Exact P4 ledger for all 82 active web workflows: semantic policy, every reachable native method/variant, reviewed native idempotency, and optional reported divergence. |
| `_web/registry.py` | Closed web disposition registry over every `Operation`: 82 executable typed handlers plus explicit composite dispositions. |
| `_studio/catalog.py` | Typed P5.1 Studio list/get service over neutral artifact operation records. |
| `_studio/classifiers.py` | Closed neutral-artifact family classifier shared by Studio catalog selection. |
| `_studio/data_views.py` | P5.6 typed data-table and mind-map generation plus dual-backing catalog selection. |
| `_studio/exports.py` | P5.6 explicit Drive-export service over the typed semantic operation. |
| `_studio/management.py` | P5.8 management, slide revision, retry, and report-suggestion services over typed backend operations. |
| `_studio/lifecycle.py` | P5.8 lifecycle status service and unchanged public lifecycle-terminal polling coordination. |
| `_studio/representations.py` | P5.8 neutral artifact/mind-map representation selection and dispatch to P5.7 retrieval/serialization clients. |
| `_web/codec/` | P3/P6 web response ownership: notebook, source, artifact, Chat, label, collection, sharing, Research, settings, suggestions, and report/guide codecs return frozen neutral records; `documents.py` alone returns the approved exported `StructuredDocument` value exemption. Codec bindings are tied to cassette-backed golden families and never call public parsing factories. |
| `_web/codec/chat.py` | P6.1 unary Chat request/response codec over neutral records, retaining the streamed parser as a monkeypatchable compatibility seam. |
| `_web/codec/chat_stream.py` | Retained streamed-response parser; credential-aware request construction delegates outside `_web` to `_chat/stream_request.py`. |
| `_web/studio_documents.py` | P5.4 web workflow binding for report/video source resolution and generation kickoff; mixed into `WebRpcBackend` to keep the composed backend below the module-size ratchet. |
| `_web/studio_facade.py` | P5.8 web bindings for artifact management, lifecycle status, suggestions, and representation discovery/content reads. |
| `_web/studio_media.py` | Shared P5.2/P5.3/P5.5 web generation handlers for Audio, Quiz/Flashcards, and Infographic/Slide Deck; inherits the document-family RPC/source helpers and keeps the composed backend below the module-size ratchet. |
| `_web/studio_data.py` | P5.6 web handlers for data-table/mind-map generation and Drive export; composes with the media/document handlers while keeping the backend module below the size ratchet. |
| `_web/codec/studio_documents.py` | P5.4 exact report/video request encoders and generation-status decoder over backend-neutral records. |
| `_web/codec/notes.py` | P6.3 mixed note-row codec: normalizes flat/wrapped envelopes, classifies deleted and note-backed mind-map rows, preserves exact-id selection, and emits only neutral `NoteRecord` values. |
| `_web/codec/labels.py` | P6.4 shared source-label/collection codec: owns both wire dialects behind `LabelKind` and emits only neutral `LabelRecord` values; since P9.3 also the row-facing `encode_*`/`decode_*_result` payload builders and the dialect/scope contract guards. |
| `_web/codec/research.py` | P6.2 DiscoverSources codec: owns fast/deep start, poll, cancel, and report-before-web import request grammar and decodes responses into neutral Research records without selecting or dispatching an RPC. |
| `_web/codec/settings.py` | P6.6 account settings/limits/language request grammar and tolerant neutral decoders. |
| `_web/codec/suggestions.py` | P6.6 prompt/report suggestion request grammar and neutral decoders. |
| `scripts/audit_operation_catalog.py` | Single build/audit CLI for the deterministic ADR-0022 projection: exact semantic authorities, native bindings, public/root-client dispositions, evidence, omissions, and divergences. |
| `scripts/_operation_catalog_specs.py` | Reviewed semantic operation specifications, owners/policies/routes, native/web bindings, public methods, and dispositions. |
| `scripts/_operation_catalog_authorities.py` | Exact RPC/stream/upload/download/orchestrator authority allocations, semantic discriminators, and recency contracts. |
| `scripts/_operation_catalog_ast.py` | AST-derived public API, direct transport authority, application-caller, and recency audits. Since P9.2, module-level binding rows (`CodecBinding`/`CustomBinding` with a literal `NativeCallSpec`) are execution-authority sites alongside `_rpc_call` call sites; `derive_row_authorities()` yields the per-row `(operation, native) -> site` allocation and `audit_row_bindings()` checks each row's declared natives against the web call-policy ledger. |
| `scripts/_operation_catalog_evidence.py` | Per-variant decoder/golden scope and disposition, runtime-override dataflow/test proof, idempotency, and captured live-RPC evidence. |
| `tests/_guardrails/test_operation_catalog.py` | Fails closed on missing/duplicate/unallocated authorities, namespace/root-client dispositions, codec/golden evidence, override proof, unsupported variants, schema drift, and committed-baseline mismatch. |
| `_request_types.py` | Shared authed POST request construction types: `AuthSnapshot`, `BuildRequest`, `PostBody`, and materialization helpers. |
| `_transport_errors.py` | Transport exceptions, `Retry-After` parsing, and terminal `Kernel.post` error mapping for retry/auth middleware. |
| `_streaming_post.py` | Size-capped streaming POST helper used by `Kernel.post`. |
| `_runtime/rpc_call.py` | HTTP-shaped runtime request/response envelopes, pipeline-call type, and chain-composition primitive |
| `_runtime/rpc_call_state.py` | Closed typed `RpcCallState` configuration/progress carrier shared by identity across attempts |
| `_conversation_cache.py` | Per-instance true-LRU conversation cache for `ChatAPI` (caps conversation count via `MAX_CONVERSATION_CACHE_SIZE` and per-conversation turns via `MAX_TURNS_PER_CONVERSATION`) |
| `_polling_registry.py` | Pending-poll registry for long-running artifact generations |
| `_cookie_persistence.py` | Cookie-jar persistence + `__Secure-1PSIDTS` rotation |
| `_runtime/contracts.py` | Shared runtime Protocols consumed by sub-clients |
| `_idempotency.py` | Mutating-RPC idempotency policy registry and the web-adapter commit-uncertainty predicate (`transport_may_have_committed`); re-exports the probe-then-retry names; ADR-0005 is the taxonomy source |
| `_idempotency_create.py` | Registry-free probe-then-retry wrapper (`idempotent_create`, `mark_unconfirmed`, result provenance) parameterised by a commit-uncertainty predicate; `semantic_may_have_committed` is the neutral `BackendError` predicate (P9.2) |
| `_idempotency_policy.py` | Declarative per-RPC idempotency classification data, applied to `IDEMPOTENCY_REGISTRY` via `register_default_policies` at `_idempotency` import time (#1331). Holds the load-bearing two-pass seeding order (pre-seed `register()` → `_seed_defaults()` → post-seed `register()` + the read/set-op loop). |
| `_atomic_io.py`, `io.py` | Atomic JSON write/update internals and public I/O re-export surface for CLI boundary compliance |
| `exceptions.py` | Public exception hierarchy plus safe diagnostic preview/redaction helpers |
| `paths.py`, `migration.py` | Profile-aware path resolution and locked migration from the legacy flat layout |
| `_types/`, `types.py` | Dataclass implementation package and public type/re-export facade |
| `_types/documents.py` | `StructuredDocument` / `DocumentBlock` / `TextSpan` / `TableCell` / `DocumentAnnotation` / `BlockKind` / `BlockStyle` / `ListStyle` / `ListInfo` — the transport-neutral parsed-document types behind `SourceFulltext.document` and `AskResult.answer_document`, carrying the character offsets citations anchor to (#2128, #2120). `StructuredDocument.render()` derives the readable flat rendering (`SourceFulltext.rendered_content`) from the same tree, and is what `utils.resolve_chat_reference_passage` returns once it has resolved a citation by offset (#2211); `DocumentBlock.table_rows` carries the table cell ranges that rendering separates on, as offsets, so the coordinate space is untouched (#2230) |
| `_types/labels.py` | `Label` pure-value type (source-label topic grouping; `source_ids` only, no artifact members) re-exported by `types.py` |
| `_types/collections.py` | `Collection` pure-value type (account-level notebook grouping; `notebook_ids`, no notebook parent) re-exported by `types.py`; decodes positionally (its own strict descent, not `LabelRow` — populated members are bare notebook-id strings, not `LabelRow`'s wrapped-singleton source ids) |
| `_row_adapters/artifacts.py` | `ArtifactRow` typed view over raw positional artifact RPC rows, plus `ReportSuggestionRow` over `GET_SUGGESTED_REPORTS` rows |
| `_row_adapters/chat.py` | Streamed-chat row adapters (`AnswerRow` / `CitationRow` / `CitationDetail` / `StreamFrameRow` / `ErrorPayloadRow`) that centralise the chat wire positions `_chat/wire.py` used to open-code (#1491). `AnswerRow.document` and `CitationDetail.fragment_elements` delegate the document tree to `_row_adapters/documents.py` (#2120) |
| `_row_adapters/documents.py` | `TailwindDoc` tree adapters (`DocumentBodyRow` / `StructuralElementRow` / `ParagraphRow` / `ParagraphElementRow` / `TextRunRow` / `TableRow` / `BulletInfoRow` / `AnnotationEntryRow`) plus the `build_document` / `build_blocks` builders. One decoder for all three carriers of the tree — source fulltext, chat-answer `responseDoc`, and a citation's `TailwindDocFragment` — so citation offsets on both sides share a coordinate space (#2128, #2120) |
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
| `_artifacts.py` | `client.artifacts` compatibility facade — validates public inputs, delegates to typed Studio services, and projects existing public return/error types without native RPC authority. |
| `_chat/api.py` | `client.chat` API |
| `_research.py` | `client.research` API |
| `_research_import.py` | Free-function helpers for `ResearchAPI` source import + verification: URL normalization, the report-source predicate, imported-entry/merge helpers, and the #1961 idempotency pre-filter (skip already-present URLs) with its `already_present` side-channel carrier. Split out of `_research.py` under the ADR-0008 module-size ratchet. |
| `_notes.py` | `client.notes` API |
| `_sharing.py` | `client.sharing` API — transport-free facade over the semantic Sharing service |
| `_labels.py` | `client.labels` API — source labels (topic groupings); pure-RPC like `SharingAPI`, plus a narrow `list_sources` callable for the membership→`Source` join in `sources()` |
| `_collections.py` | `client.collections` API — account-level notebook groups; reuses the label RPCs (type-3, null notebook parent, `source_path="/"`), plus a narrow `list_notebooks` callable for the membership→`Notebook` join in `notebooks()` |
| `_settings.py` | `client.settings` transport-free compatibility facade over the P6.6 semantic Settings service. |
| `_note_service.py` | Semantic note/note-backed-mind-map workflow plus bounded legacy compatibility owner |
| `_mind_map.py` | Deferred note-backed compatibility adapter outside the migrated mind-map facade |
| `_mind_maps_api.py` | `client.mind_maps` API — transport-free dual-service facade over semantic note-backed and interactive Studio mind maps (#1256) |
| `_artifact/downloads.py` | Retired P5.8 compatibility exports for public download result/security helpers; representation orchestration lives in `_studio/representations.py`. |
| `_artifact/_redirect_guard.py` | Per-redirect-hop host/scheme revalidation for downloads — rejects off-allowlist / non-HTTPS redirect targets before the request is sent (#1521) |
| `_artifact/_download_client.py` | Download trusted-host allowlist + transport-aware client factory — wires the #1521 redirect guard for httpx (event hook) or the opt-in curl_cffi (`get_guarded` manual loop) |
| `_studio/downloads.py` | Representation byte retrieval client; reuses the canonical download-client factory, trusted-host predicate, and per-hop redirect guard |
| `_studio/serialization.py` | RPC-free local serializers for Studio text, JSON, and CSV representations |
| `_artifact/formatters.py` | Markdown, HTML, and plain text formatters for artifacts |
| `_artifact/payloads.py` | Stable CREATE_ARTIFACT / GENERATE_MIND_MAP request payload builders |
| `_artifact/validation.py` | Input-validation guards for the `ArtifactsAPI` facade (`generate_report` format coercion, `export` exactly-one-of target), kept in a sibling module so the facade stays under the module-size ratchet (#1874) |
| `_artifact/generation.py` | Retired P5.8 import-compatible helper module; family services and `_studio/management.py` own generation/management behavior. |
| `_artifact/listing.py` | Listing and filtering operations for notebook artifacts |
| `_artifact/polling.py` | Poll coordination service for artifact generation tasks |
| `_source/add.py` | Core service layer for adding text, URL, or Google Drive sources |
| `_source/batch.py` | True-batch URL `ADD_SOURCE` service for the existing MCP/REST batch endpoints: typed positional outcomes, omitted-row reconciliation, and fail-closed transport/duplicate ambiguity policy |
| `_source/drive_import.py` | Auto-route add-from-Drive (#1884): download + upload the upload-only Drive types (epub/docx/txt/…); native import (`add_drive`) instead takes Docs/Slides/Sheets + PDF by reference; header-first cookie-authed streaming fetch behind injected seams |
| `_source/content.py` | Core service layer for fetching source HTML/markdown content |
| `_source/markdown.py` | Source fulltext HTML-to-Markdown conversion policy, including Markdown-source and LaTeX/table handling |
| `_source/listing.py` | Core service layer for listing notebook sources |
| `_source/polling.py` | Poll coordination service for active source conversions |
| `_source/upload.py` | Concurrency-gated upload pipeline for source files |
| `_source/_upload_decode.py` | Pure decode/validation helpers for the upload pipeline (URL redaction, ADD_SOURCE_FILE source-id extraction, content-type policy), extracted from `upload.py` |
| `_source/upload_payloads.py` | Stable source upload registration, rename, and resumable-upload request builders |
| `_notebook_metadata.py` | Metadata protocol schemas for sub-clients |
| `_url_utils.py`, `urls.py` | URL parsing/validation internals and the public URL helper facade |
| `_sharing_manager.py` | Direct sharing management logic |
| `_version_check.py` | Dynamic client-side version deprecation guard |
| `_version_info.py` | Human-facing `version_string()` — package version + short git commit (embedded by `hatch_build.py` at build time, or live `git` from a checkout) |
| `_chat/notes.py` | Chat-adjacent note saving workflow adapter |
| `_chat/history.py` | Server-backed complete-history snapshot and user-question turn counting for authoritative `AskResult.turn_number` values |
| `_chat/wire.py` | Streamed-chat wire request construction + response parsing for the chat client |
| `_chat/transport.py` | Chat-specific error mapping over the shared transport pipeline |
| `_chat/deleted_tracker.py` | Bounded `RecentlyDeletedConversations` set — `delete_conversation` records the id (under the conversation lock) so a concurrent null-conversation ask, after acquiring that lock, detects a mid-flight delete and drops `resolved_id_override` to recover the server's real conversation id post-POST (#1875) |
| `_runtime/pipeline.py` | Composes the runtime behaviors in the canonical ADR-0009 order |
| `_runtime/*_behavior.py` | Modular runtime behaviors (drain, metrics, semaphore, retry, auth refresh, tracing) |
| `rpc/types.py` | RPC method IDs (source of truth) |
| `auth.py` | Authentication facade — **almost pure re-exports** (the only remaining function body is `async def enumerate_accounts`, which binds `_poke_session` as a default dependency; ADR-0003 records the optional-`async` audit command). Every other top-level name forwards from the relevant `_auth/*` module: `auth._validate_required_cookies` is identity-equal to `_auth.cookie_policy._validate_required_cookies`, and `load_auth_from_storage` / `AuthTokens` live in `_auth/tokens.py`. **ADR-0003's flat-re-export goal was closed by ADR-0014.** Tests that need to rebind policy names patch `_auth.cookie_policy.X` directly. |
| `_auth/paths.py` | Storage paths and filesystem helpers |
| `_auth/extraction.py` | Cookie/token extraction from browser sessions |
| `_auth/cookies.py` | Compatibility cookie loaders/converters and logging boundaries over the pure codecs + `_update_cookie_input` |
| `_auth/cookie_policy.py` | Cookie-domain allowlist, `build_cookie_domain_allowlist` builder, and policy decisions |
| `_auth/cookie_semantics.py` | Dependency-bottom cookie scalar/row codecs: shape, expiry, legacy/rookiepy adaptation, stdlib construction, and row serialization |
| `_auth/cookie_types.py` | Canonical immutable `Cookie`/`CookieJar` values; depends downward only on cookie policy and semantics (ADR-0032) |
| `_auth/cookie_filter.py` | Pure raw capture/domain filter and value-free malformed-row diagnostics; no paths, I/O, locks, commits, or lifecycle state (ADR-0034) |
| `_auth/profile_account.py` | Dependency-bottom immutable account/directive/domain/session values and pure namespace parsers; consumed by profile documents, `ProfileStore`, and the raw storage adapters (ADR-0034) |
| `_auth/profile_document.py` | Recursively immutable, lossless raw profile snapshot with isolated typed views and copy-on-write cookie/namespace updates; consumed by the pure cookie merge leaf and its storage transaction adapter (ADR-0034) |
| `_auth/cookie_merge.py` | Pure immutable cookie snapshot/CAS and permanent no-baseline overlay decisions. Its post-merge baseline selects accepted identities from authoritative final rows and retains rejected identities from the old baseline; no paths, locks, I/O, logging, facade, or lifecycle dependencies (ADR-0034) |
| `_auth/credential_io.py` | Sealed typed profile/master-token commit capability over the sole unchecked atomic forwarder (ADR-0034) |
| `_auth/master_token_types.py` | Immutable redacted token value + permissive pure legacy-record codec (ADR-0034) |
| `_auth/master_token_file.py` | Explicit-path one-sample raw/typed token I/O + canonical bounded-lock commit (ADR-0034) |
| `_auth/mint_service.py` | Stateless master-token exchange/cookie minting + sole raw RotateCookies wire; no disk or policy imports (ADR-0034) |
| `_auth/master_token_bootstrap.py` | One-store bootstrap/re-mint/missing-storage coordinator; owns ordering, outcomes, and cancellation settlement without runtime/client or arbitrary token-file capability (ADR-0034) |
| `_auth/profile_store.py` | Path-owned fresh document/session/account/derived-token reads, token/account writes, blocking cookie transactions, and bounded remint/login/minted replacement; minted owns latest-owner/filter/rebind/commit (ADR-0034) |
| `_auth/profile_migration.py` | Legacy account context, lossless two-read resolution, promotion, retryable single-flight scheduler/exit drain, and login/account reconciliation (ADR-0034) |
| `_auth/account_email.py` | Generation-safe persisted/live account-email matching, probing, cache results, and exact-document CAS self-heal |
| `_auth/browser_cookie_recovery.py` | Shim: re-exports the captured-cookie validate/heal seam from `psidts_recovery.py` (removed at next major) |
| `_auth/browser_state_validation.py` | Shim: re-exports `heal_captured_state` from `browser_capture.py` (removed at next major) |
| `_auth/browser_capture.py` | One deep module for the browser launch→capture→filter→heal→persist core (ADR-0033 merge: absorbed `browser_state_validation.py` + `login_wait_trace.py`), lazy `playwright`; shared by the interactive CLI login adapter (`cli/services/playwright_login.py`) and the layer-3 headless re-auth layer (ADR-0021). Carries the login-wait DEBUG tracing (host-only `trace_url`, inert when DEBUG is off) and the never-raising `heal_captured_state` |
| `_auth/_browser_cookie_filter.py` | Shim: re-exports `storage.py`'s exact alias of the canonical `cookie_filter` function (removed at next major) |
| `_auth/navigation_errors.py` | `net::ERR_*` extraction + the narrow/broad navigation-failure predicates (`is_navigation_race` vs `is_navigation_failure`); pure leaf of `browser_capture.py` (ADR-0008, #2257) |
| `_auth/browser_launch_errors.py` | `CHANNEL_BROWSERS` registry + `classify_launch_failure` launch-failure triage (not-installed / bundled-Chromium-missing / Windows `spawn UNKNOWN` execution veto); pure leaf of `browser_capture.py` (ADR-0008) |
| `_auth/headless_reauth.py` | Layer-3 headless re-auth decision layer: opt-in/profile-gated, typed honest outcomes (`HeadlessReauthStatus`); drives `run_browser_capture(headless=True, interactive=False)`. Local-unattended-only |
| `cli/label_cmd.py` | `label` command group (list/sources/generate/create/rename/emoji/add/remove/delete); thin Click shells over `client.labels`, `_app.labels`, and the label-listing service (ADR-0008/0021) |
| `cli/collection_cmd.py` | `collection` command group (list/notebooks/create/rename/add/remove/delete); account-level (no `--notebook` option); thin Click shells over `client.collections` + `_app.collections` (ADR-0008/0021) |
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
├── _backend.py                  # Private semantic backend port, capabilities, errors, and deadline handoff (P1)
├── _binding.py                  # Neutral binding rows, table, audit, and dispatch function (P9.0)
├── _backend_compat.py           # Closed backend-error to legacy public-exception projector (P2)
├── _backoff.py                  # Shared retry backoff calculation
├── _callbacks.py                # Sync/async callback invocation helper
├── _client_composition.py       # Production-only client composition root
├── _deadline.py                 # RuntimeDeadline helper for aggregate timeouts
├── _deprecation.py              # Immutable auth-storage specs + gated deprecation emitters
├── _env.py                      # Runtime environment/default endpoint helpers
├── _idempotency.py              # Mutating-RPC idempotency registry + adapter predicate
├── _idempotency_create.py       # Registry-free probe-then-retry wrapper + neutral predicate
├── _idempotency_policy.py       # Declarative per-RPC idempotency classification data (register_default_policies)
├── _kernel.py                   # Concrete Kernel transport core
├── _logging.py                  # Redaction + correlation logging internals
├── _secrets.py                  # Canonical runtime secret registry (cookie names + secure/host umbrellas + token/API-key shapes) the redaction patterns derive from
├── _lookup.py                   # unwrap_or_raise — shared single-row-lookup helper for get/get_or_none
├── _serving.py                  # Shared bootstrap for both HTTP entry points: single-source loopback classification (IPv4-mapped-IPv6-aware) + non-loopback bind guard (mcp/server __main__ + server/_auth all route through it)
├── _loop_affinity.py            # Event-loop affinity guard helper (assert_bound_loop free function)
├── _loop_bound.py               # LoopBoundPrimitive mixin — template-method set_bound_loop + _on_loop_rebind hook for the loop-bound collaborators
├── _error_injection.py          # Synthetic-error env-var resolver + startup guard
├── _request_types.py            # AuthSnapshot, BuildRequest, PostBody, request materialization helpers
├── _web_cookie_provider.py      # Frozen generation/session values + provider/session ports (P8)
├── _web_request_auth.py         # Credential-to-wire adapter over immutable web generations (P8)
├── _transport_errors.py         # Transport exceptions, Retry-After parsing, Kernel.post error mapping
├── _streaming_post.py           # Size-capped streaming POST helper
├── _curl_cffi_transport.py      # Opt-in curl_cffi browser-impersonation transport (NOTEBOOKLM_TRANSPORT=curl_cffi)
├── _rpc_executor.py             # Behaviorless WebExecutionRuntime compatibility name
├── _rpc_semaphore.py            # Loop-bound logical-RPC concurrency owner
├── _client_metrics.py           # Telemetry / metrics seam
├── _transport_drain.py          # In-flight transport drain coordinator
├── _reqid_counter.py            # Request-counter / request-id helpers
├── _conversation_cache.py       # Per-instance true-LRU conversation cache (bounded conversation count + per-conversation turns)
├── _polling_registry.py         # Artifact polling helpers
├── _cookie_persistence.py       # Cookie-jar persistence + __Secure-1PSIDTS rotation
├── _note_service.py             # Semantic NoteService + deferred LegacyNoteBackedService
├── _mind_map.py                 # Deferred NoteBackedMindMapService compatibility adapter
├── _mind_maps_api.py            # MindMapsAPI — semantic dual-service facade (#1256)
├── _notebook_metadata.py        # Metadata protocols
├── _operations.py               # Closed semantic operation/call-policy vocabulary (P0)
├── _projectors.py               # Neutral record-to-public compatibility projectors (P2/P3/P5/P6)
├── _artifact_records.py         # Neutral artifact parse-failure records split from the shared record hub
├── _notebook_records.py         # Neutral notebook inputs/results/records and typed operation definitions
├── _note_records.py             # Neutral plain-note inputs/results/records and typed operation definitions
├── _source_records.py           # Neutral source inputs/results/records and typed operation definitions
├── _notebook_mutation_service.py # Transport-neutral notebook mutation service (P2.2)
├── _notebook_guide_service.py   # Transport-neutral notebook summary/description service
├── _mutation_services.py        # Transport-neutral URL-source mutation service (P2.3, live)
├── _read_services.py            # Transport-neutral notebook/source list/get services (P2.1)
├── _source_service.py           # Transport-neutral source content/mutation/wait service
├── _label_service.py            # Transport-neutral source-label/collection service (P6.4)
├── _label_records.py            # Neutral source-label/collection records/operation definitions (P6.4)
├── _research_service.py         # Transport-neutral Research service (P6.2)
├── _research_records.py         # Neutral Research records/operation definitions (P6.2)
├── _research_neutral.py         # Research public-model to neutral-record conversion helpers
├── _settings_service.py         # Transport-neutral settings service (P6.6)
├── _settings_records.py         # Neutral settings records/operation definitions (P6.6)
├── _suggestion_service.py       # Transport-neutral suggestion service (P6.6)
├── _chat_records.py             # Neutral Chat records/operation definitions (P6.1)
├── _chat/                       # Chat facade/service plus retained compatibility wire seams
│   ├── service.py               # Transport-neutral six-operation Chat service (P6.1)
│   ├── stream_request.py        # Credential-aware streamed request encoding outside _web
│   └── stream_decode.py         # Stream parser result -> neutral Chat records
├── _sharing_service.py          # Transport-neutral Sharing service (P6.5)
├── _sharing_records.py          # Neutral Sharing records/operation definitions (P6.5)
├── _records.py                  # Compatibility re-export hub for neutral P2/P5/P6 DTOs/definitions
├── _studio/                     # Private Studio family package
│   ├── catalog.py               # Heterogeneous neutral list/get catalog (P5.1)
│   ├── classifiers.py           # Closed family classifier (P5.1)
│   ├── audio.py                 # Audio generation/readiness/download metadata service (P5.2)
│   ├── interactive.py           # Quiz/Flashcards generation/readiness service (P5.3)
│   ├── documents.py             # Report/video generation and metadata families (P5.4)
│   ├── visuals.py               # Infographic/slide-deck generation/readiness family (P5.5)
│   ├── data_views.py            # Data-table/mind-map generation and catalog family (P5.6)
│   ├── exports.py               # Explicit Drive companion export service (P5.6)
│   ├── downloads.py             # Trusted remote representation byte client (P5.7)
│   ├── serialization.py         # RPC-free local representation serializers (P5.7)
│   ├── management.py            # Revision/retry/rename/delete + suggestions (P5.8)
│   ├── lifecycle.py             # Lifecycle observation and terminal polling (P5.8)
│   ├── representations.py       # Representation selection/retrieval orchestration (P5.8)
│   └── mind_maps.py             # Interactive Studio mind-map family (P6.3)
├── _url_utils.py                # URL validation helpers
├── _sharing_manager.py          # Sharing management logic
├── _version_check.py            # Deprecation version guard
├── _version_info.py             # version_string(): version + short git commit
├── _research_task_parser.py     # Research task result-type parser
├── _research_import.py          # ResearchAPI import/verification helpers + #1961 idempotency pre-filter
├── _redact.py                   # Transport-neutral secret/home-path/file-link scrubber (redact(msg, max_length)); shared chokepoint under both mcp/_errors.py and server/_errors.py
├── _web/                        # Private web implementation of the semantic backend port
│   ├── __init__.py              # Lazy private WebRpcBackend re-export (leaf-codec safe)
│   ├── backend.py               # Single semantic backend + client-runtime owner
│   ├── runtime.py               # Sole web RPC encode/dispatch/decode engine
│   ├── deadlines.py             # Closed semantic deadline-authority ledger
│   ├── deadline_rpc.py          # Deadline-bound legacy composite caller
│   ├── chat.py                  # P6.1 Chat workflow handlers
│   ├── chat_transport.py        # Streamed Chat transport adapter and bounded error translation
│   ├── error_policy.py          # Closed web error classification/diagnostics ledger
│   ├── failure_projection.py    # Bounded public-exception graph to neutral failure records
│   ├── labels.py                # P6.4 source-label/collection workflow handlers
│   ├── research.py              # P6.2 Research workflow handlers
│   ├── sharing.py               # P6.5 Sharing workflow handlers
│   ├── settings_suggestions.py  # P6.6 prompt-suggestion composite handler
│   ├── bindings/                # P9.3 per-domain binding rows
│   │   ├── __init__.py          # WEB_BINDING_ROWS union
│   │   ├── labels.py            # label/collection codec rows
│   │   └── settings.py          # settings/suggestion codec rows
│   ├── policy.py                # P4 semantic/native policy parity ledger (reporting only)
│   ├── registry.py              # Closed active/unsupported web dispositions
│   ├── studio_documents.py      # P5.4 web report/video workflow handlers
│   ├── studio_media.py          # P5.2/P5.3/P5.5 web family handlers
│   ├── studio_data.py           # P5.6 data-view generation and Drive-export handlers
│   ├── studio_facade.py         # P5.8 management/lifecycle/suggestion/representation handlers
│   ├── source_variants.py       # Source add/content/refresh/Drive/upload/wait workflow handlers
│   ├── transport.py             # P9.1 WebTransport call/stream verbs, WebRequest/WebStreamRequest
│   └── codec/                   # P3 web response codecs producing neutral records/value exemptions
│       ├── __init__.py          # Private codec re-exports
│       ├── artifacts.py         # Artifact/mind-map/report-suggestion rows -> neutral records
│       ├── chat.py              # Unary Chat codecs over neutral records (P6.1)
│       ├── chat_saved_note.py   # Saved-from-Chat CREATE_NOTE encoding
│       ├── chat_stream.py       # Retained streamed response parser
│       ├── collections.py       # Strict collection tuples -> neutral records
│       ├── documents.py         # Tailwind body -> exempt StructuredDocument value graph
│       ├── labels.py            # Shared source-label/collection tuples -> neutral records
│       ├── notebooks.py         # Project + notebook-guide payloads -> neutral records
│       ├── notes.py             # Mixed web note rows -> neutral Note/MindMap records
│       ├── mind_maps.py         # Interactive mind-map tree/create response codecs
│       ├── research.py          # DiscoverSources grammar and neutral decoders (P6.2)
│       ├── settings.py          # Account settings/limits/language neutral codec (P6.6)
│       ├── sharing.py           # Share status/user rows -> neutral records
│       ├── sources.py           # Source row variants -> neutral records
│       ├── studio_documents.py  # Exact report/video request and status codecs
│       └── suggestions.py       # Prompt/report suggestion neutral codec (P6.6)
├── _app/                        # Transport-neutral business-logic layer (CLI/MCP/HTTP adapters share it)
│   ├── __init__.py              # Re-exports the neutral primitives
│   ├── artifacts.py             # Click-free artifact core: get/rename/delete/export + poll/wait/retry; kind-aware mind-map dispatch (mind_maps.list for rename, notes.list_mind_maps for delete), get_artifact raises ArtifactNotFoundError, typed Rename/Export results + ArtifactStatusView/status_view neutral status DTO (CLI builds every --json envelope from the typed fields)
│   ├── auth_check.py            # Click-free `auth check` diagnostics core: run_auth_check(plan, read_env_auth_json=…) -> AuthCheckResult (storage-exists/json-valid/cookies-present/SID + optional token-fetch); AuthCheckPlan carries pre-resolved values + the auth_source display label; inline-auth read injected (CLI owns the AuthSource plan-build + Rich table + exit code)
│   ├── chat.py                  # Click-free chat core: conversation-id selection ladder + configure mode/goal/length dispatch + history fetch/format-as-data + ask save-as-note workflow (raises public ValidationError; status emitted into injected ProgressSink)
│   ├── doctor.py                # Click-free doctor core: run_checks(*, fix, paths) -> DoctorReport (five checks incl. headless-reauth readiness + fixes + has_failures; DoctorPaths injects the path helpers; CLI owns rendering/exit codes)
│   ├── download.py              # Click-free download core: DownloadPlan/Result + build_download_plan/execute_download (injected resolvers; CLI builds the --json envelope from the typed DownloadResult)
│   ├── download_specs.py        # Canonical artifact-download registry: type/binding + per-format extension/MIME descriptors; derives DownloadTypeSpec projections, MIME lookup, and adapter schema enums
│   ├── errors.py                # classify(exc) -> ClassifiedError (category + retriable); class-sensitive
│   ├── events.py                # ProgressEvent + ProgressSink Protocol (neutral progress seam)
│   ├── generate.py              # Click-free `generate` executor: execute_generation (injected notebook/source resolvers preserve the RPC fast paths) + GenerationExecutionResult; re-exports the plan/retry surface so `_app.generate` is the single import point
│   ├── generate_plans.py        # Click-free `generate` plan-building: enum/format maps, GenerationPlan/GenerationKind/GenerationPlanValidationError, build_generation_plan + per-kind builders (parameter_explicit/language_resolver injected)
│   ├── generate_retry.py        # Click-free `generate` retry/wait: GenerationOutcome, generate_with_retry, handle_generation_result, status extractors, spinner status-line formatter (wait_context/wait_start_sink neutral seams)
│   ├── labels.py                # Click-free label core: create/sources/generate/rename/emoji/add/remove/delete + the composite resolve_label_id (<id|name>) resolver + LabelResolutionError (injected notebook/source resolvers; members→titles JOIN render stays in cli/services/label_listing.py)
│   ├── collections.py           # Click-free collection core: list/notebooks/create/rename/add/remove/delete + the composite resolve_collection_id (<id|name>) resolver + CollectionResolutionError (account-level; no notebook scope)
│   ├── language.py              # Click-free language core: SUPPORTED_LANGUAGES catalog + is_supported_language + LanguageConfigStore (injected config-path/home/atomic-update; get/save/get_language/set_language)
│   ├── login_cookie.py          # Click-free login/cookie-import operations: request validation, browser-jar probing, account projection, and profile-write orchestration over call-time public auth capabilities
│   ├── master_token.py          # Click-free master-token operations: bootstrap/remint/status plans and results with bounded credential-bearing context, narrow status errors, and call-time public auth capabilities
│   ├── mcp_install.py           # Click-free `mcp install <client>` core: supported-client catalog (claude-desktop/claude-code/cursor/windsurf) + per-OS resolve_config_path + uvx build_server_block + merge_server_config read-modify-merge into mcpServers (created/updated/unchanged; never clobbers unrelated keys); UnsupportedClientError. CLI owns the atomic write (cli/mcp_cmd.py)
│   ├── notebooks.py             # Click-free notebook core: create/delete/rename/describe(summary)/metadata fetch+compute (injected resolve_notebook_id; summary/metadata serializers stay in cli/notebook_cmd.py)
│   ├── notes.py                 # Click-free note core: create/get/save/rename/delete (typed-facade only — notes.create returns a Note) + content-preserving rename (resolve_note_content); found-flag results map to the CLI NOT_FOUND/exit-1 path (injected notebook/note resolvers)
│   ├── pagination.py            # Transport-neutral bounded-slice paginate(items, limit, offset) -> (page, {total,offset,has_more}) with bound validation; the shared slice under both the MCP *_list tools and the REST list-route envelope (Option B-lite)
│   ├── profile.py               # Click-free profile core: gather_profile_list -> ProfileEntry rows (injected list_profiles/resolve_profile/get_storage_path/read_account_metadata), is_protected_profile delete-guard decision, set_default/retarget_default config.json mutators (CLI keeps the locked _atomic_write_config + click.confirm + Rich render)
│   ├── research.py              # Click-free `research` status/wait core: poll_and_classify -> ResearchStatusResult, ResearchWaitPlan/Result + execute_research_wait (resolver/importer/wait-context injected), validate_research_wait_flags (-> ValidationError); returns typed results only (CLI owns the --json envelope)
│   ├── resolve.py               # Click-free validate_id + resolve_ref (AmbiguousIdError/Resolution)
│   ├── serialize.py             # to_jsonable(obj) recursive JSON-able conversion (enum-before-primitive) + source_summary, the narrow {id,title,type,url} shape the add envelopes publish (kept narrow on purpose: adapter-specific per-source fields are composed on top in the adapter, not added here)
│   ├── session.py               # Click-free session-context core: `use` verify_and_set_notebook (injected resolve_notebook_id) + `status` read_status(StatusInputs) read+project -> StatusReport + `auth logout` execute_logout(LogoutInputs) filesystem-teardown -> typed LogoutOutcome (path/context/clear_context helpers injected via bundles; CLI owns Rich render + exit codes)
│   ├── sharing.py               # Click-free sharing core: status/set_public/set_view_level/add_user/update_user/remove_user (injected resolve_notebook_id; permission/view-level display + str→enum parse stay in cli/share_cmd.py)
│   ├── skill.py                 # Click-free skill-install core: TARGETS/SCOPES catalog + path/version helpers + classify_target (create/up_to_date/overwrite) + report_mixed_no_clobber_up_to_date (CLI owns the atomic write + packaged-source loader)
│   ├── source_add.py            # Click-free `source add` core: input detection + URL SSRF/upload-path validation + add workflow (SourceAddPlan/Result; CLI builds the --json source-summary from the typed result via the neutral serialize.source_summary helper)
│   ├── source_batch.py          # Transport-neutral batch-add policy shared by the MCP tool + REST route (#1871): MAX_BATCH_URLS cap + batch_item_is_fatal (fatal auth/rate-limit/5xx classification via _app.errors.classify → abort the batch; per-URL 4xx-input failures isolate). Neutral frozenset, not server's CATEGORY_STATUS (the _app boundary forbids fastapi); parity pinned by tests/server/test_source_batch_parity.py
│   ├── source_clean.py          # Click-free `source clean` core: junk-source classification + batched-deletion orchestration (SourceCleanResult; injected list/delete/confirm callables)
│   ├── source_content.py        # Click-free read-only source-content fetchers for get/fulltext/guide/stale (typed plan/result pairs)
│   ├── source_listing.py        # Click-free `source list` fetch core: fetch_sources (label_filter resolution; label_resolver injected)
│   ├── source_mutations.py      # Click-free source delete/delete-by-title/rename/refresh/add-drive core: resolvers + SourceMutationError + typed results (validate_id/resolve_source_id injected; confirmer injected)
│   ├── source_research.py       # Click-free `source add-research` start/wait/import workflow + validate_add_research_flags (importer injected; SourceAddResearchPlan/Result)
│   ├── source_wait.py           # Click-free `source wait` validation/outcome core: execute_source_wait + typed SourceWaitOutcome (wait_context injected) + wait_all_sources (delegates the single-snapshot loop to client.sources.wait_all_until_ready — one notebook poll per tick, order-preserving; #1870) shared by the MCP tool + REST route (#1871) + the MAX_WAIT_TIMEOUT / MAX_WAIT_SOURCE_IDS caps
│   └── views.py                 # Transport-neutral output-projection views: share_status_view (access/permission/view_level enum→label), source_view (kind/status_label/drive_status_label + is_drive_degraded added), notebook_view (role_label added), notebook_viewed_keys (last_viewed_at + its deprecated modified_at alias, for hand-built CLI JSON envelopes), ask_result_view (raw_response debug blob stripped); shared by the MCP tools + REST routes so both emit the identical enriched shape (Option B)
├── _runtime/                    # Client-runtime subpackage (promoted from flat _runtime_*.py, #1328)
│   ├── __init__.py              # Re-exports the cluster's public names
│   ├── auth.py                  # AuthRefreshCoordinator (refresh task + auth-snapshot lock)
│   ├── config.py                # DEFAULT_* knobs + module-level constants
│   ├── contracts.py             # Shared runtime Protocols consumed by feature APIs
│   ├── helpers.py               # is_auth_error / AUTH_ERROR_PATTERNS / keepalive helpers
│   ├── init.py                  # Runtime collaborator construction + validation
│   ├── lifecycle.py             # Open/close lifecycle seam (loop affinity + keepalive task)
│   ├── pipeline.py              # Immutable ordered runtime behavior pipeline
│   ├── rpc_call.py              # Typed request/response envelopes + pipeline-call shape
│   ├── rpc_call_state.py        # Typed RpcCallState configuration/progress carrier
│   ├── auth_refresh_behavior.py # Auth-refresh behavior
│   ├── drain_behavior.py        # Drain behavior
│   ├── metrics_behavior.py      # Metrics behavior
│   ├── retry_behavior.py        # Retry behavior
│   ├── semaphore_behavior.py    # RPC concurrency behavior
│   ├── tracing_behavior.py      # Tracing behavior
│   ├── web_backend_session.py   # Detached private backend session over its own Kernel (P8)
│   ├── web_cookie_provider.py   # Existing-auth compatibility adapter behind the provider port (P8)
│   └── transport.py             # Runtime-pipeline transport wrapper
├── _source/                     # Source-feature subpackage (promoted from flat _source_*.py, #1328)
│   ├── __init__.py              # Re-exports the cluster's public service classes
│   ├── _upload_decode.py        # Pure URL/source-id/content-type decode + validation helpers (extracted from upload.py)
│   ├── add.py                   # Source addition coordinator
│   ├── batch.py                 # True-batch URL ADD_SOURCE coordinator + typed positional outcomes for MCP/REST batch adapters (#2115)
│   ├── content.py               # Source content fetcher
│   ├── markdown.py               # Source fulltext HTML-to-Markdown conversion policy
│   ├── drive_import.py          # Auto-route add-from-Drive (#1884): DriveImportService + DriveFetcher — parse id/URL, cookie-authed header-first streaming download of the upload-only Drive types (epub/docx/txt/…), confirm-token handling + 0600 temp cleanup, then hand to add_file (native Docs/Slides/Sheets → pointer error)
│   ├── listing.py               # Source listing helper
│   ├── polling.py               # Source polling coordinator
│   ├── upload.py                # Gated source upload service
│   └── upload_payloads.py       # Source upload request payload builders
├── _artifact/                   # Artifact-feature subpackage (promoted from flat _artifact_*.py, #1328)
│   ├── __init__.py              # Re-exports the cluster's public service classes/builders
│   ├── _download_client.py      # Download trusted-host allowlist + transport-aware client factory (httpx event hook / curl_cffi get_guarded)
│   ├── _redirect_guard.py       # Per-redirect-hop host/scheme revalidation for downloads (#1521)
│   ├── downloads.py             # Retired P5.8 compatibility exports for download helpers
│   ├── formatters.py            # Artifact formatting helpers
│   ├── generation.py            # Retired P5.8 import-compatible generation helper module
│   ├── generation_workflow.py   # Shared backend-driven artifact generation workflow
│   ├── payloads.py              # Stable artifact request payload builders
│   ├── validation.py            # Facade input-validation guards (generate_report coercion, export exactly-one-of) (#1874)
│   ├── listing.py               # Artifact listing helper
│   └── polling.py               # Artifact polling coordinator
├── _row_adapters/               # Positional-RPC-row adapters subpackage (promoted from flat _row_adapters_*.py, #1328)
│   ├── __init__.py              # Re-exports the typed row views
│   ├── artifacts.py             # Artifact + GET_SUGGESTED_REPORTS row adapters (ArtifactRow / ReportSuggestionRow)
│   ├── chat.py                  # Streamed-chat row adapters (AnswerRow / CitationRow / CitationDetail / StreamFrameRow / ErrorPayloadRow) — closes the chat positional-decode perimeter (#1491); the document tree is delegated to documents.py (#2120)
│   ├── documents.py             # TailwindDoc tree adapters (DocumentBodyRow / StructuralElementRow / ParagraphRow / ParagraphElementRow / TextRunRow / TableRow / BulletInfoRow / AnnotationEntryRow) + build_document/build_blocks — one decoder for source fulltext, chat responseDoc, and citation fragments (#2128, #2120)
│   ├── labels.py                # Source-label row adapter
│   ├── notebooks.py             # SUGGEST_PROMPTS suggestion-row adapter (PromptSuggestionRow / unwrap_prompt_suggestions)
│   ├── notes.py                 # Note and mind-map row adapter
│   ├── research.py              # POLL_RESEARCH row adapters (ResearchTaskRow / ResearchTaskInfoRow / ResearchResultRow) — drains the research parser's single-level positional reads (#1501)
│   └── sources.py               # Source row adapter
├── _chat/                       # Chat-feature subpackage — facade + helpers unified (#1328)
│   ├── __init__.py              # Re-exports ChatAPI so `from ._chat import ChatAPI` keeps resolving
│   ├── api.py                   # ChatAPI facade (was _chat.py)
│   ├── history.py               # Server-backed complete-history turn counting
│   ├── notes.py                 # Note saving workflow adapter
│   ├── wire.py                  # Streamed-chat wire request/response parser
│   ├── transport.py             # Chat error mapping
│   └── deleted_tracker.py       # Bounded RecentlyDeletedConversations set — serializes null-ask vs delete (#1875)
├── _auth/                       # Auth subpackage (forwarded through auth.py facade)
│   ├── __init__.py
│   ├── paths.py                 # Storage paths and filesystem helpers
│   ├── extraction.py            # Cookie/token extraction from browser sessions
│   ├── cookies.py               # Compatibility cookie loaders/converters + _update_cookie_input
│   ├── cookie_policy.py         # Domain allowlist + cookie-domain builder and policy
│   ├── cookie_semantics.py      # Dependency-bottom cookie scalar/row codecs
│   ├── cookie_types.py          # Canonical immutable Cookie/CookieJar values (ADR-0032)
│   ├── profile_account.py       # Immutable account/directive/domain/session values + pure parsers (ADR-0034)
│   ├── profile_document.py      # Lossless immutable raw profile + typed views/copy-on-write operations (ADR-0034)
│   ├── cookie_merge.py          # Pure snapshot/CAS + permanent no-baseline cookie decisions (ADR-0034)
│   ├── cookie_filter.py         # Pure raw capture/domain filter + value-free diagnostics (ADR-0034)
│   ├── credential_io.py         # Sealed typed profile/master-token commit spine (ADR-0034)
│   ├── profile_store.py         # Path-owned reads + cookie/account/remint/login/minted transactions (ADR-0034)
│   ├── profile_migration.py     # Legacy account resolution/promotion/scheduler/write composition (ADR-0034)
│   ├── storage_lock.py          # Process-default raw-path registry + platform lock gateway + bounded retry (ADR-0034)
│   ├── browser_cookie_recovery.py # Shim: re-exports validate/heal/validate_with_recovery from psidts_recovery.py (removed at next major)
│   ├── browser_state_validation.py # Shim: re-exports heal_captured_state from browser_capture.py (removed at next major)
│   ├── browser_capture.py       # One browser launch→capture→filter→heal→persist core (ADR-0033 merge; lazy playwright)
│   ├── _browser_cookie_filter.py # Shim: re-exports storage's canonical cookie_filter alias (removed at next major)
│   ├── browser_launch_errors.py # Channel registry + launch-failure triage (pure leaf of browser_capture)
│   ├── navigation_errors.py    # net:: classification: navigation race vs failure (pure leaf of browser_capture)
│   ├── login_wait_trace.py      # Shim: re-exports the login-wait tracing from browser_capture.py (removed at next major)
│   ├── headless_reauth.py       # Layer-3 headless re-auth (opt-in; typed outcomes; local-unattended-only)
│   ├── account_types.py         # Dependency-neutral Account/Playwright repair-result values with historical identity
│   ├── account_repair.py        # One-shot typed Playwright account-repair operation + call-time composition
│   ├── account.py               # Account network probing/selection + compatibility repair adapter
│   ├── account_email.py         # Generation-safe account-email matching, probing, caching, and CAS self-heal
│   ├── session.py               # Auth-session refresh implementation via `refresh_auth_session()` and explicit collaborators
│   ├── web_provider_storage.py  # Whole stored-auth transaction adapter + provider bootstrap (P8)
│   ├── web_provider_refresh.py  # Whole refresh-policy transaction adapter (P8)
│   ├── storage.py               # v0.x facade; raw adapters + minted snapshot/error + token policy
│   ├── storage_writer.py        # Shim: re-exports the writer API from storage.py (removed at next major)
│   ├── storage_transaction.py   # Shim: re-exports transaction aliases through storage.py (removed at next major)
│   ├── keepalive.py             # Keepalive/PSIDTS rotation policy + raw-wire re-exports
│   ├── psidts_recovery.py       # Inline PSIDTS recovery (issue #865) + the one load→heal→retry composition + the captured-cookie validate/heal seam
│   ├── master_token_types.py    # Dependency-bottom MasterTokenError/value + pure legacy-record codec
│   ├── master_token_file.py     # Explicit-path one-sample token I/O + canonical bounded-lock commit (ADR-0034)
│   ├── mint_service.py          # Stateless exchange/mint network owner + sole raw RotateCookies wire
│   ├── master_token_bootstrap.py # One-store bootstrap/re-mint/missing-storage coordinator (ADR-0034)
│   ├── master_token.py          # Headless v0.x adapters + late-bound bridges (ADR-0023/ADR-0034)
│   ├── recovery.py              # One-shot L2.5/L3/L4 coordinator + synchronized ColdRecoveryState
│   ├── single_flight.py         # Cross-loop flight/leader-task/success-epoch owner
│   ├── refresh.py               # Token driver + late-bound cold-coordinator adapter and L2.5 policy
│   └── tokens.py                # AuthTokens container + load_auth_from_storage loader
├── _types/                      # Dataclass implementation package re-exported by types.py
│   ├── __init__.py
│   ├── artifact_content.py       # Typed artifact media, slide/infographic content, and per-user state records (#2135, #2136)
│   ├── artifacts.py
│   ├── chat.py
│   ├── common.py
│   ├── documents.py             # StructuredDocument / DocumentBlock / TextSpan / TableCell / DocumentAnnotation / BlockKind / BlockStyle / ListStyle / ListInfo — parsed-document types behind SourceFulltext.document and AskResult.answer_document, carrying the offsets citations anchor to (#2128, #2120)
│   ├── labels.py                # Label pure-value type (source membership; no kind/artifact_ids)
│   ├── collections.py           # Collection pure-value type (account-level notebook grouping; notebook_ids)
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
├── _settings.py                 # SettingsAPI facade over semantic SettingsService (P6.6)
├── _labels.py                   # LabelsAPI — client.labels (source labels: generate/create/list/…)
├── _collections.py              # CollectionsAPI — client.collections (account-level notebook groups; reuse label RPCs, type-3)
├── notebooklm_cli.py            # Entry-point assembler — imports + registers cli/ groups
├── mcp/                         # MCP server (opt-in `mcp` extra) — transport-neutral adapter over _app/, sibling to cli/
│   ├── __init__.py              # Re-exports create_server / SERVER_NAME / SERVER_INSTRUCTIONS
│   ├── __main__.py              # `notebooklm-mcp` entrypoint: argparse (--profile/--transport/--host/--port/--log-level), stderr logging, loopback HTTP bind guard + fail-closed auth guard (non-loopback bind requires a bearer token AND/OR self-hosted OAuth); composes the auth provider (build_auth) and passes it to create_server on the http path
│   ├── server.py                # create_server(profile, client_factory, auth): FastMCP server; lifespan binds one NotebookLMClient; register_all tool-registration seam; auth passed explicitly (never reads the token env)
│   ├── _auth.py                 # Remote-transport bearer auth: McpBearerAuthProvider(TokenVerifier) with constant-time hmac.compare_digest over NOTEBOOKLM_MCP_TOKEN (env-only, never logged/repr'd); build_auth_provider/get_configured_token; build_auth(token, oauth) composes bearer | OAuth | MultiAuth | None (IdP-agnostic) — mirrors server/_auth.py, NOT fastmcp StaticTokenVerifier
│   ├── _oauth.py                # Optional self-hosted OAuth 2.1 AS for claude.ai (OAuth-only connector UI): SelfHostedOAuthProvider(InMemoryOAuthProvider) + a password-gated /login (override authorize()→stash SDK-validated (client,params) under a single-use sid→/login→InMemoryOAuthProvider.authorize); scrypt password digest + per-IP throttle, capped DCR + evict-oldest pending stash, atomic 0600 persistence of clients+tokens; get_oauth_config/build_oauth_provider (env NOTEBOOKLM_MCP_OAUTH_PASSWORD + _BASE_URL). Composed with the bearer via MultiAuth
│   ├── _host_guard.py           # LoopbackHostGuardMiddleware: ASGI guard that rejects HTTP requests with a non-loopback Host header (403; DNS-rebinding guard, #1869) on the loopback-bound HTTP transport via _serving.host_header_is_loopback; skipped when allow_external (REST-parity bearer/OAuth auth is mandatory there) — mirrors server/_auth
│   ├── _urlcheck.py             # _validate_bare_https_origin(url, env) — shared "bare public https origin" check (https scheme, host, no path/query/fragment); guards the OAuth base URL AND the file-transfer public URL so a /mcp-suffixed/non-https value can't mint broken links
│   ├── _filelink.py             # HMAC-signed self-describing file-transfer tokens (ADR-0024): FileLinkSigner.sign(payload, ttl→injects exp)/verify(token, op) (stdlib hmac/base64/json; pre-decode length cap, base64url re-pad, compare_digest, exp+op check) + FileTransferConfig(signer, base_url).upload_url(ttl=UPLOAD_TTL 15m; WIDGET_UPLOAD_TTL 1h for the ADR-0027 widget pool)/download_url (DOWNLOAD_TTL 30m); FileLinkError
│   ├── _fileroutes.py           # register_file_routes(mcp, config): the /files/{dl,ul} custom routes mounted on the FastMCP http app (ADR-0024). GET /files/dl streams the artifact (download core → FileResponse, meaningful filename, inside-tempdir assert, BackgroundTask cleanup); GET /files/ul = minimal upload page (file picker + raw-body fetch POST); POST|PUT /files/ul streams request.stream() into a 0600 temp under a running byte cap (real DoS guard) + Content-Length early 413 → neutral source_add core. Signed token is the sole auth (custom routes bypass the bearer gate); HTML pages set no-referrer/no-store/DENY; local _safe_upload_name (no server/ import)
│   ├── _uploadwidget.py         # register_upload_widget(mcp, config): OPT-IN in-app MCP-App upload widget (ADR-0027, NOTEBOOKLM_MCP_UPLOAD_WIDGET=1 → also auto-enables stateless HTTP). ONE ui:// resource (file-picker HTML, profile=mcp-app mime) + source_add_widget tool; both ui/resourceUri (claude.ai) and openai/outputTemplate (ChatGPT) point at it. Emits the render gates via FastMCP meta=/app=: _meta.ui.domain = sha256("<public-url>/mcp")[:32] + ".claudemcpcontent.com", flat ui/resourceUri, ui.csp; the widget POSTs bytes to /files/ul (reuses ADR-0024). Off the default surface unless enabled
│   ├── _context.py              # AppState dataclass (client + optional file_transfer) + get_client(ctx) / get_file_transfer(ctx) (lifespan-bound) + get_client_from_app(request) (the guarded private-attr accessor for the bare-Request custom routes)
│   ├── _errors.py               # Structured tool-error projection (CATEGORY_TABLE/ERROR_CODES/mcp_errors/to_tool_error/tool_error_payload) over _app.errors.classify
│   ├── _resolve.py              # resolve_notebook/resolve_source/resolve_note/resolve_artifact — name + partial-id resolution over _app.resolve plus exact-title matching
│   ├── _confirm.py              # needs_confirmation() both-mode envelope + READ_ONLY/DESTRUCTIVE ToolAnnotations
│   ├── _coerce.py               # coerce_list(value) — tolerant list-param normalizer (real list/tuple, JSON-array string, comma string, scalar → list[str]; None stays None for the "all sources" contract); used by studio_generate/chat_ask source_ids
│   ├── _paginate.py             # paginate(items, limit) — bounded page + {total, has_more} for the *_list tools (client-side slice; RPCs don't page); DEFAULT_LIMIT=50
│   └── tools/                   # Per-domain tool modules; each exposes register(mcp) wired by server.register_all
│       ├── __init__.py          # Tools package marker (no click/rich/cli)
│       ├── _content_sanity.py   # _annotate_thin_warnings/_thin_content_warning — advisory thin/soft-404/bot-challenge web-page warning over _app.source_content (used by source_wait + source_add batch)
│       ├── _fileupload.py       # file-transfer slice of the source tools: _broker_upload (signed-URL upload_required) + _decode_upload_b64/_add_bytes (in-channel base64 byte upload for source_add(source_type="file", bytes_base64=…)) + the shared _add_one plan/execute seam (split from sources.py for the ADR-0008 size budget)
│       ├── _passthrough.py      # Shared pass-through resolvers (passthrough_notebook_id/passthrough_child_id) for the CLI-shaped _app executors
│       ├── _preview.py          # title_for_id() — shared id→title lookup for the delete tools' needs_confirmation previews
│       ├── _studio_items.py     # cross-type Studio plumbing: studio_items (merge notes+artifacts into one items list) + resolve_studio_item (cross-type ref → StudioResolvedItem) for studio_list/studio_rename/studio_delete (split from studio.py for the ADR-0008 size budget)
│       ├── _studio_download.py  # download plumbing shared by studio.py + _fileroutes.py: consumes _app.download_specs directly; registry-derived DownloadType/DownloadFormat schema aliases + _resolve_artifact_id / _broker_download / transport helpers (split from studio.py for the ADR-0008 size budget)
│       ├── _studio_payloads.py  # wire-shape projection helpers for the Studio generate/rename tools: _generation_payload (GenerationExecutionResult → response dict; mind-map → bare node tree + mind_map_id via _mind_map_tree/_mind_map_id) + _artifact_rename_payload (split from studio.py for the ADR-0008 size budget)
│       ├── _waitagg.py          # source-wait outcome aggregation shared by source_wait + source_add(wait=True): _wait_all_sources (concurrent per-source wait) + _aggregate_wait_outcomes (typed SourceWaitOutcome → {ok, ready, timed_out, failed, not_found} + thin-warning annotation) (split from sources.py for the ADR-0008 size budget)
│       ├── notebooks.py         # notebook_list/create/describe/rename/delete over _app.notebooks
│       ├── sources.py           # source_list/read/rename/delete/wait/add over _app.source_* (add: url/text/file/youtube via source_add, drive via source_mutations); source_add folds in wait=True (single-mode add + wait composed via _waitagg) and bytes_base64/filename (in-channel small-file byte upload via _fileupload) — #1890
│       ├── sources_drive.py     # source_add_drive_file tool (#1884): discrete verb over _app.source_mutations.execute_source_add_drive_file — downloads + uploads the upload-only Drive types (kept out of the ceiling'd sources.py; own register())
│       ├── chat.py              # chat_ask (client.chat.ask + get_history recall + suggest_followups) + chat_configure (_app.chat.execute_configure) + suggest_prompts (client.notebooks.suggest_prompts surface selector)
│       ├── notes.py             # note_save (create-or-update upsert) over _app.notes; note reading/renaming/deleting fold into the cross-type Studio tools
│       ├── studio.py            # hosts the Studio tools: studio_list (merges notes+artifacts via _studio_items.studio_items; surfaces each artifact's generation_prompt in the summary listing / the item= single-fetch — folded studio_get_prompt in #1896) / generate / status / download (via _studio_download) / rename / retry / studio_delete — both rename and delete are cross-type via _studio_items.resolve_studio_item (note→_app.notes.execute_note_rename/execute_note_delete, artifact→_app.artifacts kind-aware core); enum dispatch over _app.generate + _app.download; stateless poll via _app.artifacts.poll_artifact
│       ├── research.py          # research_start (client.research.start) + research_status (_app.research.poll_and_classify) + research_import
│       ├── sharing.py           # share_status/set_access/set_user/remove_user (thin adapters over client.sharing; set_access folds public+view_level, set_user upserts add/update; string-labeled enums; view_level surfaced only when set)
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
    ├── _download_specs.py       # Click projection of the shared download registry: help/examples + legacy slide_format parameter only
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
    ├── collection_cmd.py        # collection list/notebooks/create/rename/add/remove/delete (account-level)
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
        ├── auth_refresh.py      # Missing-storage bootstrap from the exact sibling master token
        ├── auth_source.py       # Single source of truth for the active CLI auth source (Click-context precedence resolver; stays in cli/ — reads ctx.obj + NOTEBOOKLM_AUTH_JSON)
        ├── confirming_mutation.py # Shared confirmed-mutation pipeline for CLI resources
        ├── download.py          # CLI adapter over _app/download.py: re-exports plan/spec types, injects cli.resolve resolvers (keeps resolve_notebook_id patch seam), projects DownloadResult → envelope dict
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
        │   ├── master_token.py   # Interactive oauth_token capture only (coordination in _auth/master_token_bootstrap.py; v0.x adapters in _auth/master_token.py)
        │   ├── outcomes.py
        │   ├── profile_targets.py
        │   ├── refresh.py
        │   └── rookie_cookies_errors.py
        ├── playwright_login.py  # Playwright-driven Google login service
        ├── playwright_redaction.py # Subprocess-output redaction helpers for the Playwright login service
        ├── polling.py           # Shared polling helpers for CLI wait commands
        ├── research.py          # `research wait` CLI adapter over `_app/research.py` — re-exports plan/result/outcome; injects cli.resolve.resolve_notebook_id + cli.research_import.import_research_sources defaults (preserves their patch seams)
        ├── session_context.py   # Notebook-context CLI adapter over `_app/session.py` for `use`/`status`/`auth logout` — re-exports the typed result classes; builds the injected StatusInputs/LogoutInputs bundles from its own session_context-namespace path helpers (read at call time, preserving the get_context_path/get_storage_path/clear_context patch seams)
        ├── source_listing.py    # `source list` CLI adapter over `_app/source_listing.py` — owns the ListSpec/prepare_list presentation half; injects resolve_label_id into the neutral fetch_sources
        ├── source_mutations.py  # Source-mutation CLI adapter over `_app/source_mutations.py` — re-exports plan/result/error/helpers; injects cli.resolve validate_id + resolve_source_id (preserves the resolve_source_id monkeypatch seam) and the click.confirm confirmer
        ├── source_research.py   # `source add-research` CLI adapter — thin wrapper over `_app/source_research.py` (injects the rich-coupled importer; re-exports plan/result + validate_add_research_flags; preserves the import_research_sources monkeypatch seam)
        └── source_serializers.py # Shared JSON serializers for source CLI output; source_row_payload is the ONE row shape emitted by both `source list --json` and `source get --json` (summary + status axis + the CLI-spelled Drive axis, which lives here rather than in _app because MCP/REST spell it differently), so the two paths cannot drift apart
└── server/                      # Single-tenant REST API adapter (the third _app adapter, after cli/ and mcp/; behind the optional `server` extra). EXPERIMENTAL: /v1 surface may change, excluded from the api-compat gate. Imports no click/rich/cli.
    ├── __init__.py              # Re-exports create_app + SERVER_NAME; importing it without the `server` extra fails on the fastapi import
    ├── __main__.py              # `notebooklm-server` entry: argparse + NOTEBOOKLM_SERVER_* env defaults + loopback-bind guard + fail-closed token check
    ├── app.py                   # create_app(*, client_factory=None) -> FastAPI; ASGI lifespan binds one client; public /healthz; auth-gated /v1 mount (docs/redoc/openapi disabled)
    ├── _context.py              # AppState (lifespan-bound client + pending registry) + get_client / get_pending FastAPI dependencies
    ├── _limits.py               # Lifespan-owned REST route-group concurrency limiters for expensive source/chat/research/artifact work
    ├── _auth.py                 # Bearer-token (constant-time, 401) + loopback-Host (DNS-rebinding guard, 403) dependency for /v1
    ├── _errors.py               # ErrorCategory -> HTTP status table + _redact + the classify-once exception handler emitting {error:{category,message}}
    ├── _pagination.py           # Opt-in, non-breaking list-route envelope: paginate_envelope(items, key=…, limit, offset, **extra) — default (no limit) returns the full list under its existing key unchanged; ?limit= slices via _app.pagination.paginate + adds a meta:{total,has_more,limit,offset} block (Option B-lite)
    ├── _pending.py              # In-process pending-id registry (per-notebook provenance for poll -> 200-pending vs 404)
    └── routes/                  # Per-resource FastAPI routers; handlers call _app.serialize.to_jsonable directly
        ├── __init__.py          # Aggregates the resource routers for the app factory
        ├── _passthrough.py      # Pass-through resolvers handed to the _app cores (REST works in full ids)
        ├── notebooks.py         # /v1/notebooks list/get/create/rename(PATCH)/delete + GET /{id}/suggested-prompts (client.notebooks.suggest_prompts; surface→mode map pinned to MCP)
        ├── sources.py           # /v1/notebooks/{id}/sources list/get/add(url·text·file·drive·batch)/rename(PATCH)/wait/delete + poll-the-resource status
        ├── notes.py             # /v1/notebooks/{id}/notes list/get/create/update(PUT)/delete — thin adapter over client.notes
        ├── chat.py              # POST /v1/notebooks/{id}/chat — blocking ask (no SSE) + POST /chat/configure over _app.chat.execute_configure
        ├── artifacts.py         # /v1/notebooks/{id}/artifacts list/generate/poll/download/rename(PATCH)/retry/delete + GET /{id}/prompt (per-kind generate-option validation pinned to core maps; registry-projected poll; server-generated temp download path)
        ├── research.py          # /v1/notebooks/{id}/research start(202)/status/cancel/import — split-tool shape over client.research + _app.research.poll_and_classify (poll_id = report_id or task_id)
        ├── share.py             # /v1/notebooks/{id}/share status/public/users/view-level over _app.sharing
        └── meta.py              # GET /v1/server/info — version + local auth-health probe (run_auth_check) + opt-in account block; scrubs the on-disk storage path (mirrors MCP server_info)
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
- [ADR-0016](./adr/0016-auth-identity-and-core-logger-compatibility.md) — Auth identity + core logger compatibility (Accepted).
- [ADR-0017](./adr/0017-public-facade-private-implementation.md) — Public-facade / private-implementation re-export convention (Accepted).
- [ADR-0018](./adr/0018-deprecation-strategy.md) — Deprecation strategy (Accepted).
- [ADR-0019](./adr/0019-error-and-return-contract.md) — Error-and-return contract for the public API (Accepted; the breaking half shipped in v0.8.0).
- [ADR-0020](./adr/0020-sealed-async-result-types.md) — Sealed async result types for artifact generation (Proposed; recommends continued deferral).
- [ADR-0021](./adr/0021-transport-neutral-app-layer.md) — Transport-neutral application layer (`_app/`) (Accepted; boundary enforced by `tests/_guardrails/test_app_boundary.py`, classify↔error_handler agreement by `tests/_guardrails/test_classify_error_handler_consistency.py`).
- [ADR-0022](./adr/0022-regenerable-baselines.md) — Regenerable test baselines (Accepted).
- [ADR-0023](./adr/0023-master-token-headless-auth.md) — Master-token headless auth (Accepted; the L4 unattended re-mint path, `[headless]` extra).
- [ADR-0024](./adr/0024-mcp-remote-file-transfer.md) — Remote-MCP file transfer via signed-URL side-channel (Accepted; implemented).
- [ADR-0025](./adr/0025-mcp-tool-granularity.md) — MCP tool granularity (Accepted).
- [ADR-0026](./adr/0026-mcp-studio-surface.md) — MCP Studio surface — notes + artifacts unified (Accepted).
- [ADR-0027](./adr/0027-mcp-app-upload-widget.md) — In-app MCP-App upload widget (Accepted; experimental / opt-in, `NOTEBOOKLM_MCP_UPLOAD_WIDGET=1`).
- [ADR-0028](./adr/0028-gemini-notebook-rename.md) — Proposed public package/client rename for Google's Gemini Notebook rebrand.
- [ADR-0029](./adr/0029-canonical-storage-writer.md) — Single canonical `storage_state.json` writer (Accepted; rolling out).
- [ADR-0030](./adr/0030-one-recovery-ladder.md) — One recovery ladder for auth cold-start/refresh (Accepted; rolling out; companion to ADR-0029).
- [ADR-0031](./adr/0031-credential-tier-auth-model.md) — Credential-tier auth model (Proposed; implementation remains staged and partly deferred).
- [ADR-0032](./adr/0032-auth-domain-types.md) — Auth domain types and the `AuthTokens` compatibility runway (Accepted; incremental).
- [ADR-0033](./adr/0033-auth-consolidation-policy.md) — Auth consolidation ownership and write-boundary policy (Accepted).
- [ADR-0034](./adr/0034-auth-storage-object-model.md) — Auth storage object model and incremental extraction (Accepted).
- [ADR-0035](./adr/0035-semantic-backend-boundary.md) — Private semantic backend boundary and P0-P8 migration order (Accepted).

## See also

- [`CLAUDE.md`](../CLAUDE.md) — quick-start commands, common pitfalls, and the PR workflow for AI agents working in this repo (the per-file index + repository tree now live in [File map](#file-map) above).
- [`docs/development.md`](./development.md) — how to add a new feature API.
- [`docs/refactor-history.md`](./refactor-history.md) — historical narrative of the multi-phase refactor + downstream migration tables.
- [`docs/python-api.md`](./python-api.md) — public Python API surface.
- [`docs/auth-cookie-lifecycle.md`](./auth-cookie-lifecycle.md) — cookie keepalive, rotation, and PSIDTS recovery.
- [`docs/rpc-development.md`](./rpc-development.md) — capturing and debugging new RPCs.
- [`docs/rpc-reference.md`](./rpc-reference.md) — RPC payload structures.
