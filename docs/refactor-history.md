# Tier 12 / Tier 13 Refactor — Historical Record

> **Status:** Shipped in v0.5.0.
> **Current runtime shape:** [`docs/architecture.md`](architecture.md).
> **Ratifying decision:** [ADR-013 — composable session capabilities](adr/0013-composable-session-capabilities.md), which supersedes [ADR-010 — Session/Kernel split](adr/0010-session-kernel-split.md).
> **Last updated:** 2026-05-21

> **⚠️ Superseded in part by [ADR-014](adr/0014-feature-local-runtime-adapters.md).**
> A later refactor removed the `Session` facade class entirely and renamed the
> `_session_*` home modules to `_runtime_*` (`_session_contracts` →
> `_runtime_contracts`, `_session_auth` → `_runtime_auth`, `_session_lifecycle`
> → `_runtime_lifecycle`, `_session_config` → `_runtime_config`,
> `_session_helpers` → `_runtime_helpers`). The module names in the tables below
> are the **v0.5.0 as-shipped** names, not the current tree — see
> [`docs/architecture.md`](architecture.md) for the current module map.

This document is the historical record of the Tier 12 / Tier 13 refactor arc.
It exists for two audiences:

- **Maintainers** who want to understand the design intent — why the
  feature-facing `Session` boundary was rebuilt around composable
  capability Protocols rather than a single broad Protocol — and
  what tradeoffs that locked in.
- **First-party callers and test suites** that imported underscore-prefixed
  (`notebooklm._...`) internals during Tier 11 or earlier, and need
  to migrate to the new module paths.

If your code only uses the documented public API
([`docs/python-api.md`](python-api.md)), nothing changed for you — the
v0.4.1 public surface was a binding constraint on the whole arc.

## Background

Tier 12 ("middleware chain") and Tier 13 ("Session/Kernel split")
restructured the **private** core layer of `notebooklm-py`:

- **Tier 12** extracted the authed HTTP path into a composable
  middleware chain (`AuthedHttpClient` Protocol, `Middleware` Protocol,
  `RequestContext`). Tracing, metrics, drain bookkeeping, error
  injection, 429 / 5xx retry, 401 auth-refresh, and the global RPC
  concurrency semaphore moved into discrete middleware modules and
  off of the monolithic transport class.
- **Tier 13** split the `ClientCore` god-object into `Session` (the
  orchestrator) and `Kernel` (the pure transport core — owns the
  `httpx.AsyncClient` and the cookie jar). It also renamed every
  `_core_*` module out of the legacy `_core` namespace into final
  homes (`_session_auth`, `_session_lifecycle`, `_request_types`,
  `_transport_errors`, `_streaming_post`, `_rpc_executor`, and so on).
- The **capability refactor** that followed (ADR-013) replaced the
  broad `Session` Protocol that Tier 13 originally shipped with a
  composable set of narrow capability Protocols
  (`RpcCaller`, `LoopGuard`, `OperationScopeProvider`,
  `AsyncWorkRuntime`) plus feature-local runtime Protocols
  (`ChatRuntime`, `ArtifactsRuntime`, `UploadRuntime`). Feature APIs
  now depend on the narrowest slice of capability they actually use,
  not on the orchestrator class.

The public `NotebookLMClient` API — `client.notebooks.*`,
`client.sources.*`, `client.chat.*`, `client.artifacts.*`, and
friends — was preserved across all three movements.

## What changed by audience

### Downstream callers using the public API

Nothing. Every method, property, and attribute reachable through
`NotebookLMClient` at v0.4.1 still works with the same signature and
return type:

```python
from notebooklm import NotebookLMClient, AuthTokens
from notebooklm.rpc import RPCMethod

async with await NotebookLMClient.from_storage() as client:
    notebooks = await client.notebooks.list()
    await client.sources.add_url(notebook_id, url)
    result = await client.chat.ask(notebook_id, question)
    status = await client.artifacts.generate_audio(notebook_id)
```

The one additive surface change: `client.chat.save_answer_as_note(...)`
is new. `client.notes.create_from_chat(...)` continues to work but
emits a `DeprecationWarning` and forwards to the chat-owned workflow.

See [`docs/stability.md`](stability.md) for the public stability
contract.

### First-party callers and test suites

Underscore-prefixed names are **not** part of the documented public
surface and may move again in future tiers. The tables below are
provided as a courtesy for first-party callers and test suites that
imported them during Tier 11 or earlier.

**Quick guidance:**

