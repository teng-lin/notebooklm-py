# Session method retention (ADR-014 Rule 4)

Source classification for every method (and `@property`) currently defined on
`Session` in [`src/notebooklm/_session.py`](../src/notebooklm/_session.py).

**Companion lint:** [`tests/_lint/test_session_retention.py`](../tests/_lint/test_session_retention.py)
AST-parses `_session.py`, enumerates every method/property on the `Session`
class, and asserts each one appears in the inventory below with a valid
disposition. Adding a new method without a row here fails the lint at PR time.

**Status:** Wave 10 of the [session-decoupling plan](session-decoupling-plan-2026-05-26.md)
(Phase 3, Task 5.1). Wave 11 (sub-waves 11a–11c) will delete the
`delete in Wave 11` rows in three clusters and move them to the **Deleted**
section at the bottom of this file. Wave 11c also tightens the lint to
forbid any `delete in Wave 11` rows once the cluster deletions land.

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
| `compatibility forward` | One-line forward to a collaborator method; kept only because in-tree callers (mostly tests) reach it via `Session`. Wave 11 deletes these and migrates callers to the owning collaborator. |

## Dispositions

| Disposition | Meaning |
|---|---|
| `retain — <reason>` | Stays on `Session` after Wave 11. |
| `delete in Wave 11 (<cluster>)` | Scheduled for deletion in one of the three Wave 11 sub-wave PRs. Cluster names match [phase-3.md](../.sisyphus/phases/session-decoupling/phase-3.md): `drain-and-operation` (11a), `metrics-and-kernel` (11b), `transport-and-reqid` (11c). |

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
| `update_auth_tokens` | RefreshAuthCore Protocol surface | retain — `refresh_auth_session(core)` calls `core.update_auth_tokens(...)` at [`_auth/session.py:67`](../src/notebooklm/_auth/session.py); also referenced in the AST-guard prose at `tests/unit/test_concurrency_refresh_race.py:386` (the guard inspects `AuthRefreshCoordinator.update_auth_tokens` directly, but the Session-side delegate is the Protocol seam) |
| `update_auth_headers` | RefreshAuthCore Protocol surface | retain — `refresh_auth_session(core)` calls `core.update_auth_headers()` at [`_auth/session.py:68`](../src/notebooklm/_auth/session.py) |
| `register_drain_hook` | compatibility forward | delete in Wave 11 (`drain-and-operation`) — one-line forward to `TransportDrainTracker.register_drain_hook`; migrate callers to `session.collaborators.drain_tracker` |
| `operation_scope` | compatibility forward | delete in Wave 11 (`drain-and-operation`) — forward to `TransportDrainTracker.operation_scope` |
| `drain` | compatibility forward | delete in Wave 11 (`drain-and-operation`) — forward to `TransportDrainTracker.drain` |
| `metrics_snapshot` | compatibility forward | delete in Wave 11 (`metrics-and-kernel`) — forward to `ClientMetrics.snapshot` |
| `_increment_metrics` | compatibility forward | delete in Wave 11 (`metrics-and-kernel`) — forward to `ClientMetrics.increment` |
| `record_upload_queue_wait` | compatibility forward | delete in Wave 11 (`metrics-and-kernel`) — forward to `ClientMetrics.record_upload_queue_wait` |
| `_emit_rpc_event` | compatibility forward | delete in Wave 11 (`metrics-and-kernel`) — forward to `ClientMetrics.emit_rpc_event` |
| `kernel` (property) | compatibility forward | delete in Wave 11 (`metrics-and-kernel`) — forward to `self._kernel`; readers migrate to `session.collaborators.kernel` |
| `live_cookies` | compatibility forward | delete in Wave 11 (`metrics-and-kernel`) — forward to `self.get_http_client().cookies` |
| `authuser` (property) | compatibility forward | delete in Wave 11 (`metrics-and-kernel`) — forward to `self.auth.authuser` |
| `account_email` (property) | compatibility forward | delete in Wave 11 (`metrics-and-kernel`) — forward to `self.auth.account_email` |
| `authuser_query` | compatibility forward | delete in Wave 11 (`metrics-and-kernel`) — forward to `auth.authuser_query(self.authuser, self.account_email)` |
| `authuser_header` | compatibility forward | delete in Wave 11 (`metrics-and-kernel`) — forward to `auth.format_authuser_value(...)` |
| `get_http_client` | RefreshAuthCore Protocol surface / compatibility forward | delete in Wave 11 (`metrics-and-kernel`) — forward to `Kernel.get_http_client`; the `RefreshAuthCore` Protocol still references `get_http_client`, so Wave 11b MUST first migrate the Protocol (or `refresh_auth_session(core)`) to call `core.collaborators.kernel.get_http_client()` before the Session forward can be removed |
| `next_reqid` | compatibility forward | delete in Wave 11 (`transport-and-reqid`) — forward to `ReqidCounter.next_reqid` |
| `bound_loop` (property) | compatibility forward | delete in Wave 11 (`transport-and-reqid`) — forward to `ClientLifecycle.get_bound_loop` with defensive `isinstance` |
| `_refresh_request_for_current_auth` | compatibility forward | delete in Wave 11 (`transport-and-reqid`) — forward to `SessionTransport.refresh_request_for_current_auth`; the AST guard at `tests/unit/test_concurrency_refresh_race.py:222` already inspects `SessionTransport.refresh_request_for_current_auth` directly, so no guard migration needed |
| `_perform_authed_post` | compatibility forward | delete in Wave 11 (`transport-and-reqid`) — forward to `SessionTransport.perform_authed_post`; production callers (`_chat_transport`) already call `SessionTransport.perform_authed_post` directly. Verify no surviving production caller via `rg "session\._perform_authed_post\(\|core\._perform_authed_post\(\|host\._perform_authed_post\b" src/notebooklm` before deleting. Test callers in `tests/unit/test_authed_post_pipeline.py` migrate to `session.session_transport.perform_authed_post` or `make_fake_core` |
| `transport_post` | compatibility forward | delete in Wave 11 (`transport-and-reqid`) — forward to `_perform_authed_post`; no production callers (`rg "\.transport_post\(" src/ tests/` returns 0) |
| `save_cookies` | RefreshAuthCore Protocol surface / compatibility forward | delete in Wave 11 (`transport-and-reqid`) — forward to `ClientLifecycle.save_cookies`; the `RefreshAuthCore` Protocol still references `save_cookies`, so Wave 11c MUST first migrate the Protocol (or `refresh_auth_session(core)`) to call `core.collaborators.lifecycle.save_cookies(core, jar, path)` before the Session forward can be removed |

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

Wave 11 sub-wave PRs append entries here (preserving the deleting PR's SHA in
the section sub-header) as the `delete in Wave 11` rows above are dropped.
Empty at Wave 10.
