# Migration: Tier 12 → Tier 13

Tier 12 ("middleware chain") and Tier 13 ("Session/Kernel split") restructured
the **private** core layer of `notebooklm-py`. The **public** API
(`NotebookLMClient`, `AuthTokens`, the dataclasses re-exported from
`notebooklm`, the exceptions, the enums, and `notebooklm.rpc.RPCMethod`) is
unchanged — `client.notebooks.list()`, `client.sources.add_url(...)`,
`client.chat.ask(...)`, `client.artifacts.generate_audio(...)` and friends keep
the same signatures and behavior.

This page catalogs the renames, moves, and deletions for first-party callers
or test suites that imported underscore-prefixed (`notebooklm._...`)
internals. Underscore-prefixed names are **not** part of the documented public
surface ([`docs/stability.md`](stability.md)) and may move again in future
tiers; the table below is provided as a courtesy for downstream code that
needs to migrate.

If your code only uses the documented public API, you do not need to do
anything.

## Quick guidance

- **Stop importing from `notebooklm._core`** for new code. The module is now a
  compatibility shim that re-exports the concrete `Session` orchestrator from
  `notebooklm._session`. Existing imports still resolve, but the symbols
  documented below live on their final home modules now and the shim may be
  removed in a later refactor.
- **Prefer the public surface** — `notebooklm.NotebookLMClient`,
  `notebooklm.AuthTokens`, `notebooklm.rpc.RPCMethod`, and the types/exceptions
  re-exported from the top-level package.
- **Feature APIs** (`NotebooksAPI`, `SourcesAPI`, `ArtifactsAPI`, `ChatAPI`,
  `ResearchAPI`, `NotesAPI`, `SharingAPI`, `SettingsAPI`) now depend on the
  **narrow capability Protocols** in `notebooklm._session_contracts`
  (`RpcCaller`, `LoopGuard`, `OperationScopeProvider`, `AsyncWorkRuntime`)
  or on feature-local runtime Protocols (`ChatRuntime`, `ArtifactsRuntime`,
  `UploadRuntime`) defined in their owning modules — not on the concrete
  `ClientCore`/`Session` class and not on a broad `Session` Protocol. The
  broad `Session` Protocol that existed transiently between Tier 13 and
  the capability refactor was deleted in Phase 7 of the refactor arc
  (see ADR-013 and `docs/refactor.md`). They continue to be created by
  `NotebookLMClient`; only the type they're written against has narrowed.

## Renamed modules

The following private modules were renamed without any change to the symbols
they expose. Every named export carries forward; only the file path changed.

**The old `_core_*` module paths no longer resolve.** Tier 13 PR 13.8 removed
the per-module shims; `import notebooklm._core_auth` (and the other rows
below) now raises `ImportError`. Update your imports to the new path on the
right.

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
| `notebooklm._core_transport` | `notebooklm._authed_transport` |

`notebooklm._core` itself still exists as a compatibility shim that re-exports
the `Session` class (aliased as `ClientCore` for legacy callers) plus the
constants and helpers in the table below. New code should import from the
post-rename modules directly.

## Moved and renamed symbols

The Session/Kernel split (Tier 13) introduced new home modules for the
orchestrator and its transport collaborator. Existing helper names did not
change, only their home module did.

| Tier 12 symbol | Tier 13 home | Notes |
|---|---|---|
| `notebooklm._core.ClientCore` (class) | `notebooklm._session.Session` | `ClientCore` still resolves via the `_core` shim. New code should use `notebooklm._session.Session`. Feature APIs accept the narrow capability Protocols in `notebooklm._session_contracts` (`RpcCaller`, `AsyncWorkRuntime`, etc.) or a feature-local runtime; the broad `Session` Protocol was retired in the capability refactor — see ADR-013. |
| `notebooklm._core.MAX_RETRY_AFTER_SECONDS` | `notebooklm._authed_transport.MAX_RETRY_AFTER_SECONDS` | Re-exported via `_session` and the `_core` shim. |
| `notebooklm._core.DEFAULT_*` (timeouts, concurrency knobs) | `notebooklm._session_config.DEFAULT_*` | Re-exported via `_session` and the `_core` shim. |
| `notebooklm._core.AUTH_ERROR_PATTERNS`, `notebooklm._core.is_auth_error` | `notebooklm._session_helpers` | Re-exported via `_session` and the `_core` shim. |
| `notebooklm._core.ERROR_INJECT_ENV_VAR` | `notebooklm._error_injection.ERROR_INJECT_ENV_VAR` | Re-exported via `_session` and the `_core` shim. |
| `notebooklm._core._SyntheticErrorTransport` (class) | _Removed_ | Synthetic-error substitution moved into `notebooklm._middleware_error_injection.ErrorInjectionMiddleware` in Tier 12 PR 12.6. The env-var resolver (`_get_error_injection_mode`) and startup guard (`_refuse_synthetic_error_outside_test_context`) survive in `notebooklm._error_injection`. |
| `notebooklm._core.AuthRefreshCoordinator` | `notebooklm._session_auth.AuthRefreshCoordinator` | The class itself is unchanged; only the home module moved. The shim still re-exports it. |
| `notebooklm._core.TransportDrainTracker` | `notebooklm._transport_drain.TransportDrainTracker` | Same — shim re-exports. |
| `notebooklm._core.ClientMetrics` | `notebooklm._client_metrics.ClientMetrics` | Same — shim re-exports. |
| `notebooklm._core.ReqidCounter` | `notebooklm._reqid_counter.ReqidCounter` | Same — shim re-exports. |
| `notebooklm._core.CookiePersistence` | `notebooklm._cookie_persistence.CookiePersistence` | Same — shim re-exports. |
| `notebooklm._core.ClientLifecycle` | `notebooklm._session_lifecycle.ClientLifecycle` | Same — shim re-exports. |
| `notebooklm._core.RpcExecutor` | `notebooklm._rpc_executor.RpcExecutor` | Same — shim re-exports. |
| `notebooklm._core.AuthedTransport` | `notebooklm._authed_transport.AuthedTransport` | Same — shim re-exports. |

