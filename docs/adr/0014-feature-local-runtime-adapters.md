# ADR-0014: Feature-local runtime adapters as Protocol satisfiers

> **Current state (2026-06-11).** The normative body below describes the state
> *at decision time*, when a concrete `Session` facade class and module still
> existed. **`Session` and `_session.py` have since been deleted** (see
> [Revision history](#revision-history) → "2026-05-28 — Session elimination"),
> and the former `_session_*` / `_runtime_*` collaborators now live under the
> `_runtime/` package (for example `_runtime/init.py`,
> `_runtime/transport.py`; the `SessionTransport` / `SessionCollaborators`
> classes are now `RuntimeTransport` / `RuntimeCollaborators`). The
> feature-local composite Protocols and adapter dataclasses discussed below
> were also retired when direct keyword-only collaborator injection proved
> clearer for their single consumers. Treat in-body references to `Session`,
> `_session.py`, `_session_*`, and exact `client.py:NNN` line numbers as
> historical; the
> live runtime shape is documented in
> [`docs/architecture.md`](../architecture.md).

## Status

Accepted (#1082; Stage-B issue #1084; MiddlewareChainHost issue #1085).
Rule 3 Stage B closed by the post-refactoring plan 2026-05-27 Stage B1
(#1086 / #1089 / #1091). Rule 4 deepening closed by the same plan's
Stage B2 (#1090 / #1092 / this PR) — the chain ownership carve-out is
recorded under [Revision history](#revision-history) below.

## Context

[ADR-0013](./0013-composable-session-capabilities.md) introduced narrow capability Protocols
(`RpcCaller`, `LoopGuard`, `OperationScopeProvider`, `AuthMetadata`, `Kernel` in
the then-current `_session_contracts.py`) plus feature-local
composite runtime Protocols (`ChatRuntime` in `_chat.py`, `ArtifactsRuntime` in
`_artifacts.py`, `UploadRuntime` in `_source_upload.py`). The goal was to decouple
feature APIs from a concrete `Session` god-object.

At compile time, the goal was achieved. Every feature API type-checks against the
narrowest Protocol it needs, mypy verifies the satisfaction, and the
the contracts module docstring enforces the "≥2 consumers ⇒ shared
Protocol; otherwise feature-local" promotion rule. Current shared
contracts live in `_runtime/contracts.py` and are only `Kernel`,
`RpcCaller`, and `LoopGuard`; `AuthMetadata` and `OperationScopeProvider`
are local to their only consumers.

At runtime, the goal was not achieved. `NotebookLMClient.__init__`
([`client.py:305-342`](../../src/notebooklm/client.py)) passes `self._session` (a
`Session` instance) to every feature API. `Session` was kept as the universal
satisfier of every Protocol. This produces four observable consequences:

1. **`Session` must satisfy the union of every feature Protocol.** Adding a method
   to `ChatRuntime` requires `Session` to expose (or forward) that method.
   `Session`'s method count grows with the feature count by construction. The
   current count is ~33 methods on a 779-line class; ~24 of those are
   one-line forwards to a held collaborator.

2. **Forwards are structurally required, not accidental.** `Session.transport_post`
   exists because `ChatRuntime` requires it — `Session` must forward to
   `SessionTransport`. Deleting the forward is not possible while `Session`
   satisfies `ChatRuntime`. Earlier "shrink the facade" attempts (the
   in-flight rpc-dispatch refactor; the Stage 2 work in the architecture
   fix-plan that produced PRs #1049/#1059/#1061) consistently hit this wall.

3. **Tests monkeypatch `Session`, not the collaborator.** At runtime, the method
   body lives on `Session`. Tests that need to fake `transport_post` patch
   `Session.transport_post`, not `SessionTransport.perform_authed_post`. The
   [ADR-0007](./0007-test-monkeypatch-policy.md) forbidden-monkeypatch
   allowlist (~30 file-level entries today) is the visible gravity well this
   creates — every entry pins a Session-shaped surface that ADR-0013's narrow
   Protocols _should_ have eliminated.

4. **`RpcOwner` Protocol carries underscore-prefixed `Session` internals.**
   `RpcExecutor` declares an `RpcOwner` dependency
   ([`_rpc_executor.py:59-78`](../../src/notebooklm/_rpc_executor.py)) listing
   `_kernel`, `_perform_authed_post`, `_await_refresh`, `_increment_metrics`.
   This is not "narrow contract"; it is "private surface of `Session`, structurally
   typed". The leakage persists because `RpcExecutor` receives a `Session`-shaped
   owner at construction.

The architectural pressure is real and ongoing. Every feature added between v0.5.0
and today has either grown `Session` or required a new compatibility forward.
ADR-0013 framed the _interface_ model correctly; what was missing is the matching
_implementation_ model.

## Decision

Capability Protocols remain conceptually as defined by ADR-0013, but their
homes changed after the migration. Current shared contracts live in
`_runtime/contracts.py`, and single-consumer capabilities live beside their
owners. **This ADR does not change the public API.** Six
implementation rules change how those interfaces are satisfied at runtime.

### Rule 1 — Single-collaborator Protocols are satisfied directly (after method push-down)

| Protocol                                                             | Satisfier (post-migration)                                                                | Migration prerequisite                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| -------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `RpcCaller`                                                          | `RpcExecutor` directly                                                                    | none — already structurally satisfies (TYPE_CHECKING assertion at [`_rpc_executor.py:463`](../../src/notebooklm/_rpc_executor.py))                                                                                                                                                                                                                                                                                                                               |
| `LoopGuard`                                                          | `ClientLifecycle` directly                                                                | **push down `assert_bound_loop()`** — currently lives on `Session.assert_bound_loop` (`_session.py:486`), which calls `_loop_affinity.assert_bound_loop(self.bound_loop)`. `ClientLifecycle` already owns `get_bound_loop` (`_session_lifecycle.py:271`); the push-down adds a trivial `assert_bound_loop()` method that calls the free function with `self.get_bound_loop()`. |
| `OperationScopeProvider`                                             | `TransportDrainTracker` directly                                                          | **push down `operation_scope(label)`** — currently lives on `Session.operation_scope` (`_session.py:495`) as an async context manager wrapping `begin_transport_post` / `finish_transport_post` (both already on `TransportDrainTracker` at [`_transport_drain.py:139,196`](../../src/notebooklm/_transport_drain.py)). The push-down moves the contextmanager wrapper to the tracker.                                       |
| `DrainHookRegistration` (feature-local in `_artifacts.py`)           | `TransportDrainTracker` directly                                                          | **push down `register_drain_hook(name, hook)` + the underlying `_drain_hooks` storage** — currently lives on `Session.register_drain_hook` (`_session.py:421`). The push-down moves both the method and the storage onto the tracker.                                                                                                                                                                                        |
| `AsyncWorkRuntime` (composes `LoopGuard` + `OperationScopeProvider`) | satisfied **transitively** by `ArtifactsRuntimeAdapter` / `UploadRuntimeAdapter` (Rule 2) | depends on the push-downs above. No dedicated `_AsyncWorkAdapter` — per Rule 2, trivial composites do not get adapter middlemen.                                                                                                                                                                                                                                                                                                                                 |
| `AuthMetadata`                                                       | `AuthTokens` (`client._auth`) directly                                                     | `SourceUploadPipeline` receives the client-owned `AuthTokens` via `auth=client._auth`; `AuthTokens` structurally provides the `authuser` and `account_email` members required by `_source/upload.py::AuthMetadata`.                                                                                                                                                                                                                                             |
| `Kernel` (Protocol)                                                  | the concrete `Kernel` class                                                               | none — unchanged                                                                                                                                                                                                                                                                                                                                                                                                                                                 |

**Why the push-downs are part of this ADR's mandate, not pre-existing.** The
Protocol satisfiers were synthesised on `Session` because `Session` was the
universal satisfier. Now that the runtime contract is "the collaborator
satisfies its own Protocol", the methods must live where the Protocol points.
The push-downs are mechanical (the underlying primitives already exist on the
collaborators) but they are non-trivial enough to be sequenced as a dedicated
migration wave (Wave 0.5 — push Protocol-satisfying methods down to
collaborators) ahead of the adapter introductions.

### Rule 2 — Composite Protocols are satisfied by a feature-local adapter when the adapter earns its keep

**Adapter threshold (intent-based).** Introduce a frozen-dataclass adapter when
**at least one** of the following holds:

- a downstream module _intentionally_ consumes the composite Protocol as a
  single dependency (e.g. an HTTP-level helper that takes the whole runtime),
  **or**
- delegation changes the call shape (the adapter method does more than forward
  args 1:1 to a single underlying collaborator), **or**
- multiple consumers share the same composite and the share-cost amortises.

If none of these hold — the composite has a single consumer, all delegates are
1:1, and no other module takes it as a parameter — the consumer takes the
underlying collaborators directly via constructor injection. No adapter
middleman.

The earlier numeric heuristic ("≥3 capabilities OR ≥1 non-trivial delegate")
was a proxy for the intent test above. It is replaced because counting
capabilities does not capture _why_ you would want a named runtime satisfier:
either some consumer demands it, or the call shape adapts. Counting alone
incentivises adapters where they bring no value.

When an adapter is used, the frozen dataclass holds the collaborators a
composite Protocol requires and exposes the Protocol methods as delegates.
Adapters live in the same module as the Protocol they satisfy.

**Corollary — dead Protocols.** If a composite Protocol is no longer consumed
(no downstream module takes it as a parameter; its sole consumer was migrated
to direct injection), delete the Protocol along with the migration. Protocols
that exist only as type-hint documentation drift out of sync with the code.

```python
# in _artifacts.py, next to ArtifactsRuntime
@dataclass(frozen=True)
class ArtifactsRuntimeAdapter:
    """Concrete satisfier of :class:`ArtifactsRuntime` per ADR-0014.

    Earns its keep under Rule 2 because:
      - composite has 3 capabilities (RpcCaller + AsyncWorkRuntime + DrainHookRegistration),
      - ArtifactsAPI takes the whole composite as a single dependency (consumer-demand),
      - register_drain_hook is a meaningful named affordance worth exposing as one method.
    """

    rpc: RpcCaller
    drain: TransportDrainTracker     # satisfies OperationScopeProvider + DrainHookRegistration after Wave 0.5
    lifecycle: ClientLifecycle       # satisfies LoopGuard after Wave 0.5

    async def rpc_call(self, *args: Any, **kwargs: Any) -> Any:
        return await self.rpc.rpc_call(*args, **kwargs)

    def operation_scope(self, label: str) -> AbstractAsyncContextManager[None]:
        return self.drain.operation_scope(label)

    def register_drain_hook(self, name: str, hook: Callable[[], Awaitable[None]]) -> None:
        self.drain.register_drain_hook(name, hook)

    def assert_bound_loop(self) -> None:
        self.lifecycle.assert_bound_loop()
```

`UploadRuntimeAdapter` follows the same pattern in `_source_upload.py`
(`SourceUploadPipeline` already takes `Kernel` and `AuthMetadata` as separate
parameters, so its adapter covers only the composite Protocol part).

`ChatRuntime` does **not** get an adapter — the Rule 2 Corollary applies. Once
`_chat_transport.chat_aware_authed_post` is refactored to take `SessionTransport`
directly (Wave 4.1 Step 0), `ChatRuntime` has no remaining consumer and is
deleted. `ChatAPI` takes the four underlying collaborators (`RpcExecutor`,
`SessionTransport`, `ReqidCounter`, `ClientLifecycle`) as keyword-only
constructor parameters.

### Rule 3 — `NotebookLMClient.__init__` is the composition root

`NotebookLMClient.__init__` wires each feature with its satisfier — a
collaborator for single-Protocol features, an adapter for composite ones.
Construction reads as an explicit wiring diagram.

The migration ships in two stages so the structural change does not balloon:

- **Stage A (this plan):** `Session` exposes the collaborator bundle as a
  single typed attribute (`Session.collaborators -> SessionCollaborators`).
  `NotebookLMClient.__init__` reads `self._session.collaborators.<x>` for
  feature wiring. `Session` continues to construct the bundle internally via
  `_session_init.build_collaborators`.
- **Stage B (Wave 7 follow-up, separate change):** ownership of
  `build_collaborators` moves to `NotebookLMClient.__init__`. `Session` takes
  the bundle as a constructor argument. This is the strategic end-state but is
  a wider blast radius (every `Session(...)` test-construction site updates)
  and is deferred so the in-flight ADR-0014 migration stays bounded.

Stage A is sufficient to discharge the runtime decoupling: features stop
receiving `Session` (they receive the collaborator or adapter directly), and
the only `Session` reference at the composition root is `self._session.collaborators`,
which is one attribute access — not a discoverability hub of method names.

### Rule 4 — `Session` retains only orchestration plus the documented middleware-chain seams

After migration, `Session` owns:

- Construction delegation (`__init__` → `_session_init.build_collaborators`).
- Lifecycle (`open`, `close`, keepalive task, `is_open`, drain-on-close).
- The collaborator graph held as attributes.
- The few methods downstream tests still pin via AST-guard
  (`tests/_guardrails/test_public_surface_manifest.py`,
  `tests/unit/test_concurrency_refresh_race.py`).
- **Live middleware-chain seams that legitimately route through `Session`.**
  **Chain-ownership carve-out (post-refactoring plan 2026-05-27 Stage B2):**
  the storage backing the chain's tunables — `_authed_post_chain_terminal`,
  `_authed_post_chain`, `_rate_limit_max_retries`,
  `_server_error_max_retries`, `_refresh_retry_delay` — moved off `Session`
  onto `MiddlewareChainHost`
  ([`_middleware_chain_host.py`](../../src/notebooklm/_middleware/chain_host.py))
  in Stage B2 PR 1 (#1090). PR 2 (#1092) then split
  `wire_middleware_chain` / `build_session_transport` to take
  `chain_host: MiddlewareChainHost` directly, so the live chain reads
  the host and the Session-side names are exclusively writable
  `@property` descriptors that forward to the host (test-seam only;
  no longer the load-bearing dereference on the hot path). The
  long-standing fixture-rebind contract is preserved end-to-end — a test
  that writes `core._<attr> = ...` still steers the live chain because
  the descriptor's setter writes through to `chain_host._<attr>`. The
  per-attr contract is now historical; current code binds the host directly
  from `ClientComposed`. `Session.assert_bound_loop` remains captured via lambda by
  `build_session_transport`'s `bound_loop_check` (not migrated to the
  host — it forwards to `ClientLifecycle` per Rule 1).

`Session` stops being passed _to_ feature APIs. It stops satisfying capability
Protocols intended for feature consumption. The compatibility forwards on
`Session` (drain/metrics/kernel/authuser/save_cookies forwards that exist only
because features used to reach through `Session`) are removed as Wave 5
deletes them.

**Explicit retention list** — surfaces that remain on `Session` post-migration:

- `Session._chain_host` — instance reference to the `MiddlewareChainHost`
  that owns the live middleware chain. The host owns
  `_authed_post_chain_terminal` (chain leaf), `_authed_post_chain`
  (installed chain slot), the three retry-budget tunables
  (`_rate_limit_max_retries`, `_server_error_max_retries`,
  `_refresh_retry_delay`), and the dynamic `await_refresh` delegate.
  `wire_middleware_chain` and `build_session_transport` take
  `chain_host: MiddlewareChainHost` directly; the chain's provider
  lambdas and the transport's `chain_provider` closure read the host
  attributes live. Tests rebind through `core._chain_host._<attr>`.
  The transitional Session-side writable `@property` descriptors and
  the `Session._await_refresh` delegate that briefly forwarded reads
  / writes to the host during the chain-ownership carve-out have been
  removed; the host is now the sole owner with no Session-side aliases.
- `Session.update_auth_tokens` — AST-guarded by `test_concurrency_refresh_race.py`.
- `Session.open` / `close` / `is_open` / `_keepalive_loop` — lifecycle.
- ~~`Session.collaborators`, `Session.session_transport`, `Session.rpc_executor`~~
  — three typed accessors per Rule 3 Stage A. **Deleted under Rule 3 Stage B
  by Stage B1 PR 2 of the post-refactoring plan 2026-05-27 (#1089 commit
  `313bbef1`)** — `compose_session_internals()` became the composition root
  and later moved to `_session_init.py` during Session-elimination Phase 1;
  the three accessor properties and the `_get_rpc_executor` lazy factory all
  collapsed to direct reads on the returned `ComposedSession`. The previous
  Task 6.3 AST lint protecting these accessors no longer has live callers to
  protect.

Everything else on `Session` after Wave 5 should be either listed here or
deleted.

### Rule 5 — Collaborators take their direct dependencies

Any collaborator that currently dereferences `self._owner.X` is migrated to
take its actual dependencies directly. `RpcExecutor` is the canonical case:

```python
# before
class RpcExecutor:
    def __init__(self, owner: RpcOwner, *, decode_response, is_auth_error, sleep, ...):
        self._owner = owner

    async def _execute_once(self, ...):
        self._owner._kernel.get_http_client()             # pre-open guard
        self._owner._increment_metrics(...)
        response = await self._owner._perform_authed_post(...)
        await self._owner._await_refresh()

# after
class RpcExecutor:
    def __init__(
        self,
        *,
        kernel: Kernel,
        transport: SessionTransport,
        auth_refresh: AuthRefreshCoordinator,
        metrics: ClientMetrics,
        decode_response, is_auth_error, sleep, ...
    ):
        self._kernel = kernel
        self._transport = transport
        self._auth_refresh = auth_refresh
        self._metrics = metrics

    async def _execute_once(self, ...):
        self._kernel.get_http_client()                    # same guard, direct
        self._metrics.increment(...)
        response = await self._transport.perform_authed_post(...)
        await self._auth_refresh.await_refresh()
```

The `RpcOwner` Protocol disappears. Same exercise for any other collaborator
holding an `_owner` reference.

### Rule 6 — Adapters live next to their feature

`ArtifactsRuntimeAdapter` lives in `_artifacts.py`. `UploadRuntimeAdapter` lives
in `_source_upload.py`. They are concrete implementations of feature-local
composite Protocols.

The Chat feature gets no adapter (Rule 2 Corollary): once
`_chat_transport.chat_aware_authed_post` is refactored to take `SessionTransport`
directly, `ChatRuntime` has no remaining consumer and is deleted. `ChatAPI`
takes the underlying collaborators as keyword-only constructor parameters
instead.

The ADR-0013 promotion rule (≥2 consumers ⇒ shared Protocol in
`_runtime/contracts.py`) is unchanged. Adapters are _not_ promoted to the
shared contracts module; it stays interface-only.

## Consequences

**Migration outcome:** Migration completed in PRs #1064–#1082; ADR-0007 Session-shaped allowlist entries drained; later Session-elimination work moved the remaining lifecycle/public-surface duties onto `NotebookLMClient` and client-owned collaborators. The two Wave-7 follow-ups (#1084 Stage B and #1085 MiddlewareChainHost) were closed by the post-refactoring plan 2026-05-27 — Stage B1 (#1086 / #1089 / #1091) and Stage B2 (#1090 / #1092 / this PR) respectively. See [Revision history](#revision-history) for the chain-ownership carve-out introduced under Stage B2 and the 2026-05-28 elimination note.

**Wanted:**

- The deleted `Session` method count stopped growing with feature count.
  New features receive direct collaborators, or a feature-local adapter only
  when Rule 2's "earns its keep" test is met.
- `RpcOwner` Protocol disappears entirely. No more underscore-prefixed
  Session-internal members in a "narrow" contract.
- Tests fake the adapter or single collaborator, not `Session`. The ADR-0007
  forbidden-monkeypatch allowlist becomes drainable — the surface tests pinned
  (Session method bodies) is gone.
- Independent feature testability. A test for `ChatAPI` constructs a narrow
  set of fake collaborators (just a fake `RpcExecutor`, depending on what the
  test exercises). It does not need to know `Session` exists.
- Construction is explicit. `NotebookLMClient.__init__` reads as a wiring
  diagram. Replaces "Session has every method" with "every feature gets exactly
  what it asked for".
- Closes ADR-0013's runtime story. ADR-0013 framed the interface model; this ADR
  completes the implementation model.

**Unwanted:**

- Each new feature requires a new adapter (5-10 lines). Small ongoing cost. The
  cost is local to the feature module and visible at construction time — preferable
  to invisible growth of `Session`.
- Wider client composition wiring. Mitigated by
  `_client_assembly.py::_assemble_client(...)` and `_runtime/init.py`
  centralizing collaborator construction.
- Migration churn for existing tests. Tests that constructed a `Session` and then
  patched a method must migrate to fake-adapter construction. The ADR-0007
  program already pays for this migration; this ADR aligns the destination.
- Adapter method bodies are formally one-line forwards. They could be
  auto-generated. We do not — explicit method bodies keep the Protocol contract
  visible at the satisfier and let mypy catch shape mismatches at the adapter.

**Neutral:**

- `Session` may eventually be renamed (e.g. `_SessionLifecycle`, `_ClientCore`)
  once it owns only lifecycle and the collaborator graph. Out of scope for this
  ADR; defer until after the migration completes and the right name is obvious.

## Alternatives considered

- **Auto-generate forwards via `__getattr__`.** Rejected. Hides the coupling
  instead of solving it. Tests still monkeypatch the auto-generated surface;
  `Session`'s effective surface stays wide.

- **Dataclass-Session with method bodies on collaborators.** Equivalent end
  state, framed differently. We picked the feature-local-adapter framing
  because it co-locates the adapter with the feature that consumes it
  (single-file readability) and lets each feature evolve its runtime
  independently. A renamed-Session-as-dataclass-bag is still an option later,
  but the adapter pattern is the primitive that unlocks it.

- **Per-feature god-objects (Session-per-feature).** Each feature gets a narrow
  facade that owns its method bodies. Rejected — moves the god-object problem
  from one location to N, and doesn't share collaborator instances across
  features cleanly.

- **Keep `Session` as universal satisfier; drain the ADR-0007 allowlist by
  case-by-case exception.** Rejected. The allowlist exists because `Session`'s
  method surface is the test gravity well. Draining the allowlist while the
  gravity persists is treating the symptom; it grows back as new features are
  added.

- **Stop the decomposition; embrace `Session` as god-object.** Rejected. Gives
  up ADR-0013's gains. The maintenance cost of `Session` has been measurable
  across the multi-phase refactor program.

## Migration

The migration ran as a staged series of per-wave PRs, recorded in the
[Revision history](#revision-history) below; the standalone implementation
plan that scoped those waves has been retired now that the work is complete.
The post-refactoring plan 2026-05-27 (Stage B1 + Stage B2) closed the two
deferred follow-ups — see [Revision history](#revision-history).

## Revision history

### 2026-05-27 — Rule 3 Stage B closure (post-refactoring plan 2026-05-27 Stage B1, #1086 / #1089 / #1091)

Issue #1084 (deferred Rule 3 Stage B) closed. `compose_session_internals()`
became the composition root and later became
`_runtime/init.py::compose_client_internals`:
`Session.__init__` was
narrowed to `(*, collaborators, config, auth)` and the Stage A accessor
properties (`Session.collaborators`, `Session.session_transport`,
`Session.rpc_executor`) plus the `_get_rpc_executor` lazy factory were
deleted. Feature wiring now reads `composed.collaborators` /
`composed.transport` / `composed.executor` from the `ComposedSession`
returned by the helper. The deletion is recorded in the **Deleted**
section of this ADR's revision history.

### 2026-05-27 — Rule 4 chain-ownership carve-out (post-refactoring plan 2026-05-27 Stage B2, #1090 / #1092)

Issue #1085 (deferred `MiddlewareChainHost` extraction) closed.

- **#1090** introduced
  [`_middleware_chain_host.py`](../../src/notebooklm/_middleware/chain_host.py).
  The chain's tunable storage (`_authed_post_chain_terminal`,
  `_authed_post_chain`, `_rate_limit_max_retries`,
  `_server_error_max_retries`, `_refresh_retry_delay`) moved from
  `Session` instance attributes onto `MiddlewareChainHost` fields.
  Session retained the five names as writable `@property` descriptors
  that forward to the host so the long-standing fixture-rebind seam
  (`core._<attr> = ...` writing through to the live chain) survived
  the move. `_await_refresh` was routed through
  `MiddlewareChainHost.await_refresh` (dynamic delegation to
  `host._auth_refresh.await_refresh()`) for the same reason.
- **#1092** split
  `_runtime/init.py::wire_middleware_chain` / `build_runtime_transport`
  to take `chain_host: MiddlewareChainHost` directly. The chain's
  provider lambdas (`chain_provider`,
  `rate_limit_max_retries_provider`,
  `server_error_max_retries_provider`, `refresh_retry_delay_provider`)
  and the transport's `chain_provider` closure now dereference
  `chain_host.<attr>` directly on every attempt, instead of routing
  through the Session descriptors.

### 2026-05-27 — Carve-out close-out (delete Session-side chain descriptors)

The Session-side writable `@property` descriptors
(`_authed_post_chain_terminal`, `_authed_post_chain`,
`_rate_limit_max_retries`, `_server_error_max_retries`,
`_refresh_retry_delay`) and the `_await_refresh` delegate that briefly
bridged tests to `MiddlewareChainHost` during the carve-out have been
deleted. The chain host is the sole owner; the composition root
addresses it directly (`chain_host._authed_post_chain =
wired.authed_post_chain`, `authed_post_chain_terminal=chain_host._authed_post_chain_terminal`)
and tests rebind through `core._chain_host._<attr>` /
`core._chain_host.await_refresh()`. The Session-side retention list
keeps only the `_chain_host` reference (so feature code and tests can
reach the host); the descriptors and the `_await_refresh` delegate
are recorded in the historical deletion notes below.

### 2026-05-27 — Rule 2 adapter retirement (runtime-adapter decision)

The Rule 2 example dataclasses `ArtifactsRuntimeAdapter` and
`UploadRuntimeAdapter` introduced for the artifact and upload features
were retired. Each adapter only hid three stable collaborators
(`RpcCaller` + `TransportDrainTracker` + `ClientLifecycle`) and had
exactly one production satisfier, so they sat at the bottom of Rule
2's keep-vs-delete spectrum. The feature constructors now take their
three runtime collaborators (`rpc` + `drain` + `lifecycle`) as
keyword-only arguments directly — mirroring the post-ADR-0014 `ChatAPI`
pattern. The feature-local composite Protocols (`ArtifactsRuntime`,
`UploadRuntime`) and the local `DrainHookRegistration` Protocol were
deleted with their adapters; the mypy structural-satisfier guards near
the adapter definitions are no longer needed because the constructors
type each slot against its narrow shared Protocol directly. The
post-migration table and example code earlier in this ADR document the
historical Rule 2 satisfier pattern; this revision-history note is the
authoritative current-state pointer.

### 2026-05-28 — Host-protocol removal (plan `host-protocol-removal`)

Lifecycle / auth refresh no longer consume Session-shaped host
protocols (host-protocol-removal plan, Waves 1-3, PRs #1131-#1134).
The `_LifecycleHost` and `RefreshAuthCore` Protocols and the
`typing.cast(_LifecycleHost, core)` site in `_auth/session.py` were
deleted in Wave 2 (PR #1133); `refresh_auth_session` now takes the
five concrete collaborators (`auth`, `kernel`, `auth_coord`,
`lifecycle`, `cookie_persistence`) as keyword-only arguments and
`ClientLifecycle.save_cookies` takes the `CookiePersistence`
collaborator directly. Wave 3 (PR #1134) deleted the Session-level
auth/lifecycle forwards (`Session.lifecycle`, `Session.update_auth_tokens`,
`Session.update_auth_headers`) the retired Protocols had backed;
`NotebookLMClient.auth` now reads `self._auth` directly (set in
`__init__`) and `SourceUploadPipeline(auth=self._auth)` is wired the
same way. Wave 4 (PR for this revision) added regression lints under
`tests/_guardrails/test_session_runtime_boundaries.py` plus extensions to
`test_client_composition.py` and `test_no_session.py` so neither host
Protocol nor the deleted Session forwards can quietly come back.
The auth-refresh path is now fully explicit-collaborator-driven and
ADR-0014 Rule 3 holds end-to-end on the refresh code path as it
already did on the feature constructors.

### 2026-05-28 — Session elimination (plan `session-elimination-plan`)

`NotebookLMClient` now owns the final runtime graph directly: `ClientComposed`,
the `SessionCollaborators` bundle, the `RpcExecutor`, and the public feature
APIs. The concrete `Session` class and its module were deleted, along with the
session-method retention document and helper factory. Lifecycle entry points
(`__aenter__`, `__aexit__`, `close`, `drain`, and `is_connected`) call
`ClientLifecycle` and `TransportDrainTracker` directly. Static lints now enforce
that the deleted module, deleted helper names, deleted client attribute, and
`ClientComposed.collaborators` alias cannot return.

## Related decisions

- Builds on [ADR-0013](./0013-composable-session-capabilities.md) (capability
  Protocol pattern). This ADR is the runtime-side completion of ADR-0013's
  interface-side decoupling.
- Enables completion of the [ADR-0007](./0007-test-monkeypatch-policy.md)
  allowlist drain by removing the surface those entries currently pin.
- Closes the deferred goal in [ADR-0003](./0003-auth-facade-write-through.md)
  by example — `auth.py` follows the same delegate-to-private-module pattern
  Rule 1 applies to collaborators.
- Supersedes the former "Session as facade" and lifecycle-root framing in
  [`docs/architecture.md`](../architecture.md); the current architecture is
  "NotebookLMClient as composition root".
