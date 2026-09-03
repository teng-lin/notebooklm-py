# Contributing Guide

**Status:** Active
**Last Updated:** 2026-09-02

This guide covers everything you need to contribute to `notebooklm-py`: architecture overview, testing, and releasing.

> **New contributor?** Start with [CONTRIBUTING.md](../CONTRIBUTING.md) at the
> repo root for the install/lint/test workflow and PR conventions, then come
> back here for architectural context once you're ready to write code.

---

## Architecture

> **Canonical post-refactor map:** see [`docs/architecture.md`](./architecture.md)
> for the current adapter/app/client/runtime/RPC graph and
> capability-protocol model. This section
> remains as the contributor on-ramp (package layout + adding-features
> guidance) and links out to the architecture doc rather than duplicating it.

Use the [system overview](https://teng-lin.github.io/notebooklm-py/diagrams/01-system-overview.html) while orienting to the packages and
the [testing/guardrails map](https://teng-lin.github.io/notebooklm-py/diagrams/22-testing-and-guardrails.html)
when deciding which boundary a change must exercise.

### Package Structure

```
src/notebooklm/
├── __init__.py          # Public exports
├── client.py            # NotebookLMClient main class
├── auth.py              # Public auth facade
├── types.py             # Dataclasses and type definitions
├── _app/                # Transport-neutral business logic shared by adapters
│   └── login_browser.py # Markup-free browser-login plan and orchestration
├── _browser/            # Optional Playwright-backed credential acquisition
├── _runtime/            # Neutral runtime contracts, config, helpers, init, lifecycle
├── _web/contracts.py    # Web-only Kernel and RpcCaller Protocols
├── _web/transport/      # Web runtime bundle, composition holder, HTTP transport, middleware
├── _web/wire/           # Web batchexecute codecs, overrides, and strict indexing
├── _android/            # Android gRPC/Scotty adapters, codecs, and lazy generated protos
├── _notebooks.py        # Backend-neutral NotebooksAPI + share-URL builder
├── _web/notebooks.py    # WebNotebooksAPI implementation
├── _sources.py          # Backend-neutral abstract SourcesAPI
├── _web/sources/        # WebSourcesAPI + concrete web source services
├── _web/params/         # Web batchexecute payload builders
├── _notebook_metadata.py # Neutral metadata protocols + composition service
├── _source/             # Neutral source polling/Markdown + lazy shims
├── _artifacts.py        # Backend-neutral abstract ArtifactsAPI
├── _artifact/           # Neutral artifact polling, formatting, validation, and asset transfer
├── _web/artifacts.py    # WebArtifactsAPI implementation
├── _web/artifact/       # Web artifact listing/generation/download-selection services
├── _chat.py             # Backend-neutral ChatAPI orchestration
├── _research.py         # BaseResearchAPI + shared import classification/workflows
├── _research_import.py  # Neutral import policies/classification/reconciliation
├── _web/research.py     # WebResearchAPI wire hooks + verification policy
├── _notes.py            # Backend-neutral abstract NotesAPI
├── _web/notes.py        # WebNotesAPI + NoteService implementation
├── _mind_maps_api.py    # Backend-neutral abstract MindMapsAPI
├── _web/mind_maps.py    # WebMindMapsAPI + NoteBackedMindMapService
├── _android/mind_maps.py # Selected mind-map artifact/note composition; generation seam documented
├── _labels.py           # Backend-neutral abstract LabelsAPI
├── _web/labels.py       # WebLabelsAPI implementation
├── _collections.py      # Backend-neutral abstract CollectionsAPI
├── _web/collections.py  # WebCollectionsAPI implementation
├── _settings.py         # Backend-neutral abstract SettingsAPI
├── _web/settings.py     # WebSettingsAPI + web settings helpers
├── _sharing.py          # Backend-neutral abstract SharingAPI
├── _web/sharing.py      # WebSharingAPI + legacy ShareManager
├── rpc/                 # Public power-user and compatibility facade
│   ├── __init__.py
│   └── types.py         # RPCMethod enum, constants, and compatibility re-exports
├── cli/                 # Click adapter (`*_cmd.py`) plus `cli/services/`
├── mcp/                 # FastMCP adapter (optional `mcp` extra)
└── server/              # FastAPI REST adapter (optional `server` extra)
```

### Layered Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Adapter Layer                          │
│        cli/ (Click), mcp/ (FastMCP), server/ (FastAPI)       │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│                  App Core Layer (`_app/`)                    │
│        transport-neutral request/plan/result workflows       │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│                      Client Layer                           │
│  NotebookLMClient → NotebooksAPI, SourcesAPI, ArtifactsAPI  │
│       private services compose cross-facade behavior         │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│                    Shared Runtime Layer                     │
│       CallSupervisor, ClientLifecycle, transport owners     │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│           Backend Layers (`_web/` and `_android/`)          │
│       batchexecute/HTTP or gRPC/protobuf + transfers        │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│          Raw facades (`raw.py`; `rpc/` IDs are Web-only)    │
│     WebRawAPI.call or AndroidRawAPI.unary/unary_stream      │
└─────────────────────────────────────────────────────────────┘
```

### Layer Responsibilities

| Layer | Files | Responsibility |
|-------|-------|----------------|
| **Adapters** | `cli/`, `mcp/`, `server/` | User commands/tools/routes, transport-specific input/output, auth envelopes |
| **App core** | `_app/*.py` | Transport-neutral workflows reused by adapters |
| **Client** | `client.py`, `_*.py` | High-level Python API, returns typed dataclasses |
| **Runtime** | `client.py`, `_runtime/`, `_web/transport/`, `_android/session.py`, transfer participants | `NotebookLMClient` composition root; protocol-neutral root lifecycle and call supervision; backend resource lifecycles, admission, telemetry, and feature-owned polling/upload state |
| **Web wire** | `_web/wire/*.py` | Batchexecute encoding/decoding, runtime ID overrides, strict positional access |
| **Android wire** | `_android/codecs/`, `_android/proto/` | Lazy generated protobuf contracts, request construction, and public-type projection for selected Android namespaces |
| **RPC facade** | `rpc/*.py` | Public power-user compatibility exports and method IDs |

#### Runtime seam modules

The client runtime is split across `NotebookLMClient` (composition root),
`_runtime/init.py` (neutral validation/shared collaborators),
`_web/transport/init.py` (web bundle construction),
`_web/transport/composed.py` (`ClientComposed` holder),
`_web/transport/kernel.py` (HTTP client owner), and single-responsibility collaborator
modules. (The legacy `_core.py` compatibility shim was deleted in v0.5.0;
callers import directly from the canonical modules.) Each helper exposes
a narrow Protocol surface so it can be unit-tested against a stub:

| Module | Class | Responsibility |
|---|---|---|
| `_runtime/init.py` | `SharedRuntime` helpers | Validates constructor args and builds the backend-neutral metrics/supervisor bundle. It imports no backend implementation. |
| `_web/transport/init.py` | `WebRuntime` helpers | Builds request-id/auth/kernel/persistence/transport only for Web selection or first deprecated sidecar use, wires middleware, and returns the web bundle including executor and uploader. |
| `_android/runtime.py` | `AndroidRuntime` | Immutable owner bundle for the bearer provider, gRPC session, upload/asset transports, and Phenotype provider. |
| `_web/transport/sidecar.py` | `LazyWebSidecar` | Pre-registered inert Android lifecycle proxy for deprecated root `rpc_call`; owns one-time Web materialisation, close-race serialization, reopen, and phase delegation without a drain hook or keepalive. |
| `_web/transport/composed.py` | `ClientComposed` | Write-once holder for web transport, executor, chain host, middleware metadata, and the shared runtime bundle. It owns no loop primitive or RPC semaphore. |
| `_client_metrics.py` | `ClientMetrics` | `ClientMetricsSnapshot` counters, queue-wait recorders, `on_rpc_event` async callback. |
| `_runtime/call_supervisor.py` | `CallSupervisor` | Concrete client-wide admission authority: generation-bearing call/operation leases, drain hooks, admitted child tasks, terminal RPC metrics, and the global RPC semaphore. |
| `_web/transport/reqid_counter.py` | `ReqidCounter` | Monotonic `_reqid` counter for chat backend (baseline 100000, step 100000). |
| `_web/transport/auth.py` | `AuthRefreshCoordinator` | Refresh-task lifecycle, refresh lock, `AuthSnapshot` rotation. |
| `_runtime/contracts.py` | Neutral runtime Protocol | `LoopGuard`, used by transport-neutral orchestration. |
| `_web/contracts.py` | Web transport Protocols | `Kernel` and `RpcCaller`, used only by batchexecute implementations. Single-consumer capabilities stay local to their owner modules. |
| `_runtime/lifecycle.py` | `ClientLifecycle` | Protocol-neutral resource/admission state and transactional, coalesced open/drain/close waves over immutable transport and loop-participant tuples. |
| `_web/transport/lifecycle.py` | `WebTransportLifecycle` | Web Kernel/auth epoch activation and fencing, keepalive, cookie persistence, and web resource teardown. |
| `_web/transport/runtime.py` | `RuntimeTransport` | Authenticated transport leg: admission-only epoch proof for snapshot/materialization, then `CallSupervisor` terminal accounting/semaphore and the four-middleware web chain. |
| `_web/transport/executor.py` | `RpcExecutor` | RPC dispatch executor with direct collaborator dependencies. |
| `_web/transport/request_types.py` | `AuthSnapshot`, `BuildRequest`, request materialization | Shared request construction Interface. |
| `_web/transport/errors.py` | transport exceptions, `parse_retry_after`, `raise_mapped_post_error` | Terminal `Kernel.post` error mapping for middleware retry/auth behavior. |
| `_web/transport/streaming_post.py` | `stream_post_with_size_cap` | Low-level POST streaming and response-size guard. |
| `_android/session.py` | `AndroidSession` | Selected-Android bearer/gRPC lifecycle participant: lazy channel, deadline/status mapping, and shared `CallSupervisor` admission/telemetry. |
| `_android/epoch.py` | Android workflow epoch context | Session-tagged task-local epoch propagation shared by Android namespace scope hooks and omitted-epoch session calls. |
| `_conversation_cache.py` | `ConversationCache` | Per-instance true-LRU conversation cache for `ChatAPI` continuity. Caps the conversation count (`MAX_CONVERSATION_CACHE_SIZE`) and the turns retained per conversation (`MAX_TURNS_PER_CONVERSATION`). |
| `_polling_registry.py` | `PollRegistry` | Pending-poll registry shared by long-running artifact generations. |
| `_web/transport/cookie_persistence.py` | `CookiePersistence` | Per-path typed baselines, ordered `ProfileStore` cookie merges, `__Secure-1PSIDTS` rotation, and the concrete v0.x snapshot adapter. |

Transport-neutral orchestration uses `LoopGuard` from
`notebooklm._runtime.contracts`; batchexecute implementations use `Kernel`
and `RpcCaller` from `notebooklm._web.contracts`. The single-consumer
`AuthMetadata` capability stays in `_web/sources/upload.py`. The supervisor-ownership refactor removed
`OperationScopeProvider`; artifact polling and source workflows use the shared
concrete `CallSupervisor` for operation scopes, admitted children, loop checks,
and drain hooks. The unused `AsyncWorkRuntime` composite was deleted. The broad `Session` Protocol
that previously bundled these together was deleted in the final phase
of the capability refactor (see [`docs/refactor-history.md`](refactor-history.md)
and ADR-0013); each feature now depends on the narrowest slice it needs
and takes those collaborators by keyword-only constructor argument. The
feature-local composite-runtime Protocols (`ChatRuntime`,
`ArtifactsRuntime`, `UploadRuntime`) and their adapter dataclasses that
previously bundled three capability Protocols apiece were retired once
it was clear they only hid stable collaborators with one production
satisfier; see ADR-0013 for the promotion criterion (at least two
production consumers) that still gates adding any new shared Protocol.

Private service modules sit inside the client layer but below the public
facades. They own cross-facade composition without importing sibling facades:
`_notebook_metadata.py` composes notebook metadata through a narrow source
lister without importing a concrete transport or lister; `_web/notebooks.py`
owns the direct-construction web fallback. `_web/sharing.py` owns legacy
`SHARE_ARTIFACT` mutation behavior while `_notebooks.py` owns the
transport-neutral URL builder, and
`_web/mind_maps.py` owns the web note-backed mind-map rows shared by notes and artifacts;
`_mind_maps_api.py` owns the transport-neutral unified workflows.
Facade modules keep the public method surface stable and delegate to these
services.

### Boundary Guardrails

These are the same family as the *Architecture & invariant gates* (`tests/_guardrails/`)
described below. The **pure** ones (e.g. `test_cli_boundary.py`) have been
consolidated into `tests/_guardrails/`; the **hybrids** that pair a gate with
behavioral tests (e.g. `test_public_shims.py`) keep their behavioral half in
`tests/unit/` and split the gate half into a dedicated `tests/_guardrails/`
file.

The architecture tests encode the current layer contract:

- `tests/_guardrails/test_public_surface_manifest.py` has a documented public
  import manifest. When a docs change adds or removes a supported import path,
  update the manifest in the same PR so public API drift is intentional and
  reviewable. The behavioral half of the public-shim suite (the
  `select_cited_sources` delegation, the Web-only `ResearchAPI` compatibility
  alias and research import-order boundary, the `UnknownTypeWarning` filter
  behaviour, and the deprecated `NotebookLMClient.rpc_call`
  forwarding/warning contract) stays in `tests/unit/test_public_shims.py`.
- `tests/_guardrails/test_cli_boundary.py` parses `src/notebooklm/cli/**/*.py`
  and rejects CLI imports from `notebooklm._*`, `notebooklm.rpc.*`, or
  `_private` names exposed by public modules. Promote needed symbols through a
  public facade (`notebooklm.types`, `notebooklm.auth`, `notebooklm.research`,
  etc.) before using them from the CLI.
- Auth internals may move under `notebooklm._auth` during architecture work,
  but first-party callers continue to import through `notebooklm.auth`. The
  compatibility manifest in `tests/_guardrails/test_public_surface_manifest.py`
  enforces the current first-party surface for that move; it is not a broader
  public API decision, and removing a listed name needs a separate deprecation
  plan.
- `tests/_guardrails/test_no_facade_reach_in.py` holds the AST reach-in /
  runtime-import boundary gates: notebook metadata services must not import or
  construct `SourcesAPI`; artifact/source/notebook composition services must
  not runtime-import facade APIs. Add new private services to those guard
  lists when they take ownership of cross-facade behavior. The construction /
  init-order behaviour tests — `NotebookLMClient` constructs `WebSourcesAPI`
  before `NotebooksAPI` and passes it through the legacy `sources_api=` slot,
  plus the mind-map decoupling flows — stay in
  `tests/unit/test_init_order.py`.

### Key Design Decisions

**Why underscore prefixes?** Files like `_notebooks.py` are internal implementation. Public API stays clean (`from notebooklm import NotebookLMClient`).

**Why namespaced APIs?** `client.notebooks.list()` instead of `client.list_notebooks()` - better organization, scales well, tab-completion friendly.

**Why async?** Google's API can be slow. Async enables concurrent operations and non-blocking downloads.

**How backend selection works.** An explicit `backend="web" | "android"`
wins over `NOTEBOOKLM_BACKEND`; otherwise Web is the default. Android selection
installs Android adapters for all eleven typed namespaces together, and
`client.backends` reports that immutable installed graph. The current Android
namespace objects have no Web operation collaborators. `client.rpc_call(...)`
is deprecated and not backend-neutral: `RPCMethod` values are batchexecute IDs,
so Android preserves it through a lazy, no-keepalive Web sidecar only during the
0.x warning window. New code uses the backend-selected `client.raw` property:
`raw.call(...)` on Web and `raw.unary(...)` / `unary_stream(...)` on Android.
See the Android [entry point](android/README.md) and
[architecture flow](architecture.md#android-grpc-path).

**Naming conventions.** See [`docs/conventions.md`](./conventions.md) for the
canonical tiebreakers on waiting/polling verbs (`poll_X` / `wait_for_X` /
`wait_until_X` / `await_X` / `_wait_for_X`), RPC-callable Protocol names
(`NextCall` / `RpcCallback` / `RpcCaller`), and
metrics method verbs (`record_X` vs `emit_X`). New code should pick names
from those catalogues rather than introducing parallel patterns.

### Adding New Features

**New RPC Method:**
1. Capture traffic (see [RPC Development Guide](rpc-development.md))
2. Add to `rpc/types.py`: `NEW_METHOD = "AbCdEf"`
3. Implement in appropriate `_*.py` API class
4. Add dataclass to `types.py` if needed
5. Add CLI command if user-facing

**New API Class:**
1. Create `_newfeature.py` with `NewFeatureAPI` class.
2. Type each constructor parameter against the **narrowest shared
   capability Protocol** it actually uses (`LoopGuard` from
   `_runtime/contracts.py`, or web-only `RpcCaller` / `Kernel` from
   `_web/contracts.py` — see
   [`docs/architecture.md`](./architecture.md) for the protocol
   catalog). If the capability has only one consumer, define the
   Protocol locally beside that consumer instead of promoting it to either
   contracts module. Pass each collaborator by keyword-only
   argument; do not bundle them into a feature-local composite-runtime
   Protocol unless a second production consumer materialises. **Do NOT
   depend on a broad runtime facade for type annotations** — there is no
   concrete `Session` class (the broad `Session` Protocol was deleted;
   see ADR-0013).
3. Add the wiring in `_client_assembly.py::_assemble_client(...)`, not
   directly in `client.py`. The assembly seam is shared by
   `NotebookLMClient.__init__` and the canonical test factory; set every
   constructor-time attribute there and thread concrete collaborators
   from `compose_client_internals(...)`.
4. **Tests** should inject the narrow collaborator the feature actually
   needs. `tests/_fixtures/fake_core.py:FakeSession` remains available
   for legacy broad-fixture tests, but new direct feature tests should
   prefer `MagicMock(spec=RpcCaller, rpc_call=AsyncMock(...))`-style
   fakes or local protocol fakes.
5. Export types from `__init__.py`.

---

## Concurrency Model

Multiple `notebooklm` processes (parallel CLI runs, an in-process keepalive
beside a cron-driven `notebooklm auth refresh`, container start-up races,
`xargs -P` fan-outs) can target the same `NOTEBOOKLM_HOME` simultaneously.
The library coordinates with **cross-process file locks** — the project-internal
`StorageLockManager` (`_auth/storage_lock.py`, using `flock`/`msvcrt`) for
`storage_state.json` and its sibling credential file, and the
[`filelock`](https://pypi.org/project/filelock/) package for `migration.py` and
`context.json` — so reads and writes against shared on-disk state never tear or
clobber a sibling's update. See
[ADR-0029](adr/0029-canonical-storage-writer.md) for why the split exists.

All locks are sibling files next to the resource they guard (zero-byte,
left on disk after release — both lock implementations reuse them).

| Lock file | Owner | Scope | Acquisition |
|---|---|---|---|
| `<profile>/.storage_state.json.lock` | `_auth/profile_store.py` transaction owner via `_auth/storage_lock.py`; `_auth/storage.py` retains compatibility adapters | Every mutation of `storage_state.json`: cookie CAS, typed in-band account update/clear, and remint/login/minted full replacement | Cookie methods: blocking exclusive, fail-open. Account update and minted replacement: bounded 90s, fail-closed; clear: bounded best-effort; browser/remint and login: bounded typed status. |
| `<profile>/.master_token.json.lock` | `_auth/master_token_file.py` via the `ProfileStore` derived-token methods; `_auth/storage.py` retains the arbitrary-path v0.x adapter | Writes to `master_token.json` (the durable L4 credential) | Same bounded manager acquire as above (90s deadline), fail-closed. |
| `<profile>/.storage_state.json.rotate.lock` | `_auth/keepalive.py::_poke_session` via `_auth/storage_lock.py` | Cross-process dedup of the `accounts.google.com/RotateCookies` keepalive POST | Non-blocking exclusive; skip on contention |
| `<profile>/.storage_state.json.refresh.lock` | `_auth/refresh.py` via `_auth/keepalive.py` and `_auth/storage_lock.py` | Cross-process dedup of the `NOTEBOOKLM_REFRESH_CMD` subprocess | Non-blocking exclusive; skip on contention, waiter polls asynchronously with jittered backoff |
| `<profile>/.storage_state.json.lock.bootstrap` | `_auth/master_token_bootstrap.py::MasterTokenBootstrapper.bootstrap_storage`; `_auth/master_token.py` retains the v0.x adapter | Cross-process exclusion for the FIRST-TIME mint of a profile that has only a `master_token.json` — held across the mint, whose persist takes `.storage_state.json.lock` *inside* this section (so the two must never share a path) | Non-blocking exclusive (`filelock`), retried on a 50ms async sleep up to a 90s deadline |
| `<home>/.migration.lock` | `migration.py::migrate_to_profiles` | One-shot legacy→profile layout migration on startup | Blocking exclusive, 30s timeout (raises `MigrationLockTimeoutError`) |
| `<profile>/context.json.lock` | `_atomic_io.py::atomic_update_json` through CLI context helpers; also `_auth/profile_migration.py::LegacyAccountContext.scrub` for legacy `account` cleanup | Read-modify-write of the active-notebook/account-routing context for a profile | Blocking exclusive, 10s timeout (`filelock`); migration cleanup keeps best-effort error handling and the public atomic JSON writer |

Design notes:

- **Three layered storage locks (not one).** The `.lock`, `.rotate.lock`, and
  `.refresh.lock` files protect the *same* `storage_state.json` but serve
  different access patterns: a full-replace write must not block — or be
  blocked by — a best-effort rotation poke or a refresh-cmd subprocess.
  Keeping them separate prevents any one from queueing behind another. The
  fourth sibling, `.lock.bootstrap`, is separate for a harder reason than
  throughput: the mint it guards acquires `.storage_state.json.lock` *inside*
  its critical section — and the two sides use different mechanisms
  (`filelock.FileLock` outside, `StorageLockManager` inside), so the in-process
  lock registry never sees the outer hold. What makes sharing one path fatal is
  the OS lock: both take an exclusive `flock` on the sentinel, and `flock`
  conflicts between two open file descriptions even inside one process, so the
  inner acquire would be guaranteed-unavailable rather than merely contended.
- **One derivation, four files, three different base-path policies.** Every
  credential lock path is `.<name>.<kind>` next to the storage file, computed in
  exactly one place (`_auth/paths.py::_lock_sibling`). What differs per lock is
  the *base* it is derived from, and that choice decides whether two spellings
  of one profile collapse onto the same lock:
  - `.storage_state.json.lock.bootstrap` canonicalizes **inside** the helper
    (`_bootstrap_lock_path` -> `canonical_storage_key`, degrading to
    `expanduser()` on a symlink loop), because its cold-start callers get
    whatever spelling the CLI was handed and two processes must not both mint.
  - `.refresh.lock` is alias-proof **by its callers**: both production sites
    (`refresh._fetch_tokens_with_refresh`, `refresh.try_refresh_cmd_reauth`)
    pass the already-canonicalized path they key the single flight on.
  - `.rotate.lock` is alias-proof only on the keepalive route, where
    `_client_assembly` canonicalizes the keepalive storage path once at client
    assembly; the PSIDTS rotation-recovery route passes its load path through
    unchanged.
  - `.storage_state.json.lock` never canonicalizes for I/O: `ProfileStore` and
    the compatibility transaction alias derive `_storage_state_lock_path` from
    the caller's raw path, so two processes
    reaching the same file through different path spellings (e.g. a symlink vs.
    its resolved target) can take different main-write locks and race. Callers
    are expected to reach a profile's storage file through one consistent path
    (`paths.py`'s resolvers), which holds in every in-tree call site today.

  These filenames are a cross-version contract — a mixed-version window (old CLI
  + new server on one profile) loses updates the moment two versions disagree on
  a lock name — so all four are pinned against hard-coded strings in
  `tests/unit/test_auth_lock_path_derivation.py`.
- **Per-intent fail-open/fail-closed split ([ADR-0029](adr/0029-canonical-storage-writer.md)).**
  The cookie CAS merge and the rotation/refresh-cmd pokes fail **open** on lock
  infrastructure failure (read-only home dir, NFS without `flock`, permission
  denied) rather than wedging forever — availability wins, and the CAS guard
  (or the reactive nature of a poke) keeps correctness. Full-file
  read-modify-write intents (account update, master-token persist/re-mint)
  fail **closed**, raising `LockUnavailableError`, because failing open there
  could silently overwrite a concurrent CAS delta.

`ProfileStore` also owns typed account reads, best-effort clear, and the complete
browser/remint, login/import, and minted-session replacement transactions. Remint carries the
latest whole raw `notebooklm` namespace only when requested, filters through the pure
`_auth/cookie_filter.py` leaf, and commits once. Browser capture constructs a
`RemintWriteRequest` and consumes the native `ReplaceResult` directly;
`storage.replace_from_remint` remains only the v0.x direct-call adapter. Login filtering,
required-name validation, KEEP/SET/CLEAR construction, optional backup, and commit run under one
bounded store lock. `_auth/profile_migration.py` owns the path-shaped
`replace_profile_from_login` operation and the legacy-account boundary:
`LegacyAccountMigrator` performs lossless in-band/legacy/in-band two-read resolution, typed legacy
sanitization, only-if-absent promotion, and embed-before-scrub ordering;
`LegacyAccountContext` owns the sibling file and lock. `LegacyPromotionScheduler` owns the
canonical per-path active-work registry and daemon workers. Reads only schedule and return; the
path becomes eligible again after a worker settles, and an in-band record with a stale legacy copy
schedules the privacy scrub, making lock failures and write-then-scrub interruption self-healing.
Concurrent readers still share one active worker; the
process-default exit hook drains outstanding workers within one shared budget
(30 seconds by default, configurable via `NOTEBOOKLM_PROMOTION_EXIT_TIMEOUT`),
warning if any is still running when it expires.

First-party app and CLI flows pass primitive account modes to `replace_profile_from_login` and
consume `ReplaceResult`; `storage.replace_from_login` keeps its v0.x signature and projects that
native result to `LoginWriteOutcome`. Post-`APPLIED`, outside-lock promotion/scrub remains owned by
`LoginProfileWriter`; failed store results do no sibling work. Raw source account key presence
still chooses scrub versus promotion. `AccountMetadataWriter` similarly preserves the
distinct update-then-scrub and best-effort-clear-then-scrub order, while naturally escaping store
exceptions abort before scrub. For minted persistence,
`storage.persist_minted_jar` eagerly snapshots the live jar with the raw master-token serializer
fields (including `same_site="None"`) and deep-copies the runtime-permissive email in the same
repr-hidden request before path/lock work. It does not use the filtering and SameSite-lossy
`CookieJar.from_httpx()` constructor. `ProfileStore.replace_minted_session` then owns the
same-lock latest-owner decision, default filter, destination preservation/rebind, and one commit;
the adapter translates its private refusal outside the handler to context-free canonical
`MasterTokenError`. This immutable jar-and-email snapshot is the intentional isolation correction.
An empty in-band account mapping does not
block promotion but is still preserved when remint carries the whole namespace;
a non-empty unknown-only mapping remains present and wins.

`_auth/master_token_bootstrap.py` owns the concrete bootstrap, re-mint, and
missing-storage transaction over one `ProfileStore`. Keep new behavior tests on
`MasterTokenBootstrapper`, `MintService`, `ProfileStore`, and the call-time
strict-loader seam; do not restore patches of the v0.x coarse functions in
`_auth/master_token.py`. The adapter composes one store/service/bootstrap lock
and retains late-bound bridges for legacy owner lookup, Android-ID generation,
strict reload, and default verification. Session persistence finishes before
token persistence; a missing-storage leader remains shielded to settlement
before its bootstrap lock is released. At the extraction freeze the coordinator
was 373 lines and the compatibility adapter was 463 lines. Those are historical
split measurements, not current module-size claims.

`MintService.mint_oauth()` is the single typed OAuth mint seam. New protocol
adapters supply an immutable `OAuthClientSpec` and consume a repr-safe
`MintedOAuthToken`; they do not import `gpsoauth` or invent a TTL when its
optional `Expiry` is absent or malformed. Oversized decimal expiry values are
rejected before integer conversion. Fixed mint failures retain no raw response,
dependency exception, credential-bearing cause, or traceback local, while
`MissingDependencyError` remains distinct. The web `mint()` path is pinned to
the existing Chromecast/OAuthLogin arguments and cookie-mint wire sequence.

The ownership-refactor completion snapshot measured 1,090 lines in `_auth/storage.py`, 602 in
`_auth/profile_migration.py`, 876 in `_auth/profile_store.py`, 96 in
`_auth/cookie_filter.py`, and 89 in `_auth/master_token_file.py` (2,753 total).
Use the module-size guardrail for current measurements. `storage.py`
remains the v0.x signature/result facade; the extracted owners do not create a second facade.

`_auth/tokens.py` now owns stored-auth composition: captured-inline/file sources,
`LoadPolicy`, paired `SessionSeed`/`TokenAcquisition`, final-attempt `AccountRouteResolver`, the
closed `LoadedAuth` result, and `StoredAuthLoader`. Its only structural test seam is
`TokenAcquirer`; inject that seam for ladder-result tests, and patch the call-time
`tokens._load_stored_auth` provider for `AuthTokens.from_storage`/client composition tests. Raw
loads and PSIDTS heal, file-account resolution, and the initial `ProfileStore` merge remain worker
offloads. `_auth/cookies.py` owns one-sample live/SameSite-preserving seed provenance.
`_auth/recovery.py` now owns the complete cold operation. A fresh, one-shot
`ColdRecoveryCoordinator` spells L2.5 → L3 → L4 directly; its class-owned `_drive_cold` and
`_coalesce_cold` methods are the sole ladder and flight bodies. `ColdRecoveryState` owns the
synchronized weak-loop path-lock/generation maps, and `SingleFlight` owns cross-loop flights and
canonical-path success epochs. The exact-signature module functions are compatibility adapters to
the process-default owners, not second implementations. The coordinator claims under a threading
lock before its first await and deletes all eleven collaborator references on every exit.

The sole production composition remains in `refresh.py`; its late-bound closures retain DEBUG skip
and WARNING start/failure logs, raw caller / canonical L2.5 / raw caller route timing, original
exception precedence, and exact traceback projection. Waiter cancellation propagates only after
the shared flight settles and never cancels sibling work; a leader cancellation is mirrored as the
original `CancelledError`. Cancellation before jar replacement leaves the caller jar untouched,
while later cancellation does not roll back a completed replacement. Test isolated owners through
constructor injection. Use process-default module adapters only when testing v0.x lookup seams;
their reset helpers reject live/locked work and clear only quiescent state.

`RotationState` similarly owns keepalive's weak-loop locks and per-canonical-path monotonic stamps.
The claim is atomic and occurs before the POST, so HTTP failure and cancellation consume the
60-second slot; the historical `_POKE_*` dictionaries/lock are identity views into that owner, not
independent state. `AccountRepairService` is also one-shot: cookie loading alone is offloaded,
write/clear remain synchronous, only the frozen handled exception set becomes a result, and all six
collaborators are scrubbed on every exit. Patch these services through constructor injection rather
than adding module monkeypatch sites.

PSIDTS load composition now receives the pure cookie loader as an explicit callable. Keep
conversion in `CookieJar`, raw-row fidelity in `ProfileDocument`, and persistence in `ProfileStore`;
do not restore `psidts_recovery -> cookies` or `psidts_recovery -> storage`. Preserve the sentinel,
contended-reread, acquired-full-reread, pre-POST observation, typed CAS, and post-save disk-live
winner order. Its catches are intentionally narrow: cancellation, Unicode failures, and unlisted
errors must not be normalized into a silent decline. The master-token exception follows the same
compatibility rule: define it only in `master_token_types.py`, but preserve
`__module__ == "notebooklm._auth.master_token"`, facade/storage identity, cause chains, and old
pickle payloads.

`fetch_tokens_with_domains` now loads one paired live jar and SameSite-preserving baseline, then
passes that baseline through the unchanged exact-baseline ladder. After the final fetch it captures
an immutable `CookieJar` observation on the caller thread and makes exactly one direct positional
`asyncio.to_thread` handoff to the private synchronous helper and one concrete `ProfileStore`.
`HARD_FAILURE` retains the exact selected initial/L2.5/L3/L4 baseline object; every advancing
result selects the exact returned next-baseline object. Cancellation is ordinary `to_thread`
cancellation: the caller is cancelled immediately, while an already-dispatched worker may finish
and commit. File auth alone constructs the store; inline env auth logs the existing skip and does
not persist. Patch the private typed helper/store method for these tests, not the retired private
`refresh.save_cookies_to_storage` alias. The public saver/facade and client/runtime saver-injection
seams remain exact. The completed ownership refactor's closing snapshot measured
**40 modules / 15,237 lines / 128 unique edges (117 module +
11 function-local)**. Module-only and all-scope SCC sets are both empty. The final touched production
LOC is: account 252, account-repair 132, account-types 50, cookie-types 396, cookies 961, keepalive
438, master-token 455, master-token-types 68, PSIDTS recovery 1,222, recovery 530, refresh 1,184,
single-flight 268, and storage 1,127. These are ratchet evidence, not a budget to spend.

Runtime ownership is complete. `NotebookLMClient.from_storage` registers a
`FileLoadedAuth` result's exact `ProfileStore`/baseline pair with `CookiePersistence`, without a
second disk read. A direct file client prepares its baseline once before transport construction;
missing, malformed, or invalid input becomes a sticky typed failure for canonical saves. A
fileless client records only a one-shot live compatibility projection and creates no typed state.

First-party `_from_store` persistence retains no `AuthTokens`. A missing saver routes
unconditionally through the private canonical merge. Only an explicit `cookie_saver=` routes
through `_save_v0_callback`; it lazily initializes its own retryable adapter snapshot and suppresses
the writer when the source is invalid.
The former web-specific `ClientLifecycle` owned the client
`AuthTokens` mirror and refreshed `cookie_snapshot` after open and accepted
canonical or compatibility saves. The lifecycle split moved those responsibilities into
`WebTransportLifecycle`; the current root `ClientLifecycle` owns only
protocol-neutral lifecycle waves and state. Tests inject a saver on the client when they
intend to exercise the callback contract; canonical tests target the private typed seam.
At that closing snapshot the owners measured 457 lines in
`_web/transport/cookie_persistence.py`, 618 in `_runtime/init.py`, 628 in
`_runtime/lifecycle.py`, and 992 in `client.py`; these figures are retained as
refactor evidence rather than a live inventory.

- **In-process lock before OS lock.** `StorageLockManager` takes an in-process
  `threading.Lock` keyed by the exact raw lock-path spelling *before* the OS lock, so
  threads within one process serialize before ever touching the OS primitive —
  layered under the per-loop `asyncio.Lock` dedup described below.
- **Locks are sibling files, never the resource itself.** Both lock
  implementations reuse the sentinel across invocations, so cleanup is not
  required — and a TOCTOU race between unlink and reacquire is avoided.
- **In-process serializers complement, not replace, file locks.**
  `_auth/keepalive.py::_poke_session` takes an `asyncio.Lock` keyed on
  `(event_loop, profile)` to dedupe an `asyncio.gather` fan-out before reaching
  the cross-process flock; the refresh-cmd path does the same but *across event
  loops in one process* via `_auth/single_flight.py`
  ([ADR-0030](adr/0030-one-recovery-ladder.md)) — the file lock only sees one
  contender per process per rate-limit window either way.

Path resolution for all locked resources flows through `paths.py`
(`get_storage_path`, `get_context_path`, `get_home_dir`), so a `--storage`
override or a different `NOTEBOOKLM_PROFILE` automatically yields a distinct
lock sibling and the two invocations never contend.

---

## Testing

### Prerequisites

1. **Install dependencies** (canonical contributor flow — see [docs/installation.md#e-contributor](installation.md#e-contributor) for details):
   ```bash
   uv sync --frozen --extra browser --extra dev --extra markdown
   uv run playwright install chromium
   uv run pre-commit install
   ```

   The `browser` extra is required for the default `uv run pytest` suite because
   several unit tests import and patch `playwright.sync_api`. The command
   `uv sync --frozen --extra dev` installs the test tools, but not Playwright.

   **Adapter tests need their extras.** The MCP unit tests (`tests/unit/mcp/`)
   and REST suite (`tests/server/`) `importorskip` `fastmcp` / `fastapi`, so
   without `--extra mcp --extra server` they **silently skip** — you'll see a
   green run that never exercised the adapter surface. Add both extras
   (CI installs `--extra mcp --extra server --extra impersonate`) to run them.

   CI runs the same lint gate with `uv run pre-commit run --all-files`, so local hook results should match the `quality` job. The ordinary suite then runs in a reduced 7-cell compatibility matrix on every PR: Python 3.10–3.14 on Ubuntu, plus one Python 3.12 cell each on macOS and Windows. The full 15-cell matrix (all three OSes crossed with Python 3.10–3.14) runs nightly against one resolved commit before dedicated coverage and three live-E2E jobs: full Web on Ubuntu, full Android on macOS, and read-only Web on Windows. Manual nightly dispatches also run the full compatibility matrix by default; untick `run_compatibility` for a quick E2E-only rerun.

2. **Authenticate:**
   ```bash
   notebooklm login
   ```

3. **Create read-only test notebook** (required for E2E tests):
   - Create notebook at [NotebookLM](https://notebooklm.google.com)
   - Add multiple sources (text, URL, etc.)
   - Generate artifacts (audio, quiz, etc.)
   - Set env var: `export NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID="your-id"`

### Quick Reference

```bash
# Unit + integration tests (no auth needed)
uv run pytest

# Same suite in parallel — the full suite is ~10.6k tests and runs single-process
# by default (slow). pytest-xdist (a dev dep) cuts wall-clock to ~1–2 min. CI runs
# `-n auto --dist loadgroup`; loadgroup keeps @pytest.mark.xdist_group tests pinned
# to one worker. Mirror it locally when running the whole suite.
uv run pytest -n auto --dist loadgroup

# Fast local loop — skip repo-wide audit / release-gate checks (~40s saved).
# PR CI also skips the bulk repo_lint marker (of it, only node ids named in
# the critical-guard step run); the manual repo-lint job and nightly run the
# full marker. Run `make gates` before pushing a change that could trip one.
uv run pytest tests/unit tests/integration -m "not repo_lint"

# E2E tests (requires auth + test notebook)
uv run pytest tests/e2e -m readonly        # Read-only tests only
uv run pytest tests/e2e -m "not variants"  # Skip parameter variants
uv run pytest tests/e2e --include-variants # Includes costly cinematic-video generation

# Select a profile for E2E tests
uv run pytest tests/e2e -m e2e --profile work
```

The `repo_lint` marker tags cassette-shape lint, public-surface scans,
docstring/install-doc drift guards, version-sync, and CI-script audits.
These are valuable release/CI guardrails but cost ~30–45s locally. See
[`CONTRIBUTING.md`](../CONTRIBUTING.md#fast-local-loop-skip-repo-wide-audit-checks)
for the canonical fast-loop guidance.

### Testing across boundaries and against reality

The test pyramid has three distinct jobs:

1. **Unit/component tests** use deterministic collaborators and synthetic
   outputs to cover branches, error handling, and security contracts.
2. **Composition/replay tests** cross the real in-process boundary, such as
   CLI → client → RPC, REST → client → RPC, or MCP → client. VCR replay proves
   that the recorded wire shape is handled; it does not prove that a fixture
   reflects the current external service.
3. **Reality probes** run an external executable or environment that production
   code interprets. They are required for assumptions about subprocess output,
   browser/tool availability, platform behavior, or third-party strings.

Synthetic subprocess output is still appropriate for tier-1 tests. It is not
evidence that the real executable emits that output. The initial runtime seam
audit is intentionally bounded to production behavior that interprets external
results:

| Seam | Evidence/disposition |
| --- | --- |
| `NotebookLMClient.notebooks.list()` → RPC executor → decoder | `tests/integration/test_rpc_seam_matrix.py` asserts the RPC ID, encoded `f.req` envelope, CSRF form field, and typed adapter result; response RPC-ID drift fails through the same path. |
| CLI `source list` → source service → client | `tests/unit/cli/test_source.py::TestSourceList::test_source_list_composes_cli_service_and_client_boundary` invokes the real Click command and verifies the client-backed result, so fabricated command output cannot pass. |
| MCP `notebook_list` → app/client → RPC | `tests/integration/mcp_vcr/test_notebooks.py::test_mcp_notebook_list_crosses_adapter_to_client_boundary` drives an in-memory FastMCP client through the real client and an existing RPC cassette. |
| REST `POST /v1/notebooks/{id}/sources/url` → source service → client | `tests/server/test_integration_real_client.py::TestRestSeamMatrix::test_url_add_crosses_rest_to_client_boundary` drives the real FastAPI route, client, decoder, and response projection through an existing cassette. |
| `cli/services/playwright_login.py::ensure_chromium_installed` | The programmatic probe is checked against real Playwright, and a separate required Chromium launch smoke checks usability. |
| Playwright launch/error classification | Synthetic classification tests remain valid; add a reality probe only when claiming a specific third-party message shape. |
| `_auth/refresh.py` custom refresh command | Classification/security contract only; the command is operator-supplied, so there is no fixed third-party output contract. |
| Auth refresh composition (`AuthRefreshCoordinator` → `NotebookLMClient.refresh_auth` → `_auth/session.py`) | `tests/unit/test_auth_refresh_seam.py` crosses the production assembly with a deterministic homepage response; the existing VCR test covers the stale-RPC → homepage-refresh → retry path. No live reality probe is used because it would require mutable authenticated external state and would not provide a stable CI contract. |
| `_version_info.py` git lookup | Local-tool lookup with fallback behavior; synthetic and fallback tests are sufficient. |
| `scripts/` subprocesses | Audited `audit_public_api_compat.py`, `regen_baselines.py`, `audit_test_suite.py`, and `check_base_wheel.py`: each invokes a local developer/CI tool, not a product boundary. The wheel check creates isolated environments and exercises real package installation/import behavior; wrappers and workflow placement are covered by the CI audit tests. |

The bounded MCP seam-matrix slice is
[`tests/integration/mcp_vcr/test_notebooks.py::test_mcp_notebook_list_crosses_adapter_to_client_boundary`](../tests/integration/mcp_vcr/test_notebooks.py).
It drives the in-memory FastMCP client through the registered MCP tool, the
real `NotebookLMClient.notebooks.list()` service, RPC decoding, and the MCP
structured-content projection. VCR replays the existing notebook-list
recording, so this composition check uses no live credentials or network while
still failing if the adapter bypasses or mis-wires the client boundary.

The two Playwright probes are marked `reality` and named in the required-mode
contract. In the browser-enabled CI lane, run them explicitly and serially:

```bash
uv run pytest tests/unit/cli/test_playwright_login_coverage.py \
  -m reality --require-reality
```

`--require-reality` fails when an expected probe is missing, filtered out,
skipped, xfailed, errors during setup/teardown, or does not produce exactly one
passing call-phase result. Ordinary local runs retain actionable dependency
skips. The required lane is deliberately serial and rejects xdist, because the
controller must own complete collection and phase accounting. This distinction
is deliberate: a skipped reality probe must never look like evidence that the
external assumption was tested.

### Live authentication matrix

`scripts/live_auth_matrix.py` is the maintainer/release runner for live cookie,
master-token, recovery, RPC, REST, MCP, concurrency, and crash-safety checks. It
reads the selected source/browser profiles but performs every write inside a
disposable `NOTEBOOKLM_HOME`, scrubs credential values from its report, and
removes the temporary credential copies in a `finally` block. Its default cells
are unattended:

```bash
uv run --extra browser --extra cookies --extra headless --extra mcp --extra server \
  python scripts/live_auth_matrix.py \
  --profile <source-profile> \
  --browser 'chromium::Profile 3' \
  --account <account-email> \
  --output live-matrix.json
```

The three generic human-interaction cells are available but deliberately off by
default. Start an operator-owned Chrome/Chromium with a loopback-only CDP root,
then opt in. On X11, set `DISPLAY` and `XAUTHORITY` for both commands so every
headed window appears on the maintainer's desktop:

```bash
DISPLAY=:10 XAUTHORITY="$HOME/.Xauthority" google-chrome \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/notebooklm-live-cdp

DISPLAY=:10 XAUTHORITY="$HOME/.Xauthority" \
uv run --extra browser --extra cookies --extra headless --extra mcp --extra server \
  python scripts/live_auth_matrix.py \
  --profile <source-profile> \
  --browser 'chromium::Profile 3' \
  --account <account-email> \
  --include-interactive \
  --interactive-timeout 600 \
  --cdp-url http://127.0.0.1:9222 \
  --output live-matrix.json
```

The opt-in lane runs ordinary headed Playwright login, initial headed
master-token bootstrap, and CDP-attached master-token bootstrap in separate
disposable profiles. Each cell verifies the expected credential files and a
passive live request. The CDP cell probes the endpoint before and after login,
so it also proves the matrix did not close the operator-owned browser. Remote,
credential-bearing, query-bearing, and non-root CDP URLs are rejected before
any cell runs. `--interactive-timeout` is forwarded into each CLI browser wait;
the matrix gives the child process 30 additional seconds to report failure and
tear down. Workspace/SSO, regional-account, and long-duration-expiry cases remain
account-specific manual validation.

#### The extras above are load-bearing — especially `headless`

Run the matrix with the full set shown in the commands above
(`browser`, `cookies`, `headless`, `mcp`, `server`). They are not
interchangeable with the contributor install in
[CLAUDE.md](../CLAUDE.md), which deliberately mirrors CI's test job and
includes neither `cookies` nor `headless` — CI never runs this matrix.

`headless` (`gpsoauth`) is load-bearing. Without it, master-token cells now
stop with an actionable `MissingDependencyError`:

```text
Master-token auth needs gpsoauth. Install: pip install 'notebooklm-py[headless]'
```

The dependency fault bypasses `MasterTokenError` and the recovery rung's
ordinary `False` decline. MCP/REST adapters classify it as `DEPENDENCY`; the CLI
reports the actionable configuration error. A genuinely revoked token still
reports the authentication failure. This distinction is pinned by
[#2239](https://github.com/teng-lin/notebooklm-py/issues/2239).

Similarly, without `cookies` (`rookie-cookies`) the browser discovery/login and
browser-refresh cells cannot run at all; use `--skip-browser` rather than
reading their absence as a pass.

#### Where a cell's code lives

`scripts/live_auth_matrix.py` owns isolation, execution, and reporting only. The
realistic recovery cells — storage reload, sibling re-mint, master-token
fallback, REST recovery, MCP recovery, browser refresh, and the crash-safety
writer — live in `scripts/_live_auth_scenarios/` as ordinary modules and are
launched one per child process:

```bash
python -m scripts._live_auth_scenarios.<cell>
```

Each cell exports `async def scenario() -> ScenarioResult` plus a `main()`, and
prints exactly one JSON object on stdout (the orchestrator parses it verbatim).
Assertions go through `require(...)`, never `assert`, because these programs may
run under `python -O`. Adding or renaming a cell means updating
`SCENARIO_MODULES` in `tests/unit/test_live_auth_matrix.py`, which pins the
module list, its profile constants, and child-process importability.

To run one cell by hand against a disposable home:

```bash
PYTHONPATH="$PWD/src:$PWD" NOTEBOOKLM_HOME=/tmp/live-cell \
  uv run python -m scripts._live_auth_scenarios.storage_reload
```

These modules are excluded from the shipped package on purpose and are
type-checked by the `Run type checking` CI step alongside `src/notebooklm`.

### Selecting a profile for E2E tests

The E2E suite picks up the active NotebookLM profile from (highest precedence first):

1. `--profile <name>` pytest flag
2. `NOTEBOOKLM_PROFILE` environment variable
3. `default_profile` from `~/.notebooklm/config.json`
4. `default`

The auto-created notebook ID cache files
(`generation_notebook_id`, `multi_source_notebook_id`) are written under the
active profile directory (`~/.notebooklm/profiles/<name>/`), so each profile
keeps its own cache and never reuses notebook IDs from another Google account.

#### Notebook ID env vars are profile-agnostic

The notebook ID env vars (`NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID`,
`NOTEBOOKLM_GENERATION_NOTEBOOK_ID`, `NOTEBOOKLM_MULTI_SOURCE_NOTEBOOK_ID`)
are **not** profile-scoped — they're read as-is regardless of which profile
is active. If you set them in `.env` and switch profiles, the test will try
to access notebooks that don't exist in the other Google account.

**Recommendation:** leave the generation/multi-source env vars unset and let
the per-profile cache files handle it. Only `NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID`
needs to be set; if you switch profiles often, override it inline:

```bash
NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID=<work-nb-id> \
  uv run pytest tests/e2e -m e2e --profile work
```

### Disposable E2E copy qualification

Canonical live CI treats accounts as opaque slots (`A`, `B`, and `C`) and writable E2E notebooks
as disposable copies. Before deploying the workflow cutover, complete the external operations gate
in [issue #2331](https://github.com/teng-lin/notebooklm-py/issues/2331): configure the protected
Environment, qualify the template under each enabled account/backend, and start the repository
variable at `NOTEBOOKLM_CI_ACCOUNT_SLOTS=A`. Release verification must dispatch `rpc-health.yml`
and `verify-package.yml` with `--ref main`; release and feature refs cannot reach their protected
authenticated jobs.

The canonical template describes only state the copy API promises to preserve: ready sources and
Studio artifacts. Its checked-in shape is `tests/fixtures/e2e_template_contract.json`. Notes and
chat history are deliberately absent from that contract. Provisioning creates and validates those
on the disposable `reference` copy using
`tests/fixtures/e2e_prepared_role_contract.json`.

To qualify a template locally, use a dedicated profile and keep the template ID in the documented
environment variable so it never appears on the command line:

```bash
export NOTEBOOKLM_PROFILE=agent-e2e-slot-A-web
export NOTEBOOKLM_E2E_TEMPLATE_NOTEBOOK_ID='<template-id>'
export GITHUB_RUN_ID=1 GITHUB_RUN_ATTEMPT=1
export GITHUB_REPOSITORY=teng-lin/notebooklm-py
export RUNNER_TEMP="$(mktemp -d)"

uv run python scripts/manage_ci_e2e_notebooks.py validate \
  --backend web \
  --template-id-env NOTEBOOKLM_E2E_TEMPLATE_NOTEBOOK_ID \
  --contract tests/fixtures/e2e_template_contract.json

uv run python scripts/manage_ci_e2e_notebooks.py provision \
  --backend web \
  --template-id-env NOTEBOOKLM_E2E_TEMPLATE_NOTEBOOK_ID \
  --contract tests/fixtures/e2e_template_contract.json \
  --mode full --lane nightly-web-ubuntu --account-slot A \
  --manifest "$RUNNER_TEMP/notebooklm-e2e.json" \
  --github-env "$RUNNER_TEMP/github-env"

# Run pytest only after exporting the generated github-env entries.
uv run python scripts/manage_ci_e2e_notebooks.py cleanup \
  --backend web \
  --template-id-env NOTEBOOKLM_E2E_TEMPLATE_NOTEBOOK_ID \
  --manifest "$RUNNER_TEMP/notebooklm-e2e.json"
```

Cleanup is mandatory even when validation or pytest fails. Managed full mode publishes three
distinct role IDs (`reference`, `generation`, and `multi-source`); managed read-only mode publishes
only `reference`. Both publish the activation flag last. With that flag present, pytest never
consults profile cache files, creates a role notebook, cleans copied children on first use, or
deletes a workflow-owned copy during teardown. `temp_notebook` and other function-level CRUD
fixtures retain their existing lifecycle.

Before workflow cutover, maintainers must create and validate the immutable template, grant every
enabled account copy access, upload one independently revocable master-token secret per enabled
slot, set `NOTEBOOKLM_CI_ACCOUNT_SLOTS=A`, and audit the real `protected-readonly` Environment so
only `main` deployments are allowed. Add `B` and `C` only after each passes Web and Android
qualification. Slot aliases, never account emails or token fields, are the only identity recorded
in logs. Account-wide concurrency is designed to queue at most GitHub's documented 100 pending
jobs per slot; check that queue before bulk manual dispatch.

### Test Structure

```
tests/
├── unit/                            # No network, fast, mock everything
│   ├── app/                         # _app/ transport-neutral core
│   ├── cli/                         # CLI command tests
│   └── mcp/                         # MCP adapter unit tests (importorskip fastmcp)
├── server/                          # REST adapter suite — FastAPI routes (importorskip fastapi)
├── _guardrails/                     # Architecture/invariant gates (custom AST + filesystem lint)
├── _baselines/                      # Regenerable-baseline registry (ADR-0022): derive/store/compare
├── fixtures/
│   └── baselines/                   # Committed derived baselines (types_all.json, ungated_surface.json)
├── integration/                     # Mocked HTTP responses + VCR cassettes
│   ├── test_artifacts_integration.py # ArtifactsAPI integration
│   ├── test_artifacts_drift.py      # CREATE_ARTIFACT payload drift guard
│   ├── test_auth_refresh_vcr.py     # Auth refresh token VCR test
│   ├── test_auto_refresh.py         # Keepalive/refresh integration
│   ├── test_chat_delete_conversation_vcr.py
│   ├── test_chat_multi_source_vcr.py
│   ├── test_chat_passage_resolver.py
│   ├── test_cli_session_local.py
│   ├── test_download_multi_artifact.py
│   ├── test_error_paths_vcr.py      # Synthetic and VCR error paths
│   ├── test_get_summary_drift.py    # GET_NOTEBOOK_SUMMARY drift guard
│   ├── test_notebooks_integration.py # NotebooksAPI integration
│   ├── test_notes_integration.py     # NotesAPI integration
│   ├── test_notes_idempotency.py
│   ├── test_polling_vcr.py
│   ├── test_research_deep_poll_vcr.py
│   ├── test_research_idempotency.py
│   ├── test_save_chat_as_note_integration.py
│   ├── test_session_integration.py  # Client init + RPC plumbing
│   ├── test_settings_integration.py  # SettingsAPI integration
│   ├── test_settings_vcr.py
│   ├── test_sharing_integration.py   # SharingAPI integration
│   ├── test_sharing_vcr.py
│   ├── test_skill_packaging.py      # Packaging smoke (skills, entry-points)
│   ├── test_sources_integration.py   # SourcesAPI integration
│   ├── test_vcr_comprehensive.py    # End-to-end VCR walkthrough
│   ├── test_vcr_example.py          # VCR pattern reference
│   ├── test_vcr_real_api.py         # VCR against real-API cassettes
│   ├── cli_vcr/                     # CLI → Client → RPC VCR tests
│   ├── mcp_vcr/                     # MCP adapter VCR tier (replays CLI cassettes)
│   └── concurrency/                 # Cross-process / asyncio races
└── e2e/                             # Real API calls (requires auth; incl. test_mcp*.py, test_cli_live.py)
```

The `*_drift.py` tests are payload-shape canaries: they decode a recorded
RPC response (or assemble a synthetic one) and assert the live decoder still
produces the expected dataclass. They fail loudly when Google changes a
payload field, so the failure shows up here before users hit it.

### Architecture & invariant gates (`tests/_guardrails/`)

`tests/_guardrails/` holds the project's **custom lint gates** — pytest tests that
enforce architectural decisions a general-purpose linter can't express. They are
not style checks; each file encodes one project-specific invariant, usually the
executable half of an ADR ("enforce, don't document" — un-enforced consistency
is the failure mode this directory exists to prevent).

**What belongs here vs `tests/unit/`.** This directory is the home for a *pure*
gate — a file whose whole purpose is enforcing a repo-wide invariant, with no
module-under-test. A unit test that only *embeds* a boundary assertion among
behavioral checks stays in `tests/unit/` (see *Boundary Guardrails* above). Pure
architecture gates — e.g. `test_cli_boundary.py`, `test_cassette_shapes.py`,
`test_public_surface.py` — have been consolidated into this directory; the gate
halves of former hybrids live alongside them (e.g.
`test_public_surface_manifest.py`, `test_no_facade_reach_in.py`).

**How they differ from ruff / mypy.** Ruff and mypy run in the `quality` job and
enforce *generic* rules (style, unused imports, types) from a fixed catalogue.
The `tests/_guardrails/` gates are collected by the normal `uv run pytest` run and
enforce *bespoke* rules by doing their own analysis: most parse the source with
`ast.parse` (or scan files with regex / `rglob`), and some **import the module and reflect on
the live object** — something a purely-static linter cannot do.

A representative slice (run `ls tests/_guardrails/` for the full set):

| Gate | Enforces |
|---|---|
| `test_no_raw_positional_rpc_indexing.py` | No chained positional indexing (`x[0][9][3]`) of `batchexecute` payloads outside the sanctioned `_web/rows/` — the project's #1 fragility class |
| `test_rpc_method_ids_only_in_types.py` | Obfuscated RPC IDs live only in `rpc/types.py` (the source of truth) |
| `test_no_forbidden_monkeypatches.py` | The forbidden monkeypatch shapes under `tests/` (ADR-0007) |
| `test_no_inline_deprecation_warnings.py` | No inline `warnings.warn(..., DeprecationWarning)` outside `_deprecation.py` (ADR-0018) |
| `test_cli_rpc_envelope.py` | Every *RPC-touching* Click leaf command (call graph reaches `NotebookLMClient`) routes its errors into the JSON envelope |
| `test_module_size_ratchet.py` | No module grows past the size budget (ADR-0008) — a burn-down ratchet |
| `test_v080_release_gate.py` | The v0.8.0 breaking-change set flips in lockstep at the version bump |
| `test_adr_reference_format.py` | ADR references are 4-digit and resolve to a real `docs/adr/NNNN-*.md` |
| `test_cli_boundary.py` | CLI modules import only public `notebooklm` surface plus the single `_app` core exception — no direct `_browser`, other `notebooklm._*`, `notebooklm.rpc.*`, or `_private` reach-in |
| `test_no_facade_reach_in.py` | Feature APIs and service modules don't reach into Session internals or runtime-import facade APIs |
| `test_public_surface_manifest.py` | The documented public-import manifest + re-export identity pins for `notebooklm` / `auth` / `types` / shims stay intact |

**Conventions when adding a gate:**

- **One invariant per file**, with a module docstring that states the rule, *why*
  it matters (cite the ADR), and how a violation is fixed. The assertion message
  is the contributor's first — and often only — explanation, so make it
  actionable.
- **Make the detector a pure function and self-test it** against known good/bad
  inputs in the same file, so the gate can't silently become vacuous (a regex
  that matches nothing must fail its own self-test, not pass everything).
- **Shrink-only allowlists.** A gate that would fail on pre-existing violations
  may grandfather them in an allowlist — but it must be a *one-way ratchet* that
  only shrinks (e.g. `test_module_size_ratchet.py`,
  `tests/scripts/check_method_coverage.py`). The rule lands without a giant
  cleanup PR, and the gate fails when an allowlisted entry becomes clean so it
  gets removed. (One recorded exception:
  [ADR-0033](adr/0033-auth-consolidation-policy.md) sanctioned-merge entries
  under `src/notebooklm/_auth/`, where a consolidation may add or raise an
  annotated entry at its measured LOC — see that gate's own docstring.)
- **Scan yourself too.** A gate that shows the *wrong* form in its examples
  should use placeholders (or build them at runtime) rather than excluding its
  own file, so it still polices its own references
  (`test_adr_reference_format.py`).

Most gates are fast and run in the normal loop; the slow repo-wide cassette scan
(`test_cassettes_clean.py`) carries the `repo_lint` marker (see
[Quick Reference](#quick-reference)).

**Trade-off.** Because some gates import internals and reflect on them, they
couple more tightly to implementation than a static linter — a
behavior-preserving refactor can still trip one. That coupling is deliberate: it
catches architecture drift that ruff and mypy structurally cannot see.

### The wire contract (`test_wire_contract.py`)

`batchexecute` responses are positional JSON: index `3` carries no clue what it
means, so a wrong index is *silent* — it yields a plausible value of the wrong
thing. Worse, a unit test written from the same misunderstanding will confirm the
mistake rather than catch it. That has happened here: two constants named
"failed-artifact error text" and "nested error payload" actually pointed at
`sources` and `isPubliclyReadable`, and had passing tests built around the
imaginary shape.

The gate closes that loop using two recovered reference artifacts, both checked
into `docs/`:

| file | what it pins |
|---|---|
| `android/schema.proto` | 323 messages / 868 fields with real names and tag numbers |
| `android/enums.txt` | 104 library-scoped enum blocks (94 class names) / 2180 values with exact integers |

Both come from the official Android app, which speaks the *same backend messages*
over gRPC — where fields are tag-addressed rather than positional. The two line up
exactly: **JSON index `i` == protobuf tag `i + 1`**. That equivalence is what lets
a positional client be checked against a named schema.

Four checks run:

- **Declared mappings hold** — for each entry in `_wire_contract.MAPPINGS`,
  `constant == tag - 1`. Known-wrong entries are `xfail` with an issue reference;
  the fixing PR removes the marker.
- **Nothing escapes review** — every `_*_POS`-style constant in the scanned
  modules must appear in `MAPPINGS`, `UNMAPPED`, or `PINNED`. A new positional
  read cannot land without someone recording what it points at.
- **Pinned values stay frozen** — entries in `PINNED` read `addUnused()` slots the
  proto cannot confirm, so the check freezes the value rather than validating it.
  A change-detector, not a validation.
- **Enums agree** — client enum members match the recovered backend enum, and
  declared gaps stay honest.

**Adding a constant.** Three destinations, in descending order of strength:

| destination | when | what the test does |
|---|---|---|
| `MAPPINGS` | you can name the protobuf field it reads | asserts `constant == tag - 1` |
| `PINNED` | the proto has no name for the slot (`addUnused()`), but live evidence establishes the meaning | freezes the value; record the evidence |
| `UNMAPPED` | you don't know | nothing — but the reason is on the record |

*An honest `UNMAPPED`
entry is better than a guessed mapping* — a wrong mapping lends false confidence
to exactly the defect class this gate exists to catch. Several `UNMAPPED` reasons
carry hard-won warnings (e.g. one index collides with a block that is only safe
because rows happen to arrive in a particular order); read them before assuming an
index is fine.

Widening `_SCANNED_DIRS` / `_SCANNED_FILES` in the test is the intended way to
bring more of the client under the contract — the coverage check will then demand
a registry entry for each newly-visible constant.

**Caveats.** The proto declares several messages twice (wire vs the app's local
persistence schema, with *different* tags), so lookups take a section hint and
refuse ambiguous matches rather than guessing. And the upstream extractor emits
the literal placeholder `fieldType` where it could not recover a name — those
fields can only be pinned by tag, with provenance recorded in the mapping's note.

### Updating baselines

Some gates freeze a *snapshot* of a value the code already derives, so a public
surface change is a deliberate, diff-visible act. These **regenerable baselines**
(ADR-0022) are registered in `tests/_baselines/registry.py` and committed under
`tests/fixtures/baselines/` (plus the CLI contract at
`tests/fixtures/cli_contract_baseline.json`):

| Baseline | Derives from | Committed file |
|---|---|---|
| `types_all` | `notebooklm.types.__all__` | `tests/fixtures/baselines/types_all.json` |
| `ungated_surface` | collected `__all__` of each ungated public module | `tests/fixtures/baselines/ungated_surface.json` |
| `cli_contract` | `build_cli_contract()` | `tests/fixtures/cli_contract_baseline.json` |
| `auth_import_graph` | static direct imports under `notebooklm._auth` | `tests/fixtures/baselines/auth_import_graph.json` |
| `auth_patch_sites` | auth test patch-site audit | `tests/fixtures/baselines/auth_patch_sites.json` |
| `browser_import_graph` | package-aware imports under `notebooklm._browser` | `tests/fixtures/baselines/browser_import_graph.json` |
| `browser_patch_sites` | browser test patch-site audit | `tests/fixtures/baselines/browser_patch_sites.json` |
| `auth_facade_patch_sites` | public `notebooklm.auth` no-growth relocation sentinel | `tests/fixtures/baselines/auth_facade_patch_sites.json` |
| `auth_family_patch_scorecard` | combined full-joint auth/browser/facade scorecard | `tests/fixtures/baselines/auth_family_patch_scorecard.json` |
| `auth_shared_mutations` | class/singleton/process-default mutation audit | `tests/fixtures/baselines/auth_shared_mutations.json` |
| `module_size` | live over-budget and ADR-0033 shrink-locked LOC | `tests/fixtures/baselines/module_size.json` |
| `storage_transaction_policy` | AST-derived lock-policy callers | `tests/fixtures/baselines/storage_transaction_policy.json` |
| `guardrail_inline_literals` | grandfathered large guardrail literals | `tests/fixtures/baselines/guardrail_inline_literals.json` |

The freeze test `test_baseline_matches_committed_file` (in
`tests/_guardrails/test_public_surface_manifest.py`) asserts each committed file
equals `derive()`. When you intentionally change a public surface — e.g. add an
export to `notebooklm.types.__all__` or a CLI option — that test fails. Regenerate
the committed files in the **same PR**:

```bash
python scripts/regen_baselines.py
git diff tests/fixtures/baselines tests/fixtures/cli_contract_baseline.json
```

Shrink-only baselines reject weakening changes during ordinary regeneration.
After reviewing an intentional ceiling increase, new policy caller, or new large
guardrail literal, acknowledge it explicitly:

```bash
python scripts/regen_baselines.py --allow-growth
```

Auth mutation projections are schema-v2 shrink-only ratchets. Review full
package/target/path/lexical-owner rows and total counts alongside the
private-name column; a non-underscore target is not necessarily public API.
They intentionally implement a finite repository grammar, not general Python
dataflow or pytest fixture discovery: use explicit family imports, direct
mutation idioms, literal finite names/containers without unpacking, the
suite-used syntactic `list(...)` wrapper, and direct local helpers with statically
resolvable arguments. If a family-related target is reported as
unresolved, simplify it until the audit can resolve it. Do not use unsupported
control flow, dynamic registries, closure capture, or fixture/plugin discovery
to justify a lower measurement; retain the prior row unless the coupling was
removed in a supported, reviewable form. Proven-fresh or proven-non-family
targets remain excluded.
Authored scenario, lifecycle-cleanup, and coverage-allowance policies live in
`tests/fixtures/policies/` and are never emitted by baseline regeneration.
The separate **Auth Patch Audit** workflow runs on manual dispatch and is a
required reusable gate in `publish.yml`, so it runs once for every release but
not for ordinary pull requests. Manual runs compare the selected branch with
its merge-base on `main` (or an explicit `base_ref`); releases compare the tag
with the previous stable release tag. It uses isolated base/head workspaces, a
bounded auth/browser unit-test coverage slice (never integration or server
tests), pytest node/parameter/fixture-closure collection, and exact
statement/branch preservation.
The initial schema-v2 scorecard is `_auth` 244 (145 private-name), `_browser`
66 (17 private-name), public facade 526, and shared-lifetime mutation 188. The
private-package total is 310; the one-site increase over the legacy projection
is a formerly invisible, statically resolvable constant-named `_auth` patch,
not new test behavior.

Run the complete AST/path/contract guard suite locally with:

```bash
make gates
```

Review the diff — each changed line is the deliberate acknowledgement of the
surface change. Regen is **dev-only**: it shells `pytest … --update-baselines`,
which both the wrapper and the `update_baselines` fixture refuse to run when a
`CI` environment is detected. **CI never regenerates — it only diffs.**

### VCR Testing (Recorded HTTP)

VCR tests record HTTP interactions for offline, deterministic replay. We have two levels:

**Client-level VCR tests** (`tests/integration/test_vcr_*.py`):
- Test Python API methods directly
- Verify RPC encoding/decoding with real responses

**CLI VCR tests** (`tests/integration/cli_vcr/`):
- Test the full CLI → Client → RPC path
- Use Click's CliRunner with VCR cassettes
- Verify CLI commands work end-to-end without mocking the client

```bash
# Run all VCR tests
uv run pytest tests/integration/

# Run only CLI VCR tests
uv run pytest tests/integration/cli_vcr/
```

Sensitive data (cookies, tokens, emails) is automatically scrubbed from cassettes.

### Cassette recording

Maintainers re-record cassettes against the live API when an RPC payload
shape changes. Recording is opt-in (`NOTEBOOKLM_VCR_RECORD=1`) and requires
a valid `notebooklm login` session.

Two notebook env vars steer which notebook the recording session targets.
**Neither UUID is committed** — both are per-maintainer secrets (notebook IDs
are linkable to a Google account):

| Env var | Used by | Notebook role |
|---------|---------|---------------|
| `NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID` | read-heavy cassettes (`list`, `download`, `get`) | A maintainer-owned notebook pre-populated with sources + artifacts. Tests only READ from it. |
| `NOTEBOOKLM_GENERATION_NOTEBOOK_ID` | mutation/generation cassettes (`add source`, `generate`, `delete`) | A **separate** maintainer-owned notebook used only for destructive/generation flows, so the read-only notebook stays pristine. |

#### One-time setup — generation notebook

Run the setup script once per Google account that records cassettes:

```bash
uv run python tests/scripts/setup-generation-notebook.py
```

The script is idempotent: it reuses the notebook whose title matches
`GENERATION_NOTEBOOK_TITLE` (defined in `tests/scripts/setup-generation-notebook.py`)
if one already exists, otherwise creates it.
It prints the notebook UUID and an `export` line. Copy the export line into
your maintainer environment (e.g. `~/.zshrc` or a profile-specific `.env`
file you do NOT commit):

```bash
export NOTEBOOKLM_GENERATION_NOTEBOOK_ID=<printed-uuid>
```

The script is a manual maintainer helper — CI never runs it.

#### Recording a cassette

```bash
# Re-record (or record-new) cassettes; sensitive data auto-scrubbed
NOTEBOOKLM_VCR_RECORD=1 uv run pytest tests/integration/test_vcr_*.py -v
```

Cassettes are stored per client tier. VCR's `cassette_library_dir` is
`tests/cassettes/web/`, so recordings land there automatically and cassette
names in test code stay tier-free (`use_cassette("notebooks_list.yaml")`).
Android gRPC captures live alongside in `tests/cassettes/android/` and are
replayed through a different seam. See
[tests/cassettes/README.md](../tests/cassettes/README.md) for the full layout
and naming convention.

> **Recording reads your real `~/.notebooklm` profile.** Normally the suite
> pins `NOTEBOOKLM_HOME` at a throwaway tmp dir (autouse `_isolate_notebooklm_home`
> in `tests/conftest.py`) so runs are reproducible and never touch your real
> profile. Under `NOTEBOOKLM_VCR_RECORD=1`, `@pytest.mark.vcr` tests instead read
> the real profile — so `get_vcr_auth()` (via `AuthTokens.from_storage()`) and
> the CLI auth path resolve live credentials to record against. CLI-VCR tests
> additionally skip their `mock_auth_for_vcr` patch in record mode for the same
> reason. Replay runs and non-VCR tests stay isolated (a stray
> `NOTEBOOKLM_VCR_RECORD` on a normal run never un-isolates a non-VCR test). The
> test must carry the `vcr` marker — most do via a module-level `pytestmark`.
> Before #1263 this deferral did not exist and cassettes could only be recorded
> with a standalone script.
>
> **Limitation:** a few cli_vcr tests (`settings` / `profile` / `doctor`)
> re-pin `NOTEBOOKLM_HOME` at their own tmp dir to isolate config/profile
> writes. They override this deferral and so are not auto-recordable through
> pytest — re-record those with a standalone script (or, as a future
> enhancement, inject `NOTEBOOKLM_AUTH_JSON` from the real storage so auth is
> resolved independently of `NOTEBOOKLM_HOME`).

The scrubbing pipeline (`tests/vcr_config.py`) redacts cookies, CSRF tokens,
emails, and other sensitive patterns before the cassette hits disk. Verify
the result with the cassette guard before committing:

```bash
# Verify recorded cassettes are clean of credentials
uv run python tests/scripts/check_cassettes_clean.py
```

#### Long-running recordings (deep research, multi-minute polling)

Recording a cassette that polls a multi-minute server-side operation — the Deep
Research lifecycle (`test_research_deep_poll_vcr.py`) is the canonical example —
hits a few non-obvious snags. Lessons from the v0.8 full-lifecycle re-record
(PR #1566):

- **`httpx.PoolTimeout` after ~15–20 min of idle polling.** The default
  `ConnectionLimits(keepalive_expiry=30.0)` keeps an idle pooled connection
  around long enough to be silently dropped server-side, and the next acquire
  stalls. In **record mode only**, build the client with a shorter keepalive and
  a generous read timeout:
  `NotebookLMClient(auth, timeout=60.0, limits=ConnectionLimits(keepalive_expiry=10.0))`.
  Note `async_client_factory` is **not** a public constructor kwarg — use the
  public `timeout=` / `limits=` seams.
- **`pytest-timeout` kills the run.** The global per-test timeout aborts a
  ~30-min recording. Mark the recording test `@pytest.mark.timeout(3600)`.
- **`start()` task_id ≠ the poll-reported task_id.** Deep Research's kickoff id
  is not the id `POLL_RESEARCH` echoes back, so a filtered
  `research.poll(task_id=…)` returns `NOT_FOUND` every poll. The record loop must
  mirror `wait_for_completion`: first poll unfiltered, then pin the
  *poll-reported* id.
- **Trim with a byte-exact text slice, not `yaml.safe_dump`.** Long deep-research
  poll bodies accumulate large markdown, so trim redundant middle `in_progress`
  polls to stay under the cassette size cap. Re-serializing via `yaml.safe_dump`
  re-wraps long scalars and breaks Windows YAML parsing (CI catches it) — slice
  the VCR-native YAML text instead.
- **Cleanliness is necessary-not-sufficient.** After recording, run the cassette
  guard (above) *and* manually grep the new file for live cookie/token/email
  shapes (`SID` / `HSID` / `SAPISIDHASH` / Bearer / the account email) — the
  name-anchored scrubber can miss credentials in un-allowlisted fields.

#### Synthetic error cassettes

> [!WARNING]
> **Error cassettes generated through this plumbing are SYNTHETIC.** They
> validate the client's exception-mapping branches (`RateLimitError`,
> `ServerError`, the auth-refresh path), NOT Google's actual error response
> shapes. If you need to validate a real-world error shape, capture a live
> recording instead — these synthetic shapes are intentionally minimal.

The `NOTEBOOKLM_VCR_RECORD_ERRORS` env var opts a recording session into
substituting the next outgoing batchexecute RPC with a synthetic error
response. Three modes are supported:

| Mode            | HTTP status | Maps to                                         |
|-----------------|-------------|-------------------------------------------------|
| `429`           | 429         | `RateLimitError` (after retry budget exhausted) |
| `5xx`           | 500         | `ServerError`   (after retry budget exhausted) |
| `expired_csrf`  | 400         | auth-refresh path (NotebookLM uses 400, not 401)|

The plumbing has three opt-in layers:

1. **Env var**: `NOTEBOOKLM_VCR_RECORD_ERRORS=<mode>` activates the
   `ErrorInjectionMiddleware` in the middleware chain (the env var is
   consulted when the client opens).
2. **Pytest marker**: `@pytest.mark.synthetic_error("<mode>")` sets the env
   var for the duration of a single test (auto-reverted on teardown). Note
   that the `synthetic_error` marker is registered dynamically in
   `tests/conftest.py:149` (rather than statically listed in `pyproject.toml`).
3. **Filename prefix**: cassettes recorded under this mode MUST be named
   `error_synthetic_<mode>_<slug>.yaml` — use
   `tests.cassette_patterns.synthetic_error_cassette_name(mode, slug)` to
   build the filename so reviewers can tell synthetic shapes apart from
   real recordings at a glance.

Example recording session (this is the workflow a maintainer uses to
record the actual error cassettes — the transport-wrapper module itself
ships only the plumbing):

```bash
NOTEBOOKLM_VCR_RECORD=1 \
NOTEBOOKLM_VCR_RECORD_ERRORS=429 \
  uv run pytest tests/integration/test_error_paths_vcr.py
```

Production behavior is unchanged when `NOTEBOOKLM_VCR_RECORD_ERRORS` is
unset — the transport wrapper is only constructed when the env var resolves
to a recognized mode, and a typo'd value resolves to `None` (the recording
session continues without substitution).

### Per-method RPC coverage gate

`tests/scripts/check_method_coverage.py` enforces, on every PR, that each
member of `RPCMethod` has **both**:

1. **A test reference** — at least one file under `tests/` (excluding the
   gate script itself) mentions the enum member by its qualified name
   (`RPCMethod.LIST_NOTEBOOKS`) OR by its raw RPC id string value
   (`"wXbhsf"`).
2. **A cassette covering the RPC id** — at least one cassette YAML under
   `tests/cassettes/web/` contains the RPC id string in its body.

The gate is a pure-text static check (no pytest, no network) and runs in the
`quality` job of `test.yml`.

**Adding a new `RPCMethod`?** Ship it with:
- a unit or integration test that imports the enum member (or asserts on its
  raw id), AND
- at least one cassette whose recorded request/response body contains the
  RPC id.

**Pre-existing gaps.** A small `PREEXISTING_GAPS` set inside the script can
grandfather methods that lacked coverage when the gate first landed. It is
currently empty. The set is a **one-way ratchet** — it must not grow. When
you backfill coverage for a grandfathered method, delete its entry from
`PREEXISTING_GAPS` in the same PR. The gate fails when a stale
`PREEXISTING_GAPS` entry has acquired full coverage so maintainers remove it.

```bash
# Run locally before pushing changes that touch RPCMethod
uv run python tests/scripts/check_method_coverage.py
```

### E2E Fixtures

| Fixture | Use Case |
|---------|----------|
| `read_only_notebook_id` | List/download existing artifacts |
| `temp_notebook` | Add/delete sources (auto-cleanup) |
| `generation_notebook_id` | Generate artifacts (CI-aware cleanup) |

### Rate Limiting

NotebookLM has undocumented rate limits. Generation tests may be skipped when rate limited:
- Use `uv run pytest tests/e2e -m readonly` for quick validation
- Wait a few minutes between full test runs
- `SKIPPED (Rate limited by API)` is expected behavior, not failure

### Writing New Tests

```
Need network?
├── No → tests/unit/
├── Mocked → tests/integration/
└── Real API → tests/e2e/
    └── What notebook?
        ├── Read-only → read_only_notebook_id + @pytest.mark.readonly
        ├── CRUD → temp_notebook
        └── Generation → generation_notebook_id
            └── Parameter variant? → add @pytest.mark.variants
```

---

## Logging and observability

### Levels — when to emit what

- **WARNING** — data loss, protocol drift, schema mismatch, unexpected non-2xx that isn't auth-recoverable. Actionable.
- **INFO** — coarse-grained lifecycle events (login complete, profile switched). Rare in library code; CLI uses INFO for user-facing progress.
- **DEBUG** — expected fallbacks, hot-path parser branches, polling status, request/response metadata. Off by default; enable via `NOTEBOOKLM_LOG_LEVEL=DEBUG` or `notebooklm -vv`.
- **Silent + comment** — best-effort discovery loops (browser cookie scan, alternative profile locations). `except` body is `pass` or `continue` with a single-line `# best-effort: <what we tried>` comment.

### Credential redaction

The package handler installed by `configure_logging()` has a `RedactingFilter` attached. It runs for every record reaching the handler, including records originating in child loggers (`notebooklm._rpc_executor`, `notebooklm._web.transport.errors`, `notebooklm._chat`, etc.) via Python logging's default propagation. The filter scrubs:

- CSRF tokens (`at=...`)
- Session IDs (`f.sid=...`)
- Google session cookies (`SAPISID`, `SID`, `HSID`, `SSID`, `__Secure-1PSID`, `__Secure-3PSID`)
- `Authorization: Bearer <token>` headers
- `Cookie: <jar>` headers

The filter pre-renders `record.exc_info` traceback into a scrubbed `record.exc_text` while preserving `record.exc_info` itself. The live exception object is not mutated.

To add a new secret pattern: edit `_REDACT_PATTERNS` in `src/notebooklm/_logging.py` and add a unit test in `tests/unit/test_logging.py` before merging.

### Attaching your own handler

`notebooklm` propagates to root by default, so `caplog`, `basicConfig`, and similar workflows work without configuration. To capture notebooklm logs in a dedicated handler:

```python
import logging
from notebooklm._logging import apply_redaction

handler = logging.handlers.SysLogHandler(...)
apply_redaction(handler)
logging.getLogger("notebooklm").addHandler(handler)
```

`apply_redaction()` attaches the `RedactingFilter` and wraps the formatter so your handler also benefits from credential scrubbing.

### Style — always lazy formatting

Use `%`-style log calls, not f-strings:

```python
logger.warning("Failed for %s in %.2fs", name, elapsed)  # OK
logger.warning(f"Failed for {name} in {elapsed:.2f}s")  # BAD
```

f-string eager evaluation defeats lazy formatting and (although the filter would still scrub via `record.getMessage()`) makes profile-time cost unconditional.

### Third-party loggers

`httpx`, `urllib3`, and `asyncio` can emit at DEBUG with full URLs and headers containing notebooklm-py credentials.

For `httpx` and `urllib3`, `configure_logging()` (run automatically at package import) attaches a *logger-level* `RedactingFilter` to each. That filter runs before records propagate to ancestor loggers, so a library consumer who enables those loggers via `logging.basicConfig(level=logging.DEBUG)` gets credential-scrubbed request URLs and headers with no extra setup — and without notebooklm-py adding any handler of its own to those loggers.

If you also want those loggers to *emit* through notebooklm-py's default handler (the CLI does this when `-vv` is set), call `install_redaction`, which adds both the filter and a default StreamHandler:

```python
from notebooklm.log import install_redaction

install_redaction("httpx", "urllib3")
```

To cover additional third-party loggers (e.g. `asyncio`) or libraries that set `propagate=False` on internal loggers (rare), pass the names explicitly:

```python
install_redaction("asyncio")
install_redaction("httpx._client", "urllib3.connectionpool")
```

### Trade-offs

The `RedactingFilter` preserves `record.exc_info` (the live exception object) so handlers like Sentry can still access it. However:

- Standard `logging.Formatter` uses `record.exc_text` (scrubbed by our filter) and does NOT re-render from `exc_info`. Safe.
- Custom formatters that ignore `exc_text` and read `exc_info` directly may render an unredacted traceback. **Mitigation**: wrap such handlers with `apply_redaction()` so the formatter is decorated and post-scrubs the final output regardless of which exception attribute it reads.
- Records propagate to root by default (`notebooklm.propagate = True`) so `caplog` and `basicConfig` work without changes. Our filter mutates the record before propagation, so downstream handlers (including root's) see the scrubbed version. **Caveat**: if a user attaches an unredacted handler directly to a child logger (`notebooklm._rpc_executor`), that handler fires *before* propagation reaches our parent handler. Mitigation: `apply_redaction(child_handler)`.
- Applications that want notebooklm logs *isolated* from root can set `logging.getLogger('notebooklm').propagate = False` themselves.

---

## CI/CD

### Workflows

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `test.yml` | Push/PR | Reduced 7-cell compatibility matrix (Ubuntu × Python 3.10–3.14, plus macOS/Windows on 3.12), linting, type checking |
| `nightly.yml` | Daily 6 AM UTC (`main`), manual dispatch on `main` | Full compatibility/coverage plus managed-copy full Web/Ubuntu, full Android/macOS, and read-only Web/Windows E2E; the full Web lane settles its generation journal before cleanup |
| `rpc-health.yml` | Daily 7 AM UTC (`main`), manual dispatch on `main` | RPC monitoring on a disposable fallback copy plus the template-read-only [Android gRPC canary](#android-grpc-canary) |
| `testpypi-publish.yml` | Manual dispatch | Publish to TestPyPI |
| `verify-package.yml` | Manual dispatch on `main` | Verify TestPyPI or PyPI install plus managed-copy E2E; artifact inventory is advisory |
| `publish.yml` | Tag push | Publish to PyPI |

The test and both publish workflows build the wheel and run
`scripts/check_base_wheel.py`. The smoke inspects wheel contents and metadata,
then proves in clean environments that the base install imports and exposes CLI
help without Playwright while installing the same wheel's `[browser]` extra does
provide Playwright. `verify-package.yml` repeats the check against the exact
wheel downloaded from the selected package index before installing the broader
verification extras.

#### Android gRPC canary

`rpc-health.yml` also runs a second, lightweight job, `android-grpc-health`, on
the same schedule and `workflow_dispatch`. It runs `scripts/android_grpc_canary.py`,
a read-only probe of the private Android gRPC backend kept deliberately separate
from the heavy Android E2E lane in `nightly.yml`. The script opens
`NotebookLMClient.from_storage(backend="android")`, mints a bearer and forces one
refresh (the generation must advance, and a re-mint that fails is the distinct
`FAIL bearer refresh-mint …` verdict), calls unary `GetProject` and
`ListChatSessions` through the public API, then re-issues both raw — `GetProject`
decoded with the full recovered `GetProjectResponse`, as `sources.list` does —
and prints per RPC a `SHAPE <rpc> <sha256>` fingerprint of the populated
`(field_path, wire_type)` set (no values, ids, or text, so it is comparable
across runs) and an `UNKNOWN <rpc> <n>` count of fields the recovered schema does
not declare. A non-zero count is the steady state (the app protos are a subset of
what the server sends; `GetProject` carries dozens), so drift is judged against a
hand-authored baseline, not zero: the job passes
`--baseline tests/fixtures/android/canary_baseline.json`
(`{"<rpc>": {"shape": "<hex>", "unknown_fields": <n>}}`), and any RPC whose live
shape or count differs prints `FAIL schema <rpc> … live=… baseline=…`. Until that
file exists the job prints `WARN baseline missing …`, runs in diagnostic mode
(green), and raises a `::warning::` annotation on the run so the inert drift
check stays visible; author the file by hand from a reviewed run's
`SHAPE`/`UNKNOWN` lines in the step summary. The bootstrap is bounded: the job
passes `--missing-baseline-grace-until 2026-09-14`, after which a missing
baseline is a `FAIL`, so the drift check cannot stay inert indefinitely. Transport or auth failures always
`FAIL`. Every emitted line has the probed notebook id replaced with
`<notebook-id>` and exception text scrubbed, so the summary carries no ids.
`GenerateFreeFormStreamed` is the backend's only unary-stream RPC and it writes a
chat turn, so the canary never calls it. Run it locally against your own profile:

```bash
NOTEBOOKLM_PROFILE=<profile> NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID=<id> \
  uv run python scripts/android_grpc_canary.py \
    --baseline tests/fixtures/android/canary_baseline.json
```

It never writes the baseline, fixtures, cassettes, or the evidence ledger; a
changed `SHAPE`/`UNKNOWN` value is a prompt to re-capture, review, and update the
baseline by hand, not something the canary fixes.

### Managed-copy live CI

Canonical CI never points pytest or full RPC health at the immutable template.
Each authenticated lane selects one opaque account slot, materializes only that
slot's master token, and creates role-specific copies. Full lanes prepare
separate `reference`, `generation`, and `multi-source` copies; Web RPC health
uses one `rpc` fallback copy. The Android RPC canary is the only lane that reads
the template directly, and it performs no notebook mutation.

The contracts are checked in at `tests/fixtures/e2e_template_contract.json` and
`tests/fixtures/e2e_prepared_role_contract.json`. Provisioning seeds the
reference copy's note and persisted Q&A, removes inherited writable children
from the other roles, and publishes the managed fixture bindings only after all
roles pass. Cleanup runs under `always()` after verification, while the next
selected run also sweeps owned, reserved-prefix copies older than 24 hours.

For a local managed-copy run, use an isolated profile and runner-style scratch
directory. Never use the template ID as a pytest fixture binding:

```bash
export NOTEBOOKLM_PROFILE=agent-e2e-local
export RUNNER_TEMP="$(mktemp -d)"
export NOTEBOOKLM_E2E_TEMPLATE_NOTEBOOK_ID=<template-id>
export GITHUB_RUN_ID=1 GITHUB_RUN_ATTEMPT=1
export GITHUB_REPOSITORY=teng-lin/notebooklm-py
uv run python scripts/manage_ci_e2e_notebooks.py provision \
  --backend web --template-id-env NOTEBOOKLM_E2E_TEMPLATE_NOTEBOOK_ID \
  --contract tests/fixtures/e2e_template_contract.json --mode full \
  --lane verify-package --account-slot A \
  --manifest "$RUNNER_TEMP/notebooklm-e2e.json" --github-env "$RUNNER_TEMP/e2e.env"
set -a; source "$RUNNER_TEMP/e2e.env"; set +a
NOTEBOOKLM_E2E_GENERATION_JOURNAL_MODE=off uv run pytest tests/e2e -m "not variants"
uv run python scripts/manage_ci_e2e_notebooks.py cleanup \
  --backend web --template-id-env NOTEBOOKLM_E2E_TEMPLATE_NOTEBOOK_ID \
  --manifest "$RUNNER_TEMP/notebooklm-e2e.json"
```

Local runs without `NOTEBOOKLM_E2E_MANAGED_COPIES=1` retain the existing
environment/profile-cache/auto-create behavior described above.

#### Account pool setup

The `protected-readonly` Environment contains one independent opaque token per
enabled slot and one template handle:

```text
NOTEBOOKLM_MASTER_TOKEN_JSON_A
NOTEBOOKLM_MASTER_TOKEN_JSON_B
NOTEBOOKLM_MASTER_TOKEN_JSON_C
NOTEBOOKLM_E2E_TEMPLATE_NOTEBOOK_ID
```

Bootstrap each authorized account in a separate local profile and upload its
token without printing it. Upload the template ID through stdin as well:

```bash
NOTEBOOKLM_PROFILE=ci-bootstrap-a notebooklm login --master-token --account <account-a>
gh secret set --repo teng-lin/notebooklm-py --env protected-readonly \
  NOTEBOOKLM_MASTER_TOKEN_JSON_A \
  < ~/.notebooklm/profiles/ci-bootstrap-a/master_token.json
printf '%s' "$NOTEBOOKLM_E2E_TEMPLATE_NOTEBOOK_ID" | \
  gh secret set --repo teng-lin/notebooklm-py --env protected-readonly \
  NOTEBOOKLM_E2E_TEMPLATE_NOTEBOOK_ID
gh variable set --repo teng-lin/notebooklm-py NOTEBOOKLM_CI_ACCOUNT_SLOTS --body A
```

Qualify A through Web/Ubuntu, Android/macOS, and read-only Web/Windows before adding B,
then C. The ordered repository variable accepts only `A`, `B`, and `C`; start at `A`, then
use `A,B` and finally `A,B,C` after the documented soak. Do not reorder it to
prefer an account. Manual dispatch input `account_rotation_base` overrides the
daily base, while stable lane offsets still shard matrix lanes.

Every authenticated job uses `notebooklm-account-<slot>` concurrency with
`queue: max` and no cancellation. This queue has GitHub's documented maximum of
100 waiting jobs per group; check queued account jobs before a bulk manual
dispatch. It is not an unbounded durable queue.

The full Web/Ubuntu nightly producer uses one runner-temp append-only journal across the
primary pytest process, the cooldown retry, and the in-lane verifier. Filtered
nightly runs, the Android/macOS lane, the read-only Windows lane, and Verify Package
explicitly set journal mode to `off`. The removed detached `verify-artifacts.yml` workflow and its persistent
generation notebook are no longer part of CI.

An unfiltered nightly run includes all three live lanes. A manual dispatch with
`test_filter` runs the requested node only on the two full lanes and omits the Windows
read-only lane, because an arbitrary node ID is not guaranteed to carry the `readonly` marker.

#### Operations runbooks

- Disable a failing slot by removing it from `NOTEBOOKLM_CI_ACCOUNT_SLOTS`;
  never rewrite the variable from a workflow. Re-bootstrap and replace only
  that slot's Environment secret for authentication failures.
- Rotate the template by creating a replacement, validating copies under every
  enabled slot and both backends, then replacing the template secret. Keep the
  previous template until one scheduled cycle succeeds; never mutate it in CI.
- For cleanup alerts, account for the lane offset when choosing
  `account_rotation_base`. In the ordered enabled-slot list, use the affected
  slot for offset-0 lanes (`nightly-web-ubuntu` and `verify-package`), the
  previous slot cyclically for `nightly-android-macos` (offset 1), two slots
  before it for `nightly-readonly-windows` and `rpc-health-web` (offset 2), and
  three slots before it for
  `rpc-health-android` (offset 3). With only one enabled slot every base is that
  slot. Confirm the intended slot in the workflow's account-plan summary before
  its authenticated job starts. Inspect account-owned titles under the exact
  `notebooklm-py-ci/` prefix in the UI for resources omitted by recent-project
  listing; delete only after checking exact title and ownership.
- For broad lifecycle failure, set the slot variable back to `A` or disable the
  scheduled workflows. During the seven-day rollback window, revert the pool
  cutover commit to restore the legacy workflows; legacy secrets remain stored
  but unused until post-soak cleanup.

### Workflow secret gates

Every generic workflow secret is protected by an approved Environment or an
allowlisted actor/ref gate. Pool credentials are stricter: every live job must
simultaneously bind literal `environment: protected-readonly`, test
`github.repository == 'teng-lin/notebooklm-py'`, and consume
`needs.resolve-target.outputs.is_standard == 'true'`. The resolver accepts only
`refs/heads/main`, publishes its immutable SHA, and every planner and
secret-bearing checkout uses that SHA.

| Gate | Where | Mechanism |
|------|-------|-----------|
| `environment: protected-readonly` | Job-level | GitHub Environment hosting the canonical secret values. Bind it **unconditionally** (`environment: protected-readonly`) so every trigger — scheduled `cron` and `workflow_dispatch` alike — sees the same secret. **Note:** the earlier conditional form (`${{ github.event_name == 'workflow_dispatch' && 'protected-readonly' \|\| '' }}`) silently broke scheduled crons once the secrets stopped existing at repo level (issue #1009); the same env binding is now the single source of truth. If you want to block `workflow_dispatch` behind manual approval, add a **required reviewers** rule on the environment — but be aware scheduled runs will then queue at the same gate. |
| `needs.resolve-target.outputs.is_standard == 'true'` plus canonical repository equality | Job-level `if:` | Pool jobs accept only a resolved `refs/heads/main`; releases, tags, and feature refs cannot reach the Environment job. |
| `github.event.sender.login == 'teng-lin'` | Job-level `if:` | Pin webhook-triggered workflows (e.g. `claude.yml`) to a specific maintainer actor. Any other actor's trigger never reaches the secret-bearing steps. |

`scripts/check_workflow_secret_gates.py` (wired into the `test.yml` quality job)
asserts the generic gate for every `secrets.*` reference except
`secrets.GITHUB_TOKEN`. For `NOTEBOOKLM_MASTER_TOKEN_JSON` and the template ID,
it additionally requires both pool gates, step scope, dynamic secret detection,
and no more than one selected master token per job. The checker rejects the
legacy bundled token name and misspelled/dynamic Environment bindings.

The checker also **rejects the conditional `environment:` shape** outright:

```yaml
# REJECTED (silently broke #1009 once the secret was migrated env-only)
environment: ${{ github.event_name == 'workflow_dispatch' && 'protected-readonly' || '' }}

# REQUIRED — unconditional binding
environment: protected-readonly
```

The empty-string fallback in the expression form means "no environment", so
secrets that live only in environments resolve to empty under that branch.
Binding the environment unconditionally is the single source of truth.

The account selector constructs only an allowlisted
`NOTEBOOKLM_MASTER_TOKEN_JSON_<slot>` name. GitHub resolves the dynamic
`secrets[...]` expression only at the materializer step, so the runner never
receives the other slot tokens. Token and template values never cross job
outputs, matrices, caches, artifacts, or summaries.

#### One-time GitHub Environment setup

The `protected-readonly` environment must be configured in the GitHub repository
settings before any workflow that references it can safely use the pool. Its
exact-`main` deployment policy is mandatory; required reviewers are optional
because they also pause scheduled runs.

> **Important — silent auto-creation**: GitHub Actions silently creates a
> referenced environment that doesn't exist, with **no protection rules**, the
> first time a workflow references it. A typo in the environment name (e.g.
> `protectd-readonly`) or a never-configured environment would therefore
> bypass GitHub-side branch and reviewer policy even though the workflow YAML
> appears to gate on it. The static checker `scripts/check_workflow_secret_gates.py`
> pins the accepted environment names to an explicit allow-list
> (`_APPROVED_ENVIRONMENTS`) to prevent typos from passing CI — but the
> *runtime* gate still depends on the manual setup below being done correctly.
> Inventory the actual Environment rules and confirm its only allowed deployment
> branch is `main` before enabling the pool.

This is a manual UI/API step — Pull Requests cannot create environments on
their own.

1. Open the repository on GitHub and navigate to
   **Settings → Environments → New environment**.
2. Name the environment **`protected-readonly`** (exact spelling — the workflow
   YAML files match this string verbatim, and the checker enforces the same
   spelling).
3. Restrict deployment branches/tags to a custom rule matching exactly
   `main`; do not use a `release/*` wildcard.
4. Review whether Required reviewers is appropriate for scheduled runs (it
   queues schedules too) and leave the wait timer at zero.
5. Save. The environment is ready for `verify-package.yml`, `rpc-health.yml`,
   and `nightly.yml`.
6. **Smoke-test the gate.** Dispatch one of the workflows above on `main` and
   verify the live job records a `protected-readonly` deployment. If required
   reviewers are configured, also confirm it pauses at "Waiting for review".
   Dispatching a feature/tag ref must stop in `resolve-target` before any
   Environment job is created. Do not enable the pool until both checks pass.

For automation-driven setup (e.g. infrastructure-as-code), the same configuration
can be applied via the GitHub REST API:

```bash
gh api -X PUT \
  /repos/teng-lin/notebooklm-py/environments/protected-readonly \
  -F wait_timer=0 \
  -F prevent_self_review=false \
  -F deployment_branch_policy[protected_branches]=false \
  -F deployment_branch_policy[custom_branch_policies]=true
gh api -X POST \
  /repos/teng-lin/notebooklm-py/environments/protected-readonly/deployment-branch-policies \
  -f name=main -f type=branch
```

#### Adding a new secret-bearing workflow

When introducing a workflow that touches `secrets.*`:

1. Pick the gate shape that matches the trigger surface:
   - Pool authentication → literal Environment plus canonical repository and
     exact-main `is_standard` job gates; resolve and checkout the SHA before
     dynamically selecting one step-scoped token.
   - Other `workflow_dispatch` only → job-level `environment: protected-readonly`.
   - `workflow_dispatch` + `schedule` → also job-level `environment: protected-readonly`
     (unconditional — issue #1009). Pair with an upstream `is_standard`
     gate so a non-maintainer's feature-branch dispatch can't reach the
     secret-bearing job at all.
   - Webhook-triggered (`issue_comment`, etc.) → job-level `if:` pinning
     `sender.login` to the maintainer.
   - Multi-branch CI (`push`, `pull_request`, nightly) → step-level `if:`
     referencing an upstream `is_standard` output.
2. Run `python scripts/check_workflow_secret_gates.py` locally to verify the
   gate is recognised.
3. If the new workflow references the `protected-readonly` environment for
   the first time, **double-check the Environment exists** (see "One-time
   GitHub Environment setup" above). GitHub Actions will **silently
   auto-create** a referenced environment that doesn't exist, **with no
   protection rules**, so a never-configured `protected-readonly`
   environment would let the workflow run without any approval gate —
   exactly the opposite of what the YAML implies. The static checker
   rejects unapproved *names* via `_APPROVED_ENVIRONMENTS`, but it cannot
   verify that GitHub-side configuration has actually been applied; that
   verification is the maintainer's responsibility per the smoke-test
   step in "One-time GitHub Environment setup".

### Troubleshooting CI/CD Auth

**First step:** Run `notebooklm auth check --json` in your workflow to diagnose issues.

#### "NOTEBOOKLM_AUTH_JSON environment variable is set but empty"

**Cause:** The `NOTEBOOKLM_AUTH_JSON` env var is set to an empty string.

**Solution:**
- Ensure the GitHub secret is properly configured
- Check the secret isn't empty or whitespace-only
- Verify the workflow syntax: `${{ secrets.NOTEBOOKLM_AUTH_JSON }}`

#### "must contain valid Playwright storage state with a 'cookies' key"

**Cause:** The JSON in `NOTEBOOKLM_AUTH_JSON` is missing the required structure.

**Solution:** Ensure your secret contains valid Playwright storage state JSON:
```json
{
  "cookies": [
    {"name": "SID", "value": "...", "domain": ".google.com", ...},
    ...
  ],
  "origins": []
}
```

#### "Cannot run 'login' when NOTEBOOKLM_AUTH_JSON is set"

**Cause:** You're trying to run `notebooklm login` in CI/CD where `NOTEBOOKLM_AUTH_JSON` is set.

**Why:** The `login` command saves to a file, which conflicts with environment-based auth.

**Solution:**
- With inline env-var auth, don't run browser `login` in CI/CD — use the env var for auth instead, and refresh it locally when needed
- With a materialized file-based profile, prefer `notebooklm auth refresh`. This repo's live-E2E workflows intentionally use the legacy `login --master-token-refresh` route because they require an unconditional fresh jar before each run

#### Session expired in CI/CD

**Cause:** Google sessions expire periodically (typically every 1-2 weeks).

**Solution (cookie-only auth):**
1. Re-run `notebooklm login` locally
2. Copy the contents of `~/.notebooklm/profiles/default/storage_state.json`
3. Update your GitHub secret

**Solution (file-based profile with `NOTEBOOKLM_MASTER_TOKEN_JSON`):** usually none
needed — the materialize step pre-mints fresh cookies from the master token each run,
and the client's layer-4 re-mint covers mid-run expiry. If the run still fails on auth,
check the materialize step's log: a `::warning::` about a failed re-mint means the run
fell back to the (possibly expired) cookie snapshot — it does not by itself identify
the cause. Inspect the login error above the warning: a transient network/gpsoauth
blip just needs a re-run, while an invalid or revoked master token requires re-running
the `notebooklm login --master-token` bootstrap and updating both secrets.

#### Multiple accounts in CI/CD

Use the opaque slot pool described in [Managed-copy live CI](#managed-copy-live-ci).
Do not bundle credentials or inject all accounts into one job. The selection
matrix may carry only the slot alias and allowlisted secret name:

```yaml
jobs:
  live:
    environment: protected-readonly
    concurrency:
      group: notebooklm-account-${{ matrix.account_slot }}
      queue: max
      cancel-in-progress: false
    env:
      NOTEBOOKLM_PROFILE: ci-${{ matrix.account_slot }}-${{ matrix.lane }}
    steps:
      - name: Materialize selected account
        env:
          NOTEBOOKLM_MASTER_TOKEN_JSON: ${{ secrets[matrix.master_token_secret_name] }}
        run: >-
          python scripts/materialize_ci_auth.py
          --account-slot "${{ matrix.account_slot }}"
          --profile "$NOTEBOOKLM_PROFILE"
```

#### Debugging CI/CD auth issues

Add diagnostic steps to your workflow:

```yaml
- name: Debug auth
  run: |
    # Comprehensive auth check (preferred)
    notebooklm auth check --json

    # Check if env var is set (without revealing content)
    if [ -n "$NOTEBOOKLM_AUTH_JSON" ]; then
      echo "NOTEBOOKLM_AUTH_JSON is set (length: ${#NOTEBOOKLM_AUTH_JSON})"
    else
      echo "NOTEBOOKLM_AUTH_JSON is not set"
    fi
```

The `auth check --json` output shows:
- Whether storage/env var is being used
- Which cookies are present
- Cookie domains (important for regional users)
- Any validation errors

---

## Getting Help

- Check existing implementations in `_*.py` files
- Look at test files for expected structures
- See [RPC Development Guide](rpc-development.md) for protocol details
- See [CONTRIBUTING.md](../CONTRIBUTING.md) for install, lint, and PR workflow
- Open an issue with captured request/response (sanitized)