## New modules introduced by Tier 12 / 13

These modules did not exist before Tier 12 began. They are listed here so that
first-party callers and test suites looking for the new seams know where to
import from.

| Module | Purpose |
|---|---|
| `notebooklm._session_contracts` | `AuthMetadata`, `Kernel`, and the four shared capability Protocols (`RpcCaller`, `LoopGuard`, `OperationScopeProvider`, `AsyncWorkRuntime`) added in the capability refactor (ADR-013). The originally-shipped broad `Session` Protocol and the standalone `DrainHookRegistration` Protocol were deleted in Phase 7 of the refactor arc; feature-local runtimes (`ChatRuntime`, `ArtifactsRuntime`, `UploadRuntime`) now live in their owning feature modules, and the canonical `DrainHookRegistration` is local to `_artifacts.py`. |
| `notebooklm._kernel` | Concrete `Kernel` transport core (owns the `httpx.AsyncClient`, exposes `post` / `cookies` / `aclose`). Wrapped by `Session` and consumed by middleware. |
| `notebooklm._middleware` | Middleware chain primitives (`AuthedHttpClient` Protocol, `Middleware` Protocol, `RequestContext`, chain composition). |
| `notebooklm._middleware_tracing` | Tier 12 PR 12.3 — request tracing middleware. |
| `notebooklm._middleware_metrics` | Tier 12 PR 12.4 — metrics collection middleware. |
| `notebooklm._middleware_drain` | Tier 12 PR 12.5 — drain bookkeeping middleware. |
| `notebooklm._middleware_error_injection` | Tier 12 PR 12.6 — test-only error-injection middleware. |
| `notebooklm._middleware_retry` | Tier 12 PR 12.7 — 429 / 5xx retry middleware. |
| `notebooklm._middleware_auth_refresh` | Tier 12 PR 12.8 — auth-refresh-on-401 middleware. |
| `notebooklm._middleware_semaphore` | Tier 12 PR 12.9 — global RPC concurrency cap. |
| `notebooklm._chat_transport` | Chat-domain consumer-side error mapping over `AuthedTransport`. Replaces the chat-side wrapper that previously lived on `_core.rpc_call`. |
| `notebooklm._request_types` | Shared dataclasses + type aliases for authed-POST request construction. Re-exports `_AuthSnapshot` and `_BuildRequest` from `_authed_transport` under the public-without-underscore names `AuthSnapshot` / `BuildRequest`, plus a new `BuildRequestResult` dataclass. |

## Deleted symbols and changed defaults

| Symbol or default | Replacement / new behavior |
|---|---|
| `notebooklm._core._SyntheticErrorTransport` (deleted) | `notebooklm._middleware_error_injection.ErrorInjectionMiddleware` (chain-resident; mode is still resolved from `NOTEBOOKLM_VCR_RECORD_ERRORS` via `_error_injection._get_error_injection_mode`). |
| Strict-decode opt-in (changed default) | `NOTEBOOKLM_STRICT_DECODE` now defaults to `1` (flipped in Tier 13 PR 13.9a). Set it to `0` to restore the legacy lenient decode. See [ADR-011](adr/0011-schema-validation-policy.md). |

## Public API guarantee

No public symbol moved or changed signature during Tier 12 or Tier 13. The
following still work exactly as documented:

```python
from notebooklm import NotebookLMClient, AuthTokens
from notebooklm.rpc import RPCMethod

async with await NotebookLMClient.from_storage() as client:
    notebooks = await client.notebooks.list()
    await client.sources.add_url(notebook_id, url)
    result = await client.chat.ask(notebook_id, question)
    status = await client.artifacts.generate_audio(notebook_id)
```

See [`docs/python-api.md`](python-api.md) for the canonical public API
reference and [`docs/stability.md`](stability.md) for the stability contract.

Tier 13 closed in v0.5.0 — see [`docs/architecture.md`](architecture.md)
for the post-refactor runtime shape (Session collaborator graph +
capability-protocol model).
