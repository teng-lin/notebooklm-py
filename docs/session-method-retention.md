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
| `RefreshAuthCore Protocol surface` | Method required by the `RefreshAuthCore` Protocol in [`src/notebooklm/_auth/session.py`](../src/notebooklm/_auth/session.py); `refresh_auth_session(core)` calls it on the Session passed as `core`. |
| `compatibility forward` | One-line forward to a collaborator method; kept only because in-tree callers (mostly tests) reached it via `Session`. Wave 11 (sub-waves 11a, 11b, 11c) deleted every compatibility forward; no row in the live inventory now carries this category. The label is retained in this glossary for the **Deleted** section below and as the disposition lint's vocabulary for any future short-lived forward. |

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
| `rpc_call` | public API forward | retain — pinned by `tests/unit/test_public_shims.py:1048-1089` (`NotebookLMClient.rpc_call` forwards through it) |
| `_authed_post_chain_terminal` | middleware chain leaf | retain — live chain leaf wired by `_session_init.wire_middleware_chain` (`authed_post_chain_terminal=self._authed_post_chain_terminal` at [`_session.py:411-417`](../src/notebooklm/_session.py)) |
| `_await_refresh` | provider-closure capture target | retain — captured as bound-method (`refresh_callable=host._await_refresh`) by [`_session_init.py:430`](../src/notebooklm/_session_init.py) |
| `assert_bound_loop` | provider-closure capture target | retain — captured via lambda (`bound_loop_check=lambda: host.assert_bound_loop()`) by `build_session_transport` at [`_session_init.py:395`](../src/notebooklm/_session_init.py); late-bound so a test reassigning `core.assert_bound_loop = mock` still steers the live check |
| `_get_rpc_semaphore` | provider-closure capture target | retain — passed as `rpc_semaphore_factory=self._get_rpc_semaphore` to `wire_middleware_chain` at [`_session.py:416`](../src/notebooklm/_session.py); has real body (lazy semaphore creation) reading `self._max_concurrent_rpcs` / `self._rpc_semaphore`, not a forward |
| `_get_rpc_executor` | lazy collaborator factory | retain — builds the `RpcExecutor` collaborator the first time `rpc_call` or the Stage A `rpc_executor` accessor needs it; real construction logic, not a forward |
| `collaborators` (property) | Stage A accessor | retain — Stage A accessor (ADR-014 Rule 3); deleted under Stage B when `build_collaborators` ownership moves to `NotebookLMClient` |
| `session_transport` (property) | Stage A accessor | retain — Stage A accessor; exposes late-bound `SessionTransport` not present on `SessionCollaborators` |
| `rpc_executor` (property) | Stage A accessor | retain — Stage A accessor; exposes lazy `RpcExecutor` not present on `SessionCollaborators` |
| `update_auth_tokens` | RefreshAuthCore Protocol surface | retain — `refresh_auth_session(core)` calls `core.update_auth_tokens(...)` from [`_auth/session.py`](../src/notebooklm/_auth/session.py); also referenced in the AST-guard prose at `tests/unit/test_concurrency_refresh_race.py:386` (the guard inspects `AuthRefreshCoordinator.update_auth_tokens` directly, but the Session-side delegate is the Protocol seam) |
| `update_auth_headers` | RefreshAuthCore Protocol surface | retain — `refresh_auth_session(core)` calls `core.update_auth_headers()` from [`_auth/session.py`](../src/notebooklm/_auth/session.py) |

## Stage-A and Rule-4 attribute capture targets (context, not lint-enumerated)

The `_rate_limit_max_retries`, `_server_error_max_retries`, and
`_refresh_retry_delay` slots on `Session` are plain instance attributes (not
methods), assigned in `__init__` from the validated config. They are
**provider-closure capture targets**: the `MiddlewareChainBuilder` reads them
through lambdas at [`_session_init.py:427-429`](../src/notebooklm/_session_init.py)
so post-construction integration-test mutation (e.g.
`session._rate_limit_max_retries = 0`) still steers the live chain. They are
**retain**ed for the same reason `_await_refresh` is retained — deletion
breaks the chain wiring. They are not enumerated by the AST lint (which scans
method definitions, not assignments to `self.X` inside `__init__`); this
section documents them for the next architecture refactor reader.

## Follow-up ADR-014 issues

The two follow-up issues filed per ADR-014 close-out (Wave 6 / Task 6.2):

- **Stage B (Rule 3 completion):** move `build_collaborators` ownership from
  `Session` to `NotebookLMClient`; delete `Session.collaborators` /
  `Session.session_transport` / `Session.rpc_executor` accessors.
- **`MiddlewareChainHost` extraction (Rule 4 completion):** extract a
  `MiddlewareChainHost` collaborator owning `_authed_post_chain_terminal` +
  the `_rate_limit_max_retries` / `_server_error_max_retries` /
  `_refresh_retry_delay` tunables; `Session` holds it like any other
  collaborator.

Both issues remain open after Wave 12; the Stage A accessors and the chain
seams on Session listed above are explicitly carved out until those issues
land.

## Deleted

The three Wave 11 sub-wave PRs (11a, 11b, 11c) each appended a
cluster-keyed section here, preserving the deleting commit's SHA in
the sub-header. This section is the historical record of every
compatibility forward that once lived on `Session`; the lint above
enforces that no Session method exists today without either a
`retain — <reason>` row in the **Inventory** above or a `deleted in
Wave 11<sub>` row in one of the cluster sub-sections below.

### Wave 11a — drain-and-operation cluster (commit `80a54fda`)

| Method | Category | Disposition |
|---|---|---|
| `register_drain_hook` | compatibility forward | deleted in Wave 11a (commit `80a54fda`) — was a one-line forward to `TransportDrainTracker.register_drain_hook`. Callers now reach the tracker directly (`session._drain_tracker.register_drain_hook(...)` in tests; production callers use `ArtifactsRuntimeAdapter.register_drain_hook`). |
| `operation_scope` | compatibility forward | deleted in Wave 11a (commit `80a54fda`) — was a forward to `TransportDrainTracker.operation_scope`. Callers now reach the tracker directly (`session._drain_tracker.operation_scope(...)` in tests; production callers use `ArtifactsRuntimeAdapter.operation_scope` / `UploadRuntimeAdapter.operation_scope`). |
| `drain` | compatibility forward | deleted in Wave 11a (commit `80a54fda`) — was a forward to `TransportDrainTracker.drain`. `NotebookLMClient.drain` now calls `self._session._drain_tracker.drain(...)` directly. |

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
