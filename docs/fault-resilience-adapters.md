# Adapter resilience fault-coverage contract

**Status:** F0 inventory for R14. This document records the adapter cases to
add after the global construction-seam audit has selected the HTTP transfer
mechanisms. It does not change retry policy, adapter wire contracts, or runtime
ownership.

**Baseline:** `65dbd21d70f5be8c892da40a3660987b4118cd1c` (2026-09-06).
This is the R14 inventory from the resilience plan. The current portable
registry contains 25 cases: 16 Web and 9 Android. On this baseline,
`uv run pytest tests/integration/faults -q` completed with 25 passing in
1.99 seconds. The PR-sized stress command (64 cohorts, seed 42, concurrency
4) completed 64/64; the shell reported 9.97 seconds. These measurements are
only a starting point; F4 must remeasure after adding adapter cases.

## Current contract and evidence boundary

The selected representative operations are Web `LIST_NOTEBOOKS` and
`CREATE_NOTEBOOK`. `LIST_NOTEBOOKS` is an `IDEMPOTENT_SET_OP` read in
`_web/policy.py`; `CREATE_NOTEBOOK` is `NON_IDEMPOTENT_NO_RETRY`. A response
lost after the create dispatch is therefore a `NetworkError` marked
`CommitState.UNKNOWN` and `unconfirmed=True`, with exactly one dispatch and no
candidate promoted to a result. The existing loopback case
`web/committed_create_disconnect` proves these client facts independently with
the fault server's commit journal.

The adapter error contracts are intentionally different projections of the
same classified exception:

| Adapter | Transient read with retries disabled | Ambiguous create |
| --- | --- | --- |
| REST | HTTP 502, `error.category == "server"`, `retriable: true` | HTTP 502, `error.category == "rpc"`, `retriable: false`, `unconfirmed: true`, reconciliation hint and operation metadata |
| MCP | `ToolError` beginning `SERVER:` and containing `retriable=true` | `ToolError` beginning `RPC:`, containing `retriable=false` and `unconfirmed=true` |
| CLI (`--json`) | exit 1, `code == "NOTEBOOKLM_ERROR"` | exit 1, `code == "UNCONFIRMED_WRITE"`, `unconfirmed: true`, `commit_state: "unknown"`, and the inspect-before-retry hint |

The REST and MCP uncertain-write projection is deliberately RPC/non-retriable:
`_app.errors.classify` gives `unconfirmed` errors `ErrorCategory.RPC` before
their underlying `NetworkError` category. The CLI has a separate funnel that
overrides its underlying `NETWORK_ERROR` advice to `UNCONFIRMED_WRITE` so a
script is never invited to retry a possible duplicate.

Existing adapter tests establish the projection but do not establish this
composition over sockets:

| Surface | Existing mapping evidence | Existing socket evidence | R14 gap |
| --- | --- | --- | --- |
| REST | `tests/server/test_errors.py`, `test_notes.py`, and `test_integration_real_client.py` exercise envelopes through `TestClient` and VCR/fakes. | None through a REST route. `tests/_fault_server/web_scenarios.py` is client-only. | Inject a production Web client routed to `HttpFaultServer` into `create_app`, then call the route. |
| MCP tools | `tests/unit/mcp/test_errors.py` and `test_notebooks.py`; `tests/integration/mcp_vcr/test_error_contract.py` uses an in-memory FastMCP client and VCR. | None through an MCP tool. | Bind the same loopback client factory to `create_server` and call the tool. |
| CLI | `tests/unit/cli/test_error_handler.py` pins the `UNCONFIRMED_WRITE` override; `tests/integration/cli_vcr/test_error_contract.py` uses recorded failures. | None through a command. | Pass a loopback-client factory via `ctx.obj["client_factory"]` and invoke the public Click command. |
| REST artifact response | `tests/server/test_artifacts.py::test_cleanup_file_response_cleans_on_disconnect` calls the response ASGI object directly. | None; the test simulates `http.disconnect`. | Use a real loopback ASGI listener and close the downstream client after the first body chunk. |
| MCP signed download | `tests/unit/mcp/test_fileroutes.py::test_slot_held_response_releases_on_stream_abort` directly raises from `FileResponse.__call__`. | None; `TestClient` consumes the response. | Use a real FastMCP HTTP listener and abort `GET /files/dl/{token}` after a body prefix. |
| MCP detached chat | Unit tool/registry tests cover start, status, cancellation, TTL, and shutdown. | None for a caller disconnect. | Disconnect an actual MCP `chat_start` caller after the task has been accepted, then prove the server-owned task completes or is explicitly cancelled during teardown. |

