# Architecture

This document is the canonical map of `notebooklm-py`'s current runtime shape.
The historical refactor narrative (including the program that first established
this layering) lives in [`docs/refactor-history.md`](./refactor-history.md).

## System shape

Start with the explorable [system overview](https://teng-lin.github.io/notebooklm-py/diagrams/01-system-overview.html).
The [adapter view](https://teng-lin.github.io/notebooklm-py/diagrams/02-adapters-and-app-layer.html) expands the frontend
boundary, and the [backend split](https://teng-lin.github.io/notebooklm-py/diagrams/06-backends-web-and-android.html) shows
where the Web and Android graphs stop sharing implementation. The complete visual index is in
[`docs/diagrams/README.md`](./diagrams/README.md).

The runtime is organized into six ownership layers:

| Layer | Owns | Must not own |
| --- | --- | --- |
| Library and frontend adapters | Python calls, Click parsing/rendering, MCP tools, REST routes | Raw RPC payload construction |
| Application layer (`_app/`) | Transport-neutral plans, resolution, waits, retries, status projection, error classification | Click, Rich, FastMCP, FastAPI, or wire decoding |
| Public client and feature contracts | `NotebookLMClient`, eleven typed namespaces, public dataclasses and exceptions | Backend selection after construction |
| Client-owned runtime | Loop binding, admission, metrics, drain, lifecycle, Web execution and transport | Domain-specific parameter or result shapes |
| Selected backend | Web batchexecute/HTTP or Android protobuf/gRPC adapters | Mixing typed namespace operations across backends |
| Transfer participants | Scotty uploads, Drive staging, artifact asset downloads, Phenotype token acquisition | Passing file bytes through the ordinary RPC executor |

CLI, MCP, and REST are frontend adapters over the same neutral application and client layers.
Backend selection occurs below them. Web remains the compatibility default; Android is an
explicit, construction-time alternative that installs Android implementations for all eleven
typed namespaces. The graphs share public contracts, root lifecycle, admission, and telemetry,
but not their wire transport, as decided in
[ADR-0035](./adr/0035-mobile-resilience-transport.md). See the
[selection workflow](https://teng-lin.github.io/notebooklm-py/diagrams/28-profile-auth-backend-selection.workflow.html) for the exact
argument/environment/profile precedence.

| Adapter | Package | Transport | Console script | Install | Failure projection |
| --- | --- | --- | --- | --- | --- |
| **CLI** | `cli/` | terminal (Click) | `notebooklm` | base | exit codes and the byte-stable `--json` envelope (ADR-0015) |
| **MCP** | `mcp/` | Model Context Protocol (FastMCP) | `notebooklm-mcp` | `mcp` extra · experimental | MCP tool error content (`CODE: message`) |
| **REST** | `server/` | HTTP (FastAPI) | `notebooklm-server` | `server` extra · experimental | HTTP status plus `{"error": {"category": "...", "message": "..."}}` |

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
The [exception hierarchy](https://teng-lin.github.io/notebooklm-py/diagrams/09-exception-hierarchy.html) shows that shared
failure vocabulary and its transport-specific projections.
The per-module index and the full tree are in [File map](#file-map) below.

## Library call flows

`NotebookLMClient` is the composition root. It constructs the shared runtime
collaborator graph, wires feature APIs to narrow runtime Protocols, and
injects stateful services such as `SourceUploadPipeline`, `NoteService`,
`NoteBackedMindMapService`, and `ArtifactDownloadService`. Feature modules
build NotebookLM params and parse domain rows; client-owned collaborators own
dispatch, transport, auth refresh, metrics, and lifecycle.

Backend preference is resolved once at construction: the explicit `backend=`
argument wins over `NOTEBOOKLM_BACKEND`, and the default is `"web"`. Selection
is all-or-nothing for the eleven typed namespaces (`notebooks`, `sources`,
`artifacts`, `chat`, `research`, `notes`, `mind_maps`, `settings`, `sharing`,
`labels`, and `collections`); `client.backends` is the read-only installed-graph
report. Explicit Android selection installs all eleven Android adapters, and
the installed namespace objects retain no Web operation collaborators. The
supported `client.raw` namespace is selected with the backend. The deprecated
`client.rpc_call(...)` wrapper is deliberately different: its `RPCMethod`
values are Web batchexecute IDs, so Android preserves it through a separate,
lazy Web compatibility sidecar during the 0.x warning window.

Use the [runtime and transport view](https://teng-lin.github.io/notebooklm-py/diagrams/03-client-runtime-and-transport.html)
for ownership, the [RPC sequence](https://teng-lin.github.io/notebooklm-py/diagrams/07-rpc-call-path.html) for call order, and
the [runtime class model](https://teng-lin.github.io/notebooklm-py/diagrams/23-runtime-class-model.html) for constructor
relationships. The [capability-contract map](https://teng-lin.github.io/notebooklm-py/diagrams/27-capability-contracts.html)
shows why feature APIs receive narrow collaborators instead of the whole client runtime.

### Web batchexecute path

On the default Web backend, public methods (`client.notebooks.list()`, `client.sources.rename()`,
`client.settings.get_user_settings()`, artifact generation, note CRUD, etc.) follow this path:

```text
+----------------------------------------------------------------+
| CLI command / MCP tool / REST route / library call             |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| NotebookLMClient.<feature>.<method>()                          |
|   feature API / service builds params and chooses RPCMethod    |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| RpcExecutor.rpc_call(...)                 satisfies RpcCaller  |
|   - pre-open validation via CallSupervisor                    |
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
|   - admission-only operation lease + resource epoch            |
|   - loop-affinity guard, auth snapshot, request materialization|
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| CallSupervisor unary scope                                     |
|   Drain -> Metrics -> Semaphore                                |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| ADR-0009 web middleware chain                                  |
|   Retry -> AuthRefresh -> ErrorInjection -> Tracing            |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| MiddlewareChainHost._authed_post_chain_terminal(...)           |
|   chain leaf — ADR-0014 Rule 4                                 |
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
`RpcCaller` per ADR-0014 Rule 1. A Web client's `raw.call` dispatches through
the same `RpcExecutor` stored in `NotebookLMClient._web_runtime`.

`NotebookLMClient.rpc_call(method, params)` is the deprecated compatibility
wrapper. It follows the same pipeline on Web. On Android, its first call enters
a supervised operation and materialises the pre-registered Web sidecar before
delegating; typed Android operations never use that sidecar.

### Android gRPC path

With `backend="android"`, the same typed namespace calls terminate in Android
adapters instead of `RpcExecutor`:

The [Android backend view](https://teng-lin.github.io/notebooklm-py/diagrams/14-android-backend.html) identifies its
participants, while the [Android call sequence](https://teng-lin.github.io/notebooklm-py/diagrams/15-android-call-path.html)
shows lazy bearer acquisition, channel creation, and result projection.

```text
NotebookLMClient.<feature>.<method>()
    → backend-neutral <Feature>API runs shared workflow orchestration
    → Android<Feature>API._operation_scope(...) for multi-call workflows
      → AndroidSession.operation_scope(...) + task-local workflow epoch
    → Android<Feature>API validates/builds a protobuf request
    → AndroidSession.unary(...) or .unary_stream(...)
    → CallSupervisor terminal scope (drain, metrics, semaphore)
    → lazy bearer acquisition + lazy gRPC channel
    → notebooklm-pa.googleapis.com
    → strict protobuf/public-dataclass projection
```

Every backend-neutral namespace base exposes the same non-abstract
`_operation_scope(label)` template hook. Its default is an async-compatible
no-op, while each Android namespace overrides it with the session's
generation-bearing scope. The Android scope binds a module-level `ContextVar`
tagged with that `AndroidSession`'s unique identity and lease epoch. Nested
namespace calls therefore inherit the original workflow fence without adding
epoch parameters to wire hooks; unrelated Android clients in the same task
cannot consume each other's tag, and nested scopes restore the outer tag on
exit.

Android optional dependencies and the master-token credential are validated at
async open; the channel itself remains lazy until the first Android RPC. Asset
downloads, Scotty uploads, Drive staging, and Phenotype acquisition are separate
lifecycle participants where their protocols require it. Cross-namespace joins
receive the already-selected Android collaborators, so a typed Android operation
does not fall back to a Web namespace object.

### Web chat ask path

The neutral `ChatAPI.ask()` owns conversation, lock, cache, ID-recovery, and
turn-number orchestration. Its single Web send/decode seam,
`WebChatAPI._stream_answer()`, is the major transport-sharing exception to the
pure `RpcExecutor` shape. Streaming chat has a custom request body and
chat-flavored error mapping, so the first ask POST goes through:

For a compact view, see the [chat sequence](https://teng-lin.github.io/notebooklm-py/diagrams/11-chat-ask-sequence.html) and
the [chat/notes class model](https://teng-lin.github.io/notebooklm-py/diagrams/26-chat-notes-class-model.html).

```text
+----------------------------------------------------------------+
| ChatAPI.ask(...)                                               |
|   - loop_guard.assert_bound_loop()                             |
|   - source-id lookup                                           |
|   - conversation lock / prior-role and cache reads             |
|   - delegate once to _stream_answer(...)                       |
+----------------------------------------------------------------+
                                 |
                                 v
+----------------------------------------------------------------+
| WebChatAPI._stream_answer(...)                                 |
|   - reqid.next_reqid()                                         |
|   - build Web streaming request                                |
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
                                 |
                                 v  _PostedAsk returns to base
+----------------------------------------------------------------+
| ChatAPI.ask(...)                                               |
|   - recover authoritative conversation id                      |
|   - update cache and construct AskResult                       |
+----------------------------------------------------------------+
```

The neutral `ChatAPI` holds `loop_guard` and the base-typed `notebooks`
collaborator plus its cache/session-hint state. `WebChatAPI` adds `rpc`,
`transport`, and `reqid`; there is no `ChatRuntime` composite or broad runtime
transport indirection.

For a new conversation, `ChatAPI.ask()` then calls its abstract
`get_conversation_id()` read; `WebChatAPI.get_conversation_id()` implements
that read through `GET_LAST_CONVERSATION_ID` on the normal `RpcExecutor` path.
Other Web chat reads use the same executor, while shared
`ChatAPI.delete_conversation()` delegates its one RPC send to
`WebChatAPI._send_delete_conversation()`. `ChatAPI.configure()` owns defaults,
custom-prompt validation, and normalization above `_send_configure()`, while
`ChatAPI.get_settings()` constructs the public model from the typed
`_read_settings()` carrier. Authoritative prior-turn counting is also shared;
each `_list_turn_roles()` hook returns typed roles plus backend-specific
exhaustion evidence, so Web keeps its row-count rule and Android keeps its
native pagination-token rule.

`AndroidChatAPI` reuses the same neutral `ChatAPI` locks, cache, source lookup,
and result construction, but its send seam calls
`AndroidSession.unary_stream(GenerateFreeFormStreamed)` and its session/history
operations use native unary gRPC methods. It does not traverse
`RuntimeTransport`, the Web middleware chain, or `Kernel.post`.

### Uploads, downloads, and polling

Some feature workflows intentionally combine RPC with non-RPC HTTP work:

The [source-ingest data flow](https://teng-lin.github.io/notebooklm-py/diagrams/12-source-ingest-dataflow.html),
[artifact lifecycle](https://teng-lin.github.io/notebooklm-py/diagrams/13-artifact-lifecycle.html), and
[transfer security boundaries](https://teng-lin.github.io/notebooklm-py/diagrams/30-transfer-security-boundaries.dataflow.html) provide
the visual counterparts to the detailed ownership table below.

| Flow | Runtime shape |
|------|---------------|
| Source file upload | The backend-neutral `SourcesAPI.add_file()` normalizes the requested title and dispatches through one `_send_upload` hook. Web and Android hooks wrap their existing upload pipelines; those pipelines retain registration, byte transfer, admission, deadlines, and lifecycle fencing, then invoke the owner-neutral base finalizer before their operation scope exits. The finalizer chooses ready/registered/processing projection and applies the best-effort title rename without retaining backend owners. Web still takes its own upload semaphore and uses epoch-fenced live Kernel cookies for Scotty start/finalize; Android retains its aggregate control-plane deadline and workflow epoch through post-upload reads/mutation. |
| Source URL/text/Drive add | `WebSourcesAPI` holds a `CallSupervisor.operation_scope` across URL workflows that combine create, optional wait, and optional rename. `SourceAddService` wraps URL and Drive mutating RPCs in `idempotent_create(...)` where those flows have stable probes. Text-source adds remain intentionally non-idempotent unless the caller handles dedupe externally. |
| Artifact generation | The backend-neutral `ArtifactsAPI` owns validation and source/language resolution for ten Studio creation methods over `_send_create_artifact`, Drive-export wrappers/target validation over `_send_export`, and artifact-copy validation/result policy over `_send_copy`. Customization choices remain an abstract typed read because the Web row and Android protobuf tables have no shared choreography. `generate_mind_map()` is the eleventh public `generate_*` method and has its own backend hook because it returns `MindMapResult` and may persist note-backed output. `WebArtifactsAPI` implements the shared creation hook with `ArtifactGenerationService` (`_web/artifact/generation.py`), whose positional `CREATE_ARTIFACT` builders live in `_web/params/artifacts.py`; Web revise, retry, and mind-map paths also use that service. When `backend="android"` is explicit, `AndroidArtifactsAPI` dispatches the evidence-admitted exact `CreateArtifact` families, including cinematic videos and data tables, plus live-proven `DeriveArtifact`. Retry and Drive export use web-derived mobile gRPC handlers; note-backed generation is native composition over current-bundle `ActOnSources` and exact `CreateNote`. `ArtifactPollingService` owns leader/follower polling over the abstract target-aware studio projection. |
| Artifact download | Web `ArtifactDownloadService` (`_web/artifact/downloads.py`) selects artifacts and decodes raw rows/interactive HTML. The neutral `AssetDownloadService` (`_artifact/downloads.py`) owns byte transfer, rejection, staging, and atomic publication, while `_artifact/_guarded_transfer.py` owns the bounded redirect/content/signature plane shared by the Android adapter. Web supplies a storage-cookie jar. The publicly selected Android asset service validates canonical hosts, clears ambient cookies on every hop, re-acquires the APK-evidenced bearer for each eligible hop, uses `alr=yes` on the exact `lh3.googleusercontent.com` and slide `contribution.usercontent.google.com` entry hosts, strips credentials permanently once a chain leaves the allowlist, applies representation byte/signature limits, corrects verified WAV output to `.wav`, and is drained with the client lifecycle. These transfers do not go through `RpcExecutor` or `Kernel.post`. |
| Notes and mind maps | `NoteService` owns Web note-row CRUD/classification through `RpcCaller`. `NoteBackedMindMapService` keeps Web row decoding and exact-content note-backed rename in `WebMindMapsAPI`; rename fetches the raw stored row and resends its content unchanged. The neutral `MindMapsAPI` composes unified list/lookup/rename/delete workflows over base-typed `ArtifactsAPI` and `NotesAPI` collaborators. For explicit Android selection, `AndroidMindMapsAPI` is publicly assembled over the Android artifact and note APIs: interactive generation/tree/mutation uses live artifact operations, while note-backed reads/rename/delete/tree and typed prefetch compose without fabricating Web rows. Note-backed generation uses the current-bundle `ActOnSources` request and persists its JSON through native `CreateNote`. |

## Cross-cutting policies

Three policies thread through the layers above and are easy to violate by
accident. Each is pinned by an ADR.

The [client resource lifecycle](https://teng-lin.github.io/notebooklm-py/diagrams/19-client-resource-lifecycle.html) shows
open, drain, close, and rollback states. The
[retry-policy workflow](https://teng-lin.github.io/notebooklm-py/diagrams/20-retry-policy-workflow.html) complements the
idempotency discussion by showing the decision path for one call.

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
[`_web/policy.py`](../src/notebooklm/_web/policy.py):

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
`notebooklm.rpc.safe_index`, re-exported from
[`_web/wire/safe_index.py`](../src/notebooklm/_web/wire/safe_index.py). It always
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

The web-only `Kernel` and `RpcCaller` Protocols live in
[`_web/contracts.py`](../src/notebooklm/_web/contracts.py), while the
transport-neutral `LoopGuard` remains in
[`_runtime/contracts.py`](../src/notebooklm/_runtime/contracts.py).
`Kernel` is the typed web transport surface implemented by the concrete
client-owned kernel and consumed by the web upload pipeline. The remaining
single-consumer capability Protocol, `AuthMetadata`, lives beside that consumer
in `_web/sources/upload.py`. The supervisor-ownership refactor removed
`OperationScopeProvider`: artifact
polling and source workflows now receive the concrete, shared `CallSupervisor`,
which owns generation-bearing operation scopes, admitted child tasks, loop
checks, and drain-hook registration. The unused `AsyncWorkRuntime` composite
and the feature-local composite runtime Protocols (`ChatRuntime`,
`ArtifactsRuntime`, `UploadRuntime`) were deleted once they no longer
represented independently varying production dependencies.

**Module-level Protocols:**

| Protocol | Responsibility |
|----------|----------------|
| `RpcCaller` (`_web/contracts.py`) | Exposes `rpc_call(method, params, ...)` — the chokepoint every web feature API uses for batchexecute calls. |
| `LoopGuard` (`_runtime/contracts.py`) | Exposes `assert_bound_loop()` — single-method cross-loop affinity check for transport-neutral orchestration. |
| `Kernel` (`_web/contracts.py`) | Pure web transport surface — `post()` method, `cookies` property, `aclose()`. Single consumer today: `SourceUploadPipeline`. |

**Feature dependencies.** Single-consumer capability shapes live next to their
owner (`AuthMetadata` in `_web/sources/upload.py`). No feature-local
composite-runtime unions or adapter dataclasses exist in production. Every
multi-capability feature takes its collaborators by keyword-only constructor
argument:

- Backend-neutral `ArtifactsAPI` takes `supervisor: CallSupervisor`, a
  base-typed notebook source-id provider, and a required backend-configured
  neutral `AssetDownloadService`; `WebArtifactsAPI` additionally takes
  `rpc: RpcCaller`, mind-map, and note collaborators.
- `WebSourcesAPI` and its `SourceUploadPipeline` share the same
  `CallSupervisor`. The pipeline also takes `rpc: RpcCaller`, the concrete web
  `Kernel`, and local `AuthMetadata`; it is itself a phased transport-lifecycle
  participant for upload clients/tasks.
- `ChatAPI` takes `loop_guard: LoopGuard` and the base-typed
  `notebooks: NotebookSourceIdProvider`; `WebChatAPI` adds
  `rpc: RpcCaller`, `transport: RuntimeTransport`, and `reqid: ReqidCounter`.

Production satisfies Protocols via the underlying collaborators
(`RpcExecutor` satisfies `RpcCaller`, the `CallSupervisor` supplied to chat
satisfies `LoopGuard`, and the concrete `Kernel` satisfies the `Kernel`
Protocol). `CallSupervisor` itself is a concrete infrastructure service, not a
feature-local Protocol or a broad runtime facade. There is no production
`Session` class in the runtime graph.
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
  _collaborators: SharedRuntime
  metrics | call_supervisor | root lifecycle
        |
        v
  ClientLifecycle orchestrates immutable transport/loop-participant tuples
      |-- Web: WebTransportLifecycle + SourceUploadPipeline
      `-- Android: AndroidRuntime participants + inert LazyWebSidecar proxy

        +----------------------------------+
        | _web_runtime: WebRuntime | None  |
        +----------------------------------+
        | reqid | auth_coord | kernel      |
        | cookie persistence | web transport|
        | executor | source uploader       |
        | composed: ClientComposed         |
        +----------------------------------+
             |
             v
  RpcExecutor.rpc_call → CallSupervisor → RuntimeTransport.perform_authed_post
      → web-only ADR-0009 chain → RuntimeTransport.terminal → Kernel.post → httpx

  Selected typed namespaces:
      web     → Web*API → RpcExecutor / Web upload and asset services
      android → Android*API → AndroidSession / Android transfer services

  Supported raw namespace:
      web     → WebRawAPI.call → installed WebRuntime
      android → AndroidRawAPI.unary/unary_stream → AndroidSession
```

| Collaborator | Module | Responsibility |
|--------------|--------|----------------|
| `NotebookLMClient` | [`client.py`](../src/notebooklm/client.py) | Public surface and composition root. Owns `_auth`, `_seams`, the neutral `_collaborators`, exactly one primary backend runtime, backend preference/reporting, the backend-selected `raw` adapter, and the eleven feature API attributes (`notebooks`, `sources`, `artifacts`, `chat`, `notes`, `mind_maps`, `research`, `settings`, `sharing`, `labels`, `collections`). An Android client also owns an inert `LazyWebSidecar` lifecycle proxy solely for the deprecated `rpc_call` warning window. Keep non-trivial additions in focused assembly/runtime/feature seams rather than accreting the composition root; the module-size ratchet in `tests/_guardrails/test_module_size_ratchet.py` is the enforceable ceiling, not a line count copied into this document. |
| `ClientSeams` | [`_web/transport/seams.py`](../src/notebooklm/_web/transport/seams.py) | Mutable holder for runtime callables that closures re-read after construction: `decode_response`, `sleep`, and `is_auth_error`. Construction-only seams such as `async_client_factory` stay on `compose_client_internals(...)` and the client-shell test helper, not on the public constructor. |
| `SharedRuntime` | [`_runtime/init.py`](../src/notebooklm/_runtime/init.py) | Backend-neutral bundle containing metrics, the shared call supervisor, and the root lifecycle after assembly. `RuntimeCollaborators` remains a private compatibility alias. |
| `WebRuntime` | [`_web/transport/init.py`](../src/notebooklm/_web/transport/init.py) | Web-only bundle containing request IDs, auth coordination, Kernel, cookie persistence, web lifecycle, composition holder, executor, and upload pipeline. |
| `AndroidRuntime` | [`_android/runtime.py`](../src/notebooklm/_android/runtime.py) | Android-only bundle containing the bearer provider, gRPC session, upload/asset transports, and Phenotype token provider. |
| `LazyWebSidecar` | [`_web/transport/sidecar.py`](../src/notebooklm/_web/transport/sidecar.py) | Pre-registered lifecycle proxy for deprecated Android `rpc_call`. It is inert at construction/open, materialises one Web runtime under a lock inside supervised admission, owns close/reopen and Web auth refresh, persists cookies after use, and never installs keepalive or a drain hook. |
| `ClientComposed` | [`_web/transport/composed.py`](../src/notebooklm/_web/transport/composed.py) | Write-once holder for the Web/raw-RPC composition (`transport`, `executor`, `chain_host`, `chain_builder`, and `middlewares`) plus the shared `runtime_collaborators`. RPC admission/semaphore policy lives on `CallSupervisor`. Pre-binding access raises a clear `RuntimeError`; the holder deliberately does not expose a broad `.collaborators` alias. |
| `CallSupervisor` | [`_runtime/call_supervisor.py`](../src/notebooklm/_runtime/call_supervisor.py) | Protocol-neutral `Admission -> Metrics -> Semaphore` policy, generation-bearing call/operation leases, cancellation-safe retained settlement, race-free admitted child spawning, and lifecycle admission transitions. Its generation counter is the single in-flight source. |
| `RpcExecutor` | [`_web/transport/executor.py`](../src/notebooklm/_web/transport/executor.py) | Single logical batchexecute RPC dispatch path. Owns request-id/started-metric bracketing, idempotency policy lookup, method-ID resolution, request encoding, response decode, RPC error mapping, and decode-time auth refresh retry. Takes its `RuntimeTransport`, `AuthRefreshCoordinator`, `ClientMetrics`, and `CallSupervisor` collaborators directly via keyword-only constructor parameters (ADR-0014 Rule 5). Enters transport through `RuntimeTransport.perform_authed_post`. |
| `RuntimeTransport` | [`_web/transport/runtime.py`](../src/notebooklm/_web/transport/runtime.py) | Authed POST collaborator. Holds an admission-only `CallSupervisor` operation lease before loop checking, auth snapshot, and request materialization; after preparation it enters the supervisor's terminal `Admission -> Metrics -> Semaphore` call scope and dispatches the four-middleware web chain. Owns `refresh_request_for_current_auth()` and `terminal()` (freshness rebuild + `Kernel.post`). Called directly by `RpcExecutor` and by `_web.transport.chat.chat_aware_authed_post`; the middleware chain leaf at `MiddlewareChainHost._authed_post_chain_terminal` continues to dispatch through `RuntimeTransport.terminal` per ADR-0014 Rule 4. |
| `MiddlewareChainHost` | [`_web/transport/middleware/chain_host.py`](../src/notebooklm/_web/transport/middleware/chain_host.py) | Owns the wired middleware chain (`_authed_post_chain`), the chain leaf (`_authed_post_chain_terminal`), the three retry-budget tunables (`_rate_limit_max_retries`, `_server_error_max_retries`, `_refresh_retry_delay`), and the dynamic `await_refresh` delegate that the auth-refresh middleware captures. The chain's provider lambdas and the transport's `chain_provider` closure read the host's attributes live, so post-construction mutation (e.g. tests setting `client._web_runtime.composed.chain_host._rate_limit_max_retries = 0`) still steers the live chain. |
| `AuthRefreshCoordinator` | [`_web/transport/auth.py`](../src/notebooklm/_web/transport/auth.py) | Owns the auth-snapshot lock and refresh task. Canonical implementation for `AuthRefreshCoordinator.snapshot(auth=...)`, `update_auth_tokens(auth=..., csrf=..., session_id=...)`, and `update_auth_headers(auth=..., kernel=...)`; callers pass explicit collaborators rather than a host object. |
| `ClientLifecycle` | [`_runtime/lifecycle.py`](../src/notebooklm/_runtime/lifecycle.py) | Protocol-neutral root lifecycle. Owns resource state, generation allocation, transactional/coalesced open and close waves, pre-hook timeout validation, loop binding, phased transport ordering, rollback, and deterministic teardown failure precedence. It owns no HTTP client, auth state, keepalive task, cookie persistence, or RPC semaphore. |
| `WebTransportLifecycle` | [`_web/transport/lifecycle.py`](../src/notebooklm/_web/transport/lifecycle.py) | Web resource participant installed directly for Web selection and owned behind `LazyWebSidecar` only after deprecated Android `rpc_call` use. Activates/fences the Kernel and auth coordinator for one epoch, owns cookie-save routing, opens/closes the Kernel, and mirrors accepted cookie state into the client-owned `AuthTokens`; only the primary Web runtime can own a keepalive task. |
| `AndroidSession` | [`_android/session.py`](../src/notebooklm/_android/session.py) | Selected-Android gRPC participant. Validates optional runtimes and activates bearer state at open, constructs its TLS channel lazily, maps gRPC status/deadline outcomes, and shares `CallSupervisor` admission/telemetry with Web calls. |
| Android workflow epoch | [`_android/epoch.py`](../src/notebooklm/_android/epoch.py) | Session-tagged task-local epoch inherited by nested namespace calls inside one Android operation scope. |
| `MiddlewareChainBuilder` | [`_web/transport/middleware/chain.py`](../src/notebooklm/_web/transport/middleware/chain.py) | Constructs the web-specific `Retry -> AuthRefresh -> ErrorInjection -> Tracing` chain; `CallSupervisor` owns the protocol-neutral outer policy. |
| `ClientMetrics` | [`_client_metrics.py`](../src/notebooklm/_client_metrics.py) | Per-instance counters (`ClientMetricsSnapshot`) + the `on_rpc_event` user callback. |
| `ReqidCounter` | [`_web/transport/reqid_counter.py`](../src/notebooklm/_web/transport/reqid_counter.py) | Monotonic `_reqid` for the chat backend; lock-protected `next_reqid(...)`. |
| `CookiePersistence` | [`_web/transport/cookie_persistence.py`](../src/notebooklm/_web/transport/cookie_persistence.py) | Per-canonical-path typed baseline state, ordered `ProfileStore` cookie merges, `__Secure-1PSIDTS` rotation, and the concrete v0.x snapshot adapter. First-party `_from_store` instances retain no `AuthTokens`; public-constructor instances preserve legacy save compatibility. |
| `IdempotencyRegistry` | [`_web/policy.py`](../src/notebooklm/_web/policy.py) | Web RPC policy/classification registry keyed by `(RPCMethod, operation_variant)`. The production registry explicitly covers every active `RPCMethod`; `UNCLASSIFIED` is retained only as a placeholder for hand-built test/future registries. `RpcExecutor._execute_once()` consults its single import-time-seeded singleton to resolve `effective_disable_internal_retries`. Transport-neutral side-effect probing (`_idempotency.idempotent_create(...)`) is separate. |
| `_web/transport/request_types.py` | [`_web/transport/request_types.py`](../src/notebooklm/_web/transport/request_types.py) | Owns `AuthSnapshot`, `BuildRequest`, and request materialization shapes shared by RPC, chat, auth refresh, and the chain terminal. |
| `_web/transport/errors.py` | [`_web/transport/errors.py`](../src/notebooklm/_web/transport/errors.py) | Owns transport-level exceptions, `Retry-After` parsing, and raw `Kernel.post` error mapping consumed by `RetryMiddleware` and `AuthRefreshMiddleware`. |
| `_web/transport/streaming_post.py` | [`_web/transport/streaming_post.py`](../src/notebooklm/_web/transport/streaming_post.py) | Low-level streaming POST helper with the response-size cap used by `Kernel.post`. |
| `Kernel` | [`_web/transport/kernel.py`](../src/notebooklm/_web/transport/kernel.py) | Pure web transport core. Owns the epoch-fenced `httpx.AsyncClient` and cookie jar; exposes `post()`, cookie accessors, and `aclose()`. `WebTransportLifecycle`, not the root lifecycle, opens and closes it. Concrete class behind the `Kernel` Protocol in `_web/contracts.py`; constructed by the web runtime initializer and called from the middleware leaf via `RuntimeTransport.terminal → Kernel.post`. |
| `_runtime/init` | [`_runtime/init.py`](../src/notebooklm/_runtime/init.py) | Transport-neutral constructor validation and shared metrics/supervisor construction. This module imports no backend implementation. |
| `_web/transport/init` | [`_web/transport/init.py`](../src/notebooklm/_web/transport/init.py) | Web transport, persistence, middleware, executor, and uploader composition; returns the complete `WebRuntime`. |
| `_loop_affinity` | [`_loop_affinity.py`](../src/notebooklm/_loop_affinity.py) | Tiny free-function `assert_bound_loop(bound_loop)` shared by the protocol-neutral helpers that directly capture a loop reference at `open()` time (`ReqidCounter`, `AuthRefreshCoordinator`, `CallSupervisor`, `ChatAPI`). `ArtifactPollingService` delegates loop checks through its shared `CallSupervisor`. Enforces ADR-0004 without coupling those helpers to the public client. |

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

The [feature-service map](https://teng-lin.github.io/notebooklm-py/diagrams/05-feature-services.html) is the compact index;
the [artifact class model](https://teng-lin.github.io/notebooklm-py/diagrams/08-artifacts-class-model.html) and
[sources class model](https://teng-lin.github.io/notebooklm-py/diagrams/25-sources-class-model.html) expand its two densest
resource domains. The [deep-research lifecycle](https://teng-lin.github.io/notebooklm-py/diagrams/21-deep-research-lifecycle.html)
and [organization/sharing map](https://teng-lin.github.io/notebooklm-py/diagrams/29-organization-and-sharing.architecture.html) cover the
remaining multi-step and cross-scope relationships.

| Service / Module | Module | Responsibility |
|-------------------|--------|----------------|
| `NoteService` | [`_web/notes.py`](../src/notebooklm/_web/notes.py) | Web note-row service managing note CRUD, note-backed content generation, and sync. |
| `NoteBackedMindMapService` | [`_web/mind_maps.py`](../src/notebooklm/_web/mind_maps.py) | Web adapter service representing mind maps backed by standard notebook notes. |
| `ArtifactDownloadService` | [`_web/artifact/downloads.py`](../src/notebooklm/_web/artifact/downloads.py) | Web raw-row selection and representation lookup for finished artifacts. |
| `AssetDownloadService` | [`_artifact/downloads.py`](../src/notebooklm/_artifact/downloads.py) | Backend-neutral guarded byte transfer, staged writing, and atomic publication. |
| `ArtifactGenerationService` | [`_web/artifact/generation.py`](../src/notebooklm/_web/artifact/generation.py) | Web `CREATE_ARTIFACT` dispatch plus revise/retry/mind-map generation. |
| `_artifact_formatters` | [`_artifact/formatters.py`](../src/notebooklm/_artifact/formatters.py) | Markdown, HTML, and plain text formatters for artifacts. |
| `_web/artifact/listing` | [`_web/artifact/listing.py`](../src/notebooklm/_web/artifact/listing.py) | Web artifact listing, raw-row decoding, and note-backed mind-map composition. |
| `_web/artifact/table` | [`_web/artifact/table.py`](../src/notebooklm/_web/artifact/table.py) | Web positional data-table row extraction. |
| `_web/` | [`_web/`](../src/notebooklm/_web) | Private home for the batchexecute web backend. Web-wire codecs, row decoding, request construction, and concrete namespace implementations live here while public namespace classes remain transport-neutral bases. Direct imports are constrained by `tests/_guardrails/test_backend_boundaries.py`. |
| `_web/rows/*` | [`_web/rows/`](../src/notebooklm/_web/rows) | Web wire-shape adapters for artifacts, chat, collections, documents, labels, notebooks, notes, research, sharing, sources, and ranked source chunks. Public notebook/sharing/collection factories stay on their dataclasses as lazy shims into this package; deep-research task parsing and conversation-role decoding live here too. Strict decode behavior is pinned in the row-adapter, chat-history, research-parser, and wire-contract tests. |
| `_web/wire/*` | [`_web/wire/`](../src/notebooklm/_web/wire) | Batchexecute envelope encoding, response/status decoding, strict positional access, and runtime RPC-ID overrides. `notebooklm.rpc` preserves the public power-user path and legacy root attributes as identity re-exports; no former deep `notebooklm.rpc.*` implementation modules remain. |
| `_types/` | [`_types/`](../src/notebooklm/_types) | Private package holding the transport-neutral enum, dataclass, and `Protocol` implementations behind the public `types.py` / per-feature public schemas. Split per domain (`artifacts.py`, `artifact_content.py`, `chat.py`, `documents.py`, `enums.py`, `labels.py`, `mind_maps.py`, `notebooks.py`, `notes.py`, `research.py`, `sharing.py`, `sources.py`, plus `common.py` for shared shapes like `ConnectionLimits`). |

## Authentication subpackage

[`auth.py`](../src/notebooklm/auth.py) is the public capability facade over
canonical implementations under [`_auth/`](../src/notebooklm/_auth) and the
optional browser-acquisition package. Most names are direct, identity-preserving
aliases. Its small function layer owns `enumerate_accounts`, installs a neutral
default L3 recovery rung, and lazily imports `_browser` for first-party login,
readiness, and OAuth-capture capabilities. Importing the base package therefore
loads neither `_browser` nor Playwright.

Three visuals separate concerns that are easy to conflate: the
[authentication architecture](https://teng-lin.github.io/notebooklm-py/diagrams/04-authentication.html) shows ownership,
the [login workflow](https://teng-lin.github.io/notebooklm-py/diagrams/10-login-workflow.html) shows credential acquisition,
and the [auth class model](https://teng-lin.github.io/notebooklm-py/diagrams/24-auth-class-model.html) shows the storage and
recovery owners. ADR-0031 names the credential tiers; ADR-0032 introduces the domain values;
ADR-0033 constrains consolidation; and ADR-0034 defines the current storage object model.

| Module | Responsibility |
|--------|----------------|
| [`_auth/tokens.py`](../src/notebooklm/_auth/tokens.py) | `AuthTokens` plus the typed stored-auth application boundary. `StoredAuthLoader` keeps inline/file source, paired seed, final-attempt route, acquisition baseline, initial store merge, and closed `InlineLoadedAuth | FileLoadedAuth` result together. `TokenAcquirer` is its sole structural seam; blocking source/account/merge work is offloaded. The v0.x classmethod and client share the call-time `_load_stored_auth` composition function. |
| [`_auth/paths.py`](../src/notebooklm/_auth/paths.py) | Storage paths and filesystem helpers, including the **single** derivation behind all four credential lock files (`.lock`, `.rotate.lock`, `.refresh.lock`, `.lock.bootstrap` — the last folded in from `master_token.py` by ADR-0033 PR 1.3, which kept every path byte-identical and every lock mechanism untouched). |
| [`_auth/storage_lock.py`](../src/notebooklm/_auth/storage_lock.py) | Dependency-bottom `StorageLockManager`: process-default exact-raw-path thread-lock identity, POSIX/Windows OS gateway, bounded synchronous retry, and manager-lifecycle cookie warning claim. Imports stdlib only; `storage`, `profile_store`, and `keepalive` share its process default. |
| [`_auth/credential_io.py`](../src/notebooklm/_auth/credential_io.py) | Sealed commit capability: the sole unchecked-atomic importer, with distinct private wrappers for complete profile and arbitrary-path master-token documents. |
| [`_auth/master_token_types.py`](../src/notebooklm/_auth/master_token_types.py) | Dependency-bottom `MasterTokenError`, immutable `MasterToken`, and pure permissive version-1 legacy-record codec. The exception keeps historical `notebooklm._auth.master_token` module/pickle identity; the credential secret is redacted from repr. |
| [`_auth/master_token_file.py`](../src/notebooklm/_auth/master_token_file.py) | Path-owned one-sample raw/typed master-token reads and canonical writes under the exact dotted sibling lock. Explicit paths remain valid for v0.x adapters; it owns no network/bootstrap policy or cache. |
| [`_auth/mint_service.py`](../src/notebooklm/_auth/mint_service.py) | Stateless durable-token network boundary. Frozen `OAuthClientSpec` values select an OAuth identity; secret-safe `MintedOAuthToken` values carry the bearer and optional bounded server expiry. Fixed OAuth failures discard raw responses, dependency causes, and credential-bearing traceback locals while preserving distinct dependency errors. The unchanged web mint supplies the Chromecast/OAuthLogin spec before MergeSession/RotateCookies. Lazy dependency loading, serialized third-party logger suppression, offload, and the sole production `gpsoauth.perform_oauth` call remain here. It owns no paths, persistence, storage/domain locks, bootstrap/recovery policy, cache, or retained secrets. |
| [`_auth/master_token_bootstrap.py`](../src/notebooklm/_auth/master_token_bootstrap.py) | Concrete path-owned bootstrap/re-mint coordinator over exactly one `MintService`, one `ProfileStore`, one bootstrap lock, and one verifier. It owns two-owner advisory checks, session-before-token persistence, strict reload, the four-state missing-storage recheck, and shield-to-settlement cancellation; token I/O routes only through its store. It imports no token file, storage facade, runtime/client, CLI, or recovery owner. |
| [`_auth/cookie_filter.py`](../src/notebooklm/_auth/cookie_filter.py) | Dependency-bottom raw capture/domain filter plus value-free malformed-row diagnostics. Pure policy/logging: no paths, files, locks, commits, documents, or lifecycle state. |
| [`_auth/profile_store.py`](../src/notebooklm/_auth/profile_store.py) | Path-owned synchronous document/session reads, derived typed master-token access, blocking cookie transactions, typed in-band account read/update/clear, and typed browser/remint, login/import, and minted-session replacement. Token methods resolve the sibling path at call time and share the store's lock manager. Minted replacement owns the same-lock latest-owner gate, default raw filter, lossless destination preservation/rebind, and one commit. Its repr-hidden request snapshots raw master-token cookie fields (`same_site="None"`) and runtime-permissive email together before path/lock work; it intentionally does not use filtering/SameSite-lossy `CookieJar.from_httpx()`. Owns no cache, baseline, live HTTP jar, legacy scheduler, or network policy. |
| [`_auth/profile_migration.py`](../src/notebooklm/_auth/profile_migration.py) | Concrete legacy-account ownership plus the path-shaped native login replacement operation: primitive keep/clear/set inputs become dependency-bottom directives, one `ProfileStore`/request/writer composition returns `ReplaceResult`, and post-login reconciliation stays outside the store lock. Also owns lossless two-read resolution, `context.json` read/scrub, only-if-absent embed-before-scrub promotion, and canonical retryable single-flight daemon scheduling with bounded exit drain. It depends downward on `ProfileStore` and account values; no loader, network, runtime, CLI, or token orchestration. |
| [`_auth/storage.py`](../src/notebooklm/_auth/storage.py) | Shrinking v0.x persistence policy/compatibility facade. Retains raw signatures/results, lock and cookie adapters, the minted live-jar snapshot adapter, and a thin arbitrary-path token writer over `MasterTokenFile`. Remint/login wrappers use exhaustive maps to project native `ReplaceResult`/`CookieMergeResult` statuses into their legacy return types; first-party capture, app, CLI, runtime, and recovery paths consume native results instead. Filter names and transaction functions remain exact compatibility aliases. |
| [`_auth/extraction.py`](../src/notebooklm/_auth/extraction.py) | Cookie/token extraction from browser sessions. |
| [`_auth/cookies.py`](../src/notebooklm/_auth/cookies.py) | Cookie maps, `_update_cookie_input`, the network-free pure storage loaders, and thin recovery-composition adapters. Each successfully converted live cookie supplies identity/value/runtime fields while SameSite comes from that same sanitized raw row, preserving first-successful duplicate behavior in the typed baseline. |
| [`_auth/cookie_policy.py`](../src/notebooklm/_auth/cookie_policy.py) | Domain allowlist, cookie-domain builder (`build_cookie_domain_allowlist`), and cookie policy decisions. |
| [`_auth/cookie_semantics.py`](../src/notebooklm/_auth/cookie_semantics.py) | Shared cookie-shape and expiry semantics used by sanitized auth loaders and persistence boundaries. |
| [`_auth/cookie_types.py`](../src/notebooklm/_auth/cookie_types.py) | The canonical `Cookie` / `CookieJar` types (ADR-0031 Stage 1): constructors from every input shape, converters to httpx/storage-state, and the cookie-set policy questions as methods. A delegating wrapper — policy still lives in `cookie_policy`/`cookies`. |
| [`_browser/browser_capture.py`](../src/notebooklm/_browser/browser_capture.py) | **One deep module for the browser launch→navigate→capture→filter→heal→persist core** (ADR-0033 sanctioned merge — absorbed `browser_state_validation.py` and `login_wait_trace.py`), lazy `playwright`; shared by the interactive `_app/login_browser.py` flow and the layer-3 headless re-auth layer (ADR-0021/ADR-0036). Both capture arms construct `RemintWriteRequest` and consume `ProfileStore.replace_from_remint() -> ReplaceResult` directly. The headless arm classifies the landing URL (authenticated→capture, redirected-to-login→`HeadlessLoginRequiredError`). `run_cdp_capture` is an alternative credential source: attach to an operator-pointed already-running Chrome over CDP (`connect_over_cdp`, disconnect-only teardown) using the SAME landing classification + cookie-domain allowlist. Absorbed sections: (1) **login-wait DEBUG tracing** — `log_observed_navigations` logs each main-frame navigation observed during the five-minute interactive wait at DEBUG so `notebooklm -vv login` is self-diagnosing when a login never lands; inert unless DEBUG is enabled (no listener attached) and it swallows every listener exception, so it can never destabilise the wait. Redaction goes through its own `trace_url`, which keeps **only** scheme + host — deliberately stricter than `extraction._safe_url` (which preserves the path outside a Google-OAuth allowlist), because this traces arbitrary SSO redirects where a federated IdP can carry a one-time assertion in the path. The two redactors are **kept distinct on purpose**. (2) **captured-state heal** — `heal_captured_state`, a best-effort in-memory PSIDTS heal for Playwright-captured state that preserves cookie attributes, returns `(state, error)` and never raises, so a failed heal cannot discard a completed sign-in. `browser_launch_errors.py` stays a separate leaf for cohesion — a channel registry plus a pure classifier, testable without a browser. |
| [`_browser/navigation_errors.py`](../src/notebooklm/_browser/navigation_errors.py) | Transport-neutral leaf for `browser_capture`: classifies a Playwright navigation failure from its message. `navigation_error_code` extracts the `net::ERR_*` token (anchored, so it cannot return arbitrary text after a stray `net::`) — used to log a failure without logging the credential-bearing URL Playwright embeds in it. |
| [`_browser/browser_launch_errors.py`](../src/notebooklm/_browser/browser_launch_errors.py) | Transport-neutral leaf for `browser_capture`: the `CHANNEL_BROWSERS` channel registry plus `classify_launch_failure`, which maps a Playwright launch failure to actionable help or to `None` so the original exception propagates. |
| [`_browser/headless_reauth.py`](../src/notebooklm/_browser/headless_reauth.py) | Layer-3 browser recovery implementation. It maps its browser-specific result onto `_auth.recovery_rungs`, resolves explicit storage paths to their own persistent browser profile, and remains opt-in/local-unattended-only. |
| [`_browser/oauth_token.py`](../src/notebooklm/_browser/oauth_token.py) | Visible EmbeddedSetup browser capture for the single-use OAuth cookie, returning only a token string or canonical `MasterTokenError`. |
| [`_auth/recovery_rungs.py`](../src/notebooklm/_auth/recovery_rungs.py) | Neutral process-level registry and closed outcome for the optional blocking L3 implementation. The public auth facade installs a lazy default; recovery reads only this leaf and never imports browser implementation code. |
| [`_auth/recovery.py`](../src/notebooklm/_auth/recovery.py) | Client-neutral recovery composition and exact paired replacement provenance. A network- and write-free mid-session adapter can reload changed file-backed cookies before external recovery while preserving a concurrently refreshed live jar. One-shot `ColdRecoveryCoordinator` owns the explicit L2.5 → L3 → L4 ladder and scrubs all eleven injected callbacks on every exit. `ColdRecoveryState` owns synchronized weak-loop path locks and success generations; class-owned `_drive_cold`/`_coalesce_cold` are the sole ladder/flight bodies, while the exact-signature legacy functions are thin process-default adapters. |
| [`_auth/single_flight.py`](../src/notebooklm/_auth/single_flight.py) | `SingleFlight` owns one cross-loop flight registry, strong leader-task set, and per-canonical-path success epochs. A leader task mirrors into a `concurrent.futures.Future`; followers use shielded settle-before-propagate bridging, so waiter cancellation never cancels shared work. Claim plus stale-epoch comparison is atomic, settled slots prompt-pop, and quiescent-only reset refuses live work. Exact module functions remain process-default adapters. |
| [`_auth/account_types.py`](../src/notebooklm/_auth/account_types.py) | Dependency-neutral immutable `Account` and `PlaywrightAccountRepairResult` values. Both retain historical `notebooklm._auth.account` module/pickle/repr identity. |
| [`_auth/account_repair.py`](../src/notebooklm/_auth/account_repair.py) | One-operation `AccountRepairService` over six exact collaborators. It claims synchronously before its first await, offloads only cookie loading, performs typed write/clear synchronously, maps only the frozen handled exception set to the legacy result, and scrubs all collaborator references on success, error, cancellation, or an unlisted exception. |
| [`_auth/account.py`](../src/notebooklm/_auth/account.py) | Account network adapter: probing `?authuser=N`, extracting the active email, formatting the wire value, and composing one `AccountRepairService` with call-time legacy seams. Typed in-band writes live in `ProfileStore`; legacy file policy lives in `_auth/profile_migration.py`; raw compatibility remains in `_auth/storage.py`. |
| [`_auth/account_email.py`](../src/notebooklm/_auth/account_email.py) | Generation-safe account-email resolution: match persisted identity to the authoritative live cookie route, probe through an injected callback when needed, and self-heal with exact-document CAS without crossing profile-session generations. |
| [`_auth/session.py`](../src/notebooklm/_auth/session.py) | `refresh_auth_session(auth=..., kernel=..., auth_coord=..., web_transport=..., cookie_persistence=..., expected_epoch=...)` implementation called by `AuthRefreshCoordinator`. Takes explicit keyword-only collaborators plus the admitted resource epoch instead of a Session-shaped owner Protocol; the previous `RefreshAuthCore` Protocol and the `update_auth_tokens` / `update_auth_headers` Session-level forwards have been removed. |
| [`_auth/refresh.py`](../src/notebooklm/_auth/refresh.py) | Token refresh driver, sole `ColdRecoveryCoordinator` production adapter, and typed `fetch_tokens_with_domains` persistence boundary. `_cold_fallbacks` supplies late-bound L2.5, cold-delegation, route, final-fetch, and jar-replacement closures while preserving exact logs and raw caller / canonical L2.5 / raw caller route timing. L2.5 remains outside the cold single-flight. The domain fetch consumes one paired live/SameSite-preserving baseline sample, carries the selected initial/L2.5/L3/L4 baseline, captures an immutable final observation, and offloads one concrete `ProfileStore` merge. `HARD_FAILURE`, the sole non-advancing result, retains the exact selected baseline; advancing outcomes return the exact next baseline. Caller cancellation during worker offload propagates immediately, without preventing an already-dispatched merge from finishing. The frozen `RefreshDeps` and v0.x compatibility ladder remain. |
| [`_auth/keepalive.py`](../src/notebooklm/_auth/keepalive.py) | Cookie keepalive and `__Secure-1PSIDTS` rotation policy. `RotationState` owns per-loop/per-canonical-path locks and monotonic attempt stamps behind one short-held threading lock; claims are stamped before POST, so failure and cancellation consume the 60-second slot. Historical raw state names are non-owning identity views into the process-default owner, and the raw RotateCookies wire remains an exact `mint_service.py` re-export. |
| [`_auth/psidts_recovery.py`](../src/notebooklm/_auth/psidts_recovery.py) | Inline PSIDTS recovery plus the generic load→validate→heal→retry composition over injected pure loaders. It owns typed raw-document observation/CAS and `ProfileStore` persistence, not cookie-module or storage-facade policy. Sentinel/contended/acquired paths preserve their distinct rereads and narrow caught-error sets; success means the post-save disk state is live, including a sibling winner. Also owns the captured-cookie `validate`/`heal` compatibility seam. |
| [`_auth/master_token.py`](../src/notebooklm/_auth/master_token.py) | Headless master-token compatibility boundary: exchange/mint remain exact v0.x adapters over `MintService`, the raw reader projects one `MasterTokenFile` sample, the writer preserves the call-time `storage.write_master_token` seam, and coarse operations compose `MasterTokenBootstrapper` with late-bound legacy-owner, Android-ID, strict-loader, and verifier bridges. `MasterTokenError` is an identity re-export from the dependency-bottom types leaf. |

The ownership-refactor completion snapshot measured 1,090 lines in `storage.py`, 602 in
`profile_migration.py`, 876 in `profile_store.py`, 96 in `cookie_filter.py`, and 89 in
`master_token_file.py`: 2,753 lines total. Those figures are historical ratchet
evidence, not a live size inventory; use the module-size guardrail for current measurements.
The migration module is internal composition, not a public `ProfileStore` extension surface.
The loader owners remain in `tokens.py` and `refresh.py`. Runtime composition consumes their closed
`FileLoadedAuth` result by registering its exact `ProfileStore`/baseline pair in runtime
`CookiePersistence`, without rereading disk. Direct clients prepare a one-shot disk baseline before
transport; fileless clients capture only the live compatibility projection. A missing saver always
uses ordered typed merges; only an explicit `cookie_saver=` retains the v0.x callback surface and
its per-key adapter snapshot. The former web-specific
`ClientLifecycle` owned the sole `AuthTokens.cookie_snapshot` mirror. The lifecycle split moved
that web responsibility to `WebTransportLifecycle`; the current root
`ClientLifecycle` owns no auth or cookie state. At the same completion snapshot, runtime owners were 457 lines in
`_web/transport/cookie_persistence.py`, 618 in `_runtime/init.py`, 628 in `_runtime/lifecycle.py`, and 992 in
`client.py`.

The completed state-ownership refactor does not change the public ladder or on-disk schema. Its
completion snapshot recorded **40 modules / 15,237 lines / 128 unique edges (117 module + 11
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
See the [CLI subsystem diagram](https://teng-lin.github.io/notebooklm-py/diagrams/16-cli-subsystem.html) for the command,
service, `_app`, client, and rendering boundaries.

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
| [`cli/master_token_login.py`](../src/notebooklm/cli/master_token_login.py) | Command driver for `notebooklm login --master-token[-refresh]`: resolves paths and renders the outcome over the public `master_token_bootstrap` / `master_token_remint` / `assert_account_writable` adapters. Coordination lives in `_auth/master_token_bootstrap.py`; the CLI-side OAuth adapter delegates interactive capture through the lazy `auth.py` capability and scrubs credential-bearing arguments from failure frames (ADR-0023/ADR-0036). |
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
or private names from public modules. The sole sanctioned private-package
exception is `notebooklm._app`, the transport-neutral business-logic layer
every adapter consumes. Browser login coordination lives in
[`_app/login_browser.py`](../src/notebooklm/_app/login_browser.py); its injected
capabilities cross the lazy `auth.py` facade into `_browser`, so no CLI module
imports `notebooklm._browser` directly. No `_auth.*` module may be imported by
the CLI either. The same test keeps
low-level helpers (`runtime`, `context`, `resolve`, `rendering`,
`auth_runtime`, `options`) from growing upward dependencies on command modules
or the `cli.helpers` compatibility facade.

## MCP adapter (`mcp/`)

The MCP server is a second thin adapter beside `cli/`, opt-in behind the `mcp`
extra and **experimental** (preview). `create_server()` builds a FastMCP server
that exposes the `_app/` cores as MCP tools driving a single long-lived
`NotebookLMClient`; run it with the `notebooklm-mcp` console script (stdio or
loopback HTTP). That client is opened **lazily**, behind a
[`ClientProvider`](../src/notebooklm/mcp/_clientprovider.py): the lifespan starts
the open in the background and yields immediately, so the MCP `initialize`
handshake never waits on Google's auth round-trip — whose budget (a 15 s
`RotateCookies` poke plus a 30 s CSRF fetch, more when the cold-recovery ladder
runs) can exceed the 30 s deadline clients give the handshake, which surfaced as
an opaque `CONNECT_TIMEOUT` (#2330). The first tool call awaits the open instead,
where an auth failure is reported as a normal categorized tool error and the
next call retries it — so a mid-session `notebooklm login` recovers the server
without a restart. It imports no `click` / `rich` / `cli` — like the CLI, it is built
on the `_app/` cores only (enforced by `tests/_guardrails/test_mcp_boundary.py`).
Failures surface as `CODE: message` strings projected from `_app.errors.classify`,
and mutating tools are confirmation-gated (they return a `needs_confirmation`
preview unless called with `confirm=true`). `notebooklm mcp install <client>`
wires it into Claude Desktop/Code, Cursor, or Windsurf, and `desktop-extension/`
packages a one-click `.mcpb` bundle. Full guide:
[`docs/mcp-guide.md`](./mcp-guide.md).
The [MCP subsystem diagram](https://teng-lin.github.io/notebooklm-py/diagrams/17-mcp-subsystem.html) shows the adapter,
application-core, client, and remote-transfer boundaries together.

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
The [REST subsystem diagram](https://teng-lin.github.io/notebooklm-py/diagrams/18-rest-server-subsystem.html) shows the
lifespan-owned client, route guards, application cores, and response projection.

## Middleware chain (ADR-0009)

The runtime chain order is pinned by
[`tests/unit/test_chain_wiring.py`](../tests/unit/test_chain_wiring.py)
(facade-level) and
[`tests/unit/test_middleware_chain_builder.py`](../tests/unit/test_middleware_chain_builder.py)
(builder-level). The order is load-bearing: changing it without
simultaneously updating the pin tests
(`test_chain_seeded_with_final_adr_009_ordering`) is a bug.

The chain list in [`MiddlewareChainBuilder.build()`](../src/notebooklm/_web/transport/middleware/chain.py)
reads outermost-first (index 0 wraps everything below it):

```text
CallSupervisor               protocol-neutral Drain → Metrics → Semaphore
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
the shared collaborator bundle, one primary backend runtime, backend
preference/reporting, the backend-selected raw adapter, and the feature API instances. The protocol-neutral `ClientLifecycle` owns resource state and
open/drain/close wave orchestration across the installed transport participants.
Web selection builds `WebRuntime`; `WebTransportLifecycle` owns the Kernel,
web auth/keepalive preparation, and cookie persistence, while
`SourceUploadPipeline` is its second transport participant. Android selection
does not build that bundle: it installs `AndroidRuntime` with the bearer provider,
`AndroidSession`, Android upload/asset services, and Phenotype transport while
replacing all eleven public namespace adapters. Its frozen lifecycle tuple also
contains the inert `LazyWebSidecar` proxy required only by the deprecated root
`rpc_call` compatibility window. Android stored-auth assembly uses the
name-only, read-only load policy: it skips PSIDTS poke/recovery and never merges
the homepage cookie observation into the profile. `CallSupervisor` owns generation admission, drain policy, RPC
metrics, and the client-wide RPC semaphore through one generation-local in-flight counter.

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
   `close`, `drain`, and `is_connected` call the root `ClientLifecycle`.

`client.raw` is the supported backend-selected escape hatch. On Web,
`WebRawAPI.call` uses the primary bundle's `RpcExecutor`; on Android,
`AndroidRawAPI.unary` and `unary_stream` use `AndroidSession`.
`NotebookLMClient.rpc_call(method, params)` is deprecated for v1.0 removal. A
Web client delegates through its installed executor. An Android client warns
once, then materialises one Web runtime through `LazyWebSidecar` inside
`operation_scope("rpc_call.sidecar")`. The proxy participates in root
open/close so forced-close races are fenced and a later client reopen restores
the already-materialised bundle. It deliberately has no drain hook and no Web
keepalive; once materialised, its own Web auth ladder and cookie persistence are
used.

Feature APIs receive the collaborator they need (`RpcExecutor` for
`RpcCaller`, `CallSupervisor` for admitted workflows/children/hooks and the
chat `LoopGuard`, and the concrete `Kernel` for web upload cookies/posting) per
ADR-0014 Rules 1 + 3 under the current supervisor ownership. Features that need more than one
capability — `ChatAPI`, `ArtifactsAPI`, and `SourceUploadPipeline` — take each
collaborator by keyword-only constructor argument. The composition wiring is centralized in
[`_client_assembly.py`](../src/notebooklm/_client_assembly.py), which is
called by both `NotebookLMClient.__init__` and the canonical test
factory.

## Testing patterns

Two policies define how tests interact with the architecture above.
The [testing and guardrails view](https://teng-lin.github.io/notebooklm-py/diagrams/22-testing-and-guardrails.html) maps the
suite taxonomy to the boundaries each tier protects.

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
`MagicMock(spec=RpcCaller, rpc_call=AsyncMock(...))`-style fakes directly via
the concrete web constructors; neutral workflow tests subclass the abstract
bases and implement their pinned hooks.

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
| Drain | Graceful-shutdown waiting on admitted transport operations to complete. Policy, generation ownership, and in-flight accounting live in `CallSupervisor`. |
| `idempotent_create(...)` | Caller-owned probe-then-create wrapper used by source-add / Drive-add flows. Distinct from the `IdempotencyRegistry` (which only classifies retry safety inside the executor). |
| `operation_variant` | Optional kwarg on `rpc_call(...)` that selects a method-variant-specific idempotency policy from the registry (e.g. `ADD_SOURCE` `"url"` vs `"drive"`). Unknown variants raise `IdempotencyVariantError`. |
| RPC method id | A short obfuscated identifier (`rpcids=`) Google uses to route batchexecute calls. Source of truth: `RPCMethod` enum in `rpc/types.py`. |
| Snapshot | An `AuthSnapshot` (see [`_web/transport/request_types.py`](../src/notebooklm/_web/transport/request_types.py)) — an immutable, point-in-time view of session id, CSRF token, authuser, and account email. Taken inside the auth-snapshot lock so a refresh racing with a transport build cannot tear. |

## File map

Per-file index plus the full `src/notebooklm` + `tests` repository tree. The tree is the hand-maintained module map that [`scripts/check_claude_md_freshness.py`](../scripts/check_claude_md_freshness.py) checks in both directions (documented paths exist; every module is documented or intentionally omitted).

### Key Files

| File | Purpose |
|------|---------|
| `client.py` | Main `NotebookLMClient` class |
| `raw.py` | Public raw descriptors, replay policy, and backend-selected escape-hatch APIs. |
| `_client_assembly.py` | Single private assembly seam (`_assemble_client`) that wires every constructor-set attribute; shared by `NotebookLMClient.__init__` and the canonical test factory (`tests/_helpers/client_factory.py`) so the two construction paths cannot drift. |
| `_web/raw.py` | Thin Web raw adapter that preserves `RpcExecutor.rpc_call` behavior. |
| `_web/transport/composed.py` | Web composition holder for transport, executor, chain host, middleware metadata, and the shared runtime bundle. |
| `_web/transport/seams.py` | Constructor-only injectable seams used by tests and collaborator construction. |
| `_android/` | Android backend package. Its package marker and selected adapter imports are dependency-free; generated protobuf modules remain lazy. Explicit Android preference installs Android adapters for all eleven public namespaces. The installed namespace graph has no Web operation collaborators; native gRPC/Scotty/asset paths and local composition cover the public contract. |
| `_android/auth.py` | Generation-fenced `BearerProvider`: off-loop typed profile reads, shared mint waves, bounded expiry caching, compare-and-clear invalidation, and secret-safe teardown. |
| `_android/phenotype.py` | Headless GMS Phenotype token provider: mints the per-account Play Books experiment `serverToken` via a single-package `getExperimentsAndConfigs` POST, TTL-caches it, and wraps it into the `x-goog-ext-202964622-bin` add-path metadata (#2302). |
| `_android/play_books.py` | Android Play Books wire codecs plus the exact tentative-state and static-metadata helpers used by its guarded one-time stale-token retry. |
| `_android/codecs/` | Typed protobuf-to-public-dataclass projection package for Android adapters. |
| `_android/codecs/account.py` | Strict frozen account-flag projection; missing account/user/premium message blocks fail closed. |
| `_android/codecs/notebooks.py` | Android project and notebook-guide projections plus bounded notebook decode/status errors. |
| `_android/codecs/sources.py` | Android source projection, enum-name mapping, duplicate handling, and strict/default drift behavior. |
| `_android/codecs/artifacts.py` | Ledgered Android artifact, representation, and exact `Artifact.artifact_user_state #18` projection with bounded decode failures; preserves recognized audio/flashcard state and unknown populated state. |
| `_android/codecs/chat.py` | Proven Android history and citation projection layered on the shared document codec. |
| `_android/codecs/documents.py` | Shared exact-`TailwindDoc` decoder plus plain-text/Markdown renderers used by source full-text, chat answers, and report downloads. |
| `_android/codecs/notes.py` | Evidence-bounded note request builders plus ordinary-note and exact-kind note-backed mind-map projections. |
| `_android/codecs/sharing.py` | Sharing-status projection for collaborator rows, permissions, public settings, limits, and policy flags from exact and repository-local wire overlays. |
| `_android/codecs/organization.py` | Strict heterogeneous organization decoder: wrapped exact `SourceId` members for labels and bare UTF-8 notebook UUID members for collections. |
| `_android/codecs/research.py` | Strict research discovery job/result projection with exact mode/status mapping and bounded drift errors. |
| `_android/errors.py` | Sanitized gRPC-status projection plus the pre-I/O unsupported-operation helper; raw transport exceptions and details never cross this boundary. |
| `_android/notebooks.py` | Selected Android notebook adapter: reads and evidence-admitted notebook create/delete/title-and-emoji update/copy/guide operations. Copy implements the neutral `_send_copy` boundary while retaining native decode validation and chat-session hints. Recent-removal uses the native route; its `INTERNAL` response for owned notebooks is folded into the same already-absent no-op the Web frontend exposes, while genuinely shared notebooks are removed natively. |
| `_android/session.py` | Lazy Google-TLS gRPC transport participating in root loop/lifecycle supervision, aggregate deadlines, per-call bearer metadata, status mapping, safe-read replay, and full stream leases. |
| `_android/epoch.py` | Session-tagged task-local epoch propagation for Android namespace workflow scopes. |
| `_idempotency.py` | Backend-neutral create probing and transport-loss outcome helpers; marks ambiguous non-idempotent writes unconfirmed while preserving confirmed authentication, validation, and backend rejections. |
| `_android/sources.py` | Selected Android source adapter: `GetProject` reads, exact URL/text/YouTube/Drive adds, freshness checks, native stale-Drive-source refresh, maintenance/content methods, generic file uploads, and Android-bearer Drive-file download followed by Android registration/upload. |
| `_android/source_search.py` | Native replay-safe `RetrieveRelevantChunks` dispatch and protobuf-to-`RelevantChunk` projection for `sources.search`. |
| `_android/source_transfers.py` | `AndroidSourceTransferMixin`: `AddSourcesAsync` (queued stub rows + acknowledgements), `AppendSource` (in-place text append) and `CopySourcesAsync` (original→copy mapping) over native gRPC (#2283); kept out of `sources.py` for the module-size budget. |
| `_android/drive_staging.py` | `DriveStagingTransfer`: stages a local file in the caller's own Drive, imports it, and deletes the staged copy. Used for the file types the mobile upload frontend will not parse (the OOXML containers `.docx`/`.pptx`), so an Android-selected client needs no Web collaborator. Built over the upload pipeline's transport, so it shares its epoch/deadline/client-tracking discipline. |
| `_android/upload.py` | Selected `AndroidUploadPipeline`: epoch-fenced generic tentative registration, strict bearer-authenticated Scotty start/finalize, and a bounded exact-origin Drive v3 metadata/media downloader for `add_drive_file`; it uses one aggregate operation deadline, independent download/upload admission, restricted temporary files, and secret-safe teardown. |
| `_android/evidence.py` | One pinned Android evidence profile for the captured app version and distinct registration/finalize user agents. |
| `_android/artifacts.py` | Publicly selected artifact adapter: aggregate listing/polling, all public generation families, live `DeriveArtifact` slide revision, retry, interactive and note-backed mind maps, strict media/slide transfers, local report/quiz/flashcard/mind-map/data-table saves, delete/rename, infographic download, report suggestions, and Drive export. |
| `_android/artifact_creation.py` | Exact Android `CreateArtifact` option/request builders for video (including cinematic code 3), tailored reports/study guides/concept explanations, flashcards, quizzes, infographics, slide decks, data tables, and interactive mind maps. |
| `_android/artifact_collaborators.py` | Narrow typed protocol for joining note-backed mind maps into Android artifact listing. |
| `_android/artifact_mutations.py` | Web-derived mobile `GenerateArtifact` retry and the `_send_export`-backed `ExportToDrive` mutation with exact request/response types, lifecycle fencing, and bounded response validation. |
| `_android/artifact_note_mind_maps.py` | Native `ActOnSources` plus `CreateNote` workflow for note-backed mind-map generation within one Android transport epoch. |
| `_android/artifact_outputs.py` | Bounded local representation decoding and atomic text publication: progressive media selection, typed/exact-protobuf prefetch, typed note-backed mind-map prefetch, app/tree parsing, TailwindDoc Markdown rendering, and BOM-prefixed data-table CSV rendering. |
| `_android/artifact_reads.py` | Notebook-scoped ownership preflights, exact Studio artifact reads, and safe selection of completed or caller-prefetched Android artifact metadata. |
| `_android/artifact_transfers.py` | `AndroidArtifactTransferMixin`: the `_send_copy` implementation for `CopyArtifactsAsync` (original→new artifact row mapping) and the abstract-read implementation for the account-level `GetArtifactCustomizationChoices` option tables over native gRPC (#2283). |
| `_android/artifact_proto.py` | Lazy handles for exact artifact/read protobuf modules and repository-local evidence overlays so public Android backend construction does not eagerly import generated descriptors. |
| `_android/note_backed.py` | Narrow adapter projecting the selected typed note-backed mind-map reader into aggregate Android artifact rows. |
| `_android/assets.py` | Publicly selected, lifecycle-drained Android asset transport. It validates canonical hosts and every hop, clears ambient cookies, performs the APK-evidenced bearer-authenticated `GET` with `alr=yes` only on exact admitted entry hosts, strips credentials after leaving the allowlist, enforces representation-specific length/stream/signature limits, corrects verified WAV destinations to `.wav`, and publishes through same-directory staging atomically. PDF and PPTX slide transfer are live-proven. |
| `_android/chat.py` | Selected Android chat adapter over typed settings/turn-role reads, settings mutation, sessions, raw turns, history deletion, the cumulative server stream, and citation-rich saved-response notes. Base `ChatAPI` owns locks/cache/follow-up orchestration, configure/settings result construction, and exhaustion-aware prior-turn counting. |
| `_android/notes.py` | Selected implementation of the eight-method Notes manifest: exact note CRUD write/read-back checks, bounded idempotent deletion polling, exact-kind note-backed mind-map list/delete, and rich saved-response creation with current-server citation fields. Unknown creation time remains `None`, raw map rows preserve the supported ID/content prefix, and genuine post-delete absence remains `None`. |
| `_android/settings.py` | Native output-language and account-limit adapter over exact `GetOrCreateAccount` and `MutateAccount`; temporary live mutation/read-back was restored in `finally`. |
| `_android/sharing.py` | Native `GetProjectDetails`/`ShareProject` adapter for status, public links, collaborator grants/updates/removals, and read-back. `set_view_level` uses the native `MutateProject` tag-9 branch and folds the written level into the fresh sharing projection. |
| `_android/mind_maps.py` | Publicly selected Android mind-map composition over base-typed artifact/note collaborators. Interactive generation/tree reads and note-backed rename/delete/tree/prefetch compose through live typed operations; note-backed generation uses Android `ActOnSources` plus native `CreateNote`. |
| `_android/organization.py` | Shared lazy-protobuf transport/building seam for exact `GetLabels` plus generated web-derived manual organization writes; every write is non-replayed and epoch-fenced by its adapter workflow. |
| `_android/labels.py` | Selected label adapter with native manual CRUD/membership, exact create-response correlation, one-member writes, strict read-backs, and live-proven automatic generation through `CreateLabel.auto_create #5`. |
| `_android/collections.py` | Complete implementation of all nine collection methods with exact create-response correlation, member-order joins, one-member non-atomic writes, and one outer lifecycle lease per workflow. Explicit `backend="android"` selects this namespace; default and Web selection remain Web. |
| `_android/research.py` | Selected synchronous and async Research adapter with native Web/Drive fast starts, native deep starts, stateful non-replayed cancel/import, replay-safe polls, and epoch-fenced workflows. |
| `_android/account.py` | Private account `GetOrCreateAccount` adapter: lazy protobuf import, one epoch lease, conservative non-replay, and no public client namespace. |
| `_android/proto/` | Checked-in generated Python protobuf package. Files are regenerated only by `scripts/regenerate_android_protos.py` with the pinned toolchain and are never generated during installation. |
| `_android/proto/google/internal/labs/tailwind/orchestration/v1/account_pb2.py` | Exact-package account `UserInfo`, `PremiumUserInfo`, `Account`, and GetOrCreateAccount request/response descriptors. |
| `_android/proto/google/internal/labs/tailwind/orchestration/v1/account_pb2_grpc.py` | Deterministic service-free companion for the exact account message overlay. |
| `_android/proto/google/internal/labs/tailwind/orchestration/v1/read_pb2.py` | Exact-package read messages and descriptors for `GetProject` and `ListRecentlyViewedProjects`. |
| `_android/proto/google/internal/labs/tailwind/orchestration/v1/read_pb2_grpc.py` | Deterministic service-free companion for the read message overlay. |
| `_android/proto/google/internal/labs/tailwind/orchestration/v1/notebooks_pb2.py` | Durable exact-package notebook mutation/guide messages imported by the cumulative service; local parser overrides remain only for live-only fields. |
| `_android/proto/google/internal/labs/tailwind/orchestration/v1/notebooks_pb2_grpc.py` | Deterministic service-free companion for the exact notebook message overlay. |
| `_android/proto/google/internal/labs/tailwind/orchestration/v1/orchestration_service_pb2.py` | Sole exact-package cumulative orchestration descriptor: 57 implemented methods, including exact `RemoveRecentlyViewedProject`, live APK-exact `DeriveArtifact`, and seventeen conventional-name signatures explicitly tracked as Web-derived inferences; the separate sharing descriptor adds two exact paths and the exception manifest is empty. |
| `_android/proto/google/internal/labs/tailwind/orchestration/v1/orchestration_service_pb2_grpc.py` | Generated `LabsTailwindOrchestrationServiceStub` exposing the cumulative exact unary and unary-stream methods. |
| `_android/proto/google/internal/labs/tailwind/orchestration/v1/sources_pb2.py` | Source-operation and `UploadFileRequest` descriptors plus the explicitly web-derived `MutateSource` request/response wrapper. |
| `_android/proto/google/internal/labs/tailwind/orchestration/v1/artifacts_pb2.py` | Artifact request/response and projection overlay, including the explicitly web-derived report-suggestion closure. |
| `_android/proto/google/internal/labs/tailwind/orchestration/v1/chat_pb2.py` | Service-free exact-package chat overlay for sessions, turns, delete, streamed answers, and the proven citation/document closure. |
| `_android/proto/google/internal/labs/tailwind/orchestration/v1/chat_pb2_grpc.py` | Deterministic generated companion for the service-free chat overlay. |
| `_android/proto/google/internal/labs/tailwind/orchestration/v1/notes_pb2.py` | Service-free exact-package note CRUD overlay. |
| `_android/proto/google/internal/labs/tailwind/orchestration/v1/notes_pb2_grpc.py` | Deterministic generated companion for the note overlay. |
| `_android/proto/google/internal/labs/tailwind/orchestration/v1/organization_pb2.py` | Exact-package organization `GetLabels` closure plus explicitly web-derived manual organization-write signatures and partial response parsers. |
| `_android/proto/google/internal/labs/tailwind/orchestration/v1/organization_pb2_grpc.py` | Deterministic service-free companion for the exact organization message overlay. |
| `_android/proto/google/internal/labs/tailwind/orchestration/v1/research_pb2.py` | Exact-package Research request/response, job/result, and enum descriptors. |
| `_android/proto/google/internal/labs/tailwind/orchestration/v1/research_pb2_grpc.py` | Deterministic service-free companion for the Research message overlay. |
| `_android/proto/labs/language/tailwind/common/protos/common_pb2.py` | Exact-package `ChatSession` and `ProjectPublicSettings` closure shared by chat and sharing without duplicate declarations. |
| `_android/proto/labs/language/tailwind/common/protos/common_pb2_grpc.py` | Deterministic service-free companion for the exact common closure. |
| `_android/proto/labs/language/tailwind/sharing/sharing_pb2.py` | Exact-package sharing requests/responses plus the separately proven two-method sharing service descriptor. |
| `_android/proto/labs/language/tailwind/sharing/sharing_pb2_grpc.py` | Generated `LabsTailwindSharingServiceStub` exposing exact `GetProjectDetails` and `ShareProject`. |
| `_android/proto/notebooklm/android/wire/v1/sharing_pb2.py` | Repository-local sharing response parser preserving scalar presence for exact `GetProjectDetails`; `ShareProject` uses the exact request type. |
| `_android/proto/notebooklm/android/wire/v1/sharing_pb2_grpc.py` | Deterministic service-free companion for the local sharing overlay. |
| `_android/proto/notebooklm/android/wire/v1/organization_mutations_pb2.py` | Repository-local organization heterogeneous `GetLabels` read decoder; manual writes use the generated web-derived organization messages. |
| `_android/proto/notebooklm/android/wire/v1/organization_mutations_pb2_grpc.py` | Deterministic service-free companion for the organization local organization overlay. |
| `_android/proto/google/internal/labs/tailwind/v1/source_settings_pb2.py` | Exact-package `SourceSettings`, `SourceStatus`, and `UserDriveSourceStatus` descriptors. |
| `_android/proto/google/internal/labs/tailwind/v1/source_settings_pb2_grpc.py` | Generated companion for the service-free SourceSettings proto; retained so the generated tree exactly matches the pinned command. |
| `_android/proto/notebooklm/internal/android/wire/v1/notebooks_pb2.py` | Repository-local notebook parser overrides for live-only emoji and guide-topic fields. |
| `_android/proto/notebooklm/internal/android/wire/v1/notebooks_pb2_grpc.py` | Deterministic service-free companion for the notebook local wire overlay. |
| `_android/proto/notebooklm/internal/android/wire/v1/artifacts_pb2.py` | Repository-local artifact table/audio/infographic wire overlay for evidence-backed fields absent from the inspected APK descriptor. |
| `_android/proto/notebooklm/internal/android/wire/v1/artifacts_pb2_grpc.py` | Deterministic service-free companion for the artifact local artifact wire overlay. |
| `_android/proto/notebooklm/internal/android/wire/v1/source_content_pb2.py` | Repository-local `LoadSource` response overlay admitting the live exact `TailwindDoc #4` branch without a Google-package import cycle. |
| `_android/proto/notebooklm/internal/android/wire/v1/source_content_pb2_grpc.py` | Deterministic service-free companion for the local source-content wire overlay. |
| `_android/proto_src/` | Minimal compile-ready cumulative Android `.proto` closure. The evidence ledger is `docs/android/proto-evidence-ledger.md`; flattened `docs/android/schema.proto` is never a compile input. |
| `_runtime/init.py` | Backend-neutral constructor validation and `SharedRuntime` construction. |
| `_web/transport/init.py` | Web runtime construction, middleware wiring, and `WebRuntime` assembly. |
| `_web/transport/kernel.py` | Concrete `Kernel` transport core (owns `httpx.AsyncClient` + cookie jar) |
| `_runtime/config.py` | `DEFAULT_*` knobs and module-level constants. `CORE_LOGGER_NAME = "notebooklm._core"` is intentionally preserved as a compatibility logging contract even though the `_core` module was deleted; renaming it silently breaks downstream `caplog`/logger filters. |
| `_runtime/call_supervisor.py` | `CallSupervisor`, `CallLease`, and `OperationLease` — shared logical-call policy and generation-isolated admission. |
| `_env.py`, `config.py` | Runtime environment defaults and the public config re-export surface |
| `_logging.py`, `log.py` | Redaction/correlation logging internals and the public logging helper surface |
| `_secrets.py` | Canonical runtime registry of must-scrub bare session-cookie names (`RUNTIME_SESSION_COOKIES`), `__Secure-*` / `__Host-*` prefix umbrellas (`SECURE_HOST_UMBRELLA_PATTERNS`, fail-closed for future names), and carrier-agnostic Google credential shapes (`AUTH_TOKEN_SHAPE_PATTERNS` — `aas_et/` / `g.a000-` / `sidts-` / `ya29.` tokens + the `AIza…` API key) that `_logging.py` redaction and `exceptions.py` scrubbing DERIVE from. Runtime code cannot import from `tests/`, so this restates the cassette sanitizer's must-scrub shapes; `tests/_guardrails/test_runtime_secret_registry_parity.py` asserts lockstep with `tests/cassette_patterns.py` on every axis — bare-cookie superset, umbrella coverage, and regex-string shape equality (issues #1517/#1518). |
| `_callbacks.py` | Sync-or-async callback invocation helper used by telemetry/retry hooks |
| `_lookup.py` | `unwrap_or_raise(obj, exc)` — the shared single-row-lookup helper backing the public `get`/`get_or_none` pair (ADR-0019 Enforcement tier-2). The four `sources`/`artifacts`/`notes`/`mind_maps` `get()` methods call it directly to raise their `*NotFoundError` on a miss (the v0.8.0 flip, issue #1247); `notebooks.get()` already raised on its own path and does not route through it. |
| `_loop_bound.py` | `LoopBoundPrimitive` — template-method base for the loop-affinity `set_bound_loop` protocol. `EpochFenced` extends it with the shared `activate(epoch)`, `fence()`, and `assert_epoch(expected)` resource-generation state used by both backends. Owners supply stable diagnostic text and choose the standard epoch-detail suffix or an exact fixed message; clear-on-rebind owners still override `_on_loop_rebind` for cached loop-bound primitives. |
| `_deprecation.py` | Deprecation helper, gated by `NOTEBOOKLM_QUIET_DEPRECATIONS`. The immutable `DEPRECATION_SPECS` table owns three auth-storage and two backend-specific root-`rpc_call` messages, replacements, since/removal versions, categories, and public-boundary stacklevels; `warn_registered_deprecation` emits them through `warn_deprecated`. `scripts/check_deprecation_targets.py` parses the table and callsites without importing application code and fails closed on malformed, missing, stale, lapsed, or structurally unresolved entries. Unrelated one-off deprecations continue to use `warn_deprecated`; `deprecations_quiet` / `_deprecations_quiet` / `_QUIET_ENV_VAR` retain the live suppression gate. ADR-0018 forbids inline `warnings.warn(..., DeprecationWarning)` outside this module — `tests/_guardrails/test_no_inline_deprecation_warnings.py` enforces it (only for `DeprecationWarning`; inline `RuntimeWarning`/`UserWarning` remains allowed). The permanent `save_cookies_to_storage(original_snapshot=None)` race advisory is therefore still an ungated `RuntimeWarning`. See `docs/deprecations.md`. |
| `_runtime/helpers.py` | `is_auth_error`, `AUTH_ERROR_PATTERNS`, `_resolve_keepalive_interval` |
| `_web/transport/error_injection.py` | Synthetic-error env-var resolver + startup guard |
| `_client_metrics.py` | `ClientMetrics` — `ClientMetricsSnapshot` counters + `on_rpc_event` callback |
| `_deadline.py` | `RuntimeDeadline` plus `from_timeout(...)` and `await_with_deadline(...)`, shared by retry, polling, Android RPC, and upload paths so aggregate timeouts clamp consistently |
| `_backoff.py` | Shared capped exponential-backoff calculation with deterministic test injection |
| `_web/transport/reqid_counter.py` | `ReqidCounter` — monotonic `_reqid` for the chat backend |
| `_web/transport/auth.py` | `AuthRefreshCoordinator` — refresh task + auth-snapshot lock |
| `_runtime/auth_refresh_retry.py` | Shared auth refresh-and-retry core for the web HTTP-status/decoded-RPC layers and Android gRPC: the once-per-logical-call `RefreshBudget` token and common `refresh_and_count` body (log/refresh/sleep/`rpc_auth_retries` metric). The old `_web/transport/auth_refresh_retry.py` path is a one-release compatibility re-export. |
| `_runtime/lifecycle.py` | `ClientLifecycle` — protocol-neutral transactional root lifecycle and phased transport orchestration |
| `_web/transport/lifecycle.py` | `WebTransportLifecycle` — Kernel/auth epoch fencing, keepalive, cookie persistence, and web resource teardown |
| `_web/transport/runtime.py` | `RuntimeTransport` — authed-POST transport wrapper that drives the middleware chain and typed transport response handling |
| `_web/transport/executor.py` | RPC dispatch executor. Takes its `Kernel`, `RuntimeTransport`, `AuthRefreshCoordinator`, and `ClientMetrics` collaborators directly via keyword-only constructor parameters (ADR-0014 Rule 5). Defines a single local `DecodeResponse` Protocol. |
| `_web/transport/request_types.py` | Shared authed POST request construction types: `AuthSnapshot`, `BuildRequest`, `PostBody`, and materialization helpers. |
| `_web/transport/errors.py` | Transport exceptions, `Retry-After` parsing, and terminal `Kernel.post` error mapping for retry/auth middleware. |
| `_web/transport/streaming_post.py` | Size-capped streaming POST helper used by `Kernel.post`. |
| `_web/transport/middleware/core.py` | HTTP-shaped middleware request/response envelope, chain composition, and middleware Protocol |
| `_web/transport/middleware/context.py` | Canonical per-request context-key vocabulary for middleware |
| `_web/transport/middleware/chain_host.py` | Mutable owner for the live middleware chain slots and retry-budget tunables |
| `_conversation_cache.py` | Per-instance true-LRU conversation cache for `ChatAPI` (caps conversation count via `MAX_CONVERSATION_CACHE_SIZE` and per-conversation turns via `MAX_TURNS_PER_CONVERSATION`) |
| `_polling_registry.py` | Pending-poll registry for long-running artifact generations |
| `_web/transport/cookie_persistence.py` | Cookie-jar persistence + `__Secure-1PSIDTS` rotation |
| `_runtime/contracts.py` | Transport-neutral `LoopGuard` Protocol |
| `_web/contracts.py` | Web-only `Kernel` and `RpcCaller` Protocols |
| `_idempotency.py` | Transport-neutral probe-then-retry workflow and unconfirmed-write marker; imports neither `_web` nor `rpc` |
| `_web/policy.py` | Web RPC idempotency types, declarative per-RPC classifications, resolution, and the one production `IDEMPOTENCY_REGISTRY` seed. Holds the load-bearing two-pass order (pre-seed `register()` → `_seed_defaults()` → post-seed `register()` + the read/set-op loop). |
| `_atomic_io.py`, `io.py` | Atomic JSON write/update internals and public I/O re-export surface for CLI boundary compliance |
| `exceptions.py` | Public exception hierarchy plus safe diagnostic preview/redaction helpers |
| `paths.py`, `migration.py` | Profile-aware path resolution and locked migration from the legacy flat layout |
| `_types/`, `types.py` | Dataclass implementation package and public type/re-export facade |
| `_types/documents.py` | `StructuredDocument` / `DocumentBlock` / `TextSpan` / `TableCell` / `DocumentAnnotation` / `BlockKind` / `BlockStyle` / `ListStyle` / `ListInfo` — the transport-neutral parsed-document types behind `SourceFulltext.document` and `AskResult.answer_document`, carrying the character offsets citations anchor to (#2128, #2120). `StructuredDocument.render()` derives the readable flat rendering (`SourceFulltext.rendered_content`) from the same tree, and is what `utils.resolve_chat_reference_passage` returns once it has resolved a citation by offset (#2211); `DocumentBlock.table_rows` carries the table cell ranges that rendering separates on, as offsets, so the coordinate space is untouched (#2230) |
| `_types/enums.py` | Canonical definitions of the 26 transport-neutral domain enums and their value helpers; `types.py` and `rpc/types.py` preserve the existing public and compatibility identities by re-exporting these same objects. |
| `_types/labels.py` | `Label` pure-value type (source-label topic grouping; `source_ids` only, no artifact members) re-exported by `types.py` |
| `_types/collections.py` | `Collection` pure-value type (account-level notebook grouping; `notebook_ids`, no notebook parent) re-exported by `types.py`; its public factory lazily delegates strict web-row decoding to `_web/rows/collections.py` |
| `_web/rows/artifacts.py` | `ArtifactRow` typed view over raw positional artifact RPC rows, plus `ReportSuggestionRow` over `GET_SUGGESTED_REPORTS` rows |
| `_web/rows/chat.py` | Shared Web chat row adapters (`AnswerRow` / `CitationRow` / `CitationDetail` / `ConversationTurnRow` / `SavedChatNoteRow` / `StreamFrameRow` / `ErrorPayloadRow`). `AnswerRow.document` and `CitationDetail.fragment_elements` delegate the document tree to `_web/rows/documents.py` (#2120) |
| `_web/rows/chat_stream.py` | Streamed-chat envelope parsing, answer/citation extraction, error-frame rejection, and UUID helpers over the typed chat/document rows |
| `_web/rows/chunks.py` | Strict `RetrieveRelevantChunks` source-group/chunk row views and `RelevantChunk` projection |
| `_web/rows/collections.py` | Strict collection-tuple decoding behind `Collection.from_api_response`'s lazy shim |
| `_web/rows/customization.py` | `GetArtifactCustomizationChoices` row views (`CustomizationChoicesRow` / `CustomizationChoiceRow` / `ReportPresetRow`) — the four Studio option families behind `ArtifactCustomizationChoices` |
| `_web/rows/documents.py` | `TailwindDoc` tree adapters (`DocumentBodyRow` / `StructuralElementRow` / `ParagraphRow` / `ParagraphElementRow` / `TextRunRow` / `TableRow` / `BulletInfoRow` / `AnnotationEntryRow`) plus the `build_document` / `build_blocks` builders. One decoder for all three carriers of the tree — source fulltext, chat-answer `responseDoc`, and a citation's `TailwindDocFragment` — so citation offsets on both sides share a coordinate space (#2128, #2120) |
| `_web/rows/labels.py` | `LabelRow` strict typed view over the raw positional label tuple `[name, sources, id, emoji]` (fails loud on schema drift) |
| `_web/rows/notebooks.py` | Notebook `Project` decoding behind `Notebook.from_api_response`'s lazy shim, plus the `SUGGEST_PROMPTS` suggestion-row view |
| `_web/rows/notes.py` | `NoteRow` typed view over raw positional note and mind-map RPC rows |
| `_web/rows/research.py` | `ResearchTaskRow` / `ResearchTaskInfoRow` / `ResearchResultRow` typed views over raw positional `POLL_RESEARCH` rows that centralise the single-level positions `_web/rows/research_task.py` used to open-code (#1501) |
| `_web/rows/research_task.py` | Internal parser for research task result-type selection |
| `_web/rows/sharing.py` | `SharedUserRow` / `ShareStatusRow` decoding behind the public sharing models' lazy shims |
| `_web/rows/sources.py` | `SourceRow` / `SourceRowShape` typed views over raw positional source RPC rows |
| `_web/rows/transfers.py` | Mapping-row views for the #2283 transfer replies (`CopiedSourceRow` / `CopiedArtifactRow` / `AddSourcesAsyncResponseRow` / `SourceAckRow`) plus the shared `unwrap_mapping_rows` envelope probe |
| `_web/notebooks.py` | `WebNotebooksAPI`, the concrete `batchexecute` notebook backend; implements the shared create/copy hooks, preserves the executor identity and web-only decoding/quota/session-hint behavior, and owns the direct-construction `SourceLister` fallback |
| `_web/sources/` | `WebSourcesAPI` and the concrete web source services: add/batch orchestration, source listing/content/search decoding, Drive import, and the resumable upload pipeline |
| `_web/artifacts.py` | `WebArtifactsAPI`, the concrete `batchexecute` artifact backend; owns web listing, mutation, generation/copy/export hooks, raw selection, customization reads, and suggestion operations |
| `_web/artifact/` | Web artifact services for listing, generation dispatch, raw download selection, and positional data-table decoding |
| `_web/chat.py` | `WebChatAPI`, the concrete streamed-query and `batchexecute` chat backend; owns request IDs, streamed transport, positional history/turn decoding, chat RPCs, and saved-chat note persistence |
| `_web/mind_maps.py` | `WebMindMapsAPI` plus `NoteBackedMindMapService`, the concrete `batchexecute` mind-map backend and shared web note-row adapter |
| `_web/notes.py` | `WebNotesAPI` plus `NoteService`, the concrete `batchexecute` notes backend and shared note-row primitives |
| `_web/note_tasks.py` | Lifecycle-aware registry for shielded web note finalize/cleanup tasks; graceful drain settles admitted work while forced close cancels and gathers it. |
| `_web/settings.py` | `WebSettingsAPI` plus account-setting request/response helpers |
| `_web/sharing.py` | `WebSharingAPI` plus the legacy `SHARE_ARTIFACT` `ShareManager` |
| `_web/params/` | Web `batchexecute` positional request payload builders, separated from backend-neutral namespace APIs |
| `_web/params/notebooks.py` | Stable `batchexecute` notebook RPC request payload builders, including `SUGGEST_PROMPTS` |
| `_web/params/chat_stream.py` | Streamed-chat URL, form-body, and source/history request construction |
| `_web/params/chat_note.py` | Saved-from-chat `CREATE_NOTE` positional payload and citation-anchor encoding |
| `_web/params/chat_session.py` | Chat session-status and generation-cancel positional payload builders (#2303) |
| `_web/transport/chat.py` | Chat-specific HTTP/error mapping over the shared authenticated streaming transport |
| `_web/wire/decoder.py` | Batchexecute response framing, status/error decoding, and process-wide byte-count drift telemetry; retains the established `notebooklm.rpc.decoder` logger category |
| `_web/wire/encoder.py` | Batchexecute request envelope and form-body encoding helpers; retains the established `notebooklm.rpc.encoder` logger category |
| `_web/wire/overrides.py` | Environment-driven runtime RPC-ID override policy, including the single cached parser and INFO-log deduplication state re-exported through `rpc/types.py` and `notebooklm.rpc` |
| `_web/wire/safe_index.py` | Strict bounds-checked positional access for decoded web payloads, compatibility-re-exported as `notebooklm.rpc.safe_index` |
| `artifacts.py`, `research.py`, `utils.py` | Public helper modules for artifact retry, research citation/report utilities, and common async helpers |
| `_notebooks.py` | Backend-neutral abstract `NotebooksAPI`; owns shared create idempotency, copy validation/ambiguity policy, lookup/update conveniences, metadata composition, and share-URL semantics. Copy ambiguity uses a typed backend policy: Web explicitly chains the original transient error, while Android suppresses the capability-bearing cause. |
| `_sources.py` | Backend-neutral abstract `SourcesAPI`; owns source identity lookup, search validation/global ranking, the `add_urls_async` / `append_text` / `copy` transfer workflows, file-upload title normalization/finalization over `_send_upload`, and the four polling workflows over neutral `SourcePoller` |
| `_artifacts.py` | Backend-neutral abstract `ArtifactsAPI`; owns artifact generation orchestration, copy/export workflows, decoded polling, family lists, lookup, customization-choice delegation, neutral formatting, and asset transfer over protected backend hooks |
| `_chat.py` | Backend-neutral abstract `ChatAPI`; owns locks, cache, deleted-conversation tracking, ID recovery, authoritative turn counting, modes, configure/settings result policy, and shared ask/delete/save-note orchestration over protected typed backend hooks |
| `_research.py` | Backend-neutral abstract `BaseResearchAPI`; owns polling helpers, raw-import materialization/provenance/classification over one `_send_import` hook, and the Android-safe import-verification workflow over abstract backend reads/mutations |
| `_research_import.py` | Backend-neutral research import classification and reconciliation helpers: immutable backend policy plus typed item/batch carriers, typed input coercion, structured-URL normalization, provenance checks, imported-result carriers, idempotency partitioning, probe outcomes, and read-timeout resolution |
| `_web/research.py` | Web implementation of `client.research`; keeps Web import encoding/decoding behind `_send_import` and its distinct established import-verification policy; shared classification preserves the historical `notebooklm._research` logger key |
| `_notes.py` | Backend-neutral abstract `NotesAPI` contract |
| `_sharing.py` | Backend-neutral abstract `SharingAPI`; owns the shared `add_user` / `update_user` workflows |
| `_labels.py`, `_collections.py` | Backend-neutral abstract `LabelsAPI` / `CollectionsAPI` contracts and their narrow membership-join callable types |
| `_web/labels.py` | Concrete `WebLabelsAPI` implementation; keeps the historical `notebooklm._labels` logger key |
| `_web/collections.py` | Concrete `WebCollectionsAPI` implementation over type-3 label RPCs; keeps the historical `notebooklm._collections` logger key |
| `_settings.py` | Backend-neutral abstract `SettingsAPI` contract |
| `_mind_maps_api.py` | Backend-neutral abstract `MindMapsAPI`; owns unified lookup/list/generate/get-tree/rename/delete composition over base-typed `ArtifactsAPI` and `NotesAPI` dependencies. `list_note_backed` and narrow typed read/write hooks remain frontend-specific (#1256). |
| `_artifact/downloads.py` | Backend-neutral asset transfer service: guarded streaming, rejection, staging, and atomic publication |
| `_artifact/_guarded_transfer.py` | Backend-neutral representation transfer loop: explicit redirect cap, application redirects, content/signature/byte limits, and atomic publication |
| `_artifact/_redirect_guard.py` | Per-redirect-hop host/scheme revalidation and credential-policy application for downloads — rejects off-allowlist / non-HTTPS redirect targets before the request is sent (#1521) |
| `_artifact/_download_client.py` | Download trusted-host allowlist + transport-aware client factory — wires redirect validation and per-hop credentials for httpx or opt-in curl_cffi |
| `_artifact/formatters.py` | Markdown, HTML, and plain text formatters for artifacts |
| `_artifact/validation.py` | Input-validation guards for the `ArtifactsAPI` facade (`generate_report` format coercion, `export` exactly-one-of target), kept in a sibling module so the facade stays under the module-size ratchet (#1874) |
| `_artifact/polling.py` | Backend-neutral poll coordination over a target-aware decoded studio projection |
| `_web/artifact/downloads.py` | Web artifact selection, representation lookup, and raw content parsing |
| `_web/artifact/generation.py` | Web generation RPC dispatch service |
| `_web/artifact/listing.py` | Web listing, row decoding, and mind-map composition |
| `_web/artifact/table.py` | Web positional data-table row extraction |
| `_web/params/artifacts.py` | Stable web artifact RPC request payload builders |
| `_web/sources/add.py` | Core service layer for adding text, URL, or Google Drive sources |
| `_web/sources/batch.py` | True-batch URL `ADD_SOURCE` service for the existing MCP/REST batch endpoints: typed positional outcomes, omitted-row reconciliation, and fail-closed transport/duplicate ambiguity policy |
| `_web/sources/transfers.py` | `SourceTransferService`: `AddSourcesAsync` (non-blocking batch add), `AppendSource` (in-place text append) and `CopySourcesAsync` (cross-notebook copy) — unconfirmed-on-transport-loss writes behind `WebSourcesAPI.add_urls_async` / `append_text` / `copy` (#2283) |
| `_web/sources/drive_import.py` | Auto-route add-from-Drive (#1884): download + upload the upload-only Drive types (epub/docx/txt/…); native import (`add_drive`) instead takes Docs/Slides/Sheets + PDF by reference; header-first cookie-authed streaming fetch behind injected seams |
| `_web/sources/content.py` | Core service layer for fetching source HTML/markdown content |
| `_source/markdown.py` | Source fulltext HTML-to-Markdown conversion policy, including Markdown-source and LaTeX/table handling |
| `_web/sources/listing.py` | Core service layer for listing notebook sources |
| `_web/sources/search.py` | Replay-safe `ASU5Oe` dispatch for ranked source-passage search |
| `_source/polling.py` | Poll coordination service for active source conversions |
| `_web/sources/upload.py` | Concurrency-gated upload pipeline for source files |
| `_web/sources/_upload_decode.py` | Pure decode/validation helpers for the upload pipeline (URL redaction, ADD_SOURCE_FILE source-id extraction, content-type policy), extracted from `upload.py` |
| `_web/params/sources.py` | Stable source upload registration, rename, and resumable-upload request builders |
| `_web/params/labels.py` | Stable CREATE_LABEL / LIST_LABELS / UPDATE_LABEL / DELETE_LABEL request payload builders (with the shared `_opts()` request-options wrapper) |
| `_web/params/collections.py` | Collection request payload builders reusing the label RPCs — null notebook_id, type-3 discriminator, `[1,3]` opts tail; add/remove notebook-membership fieldmask and `create`'s options wrapper are live-captured (PR #2009) |
| `_notebook_metadata.py` | Transport-neutral notebook metadata protocols and composition service; no concrete RPC/source-listing dependency |
| `_url_utils.py`, `urls.py` | URL parsing/validation internals and the public URL helper facade |
| `_version_check.py` | Dynamic client-side version deprecation guard |
| `_version_info.py` | Human-facing `version_string()` — package version + short git commit (embedded by `hatch_build.py` at build time, or live `git` from a checkout) |
| `_chat.py` | Abstract `ChatAPI`, shared chat orchestration, and its bounded recently-deleted-conversation tracker; `delete_conversation` records the id under the conversation lock so a concurrent null-conversation ask can recover the server's real post-POST conversation id (#1875) |
| `_web/transport/middleware/chain.py` | Constructs the middleware chain in the canonical ADR-0009 order |
| `_web/transport/middleware/*.py` | Production web middlewares (`retry`, `auth`, `error_injection`, `tracing`) plus retained historical drain/metrics/semaphore modules that are no longer wired after the supervisor migration |
| `rpc/types.py` | RPC method IDs (source of truth) |
| `auth.py` | Authentication facade — primarily eager `_auth/*` aliases, plus a deliberately small lazy capability boundary. `enumerate_accounts` retains its bound `_poke_session` dependency; the browser helpers lazily import `_browser` only when invoked, and module initialization installs `_default_headless_rung` into `_auth.recovery_rungs` without importing the browser implementation. Base imports therefore remain browser-free. Policy/storage aliases remain identity-equal to their owners, including `filter_storage_state_cookies_by_domain_policy` and `app_host_scope_note`. These internal-ledger names are intentionally absent from public `__all__`. |
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
| `_auth/mint_service.py` | Stateless typed OAuth/master-token exchange and unchanged web cookie mint + sole raw RotateCookies wire; no disk or policy imports (ADR-0023/0034) |
| `_auth/master_token_bootstrap.py` | One-store bootstrap/re-mint/missing-storage coordinator; owns ordering, outcomes, and cancellation settlement without runtime/client or arbitrary token-file capability (ADR-0034) |
| `_auth/profile_store.py` | Path-owned fresh document/session/account/derived-token reads, token/account writes, blocking cookie transactions, and bounded remint/login/minted replacement; minted owns latest-owner/filter/rebind/commit (ADR-0034) |
| `_auth/profile_migration.py` | Legacy account context, lossless two-read resolution, promotion, retryable single-flight scheduler/exit drain, and login/account reconciliation (ADR-0034) |
| `_auth/account_email.py` | Generation-safe persisted/live account-email matching, probing, cache results, and exact-document CAS self-heal |
| `_browser/browser_capture.py` | One deep browser launch→capture→filter→heal→persist implementation, lazy Playwright; depends downward on typed `_auth` storage/policy owners |
| `_browser/navigation_errors.py` | Pure `net::ERR_*` and navigation-race/failure classification leaf |
| `_browser/browser_launch_errors.py` | Pure browser-channel registry and launch-failure triage leaf |
| `_browser/headless_reauth.py` | Opt-in layer-3 browser recovery implementation behind `_auth.recovery_rungs` |
| `_browser/oauth_token.py` | Visible EmbeddedSetup OAuth-cookie capture implementation |
| `_auth/recovery_rungs.py` | Neutral process registry + closed outcome for the optional blocking L3 implementation; keeps recovery independent of browser code |
| `cli/label_cmd.py` | `label` command group (list/sources/generate/create/rename/emoji/add/remove/delete); thin Click shells over `client.labels`, `_app.labels`, and the label-listing service (ADR-0008/0021) |
| `cli/collection_cmd.py` | `collection` command group (list/notebooks/create/rename/add/remove/delete); account-level (no `--notebook` option); thin Click shells over `client.collections` + `_app.collections` (ADR-0008/0021) |
| `cli/services/label_listing.py` | `label` CLI service: the `label list` members→source-titles join (`execute_label_list`/`LabelListPlan`). Re-exports `resolve_label_id` + `LabelResolutionError` from `_app/labels.py` (the composite `<id\|name>` resolver moved to the neutral layer; the re-export keeps `from .services.label_listing import resolve_label_id` resolving for the command layer + tests) |

### Repository Structure

```text
src/notebooklm/
├── __init__.py                  # Public exports
├── __main__.py                  # `python -m notebooklm` entry point
├── client.py                    # NotebookLMClient
├── auth.py                      # Authentication facade — eager auth aliases + narrow lazy browser capabilities (see file table above)
├── types.py                     # Dataclasses
├── artifacts.py                 # Public artifact-generation retry helpers
├── config.py                    # Public config facade over _env
├── exceptions.py                # Public exception hierarchy
├── io.py                        # Public atomic-I/O facade for CLI boundary compliance
├── log.py                       # Public logging helper facade
├── migration.py                 # Legacy flat-layout to profile migration
├── paths.py                     # Profile-aware path resolution
├── research.py                  # Public research citation/report helpers
├── raw.py                       # Public backend-selected raw wire APIs and gRPC descriptors
├── urls.py                      # Public URL helper facade
├── utils.py                     # Public async utility helpers
├── _atomic_io.py                # Atomic JSON write/update helpers
├── _backoff.py                  # Shared retry backoff calculation
├── _callbacks.py                # Sync/async callback invocation helper
├── _client_assembly.py          # Shared client-assembly seam (constructor + test factory)
├── _deadline.py                 # RuntimeDeadline helper for aggregate timeouts
├── _deprecation.py              # Immutable auth/raw-call specs + gated deprecation emitters
├── _env.py                      # Runtime environment/default endpoint helpers
├── _idempotency.py              # Transport-neutral create probe/retry helpers
├── _logging.py                  # Redaction + correlation logging internals
├── _secrets.py                  # Canonical runtime secret registry (cookie names + secure/host umbrellas + token/API-key shapes) the redaction patterns derive from
├── _lookup.py                   # unwrap_or_raise — shared single-row-lookup helper for get/get_or_none
├── _serving.py                  # Shared bootstrap for both HTTP entry points: single-source loopback classification (IPv4-mapped-IPv6-aware) + non-loopback bind guard (mcp/server __main__ + server/_auth all route through it)
├── _loop_affinity.py            # Event-loop affinity guard helper (assert_bound_loop free function)
├── _loop_bound.py               # LoopBoundPrimitive + EpochFenced runtime bases
├── _curl_cffi_transport.py      # Opt-in curl_cffi browser-impersonation transport (NOTEBOOKLM_TRANSPORT=curl_cffi)
├── _hop_credentials.py         # Typed per-hop cookie/header credentials for guarded asset requests
├── _client_metrics.py           # Telemetry / metrics seam
├── _conversation_cache.py       # Per-instance true-LRU conversation cache (bounded conversation count + per-conversation turns)
├── _polling_registry.py         # Artifact polling helpers
├── _mind_maps_api.py            # Backend-neutral abstract MindMapsAPI
├── _notebook_metadata.py        # Neutral metadata protocols + composition service
├── _url_utils.py                # URL validation helpers
├── _version_check.py            # Deprecation version guard
├── _version_info.py             # version_string(): version + short git commit
├── _redact.py                   # Transport-neutral secret/home-path/file-link scrubber (redact(msg, max_length)); shared chokepoint under both mcp/_errors.py and server/_errors.py
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
│   ├── login_browser.py         # Markup-free browser-login plan/orchestration: conflict validation, path preparation, event stream, capture ordering, and account repair over injected public-auth capabilities
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
│   ├── source_play_books.py     # Click-free backend-neutral Google Play Books core (#2292/#2302): fetch_play_books + execute_source_add_play_book over client.sources.list_play_books/add_play_book
│   ├── source_research.py       # Click-free `source add-research` start/wait/import workflow + validate_add_research_flags (importer injected; SourceAddResearchPlan/Result)
│   ├── source_wait.py           # Click-free `source wait` readiness-poll core: execute_source_wait + typed SourceWaitOutcome (wait_context injected) + wait_all_sources (single-snapshot loop via client.sources.wait_all_until_ready — one notebook poll per tick, order-preserving; #1870) shared by the MCP tool + REST route (#1871) + the MAX_WAIT_TIMEOUT / MAX_WAIT_SOURCE_IDS caps
│   └── views.py                 # Transport-neutral output-projection views: share_status_view (access/permission/view_level enum→label), source_view (kind/status_label/drive_status_label + is_drive_degraded added), notebook_view (role_label added), notebook_viewed_keys (last_viewed_at + its deprecated modified_at alias, for hand-built CLI JSON envelopes), ask_result_view (raw_response debug blob stripped); shared by the MCP tools + REST routes so both emit the identical enriched shape (Option B)
├── _android/                    # Android backend; all 11 public namespace adapters selected together
│   ├── __init__.py              # Dependency-free package marker
│   ├── auth.py                  # Epoch-aware short-lived bearer provider
│   ├── account.py               # Private non-replayed account bootstrap adapter
│   ├── phenotype.py             # Headless GMS Phenotype token acquisition for Play Books
│   ├── play_books.py            # Play Books wire codecs and guarded retry helpers
│   ├── runtime.py               # AndroidRuntime owned-collaborator bundle
│   ├── codecs/                  # Android protobuf projections
│   │   ├── __init__.py          # Codec package marker
│   │   ├── account.py           # Strict frozen account projection
│   │   ├── chat.py              # Chat history/citation projection
│   │   ├── documents.py         # Shared TailwindDoc decoder + text renderers
│   │   ├── notes.py             # Note request builders and projection
│   │   ├── sharing.py           # Public-link sharing projection
│   │   ├── notebooks.py         # Project and notebook-guide decoding
│   │   ├── sources.py           # Source projection and enum mapping
│   │   ├── artifacts.py         # Ledgered artifact/representation projection
│   │   ├── organization.py      # Heterogeneous organization member decoding
│   │   └── research.py          # Strict Research job/result projection
│   ├── errors.py                # Sanitized gRPC status/error mapping
│   ├── notebooks.py             # Selected Android notebook reads/mutations, incl. native recent-removal
│   ├── session.py               # Supervised lazy gRPC transport
│   ├── epoch.py                 # Session-tagged task-local workflow epoch propagation
│   ├── retry_policy.py          # Web-registry-derived Android replay-safety manifest
│   ├── sources.py               # Selected source surface (fully native)
│   ├── source_search.py         # Native RetrieveRelevantChunks search service
│   ├── upload.py                # Epoch-fenced generic Android Scotty transaction
│   ├── drive_staging.py         # Drive round-trip for types the mobile upload frontend rejects
│   ├── evidence.py              # Pinned captured app/UA evidence profile
│   ├── artifacts.py             # Publicly selected complete Artifact API
│   ├── artifact_creation.py      # Exact CreateArtifact option/request builders
│   ├── artifact_collaborators.py # Narrow note-backed compatibility protocols
│   ├── artifact_mutations.py     # Retry and Drive export mobile mutations
│   ├── artifact_note_mind_maps.py # Native note-backed map generation/persistence
│   ├── artifact_outputs.py       # Local output decoders/renderers + atomic publication
│   ├── artifact_reads.py         # Notebook-scoped exact reads and safe prefetch selection
│   ├── artifact_transfers.py     # CopyArtifactsAsync + customization-choice table mixin (#2283)
│   ├── artifact_proto.py         # Lazy artifact/read protobuf handles
│   ├── note_backed.py            # Typed note-backed map → aggregate artifact adapter
│   ├── assets.py                # Lifecycle-drained, bearer-safe typed asset transfer
│   ├── chat.py                  # Selected Android chat reads/delete/stream/settings
│   ├── source_transfers.py      # AddSourcesAsync / AppendSource / CopySourcesAsync mixin (#2283)
│   ├── notes.py                 # Selected 8-method Notes manifest + saved-response seam
│   ├── settings.py              # Native account settings/limits adapter
│   ├── sharing.py               # Native public-link/collaborator/view adapter
│   ├── mind_maps.py             # Public typed artifact/note composition
│   ├── organization.py          # Shared lazy-protobuf organization transport seam
│   ├── labels.py                # Selected native manual labels + AI-generation seam
│   ├── collections.py           # Complete collection adapter
│   ├── research.py              # Selected Web/Drive discovery lifecycle adapter
│   ├── proto_src/               # Exact-package reads + evidence-bounded local wire overlays
│   └── proto/                   # Checked-in generated pb2/pb2_grpc modules
│       ├── __init__.py          # Dependency-free generated-package marker
│       ├── google/internal/labs/tailwind/
│           ├── orchestration/v1/
│           │   ├── account_pb2.py           # Exact account messages/descriptors
│           │   ├── account_pb2_grpc.py      # Deterministic service-free companion
│           │   ├── read_pb2.py              # Project/source read messages and descriptors
│           │   ├── read_pb2_grpc.py         # Deterministic service-free companion
│           │   ├── notebooks_pb2.py         # Exact notebook messages/descriptors
│           │   ├── notebooks_pb2_grpc.py    # Deterministic service-free companion
│           │   ├── orchestration_service_pb2.py      # Cumulative exact service descriptor
│           │   ├── orchestration_service_pb2_grpc.py # 57-method generated stub
│           │   ├── sources_pb2.py               # Source and generic-upload descriptors
│           │   ├── sources_pb2_grpc.py          # Deterministic service-free companion
│           │   ├── artifacts_pb2.py         # Exact artifact message overlay
│           │   ├── artifacts_pb2_grpc.py    # Deterministic service-free companion
│           │   ├── chat_pb2.py              # Service-free chat messages/descriptors
│           │   ├── chat_pb2_grpc.py         # Deterministic service-free companion
│           │   ├── agency/
│           │   │   ├── supported_pb2.py      # Agency capability message closure
│           │   │   └── supported_pb2_grpc.py # Deterministic service-free companion
│           │   ├── notes_pb2.py             # Exact note CRUD overlay
│           │   ├── notes_pb2_grpc.py        # Deterministic service-free companion
│           │   ├── organization_pb2.py      # Exact GetLabels messages
│           │   ├── organization_pb2_grpc.py # Deterministic service-free companion
│           │   ├── research_pb2.py          # Exact Research messages/descriptors
│           │   └── research_pb2_grpc.py     # Deterministic service-free companion
│           └── v1/
│               ├── source_settings_pb2.py       # Source settings/status descriptors
│               └── source_settings_pb2_grpc.py  # Deterministic service-free companion
│       ├── notebooklm/android/wire/v1/
│           ├── sharing_pb2.py                    # Repository-local sharing wire messages
│           ├── sharing_pb2_grpc.py               # Deterministic service-free companion
│           ├── organization_mutations_pb2.py     # Repository-local organization wire
│           └── organization_mutations_pb2_grpc.py # Deterministic service-free companion
│       ├── notebooklm/experiments/v1/
│           ├── exptsandconfigs_pb2.py      # Minimal Phenotype/Heterodyne messages
│           └── exptsandconfigs_pb2_grpc.py # Deterministic service-free companion
│       ├── notebooklm/internal/android/wire/v1/
│           ├── notebooks_pb2.py       # Repository-local notebook wire messages
│           ├── notebooks_pb2_grpc.py  # Deterministic service-free companion
│           ├── artifacts_pb2.py       # Repository-local artifact wire messages
│           ├── artifacts_pb2_grpc.py  # Deterministic service-free companion
│           ├── source_content_pb2.py       # Local LoadSource TailwindDoc overlay
│           └── source_content_pb2_grpc.py  # Deterministic service-free companion
│       └── labs/language/tailwind/
│           ├── common/protos/
│           │   ├── common_pb2.py          # Shared chat/sharing exact common messages
│           │   ├── common_pb2_grpc.py     # Deterministic service-free companion
│           │   ├── metadata_pb2.py        # upload exact request-context messages
│           │   ├── metadata_pb2_grpc.py   # Deterministic service-free companion
│           │   ├── provenance_pb2.py      # upload exact provenance messages
│           │   └── provenance_pb2_grpc.py # Deterministic service-free companion
│           └── sharing/
│               ├── sharing_pb2.py         # Exact messages + two-method sharing service
│               └── sharing_pb2_grpc.py    # Exact two-method generated sharing stub
├── _web/                        # Private batchexecute web-backend implementation package
│   ├── __init__.py              # Package boundary
│   ├── contracts.py             # Web-only Kernel and RpcCaller Protocols
│   ├── raw.py                   # Thin raw-RPC adapter over the shared Web executor
│   ├── notebooks.py             # WebNotebooksAPI
│   ├── artifacts.py             # WebArtifactsAPI
│   ├── artifact/                # Web artifact services
│   │   ├── __init__.py
│   │   ├── downloads.py         # Raw selection and representation decoding
│   │   ├── generation.py        # Generation RPC dispatch
│   │   ├── listing.py           # Listing and mind-map composition
│   │   └── table.py             # Positional data-table decoding
│   ├── params/                  # Web batchexecute request builders
│       ├── __init__.py
│       ├── artifacts.py         # Artifact request payloads
│       └── notebooks.py         # Notebook request payloads
│   └── transport/               # Web HTTP transport core
│       ├── __init__.py
│       ├── auth.py              # AuthRefreshCoordinator
│       ├── auth_refresh_retry.py # One-release compatibility re-export
│       ├── chat.py              # Chat-specific error mapping
│       ├── composed.py          # ClientComposed web composition holder
│       ├── cookie_persistence.py # Cookie-jar persistence + __Secure-1PSIDTS rotation
│       ├── error_injection.py   # Synthetic-error env-var resolver + startup guard
│       ├── errors.py            # Transport exceptions and raw POST error mapping
│       ├── executor.py          # Batchexecute RPC dispatcher
│       ├── init.py              # WebRuntime construction + middleware wiring
│       ├── kernel.py            # Concrete Kernel transport core
│       ├── lifecycle.py         # Web resource open/prepare-close/close phases
│       ├── reqid_counter.py     # Chat request-id counter
│       ├── request_types.py     # AuthSnapshot, BuildRequest, and materializers
│       ├── runtime.py           # Middleware-chain transport wrapper
│       ├── sidecar.py           # Lazy deprecated-rpc_call Web compatibility owner
│       ├── seams.py             # Constructor-only injectable seams
│       ├── streaming_post.py    # Size-capped streaming POST helper
│       └── middleware/          # ADR-0009 middleware chain
│           ├── __init__.py
│           ├── auth_refresh.py
│           ├── chain.py
│           ├── chain_host.py
│           ├── context.py
│           ├── core.py
│           ├── error_injection.py
│           ├── retry.py
│           └── tracing.py
├── _runtime/                    # Client-runtime subpackage (promoted from flat _runtime_*.py, #1328)
│   ├── __init__.py              # Re-exports only transport-neutral runtime names
│   ├── auth_refresh_retry.py    # Shared refresh budget + retry body
│   ├── call_supervisor.py       # Shared call admission, metrics, semaphore, and generation leases
│   ├── config.py                # DEFAULT_* knobs + module-level constants
│   ├── contracts.py             # Transport-neutral LoopGuard Protocol
│   ├── helpers.py               # Auth classification, Google HTTP status, sleep, keepalive
│   ├── init.py                  # Runtime collaborator construction + validation
│   └── lifecycle.py             # Root lifecycle waves + phased transport orchestration
├── _source/                     # Neutral source services + lazy compatibility exports
│   ├── __init__.py              # Lazy package-level shims for moved web service names
│   ├── batch.py                 # Backend-neutral per-URL batch result record
│   ├── drive.py                 # Drive file-id/resource-key parsing and safe display labels
│   ├── markdown.py              # Source fulltext HTML-to-Markdown conversion policy
│   └── polling.py               # Source polling coordinator
├── _artifact/                   # Artifact-feature subpackage (promoted from flat _artifact_*.py, #1328)
│   ├── __init__.py              # Re-exports the cluster's public service classes/builders
│   ├── _download_client.py      # Download trusted-host allowlist + transport-aware client factory with per-hop credentials
│   ├── _guarded_transfer.py     # Neutral bounded representation transfer + redirect loop
│   ├── _redirect_guard.py       # Per-redirect-hop host/scheme and credential-policy enforcement (#1521)
│   ├── downloads.py             # Neutral guarded asset transfer and atomic publication
│   ├── formatters.py            # Artifact formatting helpers
│   ├── validation.py            # Facade input-validation guards (generate_report coercion, export exactly-one-of) (#1874)
│   └── polling.py               # Decoded backend-neutral artifact polling coordinator
├── _web/rows/                   # Positional-RPC-row adapters subpackage (#1328)
│   ├── __init__.py              # Re-exports the typed row views
│   ├── artifacts.py             # Artifact + GET_SUGGESTED_REPORTS row adapters (ArtifactRow / ReportSuggestionRow)
│   ├── chat.py                  # Streamed-chat row adapters (AnswerRow / CitationRow / CitationDetail / StreamFrameRow / ErrorPayloadRow) — closes the chat positional-decode perimeter (#1491); the document tree is delegated to documents.py (#2120)
│   ├── chat_stream.py           # Streamed-chat envelope, answer, citation, and error-frame parsing
│   ├── chunks.py                # RetrieveRelevantChunks source-group/chunk row adapters
│   ├── collections.py           # Strict collection tuple decoder behind the public lazy shim
│   ├── customization.py         # GetArtifactCustomizationChoices option-table row adapters (#2283)
│   ├── documents.py             # TailwindDoc tree adapters (DocumentBodyRow / StructuralElementRow / ParagraphRow / ParagraphElementRow / TextRunRow / TableRow / BulletInfoRow / AnnotationEntryRow) + build_document/build_blocks — one decoder for source fulltext, chat responseDoc, and citation fragments (#2128, #2120)
│   ├── labels.py                # Source-label row adapter
│   ├── notebooks.py             # Notebook Project decoder + SUGGEST_PROMPTS suggestion-row adapter
│   ├── notes.py                 # Note and mind-map row adapter
│   ├── research.py              # POLL_RESEARCH row adapters (ResearchTaskRow / ResearchTaskInfoRow / ResearchResultRow) — drains the research parser's single-level positional reads (#1501)
│   ├── research_task.py         # Deep-research task parser
│   ├── sharing.py               # Shared-user and share-status row decoders behind public lazy shims
│   ├── sources.py               # Source row adapter
│   ├── play_books.py            # ListExpertIntelligenceContent row adapter (#2292): decode_play_books_response → PlayBook list
│   └── transfers.py             # CopySourcesAsync / CopyArtifactsAsync / AddSourcesAsync mapping-row adapters (#2283)
├── _chat.py                     # Abstract ChatAPI + shared ask/configure/settings/turn-count orchestration over typed hooks
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
│   ├── recovery_rungs.py        # Neutral registry + closed result for the optional blocking L3 rung
│   ├── account_types.py         # Dependency-neutral Account/Playwright repair-result values with historical identity
│   ├── account_repair.py        # One-shot typed Playwright account-repair operation + call-time composition
│   ├── account.py               # Account network probing/selection + compatibility repair adapter
│   ├── account_email.py         # Generation-safe account-email matching, probing, caching, and CAS self-heal
│   ├── session.py               # Auth-session refresh implementation via `refresh_auth_session()` and explicit collaborators
│   ├── storage.py               # v0.x facade; raw adapters + minted snapshot/error + token policy
│   ├── keepalive.py             # Keepalive/PSIDTS rotation policy + raw-wire re-exports
│   ├── psidts_recovery.py       # Inline PSIDTS recovery (issue #865) + the one load→heal→retry composition + the captured-cookie validate/heal seam
│   ├── master_token_types.py    # Dependency-bottom MasterTokenError/value + pure legacy-record codec
│   ├── master_token_file.py     # Explicit-path one-sample token I/O + canonical bounded-lock commit (ADR-0034)
│   ├── mint_service.py          # Typed OAuth/exchange/web-mint owner + sole perform_oauth/RotateCookies wires
│   ├── master_token_bootstrap.py # One-store bootstrap/re-mint/missing-storage coordinator (ADR-0034)
│   ├── master_token.py          # Headless v0.x adapters + late-bound bridges (ADR-0023/ADR-0034)
│   ├── recovery.py              # One-shot L2.5/L3/L4 coordinator + synchronized ColdRecoveryState
│   ├── single_flight.py         # Cross-loop flight/leader-task/success-epoch owner
│   ├── refresh.py               # Token driver + late-bound cold-coordinator adapter and L2.5 policy
│   └── tokens.py                # AuthTokens container + load_auth_from_storage loader
├── _browser/                    # Optional browser-backed credential acquisition
│   ├── __init__.py
│   ├── browser_capture.py       # Browser launch→capture→filter→heal→persist core; lazy Playwright
│   ├── browser_launch_errors.py # Channel registry + launch-failure triage
│   ├── navigation_errors.py     # Pure navigation-race/failure classification
│   ├── headless_reauth.py       # Opt-in L3 implementation behind the neutral auth rung
│   └── oauth_token.py           # Visible EmbeddedSetup OAuth-cookie capture
├── _types/                      # Dataclass implementation package re-exported by types.py
│   ├── __init__.py
│   ├── artifact_content.py       # Typed artifact media, slide/infographic content, and per-user state records (#2135, #2136)
│   ├── artifacts.py
│   ├── chat.py
│   ├── common.py
│   ├── enums.py                 # 26 transport-neutral domain enums + value helpers
│   ├── documents.py             # StructuredDocument / DocumentBlock / TextSpan / TableCell / DocumentAnnotation / BlockKind / BlockStyle / ListStyle / ListInfo — parsed-document types behind SourceFulltext.document and AskResult.answer_document, carrying the offsets citations anchor to (#2128, #2120)
│   ├── labels.py                # Label pure-value type (source membership; no kind/artifact_ids)
│   ├── collections.py           # Collection pure-value type (account-level notebook grouping; notebook_ids)
│   ├── mind_maps.py             # MindMap + MindMapKind pure-value types (#1256)
│   ├── notebooks.py
│   ├── notes.py
│   ├── research.py              # ResearchStatus enum + ResearchTask/ResearchSource/ResearchStart/MindMapResult/SourceGuide typed returns (#1209)
│   ├── sharing.py
│   └── sources.py
├── _web/                        # Web batchexecute backend implementations
│   ├── chat.py                  # WebChatAPI streamed-query/RPC adapter
│   ├── notebooks.py             # WebNotebooksAPI
│   ├── labels.py                # WebLabelsAPI (historical logger preserved)
│   ├── collections.py           # WebCollectionsAPI (historical logger preserved)
│   ├── research.py              # WebResearchAPI (historical logger preserved)
│   ├── sources/                 # WebSourcesAPI + web source services
│   │   ├── __init__.py          # WebSourcesAPI facade
│   │   ├── add.py               # Source addition coordinator
│   │   ├── batch.py             # True-batch URL ADD_SOURCE coordinator
│   │   ├── content.py           # Source content fetcher
│   │   ├── drive_import.py      # Cookie-authenticated Drive download + upload route
│   │   ├── listing.py           # GET_NOTEBOOK source listing decoder
│   │   ├── play_books.py        # Web PlayBooksService (#2292): mVtEUb list + X1snv Play Books add; Android has native parity
│   │   ├── search.py            # ASU5Oe ranked source-passage search service
│   │   ├── transfers.py         # AddSourcesAsync / AppendSource / CopySourcesAsync service (#2283)
│   │   ├── upload.py            # Gated resumable source upload pipeline
│   │   └── _upload_decode.py    # Upload URL/source-id/content-type validation
│   ├── artifacts.py             # WebArtifactsAPI
│   ├── artifact/                # Web artifact services
│   │   ├── downloads.py         # Raw selection and representation decoding
│   │   ├── generation.py        # Generation RPC dispatch
│   │   ├── listing.py           # Listing and mind-map composition
│   │   └── table.py             # Positional data-table decoding
│   ├── mind_maps.py             # WebMindMapsAPI + NoteBackedMindMapService
│   ├── notes.py                 # WebNotesAPI + NoteService
│   ├── note_tasks.py            # Lifecycle registry for shielded note finalize/cleanup tasks
│   ├── settings.py              # WebSettingsAPI + web settings helpers
│   ├── sharing.py               # WebSharingAPI + legacy ShareManager
│   ├── params/                   # Web batchexecute payload builders
│   │   ├── artifacts.py         # Artifact RPC payload builders
│   │   ├── chat_note.py         # Saved-chat CREATE_NOTE payload builder
│   │   ├── chat_session.py      # Session status/cancel payload builders
│   │   ├── chat_stream.py       # Streamed-chat URL/form request builder
│   │   ├── collections.py       # Collection RPC payload builders
│   │   ├── labels.py            # Source-label RPC payload builders
│   │   ├── notebooks.py         # Notebook RPC payload builders (SUGGEST_PROMPTS)
│   │   └── sources.py           # Source RPC/upload payload builders
│   ├── policy.py                 # Web RPC idempotency registry + one import-time seed
│   ├── wire/                     # Batchexecute request/response codecs
│   │   ├── decoder.py           # Response framing, status/error decoding, drift counter
│   │   ├── encoder.py           # Request envelope and form-body encoding
│   │   ├── overrides.py         # Runtime RPC-ID override policy and shared cache/log state
│   │   └── safe_index.py        # Strict positional payload access
│   └── rows/                    # Typed positional wire-row decoders
├── _notebooks.py                # Backend-neutral NotebooksAPI + share-URL builder
├── _sources.py                  # Backend-neutral abstract SourcesAPI
├── _artifacts.py                # Backend-neutral abstract ArtifactsAPI
├── _research.py                 # BaseResearchAPI + shared import classification/workflows
├── _research_import.py          # Neutral import policies/classification/reconciliation
├── _notes.py                    # Backend-neutral abstract NotesAPI
├── _sharing.py                  # Backend-neutral abstract SharingAPI
├── _settings.py                 # Backend-neutral abstract SettingsAPI
├── _labels.py                   # Backend-neutral abstract LabelsAPI
├── _collections.py              # Backend-neutral abstract CollectionsAPI
├── notebooklm_cli.py            # Entry-point assembler — imports + registers cli/ groups
├── mcp/                         # MCP server (opt-in `mcp` extra) — transport-neutral adapter over _app/, sibling to cli/
│   ├── __init__.py              # Re-exports create_server / SERVER_NAME / SERVER_INSTRUCTIONS
│   ├── __main__.py              # `notebooklm-mcp` entrypoint: argparse (--profile/--transport/--host/--port/--log-level), stderr logging, loopback HTTP bind guard + fail-closed auth guard (non-loopback bind requires a bearer token AND/OR self-hosted OAuth); composes the auth provider (build_auth) and passes it to create_server on the http path
│   ├── server.py                # create_server(profile, client_factory, auth): FastMCP server; lifespan binds one NotebookLMClient behind a ClientProvider and warms it in the background (never gating the handshake, #2330); register_all tool-registration seam; auth passed explicitly (never reads the token env)
│   ├── _auth.py                 # Remote-transport bearer auth: McpBearerAuthProvider(TokenVerifier) with constant-time hmac.compare_digest over NOTEBOOKLM_MCP_TOKEN (env-only, never logged/repr'd); build_auth_provider/get_configured_token; build_auth(token, oauth) composes bearer | OAuth | MultiAuth | None (IdP-agnostic) — mirrors server/_auth.py, NOT fastmcp StaticTokenVerifier
│   ├── _oauth.py                # Optional self-hosted OAuth 2.1 AS for claude.ai (OAuth-only connector UI): SelfHostedOAuthProvider(InMemoryOAuthProvider) + a password-gated /login (override authorize()→stash SDK-validated (client,params) under a single-use sid→/login→InMemoryOAuthProvider.authorize); scrypt password digest + per-IP throttle, capped DCR + evict-oldest pending stash, atomic 0600 persistence of clients+tokens; get_oauth_config/build_oauth_provider (env NOTEBOOKLM_MCP_OAUTH_PASSWORD + _BASE_URL). Composed with the bearer via MultiAuth
│   ├── _host_guard.py           # LoopbackHostGuardMiddleware: ASGI guard that rejects HTTP requests with a non-loopback Host header (403; DNS-rebinding guard, #1869) on the loopback-bound HTTP transport via _serving.host_header_is_loopback; skipped when allow_external (REST-parity bearer/OAuth auth is mandatory there) — mirrors server/_auth
│   ├── _urlcheck.py             # _validate_bare_https_origin(url, env) — shared "bare public https origin" check (https scheme, host, no path/query/fragment); guards the OAuth base URL AND the file-transfer public URL so a /mcp-suffixed/non-https value can't mint broken links
│   ├── _filelink.py             # HMAC-signed self-describing file-transfer tokens (ADR-0024): FileLinkSigner.sign(payload, ttl→injects exp)/verify(token, op) (stdlib hmac/base64/json; pre-decode length cap, base64url re-pad, compare_digest, exp+op check) + FileTransferConfig(signer, base_url).upload_url(ttl=UPLOAD_TTL 15m; WIDGET_UPLOAD_TTL 1h for the ADR-0027 widget pool)/download_url (DOWNLOAD_TTL 30m); FileLinkError
│   ├── _fileroutes.py           # register_file_routes(mcp, config): the /files/{dl,ul} custom routes mounted on the FastMCP http app (ADR-0024). GET /files/dl streams the artifact (download core → FileResponse, meaningful filename, inside-tempdir assert, BackgroundTask cleanup); GET /files/ul = minimal upload page (file picker + raw-body fetch POST); POST|PUT /files/ul streams request.stream() into a 0600 temp under a running byte cap (real DoS guard) + Content-Length early 413 → neutral source_add core. Signed token is the sole auth (custom routes bypass the bearer gate); HTML pages set no-referrer/no-store/DENY; local _safe_upload_name (no server/ import)
│   ├── _uploadwidget.py         # register_upload_widget(mcp, config): OPT-IN in-app MCP-App upload widget (ADR-0027, NOTEBOOKLM_MCP_UPLOAD_WIDGET=1 → also auto-enables stateless HTTP). ONE ui:// resource (file-picker HTML, profile=mcp-app mime) + source_add_widget tool; both ui/resourceUri (claude.ai) and openai/outputTemplate (ChatGPT) point at it. Emits the render gates via FastMCP meta=/app=: _meta.ui.domain = sha256("<public-url>/mcp")[:32] + ".claudemcpcontent.com", flat ui/resourceUri, ui.csp; the widget POSTs bytes to /files/ul (reuses ADR-0024). Off the default surface unless enabled
│   ├── _chattasks.py            # Detached chat asks for chat_start/chat_status (ADR-0024-shaped in-process state): ChatTaskRegistry (bounded + TTL-swept; idempotency-keyed via compute_chat_task_key; asyncio.create_task detachment so the remote transport's ~60s watchdog cancelling the start call cannot kill the generation; lifespan aclose) + ChatTaskEntry/ChatTaskCapacityError
│   ├── _clientprovider.py       # ClientProvider: lazy, single-flight ownership of the process-wide NotebookLMClient (#2330). The lifespan start()s the open in the BACKGROUND and yields at once, so MCP initialize is never gated on the auth round-trip (15s RotateCookies poke + 30s CSRF fetch + cold-recovery ladder > the client's 30s handshake deadline → CONNECT_TIMEOUT); get() joins the in-flight open under asyncio.shield (a cancelled waiter never aborts it), a failed open is retried by the next call (mid-session re-login recovers), aclose() cancels/closes
│   ├── _context.py              # AppState dataclass (client_provider + optional file_transfer + cancelled_research + chat_tasks) + async get_client(ctx) (awaits the lazy open) / get_file_transfer(ctx) / get_cancelled_research(ctx) / get_chat_tasks(ctx) (lifespan-bound) + async get_client_from_app(request) (the guarded private-attr accessor for the bare-Request custom routes)
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
│       ├── sources_playbooks.py # source_list_play_books + source_add_play_book tools (#2292/#2302): backend-neutral discrete verbs over _app.source_play_books — Google Play Books (Expert Intelligence) (own register())
│       ├── chat.py              # chat_ask (client.chat.ask + get_history recall + suggest_followups) + chat_configure (_app.chat.execute_configure) + suggest_prompts (client.notebooks.suggest_prompts surface selector)
│       ├── notes.py             # note_save (create-or-update upsert) over _app.notes; note reading/renaming/deleting fold into the cross-type Studio tools
│       ├── studio.py            # hosts the Studio tools: studio_list (merges notes+artifacts via _studio_items.studio_items; surfaces each artifact's generation_prompt in the summary listing / the item= single-fetch — folded studio_get_prompt in #1896) / generate / status / download (via _studio_download) / rename / retry / studio_delete — both rename and delete are cross-type via _studio_items.resolve_studio_item (note→_app.notes.execute_note_rename/execute_note_delete, artifact→_app.artifacts kind-aware core); enum dispatch over _app.generate + _app.download; stateless poll via _app.artifacts.poll_artifact
│       ├── research.py          # research_start (client.research.start) + research_status (_app.research.poll_and_classify) + research_import
│       ├── sharing.py           # share_status/set_access/set_user/remove_user (thin adapters over client.sharing; set_access folds public+view_level, set_user upserts add/update; string-labeled enums; view_level surfaced only when set)
│       └── meta.py              # server_info — package version + auth-health over _app.auth_check (no notebook arg)
├── rpc/                         # Public RPC compatibility path
│   ├── __init__.py              # Two-name public surface plus identity re-exports from _web/wire
│   └── types.py                 # Method IDs and domain-enum compatibility re-exports
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
    ├── playwright_login_io.py   # Command-side LoginIO sink + thin wrappers over the browser-login app core (#1391)
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
        │   ├── master_token.py   # Thin, frame-scrubbing delegate to the auth facade's interactive OAuth-capture capability
        │   ├── outcomes.py
        │   ├── profile_targets.py
        │   ├── refresh.py
        │   └── rookie_cookies_errors.py
        ├── playwright_login.py  # CLI-only Chromium preflight, Rich event rendering, and thin invocation wrappers over `_app/login_browser.py`
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
- [ADR-0003](./adr/0003-auth-facade-write-through.md) — `auth.py` write-through facade (Superseded — closed by [ADR-0014](./adr/0014-feature-local-runtime-adapters.md); ADR-0036 later added a narrow lazy browser-capability boundary without making Playwright import-time mandatory).
- [ADR-0004](./adr/0004-loop-affinity-contract.md) — Loop-affinity contract (Accepted; enforced by `_loop_affinity.assert_bound_loop`).
- [ADR-0005](./adr/0005-idempotency-taxonomy.md) — Mutating-RPC idempotency taxonomy (Accepted; enforced by `_web.policy.IdempotencyRegistry`).
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
- [ADR-0020](./adr/0020-sealed-async-result-types.md) — Sealed async result types for artifact generation (Accepted).
- [ADR-0021](./adr/0021-transport-neutral-app-layer.md) — Transport-neutral application layer (`_app/`) (Accepted; boundary enforced by `tests/_guardrails/test_app_boundary.py`, classify↔error_handler agreement by `tests/_guardrails/test_classify_error_handler_consistency.py`).
- [ADR-0022](./adr/0022-regenerable-baselines.md) — Regenerable test baselines (Accepted).
- [ADR-0023](./adr/0023-master-token-headless-auth.md) — Master-token headless auth (Accepted; the L4 unattended re-mint path, `[headless]` extra).
- [ADR-0024](./adr/0024-mcp-remote-file-transfer.md) — Remote-MCP file transfer via signed-URL side-channel (Accepted).
- [ADR-0025](./adr/0025-mcp-tool-granularity.md) — MCP tool granularity (Accepted).
- [ADR-0026](./adr/0026-mcp-studio-surface.md) — MCP Studio surface — notes + artifacts unified (Accepted).
- [ADR-0027](./adr/0027-mcp-app-upload-widget.md) — In-app MCP-App upload widget (Accepted; experimental / opt-in, `NOTEBOOKLM_MCP_UPLOAD_WIDGET=1`).
- [ADR-0028](./adr/0028-gemini-notebook-rename.md) — Proposed package/distribution rename for Google's Gemini Notebook rebrand; not yet an implemented identity change.
- [ADR-0029](./adr/0029-canonical-storage-writer.md) — Single canonical `storage_state.json` writer (Accepted; later refined by ADR-0033 and ADR-0034).
- [ADR-0030](./adr/0030-one-recovery-ladder.md) — One recovery ladder for auth cold-start/refresh (Accepted; companion to ADR-0029).
- [ADR-0031](./adr/0031-credential-tier-auth-model.md) — Credential-tier domain model for `_auth` (Proposed; Stage 0 implemented and later work refined by ADR-0032 through ADR-0034).
- [ADR-0032](./adr/0032-auth-domain-types.md) — Auth domain values and boundaries for `Cookie`, `CookieJar`, and `MasterToken` (Accepted; incremental adoption).
- [ADR-0033](./adr/0033-auth-consolidation-policy.md) — `_auth` consolidation ceilings and function-granular write boundary (Accepted; amended by ADR-0034).
- [ADR-0034](./adr/0034-auth-storage-object-model.md) — Current auth storage object model and owner extraction (Accepted; Phase 12C complete).
- [ADR-0035](./adr/0035-mobile-resilience-transport.md) — Explicit Android backend as a resilience transport (Accepted; all eleven namespaces now close their former Web compatibility seams).
- [ADR-0036](./adr/0036-browser-acquisition-package.md) — Browser acquisition package and neutral login orchestration (Accepted; browser implementation isolated behind lazy auth capabilities).

## See also

- [`CLAUDE.md`](../CLAUDE.md) — quick-start commands, common pitfalls, and the PR workflow for AI agents working in this repo (the per-file index + repository tree now live in [File map](#file-map) above).
- [`docs/development.md`](./development.md) — how to add a new feature API.
- [`docs/refactor-history.md`](./refactor-history.md) — historical narrative of the multi-phase refactor + downstream migration tables.
- [`docs/python-api.md`](./python-api.md) — public Python API surface.
- [`docs/auth-cookie-lifecycle.md`](./auth-cookie-lifecycle.md) — cookie keepalive, rotation, and PSIDTS recovery.
- [`docs/rpc-development.md`](./rpc-development.md) — capturing and debugging new RPCs.
- [`docs/rpc-reference.md`](./rpc-reference.md) — RPC payload structures.
