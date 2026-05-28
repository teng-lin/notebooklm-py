# Session method retention (ADR-014 Rule 4)

Source classification for every method (and `@property`) currently defined on
`Session` in [`src/notebooklm/_session.py`](../src/notebooklm/_session.py).

**Companion lint:** [`tests/_lint/test_session_retention.py`](../tests/_lint/test_session_retention.py)
AST-parses `_session.py`, enumerates every method/property on the `Session`
class, and asserts each one appears in the inventory below with a valid
disposition. Adding a new method without a row here fails the lint at PR time.

**Status:** Wave 11 of the [session-decoupling plan](session-decoupling-plan-2026-05-26.md)
(Phase 3, Task 5.2) is complete. The three sub-wave PRs (11a, 11b, 11c)
deleted every `delete in Wave 11` row and moved the entries to the
**Deleted** section at the bottom of this file. Wave 11c also tightened
the [`tests/_lint/test_session_retention.py`](../tests/_lint/test_session_retention.py)
lint to assert the **retain-only invariant**: every method on `Session`
must carry a `retain — <reason>` disposition. No method may be tagged
`delete in Wave 11` (the cluster deletions have all landed; the
transitional disposition is gone from the recognised set).

Plan [`host-protocol-removal`](../.sisyphus/phases/host-protocol-removal/phase-1.md)
closed on 2026-05-28 with Waves 0-4. The retained Session surface is now
the **Inventory** table below in full: the four lifecycle hot-path
entries (`open` / `close` / `is_open` / `_keepalive_loop`), the
`__init__` constructor, the `drain` public-API forward, the
`assert_bound_loop` provider-closure capture target, and the Stage B1
composition primitives (`_bind_transport`,
`_bind_chain_metadata`, `_bind_executor`, `_require_constructed`).
Stage A accessors (`collaborators` / `session_transport` / `rpc_executor`)
were deleted in Stage B1 PR 2 of the post-refactoring plan and are
recorded in the **Deleted** section below; the chain leaf
(`_authed_post_chain_terminal`) and the retry-budget tunables live on
`MiddlewareChainHost` per the [Chain-ownership carve-out](#chain-ownership-carve-out-closed)
section. Wave 4 of `host-protocol-removal` added regression lints
([`tests/_lint/test_session_runtime_boundaries.py`](../tests/_lint/test_session_runtime_boundaries.py),
[`tests/_lint/test_client_composition.py::test_client_self_session_access_is_allowlisted`](../tests/_lint/test_client_composition.py),
[`tests/_lint/test_session_retention.py::test_wave_3_deletions_stay_out_of_live_inventory`](../tests/_lint/test_session_retention.py))
that pin the post-Wave-3 surface from both directions so neither the
`_LifecycleHost` / `RefreshAuthCore` host shape nor the deleted
auth-forward methods can quietly come back.

## Categories

| Category | Meaning |
|---|---|
| `constructor` | `__init__` — instance setup, not a candidate for deletion. |
| `lifecycle` | `open` / `close` / `is_open` / `_keepalive_loop` — open-time + drain-on-close orchestration. |
| `public API forward` | Forward that backs a documented public surface on `NotebookLMClient`; AST-pinned by a test. |
| `middleware chain leaf` | Wired into the live middleware chain by `_session_init.wire_middleware_chain`; deletion breaks the chain. |
| `provider-closure capture target` | Read live by a provider lambda passed to `wire_middleware_chain` / `build_session_transport`; deletion breaks the chain wiring. Capture mode is noted per row. |
| `Stage A accessor` | Typed accessor added in Wave 6 so `NotebookLMClient.__init__` can wire features against collaborators (ADR-014 Rule 3 Stage A). Deleted under Rule 3 Stage B (Wave 7 follow-up). |
| `lazy collaborator factory` | Real factory body (not a forward) backing a Stage A accessor or a public-API forward. |
| `RefreshAuthCore Protocol surface` | Historical category. Was used for methods required by the `RefreshAuthCore` Protocol in `src/notebooklm/_auth/session.py`; `refresh_auth_session(core)` called them on the Session passed as `core`. The Protocol itself was deleted in Wave 2 of plan [`host-protocol-removal`](../.sisyphus/phases/host-protocol-removal/phase-1.md) (`refresh_auth_session` now takes five explicit keyword-only collaborators), and Wave 3 deleted the last two Session-level forwards (`update_auth_tokens` / `update_auth_headers`) that carried this category. No row in the live **Inventory** carries this category today; the label is retained in this glossary for the **Deleted** section rows that historically carried it. |
| `compatibility forward` | One-line forward to a collaborator method; kept only because in-tree callers (mostly tests) reached it via `Session`. Wave 11 (sub-waves 11a, 11b, 11c) deleted every compatibility forward; no row in the live inventory now carries this category. The label is retained in this glossary for the **Deleted** section below and as the disposition lint's vocabulary for any future short-lived forward. |
| `composition write-once setter` | Stage B1 PR 1 ([post-refactoring plan 2026-05-27](post-refactoring-plan-2026-05-27.md)) primitive: a `_bind_*` method that accepts exactly one bind for a late-bound dependency and raises `RuntimeError` on a second call. Reserved for `compose_session_internals`. DORMANT in PR 1 (Session.__init__ still inline-constructs the transport / chain); becomes the single assignment site in PR 2. |
| `composition guard` | Stage B1 PR 1 ([post-refactoring plan 2026-05-27](post-refactoring-plan-2026-05-27.md)) primitive: a fail-fast helper that raises `RuntimeError("Session not fully constructed: <attr> is None")` when an entry point runs before the composition root has finished binding required late-bound dependencies. Inert under inline construction (PR 1); load-bearing in PR 2 when the composition root moves into `NotebookLMClient.__init__`. |

## Dispositions

| Disposition | Meaning |
|---|---|
| `retain — <reason>` | Stays on `Session` after Wave 11. **The only valid disposition** after Wave 11c tightened the lint. |

The transitional `delete in Wave 11 (<cluster>)` disposition was used in
Wave 10 to schedule the three sub-wave cluster deletions
(`drain-and-operation` = 11a, `metrics-and-kernel` = 11b,
`transport-and-reqid` = 11c). All three cluster PRs landed; the
disposition is gone from the recognised set, and any new row that
tries to use it fails the
[`tests/_lint/test_session_retention.py`](../tests/_lint/test_session_retention.py)
lint at PR time.

## Inventory

| Method | Category | Disposition |
|---|---|---|
| `__init__` | constructor | retain — instance setup |
| `open` | lifecycle | retain — open-time setup (loop binding + keepalive task) |
| `close` | lifecycle | retain — drain + transport teardown |
| `is_open` (property) | lifecycle | retain — public open-state read |
| `_keepalive_loop` | lifecycle | retain — background task body; introspected by `test_client_keepalive` |
| `assert_bound_loop` | provider-closure capture target | retain — captured via lambda (`bound_loop_check=lambda: host.assert_bound_loop()`) by `build_session_transport` at [`_session_init.py:395`](../src/notebooklm/_session_init.py); late-bound so a test reassigning `core.assert_bound_loop = mock` still steers the live check |
| `_bind_transport` | composition write-once setter | retain — Stage B1 composition primitive (post-refactoring plan 2026-05-27); the write-once binder for `Session._transport`. Load-bearing after PR 2 — `Session.__init__` leaves `_transport` at `None` and `compose_session_internals` is the single assignment site. |
| `_bind_chain_metadata` | composition write-once setter | retain — Stage B1 composition primitive (post-refactoring plan 2026-05-27); the write-once binder for the auxiliary chain artifacts (`_chain_builder` / `_middlewares`). The `_authed_post_chain` slot itself is owned by `MiddlewareChainHost` and assigned exactly once by `compose_session_internals` (`chain_host._authed_post_chain = wired.authed_post_chain`); this binder stores only the auxiliary metadata. Load-bearing — `Session.__init__` leaves the metadata slots at `None` and `compose_session_internals` is the single assignment site. |
| `_bind_executor` | composition write-once setter | retain — Stage B1 composition primitive (post-refactoring plan 2026-05-27); the write-once binder for `Session._rpc_executor`. Load-bearing after PR 2 — the lazy `_get_rpc_executor` factory was deleted; `compose_session_internals` is the single assignment site and the binding is never re-nulled by `close()`. |
| `_require_constructed` | composition guard | retain — Stage B1 composition primitive (post-refactoring plan 2026-05-27); fail-fast helper used by `open` / `close` to assert the named binding is non-None. Load-bearing — `Session.__init__` leaves the late-bound slots at `None`, and any caller that exercises a Session outside `compose_session_internals` trips the guard. |
| `drain` | public API forward | retain — narrow public method (one-line forward to `TransportDrainTracker.drain`). Backs `NotebookLMClient.drain` so the composition root does not dereference the private `_drain_tracker` slot on the session. The Wave 11a deletion row in the **Deleted** section below refers to an earlier compatibility-forward incarnation; this row is the boundary-focused re-introduction (narrow forward, single caller, AST-guarded by `tests/_lint/test_client_composition.py::test_client_does_not_dereference_session_privates`). |

## Chain-ownership carve-out (closed)

The chain-ownership carve-out is closed. The retry-budget tunables
(`_rate_limit_max_retries` / `_server_error_max_retries` /
`_refresh_retry_delay`), the chain slot (`_authed_post_chain`), the
chain leaf (`_authed_post_chain_terminal`), and the dynamic
`_await_refresh` delegate are all owned by `MiddlewareChainHost`
([`_middleware_chain_host.py`](../src/notebooklm/_middleware_chain_host.py)).
The chain's `MiddlewareChainBuilder` provider lambdas and the
transport's `chain_provider` closure read the host attributes live;
tests rebind through `core._chain_host._<attr>`. There are no
Session-side aliases or descriptor forwards in front of the host.

## Follow-up ADR-014 issues

The two follow-up issues filed per ADR-014 close-out (Wave 6 / Task 6.2):

- **Stage B (Rule 3 completion):** move `build_collaborators` ownership from
  `Session` to `NotebookLMClient`; delete `Session.collaborators` /
  `Session.session_transport` / `Session.rpc_executor` accessors. **CLOSED by
  Stage B1 PR 2 of the post-refactoring plan (2026-05-27)** — the composition
  root moved to `compose_session_internals()` (moved from `_session.py` to
  `_session_init.py` during Session-elimination Phase 1); the three Stage A
  accessor properties and the `_get_rpc_executor` lazy factory are listed in
  the **Deleted** section below.
- **`MiddlewareChainHost` extraction (Rule 4 completion):** extract a
  `MiddlewareChainHost` collaborator owning `_authed_post_chain_terminal` +
  the `_rate_limit_max_retries` / `_server_error_max_retries` /
  `_refresh_retry_delay` tunables; `Session` holds it like any other
  collaborator. **CLOSED** — the host owns the storage and the live chain
  reads from the host directly. The transitional Session-side writable
  `@property` descriptors and the `_await_refresh` delegate that
  preserved the historical test seams during the carve-out have been
  removed; tests rebind on the host (`core._chain_host._<attr>`).

## Deleted

The three Wave 11 sub-wave PRs (11a, 11b, 11c) each appended a
cluster-keyed section here, preserving the deleting commit's SHA in
the sub-header. This section is the historical record of every
compatibility forward that once lived on `Session`; the lint above
enforces that no Session method exists today without either a
`retain — <reason>` row in the **Inventory** above or a `deleted in
Wave 11<sub>` row in one of the cluster sub-sections below.

### Session-elimination Phase 1 — client holders (2026-05-28)

| Method | Category | Disposition |
|---|---|---|
| `_get_rpc_semaphore` | provider-closure capture target | deleted in Phase 1 of plan `session-elimination-plan` — the lazy semaphore state moved to `ClientComposed.get_rpc_semaphore`, and `compose_session_internals` now passes that holder method as `rpc_semaphore_factory`. The moved holder preserves the previous `None` opt-out and lazy-once `asyncio.Semaphore` construction while removing `Session._max_concurrent_rpcs` / `Session._rpc_semaphore` ownership. |

### Wave 11a — drain-and-operation cluster (commit `80a54fda`)

| Method | Category | Disposition |
|---|---|---|
| `register_drain_hook` | compatibility forward | deleted in Wave 11a (commit `80a54fda`) — was a one-line forward to `TransportDrainTracker.register_drain_hook`. Callers now reach the tracker directly (`session._drain_tracker.register_drain_hook(...)` in tests; production wiring at `NotebookLMClient.__init__` passes the tracker as `ArtifactsAPI`'s `drain` collaborator, which calls `register_drain_hook` on it directly). |
| `operation_scope` | compatibility forward | deleted in Wave 11a (commit `80a54fda`) — was a forward to `TransportDrainTracker.operation_scope`. Callers now reach the tracker directly (`session._drain_tracker.operation_scope(...)` in tests; production wiring passes the tracker as the `drain` collaborator on `ArtifactsAPI` / `SourceUploadPipeline`, which call `operation_scope` on it directly). |
| `drain` | compatibility forward | deleted in Wave 11a (commit `80a54fda`) — was a forward to `TransportDrainTracker.drain`. **Subsequently re-introduced** as a boundary-focused narrow public forward (see the `drain` row in the **Inventory** table above) so the client composition root has a stable name to call instead of the private `_drain_tracker` slot. |

### Wave 11b — metrics-and-kernel cluster (commit `37b16a79`)

| Method | Category | Disposition |
|---|---|---|
| `metrics_snapshot` | compatibility forward | deleted in Wave 11b (commit `37b16a79`) — was a forward to `ClientMetrics.snapshot`. `NotebookLMClient.metrics_snapshot` now calls `self._session.collaborators.metrics.snapshot()`; in-tree tests reach `core._metrics_obj.snapshot()` directly. |
| `_increment_metrics` | compatibility forward | deleted in Wave 11b (commit `37b16a79`) — was a forward to `ClientMetrics.increment`. No production caller remained; the historical `_middleware_auth_refresh` reference was prose only. |
| `record_upload_queue_wait` | compatibility forward | deleted in Wave 11b (commit `37b16a79`) — was a forward to `ClientMetrics.record_upload_queue_wait`. `NotebookLMClient.__init__` now passes `collaborators.metrics.record_upload_queue_wait` to the upload pipeline; in-tree tests pass `core._metrics_obj.record_upload_queue_wait`. |
| `_emit_rpc_event` | compatibility forward | deleted in Wave 11b (commit `37b16a79`) — was a forward to `ClientMetrics.emit_rpc_event`. The live middleware chain already reads `metrics` directly; no production caller surfaced via Session. |
| `kernel` (property) | compatibility forward | deleted in Wave 11b (commit `37b16a79`) — was a forward to `self._kernel`. `NotebookLMClient.__init__` now passes `collaborators.kernel` to the upload pipeline; in-tree tests use `core._kernel`. |
| `live_cookies` | compatibility forward | deleted in Wave 11b (commit `37b16a79`) — was a forward to `self.get_http_client().cookies`. The canonical home is `Kernel.cookies` (also reachable via `Kernel.get_http_client().cookies`). |
| `authuser` (property) | compatibility forward | deleted in Wave 11b (commit `37b16a79`) — was a forward to `self.auth.authuser`. Callers read `auth.authuser` directly. |
| `account_email` (property) | compatibility forward | deleted in Wave 11b (commit `37b16a79`) — was a forward to `self.auth.account_email`. Callers read `auth.account_email` directly. |
| `authuser_query` | compatibility forward | deleted in Wave 11b (commit `37b16a79`) — was a forward to `notebooklm._auth.account.authuser_query`. Callers import the helper directly. |
| `authuser_header` | compatibility forward | deleted in Wave 11b (commit `37b16a79`) — was a forward to `notebooklm._auth.account.format_authuser_value`. Callers import the helper directly. |
| `get_http_client` | RefreshAuthCore Protocol surface / compatibility forward | deleted in Wave 11b (commit `37b16a79`) — was a forward to `Kernel.get_http_client`. The `RefreshAuthCore` and `_AuthRefreshHost` Protocols were migrated in the same commit to require a `_kernel: Kernel` slot instead of `get_http_client`; the two call sites in `_auth/session.py` and `_session_auth.py` now read `core._kernel.get_http_client()` / `host._kernel.get_http_client()`. `Session._kernel` is already an instance attribute (assigned from `collaborators.kernel` in `__init__`), so live `Session` instances satisfy the new Protocol shape without further changes. |

### Wave 11c — transport-and-reqid cluster (commit `579c7a35`)

| Method | Category | Disposition |
|---|---|---|
| `next_reqid` | compatibility forward | deleted in Wave 11c (commit `579c7a35`) — was a forward to `ReqidCounter.next_reqid`. Callers reach the counter directly (`core._reqid.next_reqid(...)` in tests; production code in `ChatAPI.ask` already uses `self._reqid.next_reqid(...)` since Wave 8). |
| `bound_loop` (property) | compatibility forward | deleted in Wave 11c (commit `579c7a35`) — was a forward to `ClientLifecycle.get_bound_loop` with a defensive `isinstance`. Tests now call `core._lifecycle.get_bound_loop()` directly; the `isinstance` guard is unnecessary because the canonical accessor on `ClientLifecycle` already returns `asyncio.AbstractEventLoop \| None`. |
| `_refresh_request_for_current_auth` | compatibility forward | deleted in Wave 11c (commit `579c7a35`) — was a forward to `SessionTransport.refresh_request_for_current_auth`. The AST guard at `tests/unit/test_concurrency_refresh_race.py:222` already inspects `SessionTransport.refresh_request_for_current_auth` directly, so no guard migration was needed. |
| `_perform_authed_post` | compatibility forward | deleted in Wave 11c (commit `579c7a35`) — was a forward to `SessionTransport.perform_authed_post`. Production callers (`_chat_transport`, `RpcExecutor`) already call `SessionTransport.perform_authed_post` directly; test callers in `tests/unit/test_authed_post_pipeline.py` / `test_chain_wiring.py` / `test_session_lifecycle.py` / `test_rate_limit_default.py` migrated to `core._transport.perform_authed_post(...)`. The keyword-only signature contract is now pinned on the canonical collaborator method via `test_chain_wiring.test_perform_authed_post_signature_unchanged`. |
| `transport_post` | compatibility forward | deleted in Wave 11c (commit `579c7a35`) — was a `parse_label`-renaming forward over `_perform_authed_post` retained for the Tier-13 chat contract. The chat path moved to `SessionTransport.perform_authed_post` directly in Wave 8; no production or test callers remained at deletion time. |
| `save_cookies` | RefreshAuthCore Protocol surface / compatibility forward | deleted in Wave 11c (commit `579c7a35`) — was a forward to `ClientLifecycle.save_cookies`. The `RefreshAuthCore` Protocol in `_auth/session.py` was narrowed in the same commit: the `save_cookies` method requirement was dropped and replaced with a `collaborators: SessionCollaborators` accessor; `refresh_auth_session(core)` now persists rotated cookies through `core.collaborators.lifecycle.save_cookies(core, jar)` (the canonical chokepoint that already serialises with keepalive and close saves). The Session host argument is widened to `_LifecycleHost` via `typing.cast` — the production `Session` satisfies both `RefreshAuthCore` and `_LifecycleHost` structurally; the cast is the typing-level acknowledgement that `RefreshAuthCore` deliberately stays narrow. Test callers in `tests/unit/test_auth_cookie_save_race.py` / `test_save_lock_contract.py` / `test_client_keepalive.py` / `test_cookie_persistence.py` migrated to `core._lifecycle.save_cookies(core, jar)`. |

### Stage B1 PR 2 — composition-root inversion (post-refactoring plan 2026-05-27)

Stage B1 PR 2 inverted the composition root: `compose_session_internals()`
owned the full collaborator-bundle / transport / chain / executor construction
sequence, and `Session.__init__` was narrowed to
`(*, collaborators, config, auth)`. Session-elimination Phase 1 moved that
helper from `_session.py` to `_session_init.py` while keeping the same
composition-root role. The Stage A accessor properties added by PR #1069
(Wave 6) and the lazy `_get_rpc_executor` factory all collapse to direct reads
on the `ComposedSession` returned by the helper.

| Method | Category | Disposition |
|---|---|---|
| `_get_rpc_executor` | lazy collaborator factory | deleted in Stage B1 PR 2 (PR #1089, commit `313bbef1`) — `compose_session_internals` constructs the `RpcExecutor` and drives `Session._bind_executor(executor)` exactly once. Callers read `core._rpc_executor` directly (it is bound by the composition root and never re-nulled by `close()`); the close-time `host._rpc_executor = None` line in `ClientLifecycle.close` was dropped in the same PR. |
| `collaborators` (property) | Stage A accessor | deleted in Stage B1 PR 2 (PR #1089, commit `313bbef1`) — `NotebookLMClient.__init__` reads `composed.collaborators` from the `ComposedSession` returned by `compose_session_internals` and stores it on `self._collaborators`. The Stage A wrapper was never an architectural goal — Wave 6 noted it would be deleted under Stage B. |
| `session_transport` (property) | Stage A accessor | deleted in Stage B1 PR 2 (PR #1089, commit `313bbef1`) — `NotebookLMClient.__init__` reads `composed.transport` from the `ComposedSession` and threads it into `ChatAPI`'s `transport=` kwarg directly. Same Stage B-under-ADR-014 disposition as `collaborators`. |
| `rpc_executor` (property) | Stage A accessor | deleted in Stage B1 PR 2 (PR #1089, commit `313bbef1`) — `NotebookLMClient.__init__` reads `composed.executor` from the `ComposedSession` and passes it directly as the `rpc` collaborator to every feature API (`SourcesAPI`, `NotebooksAPI`, `NoteService`, `ResearchAPI`, `SettingsAPI`, `SharingAPI`, `ArtifactsAPI`, `SourceUploadPipeline`, `ChatAPI`). Same Stage B-under-ADR-014 disposition as `collaborators`. The feature-local composite-runtime adapters (`ArtifactsRuntimeAdapter`, `UploadRuntimeAdapter`) named in the original PR #1089 disposition were retired in the follow-up runtime-adapter-decision change; feature constructors take their three runtime collaborators (`rpc` + `drain` + `lifecycle`) directly. |

### Wave 5 of the post-refactoring plan — public RPC entry-point retirement

| Method | Category | Disposition |
|---|---|---|
| `rpc_call` | public API forward | deleted in Wave 5 of the post-refactoring plan (2026-05-27) — `NotebookLMClient.rpc_call` now dispatches through `self._rpc_executor.rpc_call(...)` directly, where `self._rpc_executor` is the `composed.executor` reference captured during `NotebookLMClient.__init__` (same instance every feature API receives via `composed.executor`). The Session-side wrapper was a compatibility forward for the legacy direct-`AsyncMock`-assignment test idiom; the migrated tests now use `make_fake_core(rpc_call=AsyncMock(...))` injection or rebind the executor's `rpc_call` directly on a real Session. Public `NotebookLMClient.rpc_call(method, params)` signature is unchanged. |

### Wave 3 of plan `host-protocol-removal` — Session auth-forward retirement

Wave 3 finished the migration started in Waves 0-2 of plan
[`host-protocol-removal`](../.sisyphus/phases/host-protocol-removal/phase-1.md):
the `NotebookLMClient.auth` property and `SourceUploadPipeline(auth=...)`
constructor argument now read the client-owned `self._auth` field
directly (set in `NotebookLMClient.__init__` and kept aliased with
`Session.auth` per the Auth Instance Invariant), and the three
remaining Session-level auth/lifecycle forwards were deleted. Every
production caller now routes through the explicit collaborator kwargs:
`refresh_auth_session(auth=..., kernel=..., auth_coord=...,
lifecycle=..., cookie_persistence=...)` (Wave 2 signature) draws
`lifecycle` from `self._collaborators.lifecycle`; the auth-refresh
hop inside that helper invokes
`auth_coord.update_auth_tokens(auth=..., csrf=..., session_id=...)` /
`auth_coord.update_auth_headers(auth=..., kernel=...)` directly; the
four integration tests that previously poked the headers via
`core.update_auth_headers()` migrated to the same explicit-kwargs
shape against `core._auth_coord` / `client._collaborators.auth_coord`.

| Method | Category | Disposition |
|---|---|---|
| `lifecycle` (property) | public API forward | deleted in Wave 3 of plan `host-protocol-removal` — was a narrow read-only accessor returning the `ClientLifecycle` collaborator. `NotebookLMClient.refresh_auth` now passes `self._collaborators.lifecycle` to `refresh_auth_session(...)` directly (Wave 2 rewired the caller); the `tests/unit/test_concurrency_refresh_race.py` shell-client tests use the Wave 1 `build_refresh_client_shell` helper, which populates `client._collaborators` from the composed bundle so the lifecycle is reachable via the same explicit-collaborator path as production. |
| `update_auth_tokens` | RefreshAuthCore Protocol surface | deleted in Wave 3 of plan `host-protocol-removal` — was a one-line `await self._auth_coord.update_auth_tokens(auth=self.auth, csrf=csrf, session_id=session_id)` delegate retained as the `RefreshAuthCore` Protocol surface. Wave 2 deleted the `RefreshAuthCore` Protocol itself (`refresh_auth_session` now takes the five concrete collaborators as keyword-only args); no surviving caller needed the Session-level delegate. The AST guard at `tests/unit/test_concurrency_refresh_race.py::test_update_auth_tokens_has_no_await_inside_mutation_block` inspects `AuthRefreshCoordinator.update_auth_tokens` directly and is unaffected. |
| `update_auth_headers` | RefreshAuthCore Protocol surface | deleted in Wave 3 of plan `host-protocol-removal` — was a one-line `self._auth_coord.update_auth_headers(auth=self.auth, kernel=self._kernel)` delegate retained as the `RefreshAuthCore` Protocol surface. The four integration tests that previously poked it (`test_error_paths_vcr.py`, `test_auto_refresh.py` (×2), `test_session_integration.py` (×3), `test_auth_refresh_vcr.py`) migrated to the canonical coordinator method with explicit `auth=` / `kernel=` kwargs. |