VCR files remain evidence of RPC shape and adapter mapping. They cannot prove
connection-close timing, a downstream ASGI disconnect, or whether an
application write committed before its response disappeared.

## Required R14 cases

All new cases belong in a small adapter-specific F4 module, for example
`tests/integration/faults/test_adapter_faults.py`, and should share the normal
`ScenarioResult` reporting contract. `ASGITransport`, `TestClient`, and the
in-memory FastMCP client are sufficient for the first six mapping cases; they
are explicitly insufficient for the three downstream-disconnect cases.

| Case | Public entry point and fixture source | Expected public outcome and independent evidence | Counts, gates, budget, cleanup | Evidence disposition |
| --- | --- | --- | --- | --- |
| `R14-REST-read` | `GET /v1/notebooks`; `server.routes.notebooks.list_notebooks` -> `client.notebooks.list`. Reuse `Reply(503)` and `list_response` from `tests/_fault_server/http.py` and `web.py`. Create the app with a loopback Web-client context factory and `server_error_max_retries=0`. | HTTP 502 with the typed REST envelope: category `server`, retriable true, server hint. The response must not contain synthetic session data. Fault journal has one `LIST_NOTEBOOKS` request. | One upstream request; no gate needed. Client RPC timeout 0.5 s, adapter case watchdog 2 s, cleanup watchdog 2 s. Record route, status/category, retry budget, journal, client close, handlers drained, no remaining actions. | **New socket composition.** Existing VCR/`TestClient` mapping and `web/server_error_exhaustion` are reused only as contract/fixture evidence. |
| `R14-REST-create` | `POST /v1/notebooks` with `{"title":"Committed once"}`; `server.routes.notebooks.create_notebook` -> `_app.execute_notebook_create`. Reuse `Disconnect(commit_id="nb-committed")` from `web/committed_create_disconnect`. | HTTP 502 body has category `rpc`, retriable false, `unconfirmed: true`, `commit_state: "unknown"`, and a reconciliation hint; no success notebook object. Server commit journal is exactly `["nb-committed"]`. | Exactly one `CREATE_NOTEBOOK` dispatch even if the injected server retry budget is 5; one commit. No gate needed. Same 0.5/2/2 s budgets and cleanup records as the read. | **New socket composition.** Existing client scenario proves the one-send/commit facts; REST unit tests prove the unconfirmed body shape. |
| `R14-MCP-read` | MCP `notebook_list`; `mcp.tools.notebooks.notebook_list` -> `client.notebooks.list`. Reuse the same 503/list wire fixture and create `FastMCP` with the loopback factory. | `ToolError` starts `SERVER:` and includes `retriable=true`; server journal shows one read. The error text is scrubbed. | One request, no gate, retries zero, 0.5/2/2 s budgets. Explicitly finish the FastMCP client/lifespan before closing the fault server; record provider closed and no handlers/actions remain. | **New socket composition.** `mcp_vcr/test_error_contract.py` remains existing projection evidence. |
| `R14-MCP-create` | MCP `notebook_create`; `mcp.tools.notebooks.notebook_create` -> `_app.execute_notebook_create`. Reuse the committed-disconnect fixture. | `ToolError` starts `RPC:`, contains `retriable=false` and `unconfirmed=true`; no created payload. Commit journal says one committed id and request journal says one create. | One dispatch and one commit, no gate, no automatic replay; 0.5/2/2 s budgets; explicitly shut down MCP lifespan and fault server. | **New socket composition.** Existing MCP error tests prove flattening of the marker but not a real committed socket loss. |
| `R14-CLI-read` | `notebooklm list --json`; `cli.notebook_cmd.list_cmd` -> `ListSpec.fetch` -> `client.notebooks.list`. Reuse 503/list fixture. Provide the factory through `CliRunner.invoke(..., obj={"client_factory": factory})`; the factory accepts the CLI's `AuthTokens` and returns the loopback client context manager. | Click exit 1 and JSON `error: true`, `code: "NOTEBOOKLM_ERROR"`; no traceback or secret. Journal has one read. | One request, no gate, retries zero, 0.5 s RPC and 2 s runner watchdog. Close the returned client context and fault server after `CliRunner` returns; record journal/action drain. | **New socket composition.** `cli_vcr/test_error_contract.py` is existing VCR coverage for the same code mapping. |
| `R14-CLI-create` | `notebooklm create "Committed once" --json`; `cli.notebook_cmd.create_cmd` -> `_app.execute_notebook_create`. Reuse committed-disconnect fixture and the injected CLI factory. | Click exit 1; JSON contains `code: "UNCONFIRMED_WRITE"`, `unconfirmed: true`, unknown commit state, and the inspect-before-retry hint. It must not write active-notebook context or print a created id. Server observed one dispatch and one committed id. | One create / one commit, no gate, no retry. Use 0.5/2/2 s budgets. Record that no context path was written, then close client/server and require no unconsumed action. | **New socket composition.** Existing CLI unit tests verify the override with a marked exception, but not the full client path. |
| `R14-REST-download-disconnect` | `POST /v1/notebooks/{id}/artifacts/download`, then downstream response stream. The response owner is `_CleanupFileResponse` in `server.routes.artifacts`. The upstream artifact fixture comes from the F1 transfer-capable `HttpFaultServer` asset route using a valid decoded payload. | The raw loopback caller reads response headers and one body prefix then closes. The artifact is never published to a caller-owned destination; the route's private temporary directory is removed, download limiter is released, upstream response/client/writer settle, and a later REST probe succeeds. | One adapter request and one selected asset fetch. Gates: upstream full valid body, downstream first body chunk, then caller close. Give the fetch a bounded transfer timeout below a 4 s scenario watchdog; reserve 2 s for cleanup. Record received bytes/digest, temp-dir path/existence, limiter before/after, handler settlement, and recovery probe. | **New live adapter socket.** Existing direct-ASGI cleanup unit test does not qualify. This case depends on F1's direct-asset routing seam and F2's valid transfer fixture. |
| `R14-MCP-download-disconnect` | Signed `GET /files/dl/{token}` on a network-bound MCP server; owner is `_SlotHeldFileResponse` in `mcp._fileroutes`. Use a valid F1 asset fixture and a real `FileTransferConfig` token generated in the test. | Close the raw HTTP reader after one downstream body prefix. The `finally` releases the accepted download slot and removes its temp directory; token/session/capability text never enters `ScenarioResult`; a later signed download succeeds. | One accepted signed-route request and one upstream asset fetch, followed by one recovery download. Gates: fetched/spooled, first outbound body chunk, caller close. Record only token labels, byte count/digest, slot before/after, temp cleanup, and handler/task settlement. Use the same bounded transfer + 4 s scenario + 2 s cleanup allocation. | **New live adapter socket.** Existing unit test only raises an in-process stream error. |
| `R14-MCP-chat-start-disconnect` | Remote MCP `chat_start` with a full UUID notebook id and a gated `client.chat.ask`; then `chat_status`. The owner is `ChatTaskRegistry.start` / `_guard`, which calls `loop.create_task` inside `_detached_adapter_context`. | After the task claim and after the actual MCP request has entered response handling, close the caller connection. The detached ask remains pending/generating, is not cancelled by request loss, and `chat_status` later returns its result. A duplicate start while it is running returns `already_running` with the same task id and does not create a second ask. | One `chat_start` acceptance, one client ask, one duplicate attachment, one status completion; no upstream replay. Gates: task accepted, ask entered, response-send boundary, release ask. Set a finite test job timeout lower than the 4 s scenario watchdog and reserve 2 s cleanup. In `finally`, release gates, cancel/settle any retained task through registry shutdown, then close provider/listener. Record task state, accepted client epoch, task count, provider close, and no unhandled task exceptions. | **New live adapter socket.** Current in-memory MCP calls cannot cause request disconnect. This case is required even though the detached-task implementation already documents the ownership guarantee. |