- The `notebooklm._core` compatibility shim and the
  `NotebookLMClient._core` attribute alias were both deleted in
  Phase 4 (#889). Downstream code and tests must import from
  `notebooklm._session` or the other final modules directly.
- Prefer the public surface — `notebooklm.NotebookLMClient`,
  `notebooklm.AuthTokens`, `notebooklm.rpc.RPCMethod`, and the
  types / exceptions re-exported from the top-level package.
- Feature APIs (`NotebooksAPI`, `SourcesAPI`, `ArtifactsAPI`,
  `ChatAPI`, `ResearchAPI`, `NotesAPI`, `SharingAPI`, `SettingsAPI`)
  depend on the **narrow capability Protocols** in
  `notebooklm._session_contracts` (`RpcCaller`, `LoopGuard`,
  `OperationScopeProvider`, `AsyncWorkRuntime`) or on feature-local
  runtime Protocols (`ChatRuntime`, `ArtifactsRuntime`,
  `UploadRuntime`) defined in their owning modules — not on the
  concrete `Session` class and not on a broad `Session` Protocol.

#### Renamed modules

The Tier 12 `_core_*` module paths no longer resolve.
`import notebooklm._core_auth` (and every other row below) now
raises `ImportError`. Update your imports to the new path on the
right:

| Tier 12 path | Tier 13 path |
|---|---|
| `notebooklm._core_auth` | `notebooklm._session_auth` |
| `notebooklm._core_cache` | `notebooklm._conversation_cache` |
| `notebooklm._core_constants` | `notebooklm._session_config` |
| `notebooklm._core_cookie_persistence` | `notebooklm._cookie_persistence` |
| `notebooklm._core_drain` | `notebooklm._transport_drain` |
| `notebooklm._core_error_injection` | `notebooklm._error_injection` |
| `notebooklm._core_helpers` | `notebooklm._session_helpers` |
| `notebooklm._core_lifecycle` | `notebooklm._session_lifecycle` |
| `notebooklm._core_metrics` | `notebooklm._client_metrics` |
| `notebooklm._core_polling` | `notebooklm._polling_registry` |
| `notebooklm._core_reqid` | `notebooklm._reqid_counter` |
| `notebooklm._core_rpc` | `notebooklm._rpc_executor` |
| `notebooklm._core_transport` | `notebooklm._request_types` / `notebooklm._transport_errors` / `notebooklm._streaming_post` |

`notebooklm._core` itself has been deleted.

#### Moved and renamed symbols

The Session/Kernel split introduced new home modules for the
orchestrator and its transport collaborator. Existing helper names did
not change, only their home module did.

| Tier 12 symbol | Tier 13 home | Notes |
|---|---|---|
| `notebooklm._core.ClientCore` (class) | `notebooklm._session.Session` | `ClientCore` was retired. Feature APIs accept the narrow capability Protocols in `notebooklm._session_contracts` (`RpcCaller`, `AsyncWorkRuntime`, etc.) or a feature-local runtime; the broad `Session` Protocol was retired in the capability refactor — see ADR-013. |
| `notebooklm._core.MAX_RETRY_AFTER_SECONDS` | `notebooklm._transport_errors.MAX_RETRY_AFTER_SECONDS` | No longer re-exported via `_session` or `_core`. |
| `notebooklm._core.DEFAULT_*` (timeouts, concurrency knobs) | `notebooklm._session_config.DEFAULT_*` | |
| `notebooklm._core.AUTH_ERROR_PATTERNS`, `notebooklm._core.is_auth_error` | `notebooklm._session_helpers` | |
| `notebooklm._core.ERROR_INJECT_ENV_VAR` | `notebooklm._error_injection.ERROR_INJECT_ENV_VAR` | |
| `notebooklm._core._SyntheticErrorTransport` (class) | _Removed_ | Synthetic-error substitution moved into `notebooklm._middleware_error_injection.ErrorInjectionMiddleware` in Tier 12 PR 12.6. The env-var resolver (`_get_error_injection_mode`) and startup guard (`_refuse_synthetic_error_outside_test_context`) survive in `notebooklm._error_injection`. |
| `notebooklm._core.AuthRefreshCoordinator` | `notebooklm._session_auth.AuthRefreshCoordinator` | Class unchanged; only the home module moved. |
| `notebooklm._core.TransportDrainTracker` | `notebooklm._transport_drain.TransportDrainTracker` | Same. |
| `notebooklm._core.ClientMetrics` | `notebooklm._client_metrics.ClientMetrics` | Same. |
| `notebooklm._core.ReqidCounter` | `notebooklm._reqid_counter.ReqidCounter` | Same. |
| `notebooklm._core.CookiePersistence` | `notebooklm._cookie_persistence.CookiePersistence` | Same. |
| `notebooklm._core.ClientLifecycle` | `notebooklm._session_lifecycle.ClientLifecycle` | Same. |
| `notebooklm._core.RpcExecutor` | `notebooklm._rpc_executor.RpcExecutor` | Same. |
| `notebooklm._core` authed transport helpers | `notebooklm._request_types` + `notebooklm._transport_errors` + `notebooklm._streaming_post` | The interim authed-transport Adapter and catch-all helper module were retired; request types, transport errors, and streaming POST behavior now have separate owning modules. |

#### New modules introduced by Tier 12 / 13

These modules did not exist before Tier 12 began:

| Module | Purpose |
|---|---|
| `notebooklm._session_contracts` | `AuthMetadata`, `Kernel`, and the four shared capability Protocols (`RpcCaller`, `LoopGuard`, `OperationScopeProvider`, `AsyncWorkRuntime`) added in the capability refactor (ADR-013). The originally-shipped broad `Session` Protocol and the standalone `DrainHookRegistration` Protocol were deleted in the final phase. The follow-up feature-local composite-runtime Protocols (`ChatRuntime`, `ArtifactsRuntime`, `UploadRuntime`) and their adapter dataclasses introduced in their owning feature modules were retired once it was clear they only hid three stable collaborators with one production satisfier; feature constructors take their three runtime collaborators (`rpc` + `drain` + `lifecycle`) directly. |
| `notebooklm._kernel` | Concrete `Kernel` transport core (owns the `httpx.AsyncClient`, exposes `post` / `cookies` / `aclose`). Located at root (`src/notebooklm/_kernel.py`), not nested. |
| `notebooklm._middleware` | Middleware chain primitives (`Middleware` Protocol, `NextCall` callable type, `RpcRequest` / `RpcResponse` envelope dataclasses, `build_chain` composer). |
| `notebooklm._middleware_tracing` | Tier 12 PR 12.3 — request tracing middleware. |
| `notebooklm._middleware_metrics` | Tier 12 PR 12.4 — metrics collection middleware. |
| `notebooklm._middleware_drain` | Tier 12 PR 12.5 — drain bookkeeping middleware. |
| `notebooklm._middleware_error_injection` | Tier 12 PR 12.6 — test-only error-injection middleware. |
| `notebooklm._middleware_retry` | Tier 12 PR 12.7 — 429 / 5xx retry middleware. |
| `notebooklm._middleware_auth_refresh` | Tier 12 PR 12.8 — auth-refresh-on-401 middleware. |
| `notebooklm._middleware_semaphore` | Tier 12 PR 12.9 — global RPC concurrency cap. |
| `notebooklm._chat_transport` | Chat-domain consumer-side error mapping over the shared authed POST pipeline. Replaces the chat-side wrapper that previously lived on `_core.rpc_call`. |
| `notebooklm._transport_errors` | Terminal `Kernel.post` error mapping into transport exceptions consumed by retry/auth middleware. |
| `notebooklm._request_types` | Shared dataclasses + type aliases for authed-POST request construction: `AuthSnapshot`, `BuildRequest`, `PostBody`, and `BuildRequestResult`. |
| `notebooklm._streaming_post` | Size-capped streaming POST helper used by `Kernel.post`. |

#### Deleted symbols and changed defaults

| Symbol or default | Replacement / new behavior |
|---|---|
| `notebooklm._core._SyntheticErrorTransport` (deleted) | `notebooklm._middleware_error_injection.ErrorInjectionMiddleware` (chain-resident; mode is still resolved from `NOTEBOOKLM_VCR_RECORD_ERRORS` via `_error_injection._get_error_injection_mode`). |
| Strict-decode opt-in (changed default) | `NOTEBOOKLM_STRICT_DECODE` now defaults to `1` (flipped in Tier 13 PR 13.9a). Set it to `0` to restore the legacy lenient decode. See [ADR-011](adr/0011-schema-validation-policy.md). |

## Design intent

The capability refactor (ADR-013) replaced the broad
`_session_contracts.Session` Protocol that Tier 13 originally shipped.
That Protocol had become a capability bag:

```python
class Session(Protocol):
    auth: AuthMetadata
    kernel: Kernel
    async def rpc_call(...) -> Any: ...
    async def transport_post(...) -> httpx.Response: ...
    async def next_reqid(...) -> int: ...
    def assert_bound_loop(self) -> None: ...
    def operation_scope(self, label: str) -> AbstractAsyncContextManager[None]: ...
    def register_drain_hook(self, name, hook) -> None: ...
```

That shape violated the intent of
[ADR-010](adr/0010-session-kernel-split.md): feature APIs should not
depend on concrete `Session` internals. It also made dependencies
hard to read — most features only needed logical RPC calls, while
chat, uploads, and artifact polling needed narrower specialized
runtime slices.

### Design rules

The capability model is built on six rules:

1. Promote a capability to `_session_contracts.py` only when it is
   shared by more than one feature or service.
2. Keep single-feature runtime needs local to the owning feature
   module.
3. Concrete `_session.Session` may still implement many methods and
   properties, but feature-facing Protocols must not advertise
   unrelated capabilities.
4. Prefer feature-owned collaborators over widening shared session
   contracts.
5. Remove old `core` vocabulary from touched feature APIs.
6. Do not use mixins for dependency expression. Use Protocols for
   required capabilities and collaborators / services for extracted
   behavior.

### Shared capability Protocols

`src/notebooklm/_session_contracts.py` ended up containing only
shared capability Protocols:

```python
class RpcCaller(Protocol):
    async def rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
        operation_variant: str | None = None,
    ) -> Any: ...


class LoopGuard(Protocol):
    def assert_bound_loop(self) -> None: ...


class OperationScopeProvider(Protocol):
    def operation_scope(self, label: str) -> AbstractAsyncContextManager[None]: ...


class AsyncWorkRuntime(LoopGuard, OperationScopeProvider, Protocol):
    """Runtime support for feature-owned async work."""
```

The following were **not** globally promoted:

- `auth`, `kernel`, `transport_post(...)`, `next_reqid(...)` —
  needed only by uploads and chat respectively. They live on the
  concrete `Session` and on feature-local runtimes that consume
  them, not on a shared Protocol.
- `register_drain_hook(...)` — moved to a `DrainHookRegistration`
  Protocol local to `_artifacts.py`, since artifact polling is the
  only behavior that registers close-time feature cleanup.

### Feature-local runtimes

Where a feature needed a specialised slice of runtime capability,
that slice was declared as a Protocol in the feature's own module:

- `ChatRuntime` in `_chat.py` — `RpcCaller + LoopGuard +
  transport_post + next_reqid`. `transport_post` is consumed via
  `chat_aware_authed_post(...)` in `_chat_transport.py`.
- `ArtifactsRuntime` in `_artifacts.py` — `RpcCaller +
  AsyncWorkRuntime + DrainHookRegistration`. Artifact polling owns
  the only close-time cleanup hook in the codebase today.
- `UploadRuntime` in `_source_upload.py` — `RpcCaller +
  OperationScopeProvider + LoopGuard`. The upload pipeline also receives
  `kernel` and `auth` as constructor args, since uploads need
  upload-specific auth routing and live cookies — but those wires
  are explicit, not implicit through a god-object.

If a future feature ever needs the same local slice that another
feature has carved out, the rule is: promote the Protocol to
`_session_contracts.py` only when there is a real second consumer,
not on speculation.

### Note / mind-map service split

The capability refactor also separated note-row primitives from
mind-map product behavior. Before the split, `_mind_map.py` mixed
three responsibilities:

1. Backend row classification (note vs. mind-map vs. saved-chat).
2. Mind-map filtering and content extraction.
3. Saved-from-chat note encoding (chat citations + rich source
   passage anchors).

These were factored apart:

- **`_note_service.py`** owns the generic note row primitives:
  `fetch_note_rows`, `classify_row` (over a private `NoteRowKind`
  enum), `extract_content`, `create_note`, `update_note`,
  `delete_note`. Mind maps are stored in the same note-row
  collection that NotebookLM returns from `GET_NOTES_AND_MIND_MAPS`,
  so note-row classification is the right place to recognise them.
- **`_mind_map.py`** kept the mind-map boundary as
  `NoteBackedMindMapService` — `list_mind_maps`,
  `extract_content`, `delete_mind_map` — delegating to
  `NoteService` for the underlying row operations.
  `NotesAPI.list_mind_maps(...)` and `NotesAPI.delete_mind_map(...)`
  forward through this service, preserving their public signatures.
- **Saved-from-chat note encoding** moved out of `_mind_map.py` and
  into `_chat_notes.py`, where `ChatAPI.save_answer_as_note(...)`
  owns the workflow. `NotesAPI.create_from_chat(...)` survives as a
  deprecated forwarder that emits a `DeprecationWarning` and
  delegates to `ChatAPI.save_answer_as_note(...)` via an injected
  callback.

`NoteRowKind` stays private — it is an internal classification of
rows returned by the undocumented `GET_NOTES_AND_MIND_MAPS` RPC, not
a public API type.

### Constructor and naming rules

Feature APIs adopted consistent dependency-naming conventions:

- Pure-RPC features store `self._rpc`. Constructor parameter is
  named `rpc: RpcCaller`.
- Features with composite runtime needs store `self._runtime`.
- The old `self._core` vocabulary was removed from every touched
  feature module.
- Extra collaborators are keyword-only:

```python
SourcesAPI(rpc, *, uploader=source_uploader)
NotebooksAPI(rpc, *, sources_api=sources)
ChatAPI(runtime, *, notebooks=notebooks)
ArtifactsAPI(runtime, *, notebooks=notebooks, mind_maps=mind_maps,
             note_service=note_service)
NotesAPI(*, notes=note_service, mind_maps=mind_maps,
         save_chat_answer=...)
```

Compatibility aliases and fallback constructors that read missing
collaborators off the session were removed — every feature now
requires its dependencies explicitly. `NotebookLMClient` is the
single wiring root that hands them out.

## How it landed

The refactor arc shipped in four phases against the
`refactor-completion-plan` series (PRs in the #84x–#92x range).
Detailed phase notes live in the merged PR descriptions; the
high-level shape was:

- **Phase 1 — additive contracts.** New capability Protocols
  (`RpcCaller`, `LoopGuard`, `OperationScopeProvider`,
  `AsyncWorkRuntime`) were added to `_session_contracts.py`
  alongside the existing broad `Session` Protocol. Nothing was
  removed. Build stayed green.
- **Phase 2 — pure-RPC feature retyping.** `NotebooksAPI`,
  `ResearchAPI`, `SettingsAPI`, `SharingAPI` were retyped to
  `RpcCaller`, with `self._core` renamed to `self._rpc`. The
  `FakeSession` test fixture shrunk in the same commits to match
  the narrower contract.
- **Phase 3 — feature-local runtimes.** `ChatRuntime`,
  `ArtifactsRuntime`, and `UploadRuntime` were introduced in their
  owning modules. `ChatAPI`, `ArtifactsAPI`, `SourcesAPI`, and
  `SourceUploadPipeline` migrated to the new local Protocols.
  Compatibility fallbacks (the `drain_hooks=None -> session`
  fallback in `ArtifactsAPI`, the `session.kernel` / `session.auth`
  / `record_upload_queue_wait` fallbacks in `SourcesAPI`, the
  `core=` alias on `ChatAPI`) were removed in the same commits
  that updated their direct-construction test sites.
- **Phase 4 — note / mind-map split + capability cleanup.**
  `NoteService` and `NoteBackedMindMapService` were introduced,
  artifact generation and download paths were rewired through them,
  `ChatAPI.save_answer_as_note(...)` was added, and
  `NotesAPI.create_from_chat(...)` was converted to a deprecated
  forwarder. The module-level `_mind_map` wrappers were removed.
  The broad `Session` Protocol was deleted from
  `_session_contracts.py`, along with the broad `FakeSession`
  defaults shape and the broad-Protocol test pin. The `_core.py`
  compatibility shim and the `NotebookLMClient._core` attribute
  alias were both removed in this phase (#889).

Implementation tactics — line-number-specific constructor changes,
the ordering rules for compatibility-fallback removals, the
`_source_upload.py` local `RpcCaller` → `RpcCallback` rename to
avoid name collision with the new shared Protocol — are recorded in
the merged PR descriptions, not in this document. The post-refactor
runtime shape is canonicalized in [`docs/architecture.md`](architecture.md).

## See also

- [`docs/architecture.md`](architecture.md) — post-refactor runtime
  shape (`Session` collaborator graph, capability-protocol model,
  dispatch path).
- [ADR-010 — Session / Kernel split](adr/0010-session-kernel-split.md) —
  the design driver for Tier 13. **Superseded by ADR-013.**
- [ADR-011 — Schema validation policy](adr/0011-schema-validation-policy.md) —
  the strict-decode default flip.
- [ADR-012 — Implementation-surface convention](adr/0012-implementation-surface-convention.md).
- [ADR-013 — Composable session capabilities](adr/0013-composable-session-capabilities.md) —
  the ratifying decision for the capability model described here.
- [`docs/stability.md`](stability.md) — public stability contract.
- [`docs/python-api.md`](python-api.md) — canonical public API reference.
