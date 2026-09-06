# Fault-resilience case contracts

**Status:** F0 inventory for R1, R2, R7, R8, R10, and R11. Existing evidence is
identified below; every row marked **new socket evidence** remains pending until its owning
test is implemented and passes.

**Source baseline:** `65dbd21d70f5be8c892da40a3660987b4118cd1c`

This inventory turns the selected families in the resilience fault-coverage plan into concrete
cases. It is intentionally based on current public entry points, registered policy, operation
journals, and lifecycle ownership. A zero server request count is supporting evidence only. A
mutation is `NOT_SENT` only when its production operation metadata says so.

## Baseline and common contract

The mandatory registry baseline is 25 scenarios: 16 Web scenarios in
`tests/_fault_server/web_scenarios.py` and 9 Android scenarios in
`tests/_fault_server/android_scenarios.py`. The new cases below add coverage; they do not replace
or narrow any registered socket scenario. The [measured baseline](fault-resilience-coverage.md#baseline) records suite and stress durations. The plan's
provisional stress bounds remain 64 iterations per PR cohort, 400 scheduled iterations, 600 seconds
aggregate, 15 seconds per scenario, and 15 minutes per job; these are budgets, not measured results.

All new cases use one local client and only local fault services. Each case has a watchdog of at
most 5 seconds around a gate wait or task settlement unless the runner applies a smaller scenario
deadline. After releasing every gate, teardown must await callers, shared producers, poll leaders,
server handlers, response bodies, and `client.close()`. A case passes cleanup only when the fault
server reports no unexpected request or unsettled handler, the client's active generation has no
admitted calls or registered children, and a same-client recovery read succeeds. Cases that
intentionally close the client use a reopened-client recovery read instead.

The invariant labels are those in the plan: I1 replay/evidence, I2 budgets, I4 cancellation and
generation ownership, I5 shared-work progress, I6 partial-success evidence, and I8 recovery.
Transfer publication and credential-hop invariants are owned by the transfer families; R7 only
composes their proven transfer path with admission pressure.

## Production contracts that constrain the cases

- Web `LIST_NOTEBOOKS`, `GET_NOTEBOOK`, and `RENAME_NOTEBOOK` are
  `IDEMPOTENT_SET_OP`. `GET_NOTEBOOK` updates the last-viewed timestamp, but replay remains a
  last-write-wins set operation. `CREATE_NOTEBOOK` is `NON_IDEMPOTENT_NO_RETRY`: it has no client
  deduplication token and same-title candidates never authorize replay. Android derives its total
  replay table from the same registered policies.
- A mutation may replay after a failure only when its policy is replay-safe, or when positive
  production evidence is `REJECTED` or `NOT_SENT`. `UNKNOWN` never becomes replay authorization.
  Android `AddTentativeSources` with decoded gRPC `UNAUTHENTICATED` is the existing positive
  refusal producer: the server could not have registered content, so the mapper records
  `CommitState.REJECTED`.
- The Web chain is retry outside auth refresh. HTTP 5xx and 429 counters are separate. A decoded
  auth failure at HTTP 200 re-enters the executor with the same `RefreshBudget` and aggregate
  `RuntimeDeadline`, but currently constructs a fresh retry-middleware frame. Therefore its
  per-class retry counters do **not** yet survive that re-entry. R2-01 is expected to expose this
  production gap; it must not be weakened to use an HTTP-level auth status that avoids the
  recursive path.
- Android unary calls keep refresh, rate-limit, and server-error counters in one session loop.
  Both backends admit one logical RPC call before internal retries, so the RPC permit is held
  across retry and refresh attempts.
- `CallSupervisor` owns RPC admission and registered workflow children. Raw artifact transfer
  body I/O uses its backend-owned HTTP client outside the RPC semaphore. Descriptor/list and
  finalize RPCs still consume RPC permits. R7 gates these stages independently rather than
  asserting that a complete transfer holds an RPC permit.
- Artifact poll waiters shield the shared poll leader. Cancelling one or the last waiter does not
  make that waiter the leader's owner. The leader is a registered child and is settled by its
  terminal result or lifecycle drain. Web refresh and Android bearer mint use the same shielded
  waiter principle.
- `execute_generation` opens one public operation scope around kickoff and optional wait.
  Artifact generation writes are non-replayable after ambiguous transport loss. A decoded task ID
  is retained through polling; a returned terminal failure is a typed failed outcome, while poll
  transport exceptions propagate.
- Verified research import sends its mutation once. Rows visible during reconciliation are only
  candidates and the original failure still escapes. Completeness-aware artifact lookup reports
  `UNKNOWN` when an unavailable component could contain the requested artifact. Web collection
  create records a decoded mutation `CONFIRMED` before its mandatory list readback and preserves
  that primary evidence if the readback fails.

## R1 — replay and commit evidence

The six cases below cover every listed fault boundary across a replay-safe read, a replay-safe
stable-set mutation, a non-replayable create, and the protocol's real positive-refusal producer.
They are not a Cartesian product.

| Case | Backend and public entry point | Wire fixture and fault | Required result, counts, gates, and cleanup | Evidence and invariants |
| --- | --- | --- | --- | --- |
| R1-01 pre-send create | Web `client.notebooks.create("queued")` inside `client.operation(timeout=0.05)` | Reuse `tests/_fault_server/web.py:list_response` for a gated `notebooks.list()` that holds the sole RPC permit. No create action is queued on the server. | Construct with `max_concurrent_rpcs=1`, `server_error_max_retries=0`. Wait until the read owns the permit, enqueue create, allow its operation budget to expire, then release the read. Create raises `OperationTimeoutError`; its primary `CREATE_NOTEBOOK` entry is `NOT_SENT` with no attempt. Current operation-timeout projection leaves its recovery field `NONE`; retryability is represented by the timeout classification rather than rewriting the journal. Exactly 1 read request, 0 create requests, 0 create commits. Recovery list succeeds; all admissions settle. | Existing lower-layer evidence: `tests/unit/test_operation_context.py::test_rpc_queue_expiry_uses_operation_timeout_and_never_dispatches` and `tests/unit/test_operation_journal.py::test_real_web_terminal_records_verified_connect_failure_as_not_sent`. **New socket evidence.** I1, I2, I5, I8. |
| R1-02 connection refusal | Web `client.notebooks.list()` | Reserve and close a loopback port, point the production Web transport at it, and make no replacement server. This is a transport failure before any peer receives bytes; it is distinct from R1-01's production `NOT_SENT` evidence. | `server_error_max_retries=2` gives 3 connect attempts and a public `NetworkError`. No local server request or commit exists. The case records transport exception type and retry telemetry rather than inventing mutation metadata. A new local server is then attached through the same instance-owned routing seam and the same client succeeds. | Existing unit evidence for terminal classification: `tests/unit/test_operation_journal.py::test_real_web_terminal_records_verified_connect_failure_as_not_sent`. **New socket evidence.** I1, I2, I8. |
| R1-03 read response loss | Web `client.notebooks.list()` | First `LIST_NOTEBOOKS` action accepts the request and aborts before a complete response without a commit; second uses `web.py:list_response`. | `server_error_max_retries=1`; result is the second response. Exactly 2 read requests, 0 mutation commits, one server-error retry. Gate after first request recognition so ordering is deterministic. Recovery list succeeds and both handlers settle. | Existing retry cases cover 503 and truncation, but not this request-observed loss/convergence assertion. **New socket evidence.** I1, I2, I8. |
| R1-04 stable-set commit/loss | Prefer Android `client.notebooks.rename(notebook_id, "Stable")`; Web is acceptable only if its rename response fixture is already available in F1 | Reuse the production rename request/response protobuf from `tests/unit/android/test_notebook_mutations.py`. First `MutateProject` commits title then aborts `UNAVAILABLE`; second replies with the same final title. | `server_error_max_retries=1`; public result follows the backend's established return type. Exactly 2 rename requests and 2 accepted set operations, but one final notebook title. Assert the same request value on both attempts and one retry telemetry event. Gate first commit before abort. Recovery `get` returns `Stable`; channel and admission settle. | Policy evidence: `_web/policy.py` registered `RENAME_NOTEBOOK` stable-set entry and Android derived table. Existing unit mutation tests cover request/decoding only. **New socket evidence.** I1, I2, I8. |
| R1-05 positive refusal | Android `client.sources.add_text(notebook_id, "Title", "Body")` | Reuse `AddTentativeSourcesRequest` bytes from `tests/unit/android/test_source_proto_contract.py`; local gRPC returns `UNAUTHENTICATED` before registration. | Public `AuthError` has `commit_state is REJECTED`, `unconfirmed is False`, and the bound registration attempt is `REJECTED`. Exactly 1 tentative-registration request, 0 registered IDs, 0 `AddSources` requests, and no refresh/mint retry. Recovery read succeeds after restoring the synthetic credential/service. | Existing policy evidence: `tests/unit/android/test_errors.py::test_tentative_registration_auth_status_is_a_decoded_refusal`, `tests/unit/android/test_session.py::test_unary_folds_decoded_rejection_into_the_active_attempt`, and `tests/unit/android/test_source_writes.py::test_confirmed_registration_rejections_are_not_marked_unconfirmed`. **New socket evidence.** I1, I6, I8. |
| R1-06 ambiguous create commit/loss | Both registered backend scenarios stay required: Web `client.notebooks.create("Committed once")` and Android equivalent | Web uses `Disconnect(commit_id=...)`; Android uses `commit_abort(UNAVAILABLE)` with existing create protobuf reply state. | Public Web `NetworkError` / Android `ServerError` is `UNKNOWN` and unconfirmed. Exactly 1 create request and 1 create commit despite retry settings above zero. No candidate authorizes a second send. Recovery list/get observes the committed notebook where supported, and cleanup settles. | **Existing socket evidence:** Web `committed_create_disconnect`; Android `commit_lost_response`. I1, I6, I8. |

`AT_LEAST_ONCE_ACCEPTED` remains at policy/unit scope for this family. No public opt-in is needed
to satisfy the selected read/set/create matrix, and the socket harness must not manufacture one.

## R2 — retry, refresh, and aggregate deadline composition

All variants use Web `client.notebooks.list()` and the real decoded-auth re-entry path. Fixture
payloads come from `tests/_fault_server/web.py:list_response` and `homepage_response`. The request
sequence uses a server failure, a syntactically successful batchexecute response whose RPC row
decodes as auth failure, one homepage refresh, and the later response named in each case.

| Case | Exact budgets and request sequence | Required result, evidence, gates, and cleanup | Evidence status and invariants |
| --- | --- | --- | --- |
| R2-01 composed rate exhaustion | `server_error_max_retries=2`, `rate_limit_max_retries=1`, long per-RPC/operation deadline. Sequence: 503, decoded auth failure, refresh success, 503, 429, 429. | Public `RateLimitError`; exactly 5 RPC POSTs, 1 homepage refresh, 2 server retries and 1 rate retry. The old/fresh credential generations must be recorded. Gate decoded-auth response until the first retry is visible, and gate the refresh response until its single shared flight is visible. A recovery list with fresh credentials succeeds. | Existing pieces: `server_error_recovery`, `auth_refresh`, `rate_limit_exhaustion`; middleware unit tests. **New composed socket evidence.** I1, I2, I5, I8. |
| R2-02 server counter survives re-entry | `server_error_max_retries=1`, long rate and time budgets. Sequence: 503, decoded auth failure, refresh success, then 503; a success reply is queued only as an unexpected-dispatch sentinel. | Public `ServerError`; exactly 3 RPC POSTs, 1 homepage refresh, and 1 server retry. The sentinel reply is untouched. Current production is expected to dispatch it because recursive re-entry resets the local server counter; that is a regression failure requiring shared attempt state, not a reason to change the expected count. Recovery succeeds with a new public call. | No existing composed evidence. **New socket evidence; expected to reveal the current counter-reset gap.** I1, I2, I8. |
| R2-03 aggregate deadline | Sequence 503, decoded auth failure, refresh, 429. Use `server_error_max_retries=3`, `rate_limit_max_retries=3`, and `ClientConfig(runtime=RuntimeOptions(operation_timeout=0.20))`. The 429 has a `Retry-After` longer than the remaining operation budget. | Public `OperationTimeoutError`, exactly 3 RPC POSTs and 1 refresh; no post-expiry fourth POST. Use a real clock, wait for the retry/backoff gate to start, then let the operation deadline cancel it. Releasing the sleeper/gate later must not dispatch. Recovery succeeds after the original operation scope has settled. | Existing injectable-clock evidence: retry middleware deadline tests and `tests/unit/test_app_operation_scopes.py`. **New real-clock socket evidence.** I2, I4, I8. |
| R2-04 caller cancellation during backoff | Same 503, decoded-auth, refresh, 429 prefix as R2-03, with a long aggregate deadline and deterministic retry gate/sleeper notification. | Cancel the public task only after backoff begins. `CancelledError` escapes, no next RPC is sent after releasing the gate, the RPC permit and middleware task settle, and a same-client list succeeds. Counts: 3 RPC POSTs, 1 refresh, 0 post-cancel retry. | No existing composed socket evidence. **New socket evidence.** I2, I4, I8. |

R2 requires an instance-owned attempt ledger (or equivalent context) if its pending test confirms
the decoded-auth reset. It must retain separate rate and server counters; a single global attempt
counter would change the public contract.

## R7 — admission, queue ownership, and close under mixed load

These cases construct one client with `max_concurrent_rpcs=1` unless stated otherwise. The read is
`client.notebooks.list()`. The poll is `client.artifacts.wait_for_completion(...)`. The transfer
must use the public artifact download chosen and proven by R5 for that backend; R7 does not create
a smaller fake transfer path. Descriptor/finalize RPC and HTTP body gates are distinct.

| Case | Fault schedule and budgets | Required counts, cleanup, and public result | Evidence status and invariants |
| --- | --- | --- | --- |
| R7-01 mixed progress | Gate one read while it owns the sole RPC permit. Start a download whose descriptor RPC queues, and a poll waiter whose list RPC also queues. Release the read, then separately gate the download body outside RPC admission. | Before release: 1 read request, 0 descriptor requests, 0 poll requests. After read release both queued RPC stages eventually dispatch; no scheduler ordering is asserted. While body is gated, another read can acquire the RPC permit, proving body I/O is outside it. Release body; transfer integrity uses R5 assertions, poll terminates, and all work succeeds. | Existing semaphore evidence: `tests/integration/concurrency/test_max_concurrent_rpcs.py`; poll ownership evidence: `tests/unit/test_artifacts_polling_retries.py`. **New mixed socket evidence after R5.** I2, I5, I8. |
| R7-02 queued caller cancellation | Gate a read in the sole permit, enqueue a second public read, wait until supervisor queue telemetry observes it, then cancel the queued caller. | Cancelled read raises `CancelledError`, sends 0 requests, and leaves no permit. Releasing the holder allows a third read to dispatch and succeed. Counts: 2 successful read requests total, none for the cancelled call. | Existing lower-layer cancellation evidence in `tests/unit/test_operation_context.py`. **New socket evidence.** I4, I5, I8. |
| R7-03 explicit-scope queue expiry | Same holder, but enqueue a read inside `client.operation(timeout=0.05)`. | `OperationTimeoutError`, 0 requests for expired work, and no dispatch after holder release. A later read succeeds. This is the explicit public-operation-scope nested-budget variant. | Existing unit evidence: `test_rpc_queue_expiry_uses_operation_timeout_and_never_dispatches`. **New socket evidence.** I2, I5, I8. |
| R7-04 configured queue expiry | Same schedule, with `ClientConfig(runtime=RuntimeOptions(max_concurrent_rpcs=1, operation_timeout=0.05))` and no explicit operation timeout at the call site. | Same no-dispatch and recovery assertions as R7-03. The error identifies the configured default operation budget. This stays separate from R7-03 because `operation(None)` is intentionally unbounded while the default selector inherits configuration. | Existing unit evidence: `tests/unit/test_operation_context.py::test_default_operation_deadline_uses_public_timeout_type` and `tests/unit/test_app_operation_scopes.py::test_public_default_selector_inherits_configured_budget`. **New socket evidence.** I2, I5, I8. |
| R7-05 close under load | With one admitted gated read, one queued read, a gated download body, and a poll leader/follower flight, call `client.close(drain=False)`. | Active and queued public tasks end with their established cancellation/lifecycle error; no queued request dispatches during close. HTTP response/body, poll leader, refresh/poll registry entries, RPC permits, channels, and server handlers settle. Reopen creates a new generation and a read succeeds. Old-generation completion cannot publish. | Existing lifecycle evidence: `deadline_and_cancellation`, `close_reopen`, `tests/unit/test_client_lifecycle_waves.py`, and `tests/unit/test_session_close.py`. **New mixed socket evidence after R5.** I4, I5, I8. |
| R7-06 repeated cancellation during settlement | Start `execute_source_clean` with one child confirmed and another gated in cleanup, cancel its owner, then cancel it again while shielded child settlement is observable. This first-party batch uses socket-backed public deletes. | The original cancellation escapes only after owned children settle. Batch metadata keeps the confirmed first item, the interrupted item's production state, and any unscheduled tail as `NOT_SENT`; no duplicate delete is sent. No task or permit remains and recovery read succeeds. | Existing direct evidence: `tests/unit/test_source_delete_outcomes.py::test_cancel_settles_children_and_keeps_unattempted_tail`; lifecycle recancellation evidence in `tests/unit/test_client_lifecycle_waves.py`. **New socket evidence.** I1, I4, I6, I8. |

## R8 — shared refresh, mint, and poll flights

| Case | Backend, entry point, and schedule | Required result, counts, cleanup, and budgets | Evidence status and invariants |
| --- | --- | --- | --- |
| R8-01 one-of-many refresh cancellation | Web: two concurrent `client.notebooks.list()` calls receive decoded auth failures behind a gate and join one delayed homepage refresh. Cancel one waiter after both have joined; then release refresh and fresh RPC replies. | Cancelled waiter raises `CancelledError`; survivor returns the fixture notebook. Exactly 1 homepage refresh and at most 4 list POSTs (2 stale + only non-cancelled fresh replay; a cancelled replay already dispatched is allowed only when the server journal proves it preceded cancellation). Record the exact ordering. Same-client recovery succeeds and the refresh slot settles. | Existing in-process evidence: `tests/integration/concurrency/test_refresh_cancellation_propagation.py::test_waiter_cancellation_does_not_kill_shared_refresh`; coalesced socket evidence without cancellation: `auth_refresh_coalesced`. **New socket evidence.** I4, I5, I8. |
| R8-02 last poll waiter cancellation | Two public `client.artifacts.wait_for_completion(task_id)` waiters share a gated poll leader. Cancel both waiters, the second only after the first has escaped. | Both callers raise `CancelledError`; cancelling the last waiter does not directly cancel the registered leader. The leader either consumes its already-planned terminal reply or is drained by client close, exactly once. Poll request count follows the gated plan, registry becomes empty, no orphan exception is logged, and recovery read succeeds. | Existing ownership evidence: `tests/unit/test_artifacts_polling_retries.py::test_wait_for_completion_follower_cancellation_does_not_cancel_leader_or_later_waiter` and `tests/integration/concurrency/test_artifact_poll_dedupe.py`. **New socket evidence.** I4, I5, I8. |
| R8-03 shared mint failure then recovery | Android: two concurrent `client.notebooks.get(notebook_id)` calls share a synthetic bearer minter that fails once before dispatch. Repair the same minter, then issue a third get. | First wave receives the same sanitized `AuthError` identity; exactly 1 failed mint and 0 gRPC requests. Third call performs 1 new mint and 1 successful gRPC request. No replacement client is built. | Existing unit evidence: `tests/unit/android/test_auth.py::test_failure_is_shared_by_identity_then_later_call_retries`; existing socket `minter_failure` covers only one waiter. **New concurrent socket evidence.** I5, I8. |
| R8-04 delayed old generation | Web: stale generation begins a gated homepage refresh through public list calls. Force close, reopen, and complete one new-generation list/refresh before releasing the old homepage response. | Old waiter receives the established lifecycle/cancellation failure. Exactly one old and at most one new refresh. Releasing the old response cannot overwrite new CSRF/session/cookie state; a later request sends only new credentials. All old response/client resources settle. | Existing unit evidence: `tests/unit/test_web_transport_epoch_fence.py::test_auth_refresh_waiter_cannot_publish_into_reopened_generation`; `close_reopen` covers RPC generation without late refresh. **New socket evidence.** I4, I5, I8. |

## R10 — generate-and-wait workflow

Use the framework-free first-party entry `notebooklm._app.generate.execute_generation` with an
`AudioGenerationRequest`. It drives public `client.artifacts.generate_audio(...)` and
`client.artifacts.wait_for_completion(...)` under one `client.operation(USE_DEFAULT)`. Reuse the
production create/list response shapes pinned by
`tests/integration/test_artifact_generation_idempotency.py`, `tests/unit/test_artifacts_coverage.py`, and
`tests/unit/test_generation_state.py`; do not invent a second workflow decoder.

| Case | Stateful fixture, gates, and budgets | Required public result/evidence and counts | Evidence status and invariants |
| --- | --- | --- | --- |
| R10-01 accepted kickoff, failed poll | Create reply decodes task `task-1`; first artifact-list poll returns transient 503 until the poller's three transient retries are exhausted. | Propagate `ServerError`; exactly 1 create request and 4 poll RPC attempts. Operation metadata retains the confirmed kickoff/task identity where production exposes it. No second kickoff. Release all poll gates, drain leader, and recover with a list. | Existing pieces: create non-replay tests and `tests/unit/test_artifacts_polling_retries.py::test_wait_for_completion_retry_exhausted`. **New workflow socket evidence.** I1, I2, I6, I8. |
| R10-02 lost kickoff acknowledgement | Create commits `task-2` then disconnects before response. | Public `NetworkError` is `UNKNOWN`/unconfirmed. Exactly 1 create request, 1 commit, 0 poll requests, and no task ID is fabricated from server-only state. Reconciliation may report a candidate only if the current production contract does so; it cannot return success or authorize replay. Recovery list may observe `task-2`. | Existing in-process transport evidence: `tests/integration/test_artifact_generation_idempotency.py::test_create_artifact_503_does_not_re_post`. **New commit/loss socket evidence.** I1, I6, I8. |
| R10-03 terminal failure | Create returns `task-3`; list poll returns the real failed artifact status. | `execute_generation` returns the established `GenerationOutcome(status="failed", task_id="task-3", error=...)`. Exactly 1 kickoff and 1 terminal poll unless the decoder requires a preceding pending row, in which case the fixture pins 2 polls. No exception-to-success conversion and no retry of kickoff. | Existing policy evidence: `tests/unit/test_creation_conformance.py::test_first_party_waited_generation_raises_on_actual_failed_backend` for strict mind maps and CLI terminal-failure render tests. **New socket evidence for audio typed outcome.** I1, I6, I8. |
| R10-04 shared poll timeout and original budget | Create returns `task-4`. Attach a second public waiter to `task-4`; keep poll responses pending. Set request poll timeout longer than configured `operation_timeout=0.20`, and spend part of that budget behind a gated kickoff response. | One kickoff, one shared poll flight, and no poll dispatch after the original operation deadline. `execute_generation` raises `OperationTimeoutError`; the independent follower observes its own timeout/result according to its own operation budget and retains `task-4`. Follower knob mismatch warning/callback cardinality follow current policy. Registry and leader settle. | Existing lower-layer evidence: `tests/unit/test_operation_context.py::test_poll_follower_warns_for_ignored_knobs_and_callback_cardinality` and `test_poll_leader_survives_first_waiters_operation_deadline`. **New socket evidence.** I2, I4, I5, I6, I8. |

## R11 — batch settlement, complete reads, and mandatory readback

| Case | Backend and public/first-party entry | Fixture, counts, and gates | Required result/evidence and cleanup | Evidence status and invariants |
| --- | --- | --- | --- | --- |
| R11-01 earlier batch member confirmed | Web `execute_source_clean(preview, client=client)`, which calls public `client.sources.delete_many_with_outcomes` and then one public delete per occurrence | Use 12 source IDs so the first ten-member wave and an unscheduled tail are visible. Gate delete `b`; let delete `a` return confirmed, then make `b` fail and cancel/stop the wave before the tail. Exact requests depend on the intentionally concurrent first wave, so assert per-ID counts: `a=1`, `b=1`, every dispatched peer `<=1`, tail `=0`; no confirmed ID is replayed. | Escaping error metadata keeps `a` `CONFIRMED`, `b` at its production failure state, and unscheduled tail `NOT_SENT`, in original occurrence order. `whole_request_retriable` is false. Settle siblings before escape; recovery list succeeds. | Existing direct evidence: `tests/unit/test_source_delete_outcomes.py::test_cleanup_timeout_keeps_confirmed_sibling_evidence`. **New socket evidence.** I1, I2, I4, I6, I8. |
| R11-02 aggregate component unavailable | Web `client.artifacts.lookup(notebook_id, artifact_id)` and first-party strict projection `require_complete_artifact_listing`/artifact getter | Studio list returns no match; note-backed mind-map component returns 503 through its production RPC retries. Exactly one logical Studio read and one logical notes read; wire count is initial plus configured notes retries. | Lookup is `UNKNOWN` with unavailable component `notes`; strict first-party projection raises the established `RPCError` for incomplete lookup. It must not return `NOT_FOUND` or an empty authoritative result. Recovery with notes available returns the fixture artifact or authoritative absence. | Existing unit evidence: `tests/unit/test_artifact_completeness.py::test_no_hit_plus_secondary_outage_is_unknown` and `tests/unit/app/test_app_artifacts.py::test_require_complete_artifact_listing_refuses_partial_inventory`. **New socket evidence.** I2, I6, I8. |
| R11-03 confirmed mutation, readback failure | Web `client.collections.create("Research")` | Baseline `LIST_LABELS` returns the fixture set; `CREATE_LABEL` returns a decoded collection-set response and records one commit; mandatory second `LIST_LABELS` exhausts on 503 or disconnect. Gate the create response so its confirmed journal transition precedes readback. | Escaping `ServerError`/`NetworkError` retains primary mutation `CONFIRMED`, recovery `INSPECT_AND_RECONCILE`, mutation known/candidate evidence only as supported, and a separate failed readback entry. Exactly 1 create request/commit; readback wire count is initial plus configured safe-read retries. It never returns a guessed collection and never resends create. Recovery list observes the candidate without promoting it to this call's confirmed result. | Existing evidence: `tests/unit/test_collections_api.py::test_collection_readback_cancellation_retains_confirmed_mutation_journal`. **New socket evidence.** I1, I2, I6, I8. |
| R11-04 verified research-import candidates | Web `client.research.import_sources_with_verification(...)`, also exercised through `_app.research.execute_research_import(..., oneshot=False)` | Strict completed research poll supplies sources. Baseline source list succeeds; import commits or may commit then loses its response; reconciliation list exposes matching new source IDs. Gate import commit before disconnect. Exactly 1 import mutation, bounded read-only reconciliation attempts under `max_elapsed`, and no mutation replay. | Original `NetworkError`/`RPCError` still escapes as `UNKNOWN`/unconfirmed. Matching IDs appear only in `reconciliation_candidates`; `newly_imported` is never fabricated. `REJECTED`/`NOT_SENT` failures skip reconciliation and preserve their state. Recovery list succeeds. | Existing unit evidence: `tests/unit/test_research_reconciliation.py::test_visible_rows_after_loss_are_candidates_not_success`; happy VCR evidence in `tests/integration/test_research_import_verification_vcr.py`. **New socket evidence.** I1, I2, I6, I8. |

## Construction and ownership requirements

The assigned cases need the following private, default-preserving construction controls. They are
test seams, not new public API:

| Need | Current production owner | Required construction rule |
| --- | --- | --- |
| Web batchexecute and homepage refresh | `_web/assembly.py` → `_web/transport/init.py` → runtime/kernel HTTP owner | Route physical requests to the local service through an instance-owned transport/factory while retaining logical host, cookies, redirect checks, and production middleware/decoder. Do not replace the terminal after client construction. |
| Android unary RPC and bearer issue | `_android/assembly.py` → `AndroidSession`; `_android/auth.py` bearer provider/minter | Inject a local channel/target and synthetic instance-owned minter at construction. Preserve the real session retry mapper and provider single-flight. |
| Artifact polling | `_artifact/polling.py` and the client `CallSupervisor`/poll registry | Use the real namespace and registered leader. Gates live in server actions; do not substitute the poll service or its clock in socket cases. |
| Artifact transfer in R7 | Backend `_web/assets.py` or `_android/assets.py`, then `_artifact/downloads.py` guarded publication | Reuse the R5 instance-owned asset-client routing seam. Direct asset clients must be constructed with the routed factory; rebinding a module symbol only during root-client construction is insufficient. |
| Client budgets | `ClientConfig.runtime` (`RuntimeOptions.operation_timeout` and `max_concurrent_rpcs`); `ClientConfig.retry` or equivalent constructor retry knobs | R2/R7 must make these values explicit in the scenario result. Direct constructor arguments expose retry limits and RPC concurrency, while configured operation timeout must travel through `ClientConfig`. |

New scenario implementations for R1/R2 and R7/R8 belong in backend-specific sibling modules and
are registered by the existing small dispatch modules. R10/R11 workflow scenarios likewise belong
in workflow sibling modules when assigned. The coordinator owns common runner, HTTP/gRPC mechanics,
report schema, and global baseline measurements.

## F0 exit check for this inventory

- Every explicit variant in R1, R2, R7, R8, R10, and R11 has a concrete backend, public or
  first-party entry point, fixture provenance, exception/result assertion, counts, gates, budgets,
  cleanup rule, invariant set, and existing-versus-new evidence disposition.
- The known Web decoded-auth retry-counter discontinuity is recorded as a production gap and a
  failing regression target, not assumed away.
- Socket evidence remains pending where stated. The existing 16 Web and 9 Android scenarios remain
  mandatory, including both ambiguous notebook-create loss cases.
- Companion [transfer](fault-resilience-seams.md) and [adapter](fault-resilience-adapters.md) inventories cover the other families; the [coverage index](fault-resilience-coverage.md) records measured baseline evidence.