The two read cases intentionally set retry counts to zero. They test the
adapter's visible transient-error mapping with a single controlled upstream
failure. Retry recovery and exhaustion remain covered by the backend scenarios;
R14 must not add a second retry-policy test under every adapter.

## Assembly and service-mechanics map

The construction seam audit is a prerequisite for implementation. The following
map identifies the current owners and the missing narrow test mechanisms; it is
not authorization to add a broad global patch.

| Concern | Current owner / usable seam | Required F1/F4 mechanism |
| --- | --- | --- |
| Web RPC client for all three adapters | `tests._fault_server.web.build_fault_client` uses `_assemble_client(..., async_client_factory=server.client_factory)` during synchronous construction. REST accepts a zero-arg `client_factory`; MCP accepts the same shape; CLI accepts an auth-argument factory from `ctx.obj`. | Add a test helper that wraps this existing client in an async context manager for REST/MCP and adapts the ignored CLI auth argument. Keep all substitutions local to construction; do not patch module bindings across an await. |
| REST client lifetime | `server.app.create_app(client_factory=...)` owns one `AsyncExitStack` client for its lifespan and `server._context.get_client` returns it. | The adapter helper must enter the application lifespan before invoking routes and assert the factory's client has closed before fault-server teardown. |
| MCP client lifetime | `mcp.server.create_server(client_factory=...)` creates `ClientProvider`; `_shutdown` first calls `state.chat_tasks.aclose()`, then closes the provider. | Use a real FastMCP HTTP host for disconnect cases and expose only a test-local ready address/stop handle. The fixture must wait for shutdown rather than treating `server.should_exit` as cleanup evidence. |
| CLI client lifetime | `cli.auth_runtime.resolve_client_factory` reads `ctx.obj["client_factory"]`; commands use `async with` around it. | Use the exact factory injection instead of monkeypatching `NotebookLMClient` or auth globals. Supply synthetic `AuthTokens` through the existing CLI fixture; do not read a profile or mutate environment while a cohort is running. |
| Direct artifact download | REST's `download_core.execute_download` ultimately uses direct asset-client construction, while the route spools to `_CleanupFileResponse`. The current RPC-only `HttpFaultServer.client_factory` maps only `notebook.google.com` and `accounts.google.com`. | F1 must map every selected logical asset host to loopback and route direct asset-client construction through a private, instance-owned factory. The default remains the production transport. Test a missing mapping fails closed rather than reaching the network. |
| Downstream disconnect observation | `TestClient`, `ASGITransport`, and FastMCP's in-memory client consume/abstract the server body. | Add one reusable raw loopback HTTP-reader helper: wait for headers and a controlled body prefix, close the socket, then await a server-side settlement gate. It is only for R14/F4 downstream-ownership assertions. |
| REST spool cleanup evidence | `_CleanupFileResponse.__call__` owns cleanup in a `finally`; the path is local to the route. | The F4 fixture needs a per-app, default-preserving way to observe the created temporary path or a route-owned cleanup event. A process-wide `tempfile.mkdtemp` patch held across streaming is disallowed because concurrent cohorts could observe it. |
| MCP signed-download cleanup evidence | `_SlotHeldFileResponse.__call__` owns final cleanup. `_inflight_downloads` is presently a module-global single-process counter. | Record the counter before/after only in an isolated process or introduce an app-owned limiter/observer after separate review. Do not run this case concurrently while relying on a global exact count. The signed token itself must never be exported. |
| Detached job request boundary | `ChatTaskRegistry.start` performs an atomic claim and creates a plain loop task; task work enters `_detached_adapter_context`; status owns result projection. | Add a response-send gate in the live MCP test host or its test transport so the test can close after acceptance but before request completion. The gate must not wrap `ChatTaskRegistry` or replace the task factory, or it would stop testing the actual detachment path. |

## Reporting gates and acceptance checks

Every new case records a `plan` before opening the fault server. At minimum it
contains adapter, public entry point, logical routes, selected retry/job budget,
expected dispatch/commit counts, and named gates. It records a redacted
`adapter_outcome`, the server journal/commit journal, and `cleanup`. The report
must reject an unused scripted action, an unexpected request, an extra mutation
dispatch, a missing settlement record, or an unfinished task.

The following checks are mandatory for the whole R14 set:

- One transient-read failure per adapter is mapped at that adapter boundary;
  the fault action is consumed exactly once.
- One ambiguous write per adapter sends once, has one independently recorded
  commit, exposes unknown/unconfirmed evidence, and never exposes a success
  object or retry instruction.
- REST and MCP downstream abort tests prove their owning response finalizer ran.
  The MCP detached-job abort separately proves request-bound cancellation does
  not own the job.
- All teardown runs in `finally` under an independent cleanup watchdog. It
  releases gates, settles clients/providers/tasks/writers, removes only
  test-owned temporary directories, and leaves no active fault handlers.
- Reports store synthetic generation labels and route labels, never cookies,
  bearer values, signed-link tokens, full local paths, request objects, or raw
  exception locals.

## Scope and sequencing decision

R14 is an F4 adapter-boundary addition. The first six cases can use the existing
RPC-only loopback server once the shared factory adapter is added. The three
disconnect cases are blocked on the F1 transfer routing/assembly audit and,
for the REST/MCP download cases, valid F2 transfer fixtures. `chat_start` may be
implemented after the live MCP listener helper exists, but it must retain the
same F4 cleanup contract.

No F1 code should be started from this inventory. In particular, direct asset
client routing, the transfer-capable fault service, a live adapter listener, and
any temp-path or limiter observer need the global seam audit's selected
instance-owned design first.
