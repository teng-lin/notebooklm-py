# Local fault-injection and resilience guide

The fault harness runs the production NotebookLM clients against local HTTP and
gRPC services with deterministic failures. It needs no NotebookLM account and
never contacts an upstream service. Requests cross real sockets, and the local
services record request, body, response, commit, and cleanup evidence so tests
can distinguish a rejected write from a committed write whose reply was lost.

This document is the authoritative guide for the harness, its R1–R14 acceptance
contracts, construction seams, scenario registries, evidence rules, and measured
local results. The architectural decision remains in
[ADR-0038](adr/0038-local-fault-injection-harness.md).

Visual guides: [test infrastructure](diagrams/22-testing-and-guardrails.html),
[fault coverage](diagrams/39-fault-coverage.html), and
[scenario lifecycle](diagrams/40-fault-scenario-lifecycle.html). These standalone
viewers support search, light/dark themes, and export; open them from a local
checkout or use the hosted links in the [diagram catalog](diagrams/README.md#testing-and-fault-coverage).

Quick links: [commands](#run-the-harness),
[measurements](#current-measured-results),
[runner and cleanup](#runner-acceptance-budgets-and-cleanup),
[construction](#construction-seams),
[registry ownership](#scenario-registry-and-fixture-ownership), and contracts
[R1](#r1-replay-and-commit-evidence), [R2](#r2-retry-refresh-and-aggregate-deadlines),
[R3](#r3-direct-upload), [R4](#r4-upload-cancellation-and-generation-ownership),
[R5](#r5-artifact-publication), [R6](#r6-web-streamed-chat),
[R7](#r7-admission-queue-ownership-and-mixed-close),
[R8](#r8-shared-refresh-mint-and-poll-work),
[R9](#r9-credentials-redirects-and-content), [R10](#r10-generate-and-wait),
[R11](#r11-ordered-batches-complete-reads-and-readback),
[R12](#r12-android-drive-staging), [R13](#r13-connections-and-real-curl), and
[R14](#r14-adapter-boundaries-and-downstream-ownership).

## Run the harness

Install the development and backend extras, then run the socket regressions:

```bash
uv sync --frozen --extra browser --extra dev --extra markdown \
  --extra android --extra mcp --extra server
uv run pytest tests/integration/faults -q
```

The tests carry `allow_no_vcr` because they use local sockets. They require no
cassette recording and no login.

List scenarios or run the complete portable deck:

```bash
uv run python scripts/stress_fault_server.py --list-scenarios
uv run python scripts/stress_fault_server.py \
  --backend both --seed 42 --iterations 400 --concurrency 4 \
  --require-all-scenarios --timeout 120 --scenario-timeout 15 \
  --json-report /tmp/fault-stress-report.json
```

`--backend` accepts `web`, `android`, or `both`. Each iteration owns one client
and its local services. A shared-client scenario may issue several concurrent
operations. `--concurrency` limits active cohorts rather than individual RPCs.
A seed reproduces assignments for the same registry and revision; it cannot
reproduce OS scheduling or timing.

The optional real-curl lane is selected separately:

```bash
uv sync --frozen --extra browser --extra dev --extra markdown \
  --extra impersonate
uv run python scripts/stress_fault_server.py \
  --backend web --transport curl_cffi --seed 42 --iterations 40 \
  --concurrency 2 --require-all-scenarios \
  --json-report /tmp/curl-fault-report.json
```

A requested curl lane fails if its dependency is unavailable. Reports separate
selected, executed, and skipped cases. `--require-all-scenarios` rejects a run
that does not execute every selected case at least once.

## Current measured results

At candidate `d821ecf5a` on macOS 26.6.2 arm64 with Python 3.12.12:

| Selection | Result | Duration |
| --- | --- | --- |
| Portable, seed 42, concurrency 4, 400 iterations | 400/400; all 174 cases selected and executed, zero skipped | 11.66 s |
| Web real curl, seed 42, concurrency 2, 40 iterations | 40/40; all 10 cases selected and executed, zero skipped | 2.42 s |

The completed local qualification at the integration tip also produced:

| Check | Local result |
| --- | --- |
| Ordinary suite | 19,615 passed, 74 skipped in 171.44 s |
| Browser selection | 86 passed, 1 skipped in 70.99 s |
| Combined coverage | 95.19%; all five per-file floors passed |
| Qualification selection | 161 passed in 36.91 s |
| `make gates` | 2,341 passed, 1 skipped, 1 existing xfail |
| Static checks | Ruff and pre-commit passed; mypy passed 479 modules |

The portable registry contains **105 Web and 69 Android cases**. The separate
curl registry contains **10 Web cases**. These are local measurements. Remote CI
and the full compatibility matrix were still pending when this result was
recorded, so this document does not claim they are green.

The original baseline at `65dbd21d70f5be8c892da40a3660987b4118cd1c`
had 16 Web and 9 Android cases. Its focused suite ran 61 tests in 2.35 seconds,
and its 400-iteration stress run completed in 5.79 seconds. Those measurements
show the cost and coverage increase; they are not acceptance evidence for the
expanded deck.

## Scope and evidence model

The harness tests representative public operations and first-party framework-free
workflows. It reuses production middleware, protobufs, batchexecute decoding,
operation journals, retry policies, publication logic, and lifecycle ownership.
It does not implement a complete upstream emulator.

Recorded cassettes and live tests remain the authority for agreement with the
real service. The local harness covers controlled protocol and socket behavior:
retries, authentication generations, partial transfer, response loss, committed
writes, cancellation, deadlines, shared work, close/reopen, and adapter response
ownership. TLS verification and logical routing are exercised by the real-curl
lane. Browser login, real token issuance, DNS outages, kernel packet loss, and
upstream behavior outside the fixtures remain outside its claims.

The invariant labels used by the family tables are:

| Label | Required property |
| --- | --- |
| I1 | Replay follows the registered policy and independent send/commit evidence. |
| I2 | Retry, operation, transfer, and cleanup budgets remain finite and distinct. |
| I3 | Credentials follow the logical host and hop policy without leakage. |
| I4 | Cancellation and close settle work owned by the affected generation. |
| I5 | Shared work has one owner; waiter loss does not corrupt survivors. |
| I6 | Partial success, ambiguity, and prerequisite IDs remain explicit. |
| I7 | Partial transfer data is never published as complete output. |
| I8 | The same client, or the same reopened object after forced close, recovers. |

A zero server request count supports a `NOT_SENT` claim but cannot create one.
Mutation state comes from the production operation journal. `UNKNOWN` never
authorizes replay. `REJECTED` or `NOT_SENT` authorizes another mutation only when
the registered production policy permits it.

## Runner acceptance, budgets, and cleanup

Every scenario records a `plan` before allocating a listener, client, temporary
file, or child task. The plan includes backend and transport, public entry point,
fixture and fault labels, cohort IDs, configured retry and timeout budgets, and
a nonempty exact list of required checks.

A successful return is insufficient. The runner requires:

- a nonempty set of passing `ScenarioResult.require(...)` checks;
- one recorded plan and one explicit `cleanup` event;
- equality between recorded check events and the result summary;
- every declared required check to have been recorded and passed;
- no unexpected request, unused required gate, or forbidden extra mutation;
- JSON-safe evidence, including on failure and cancellation.

The scenario watchdog is 15 seconds and cleanup has an independent 5-second
runner watchdog. Scenario-owned RPC, operation, poll, job, and transfer deadlines
must expire before that boundary. Integration wrappers reserve cleanup time.
Gate waits and handler settlement use shorter local bounds, normally 2–5 seconds.
The complete stress process and CI job have their own larger aggregate limits.

Cleanup runs in `finally`. It releases gates, closes response contexts and clients,
settles producers, writers, poll leaders, registered children, listeners, gRPC
channels, native curl handles, and temporary publication paths. A secondary
cleanup error is recorded by type and must not replace an original exception,
cancellation, `KeyboardInterrupt`, or `SystemExit`.

The portable CI lane must select at least one full 174-case deck. The curl job
installs `impersonate` and selects all 10 curl cases. Ordinary Python/OS jobs run
the portable integration coverage. Local macOS results do not replace Ubuntu or
compatibility results.

## Secret-safe reports

Fault inputs use synthetic cookies, CSRF/session values, bearer generations,
capability URLs, and IDs. Raw credentials are allowed only in private in-memory
comparisons. Exported reports contain generation labels, booleans, counts,
digests, route labels, and opaque non-secret fixture IDs.

Never record raw URLs with capability queries, cookie or bearer values, CSRF or
session tokens, signed-link tokens, request objects, task names, locals,
tracebacks, arbitrary exception messages, or full temporary paths. Exceptions
and cleanup errors are reported by type. Secret scans include encoded forms.
`HttpFaultServer.assert_drained()` reports only a generic pending-action count;
it does not render routes or upload IDs.

## Construction seams

All substitutions are private and default-preserving. They are captured during
synchronous construction and remain instance-owned across awaits, close, and
reopen. No scenario patches a process-global production binding or environment
variable across an await.

| Path | Production owner and captured seam |
| --- | --- |
| Web RPC, refresh, and chat | `_assemble_client(async_client_factory=...)` reaches Web transport construction. The harness routes the kernel and homepage refresh while retaining middleware, cookies, redirect checks, streaming, and production decoding. |
| Android RPC | Android assembly captures a gRPC loader that validates the production logical target, then opens an insecure loopback channel. The real `AndroidSession`, retry mapper, and bearer provider remain active. |
| Web upload | `HttpClientFactories` forwards the captured HTTPX factory into `SourceUploadPipeline` start, finalize, and cancel clients. Registration still uses the main RPC owner. |
| Web Drive | The uploader passes a captured streaming factory into `DriveFetcher`; redirect hooks and HTTPX streaming behavior remain intact. |
| Web asset single/batch | `WebAssetDownloadService` and `_make_download_client` receive the captured transport selection and HTTPX factory. Single producer/writer and buffered batch publication both use it. |
| Android upload and Drive | Android assembly forwards the captured async-client factory into `AndroidUploadPipeline`; the same owner reaches start/finalize and `DriveStagingTransfer` stage/delete. |
| Android asset single/batch | `AndroidAssetDownloadService` receives its supported zero-argument client factory. Guarded transfer and manual redirect policy remain production code. |
| Bearer issuance | A synthetic reader/minter is injected into the real `BearerProvider`; mint count, generation, sharing, and invalidation are real provider behavior. |
| Curl session/upload | Curl routing captures both `AsyncSession` and the independent low-level `Curl` factory. Each handle retains its routing list and CA ownership until settlement. |
| REST/MCP/CLI | REST and MCP receive an async client context factory. CLI uses its existing `ctx.obj["client_factory"]` injection. Live adapter cases use real uvicorn/FastMCP listeners. |

`HttpClientFactories` keeps `None` as the production default. HTTPX transport kind
is separate from an injected client factory, so routing an HTTPX client cannot
accidentally select the curl branch. These seams add no public client option.

## Logical routing and upload sessions

HTTPX requests keep their production logical HTTPS URL, Host header, cookies,
and response request identity. `LogicalHostTransport` changes only the physical
destination to loopback, disables environment proxies, and rejects an unmapped
host before dialing. Production allowlists are never widened.

The route set is explicit: the configured notebook host, homepage and login;
Web upload sessions; Android upload paths; selected artifact storage/video hosts;
and the Drive upload, metadata, media, and exact staged-file DELETE paths. A new
host requires both a route and a failing-unmapped assembly test.

The HTTP service records logical method/host/path/upload session, headers received,
body byte count and SHA-256, body completion, response prefix, commit, and handler
settlement as separate events. Successful upload-start replies issue an exact
`(host, path, upload_id)` capability. Finalize actions opt into
`Transfer(require_session=True)` and reject a missing, unknown, cancelled, or
committed session before reading the body. Only independently validated size,
digest, and scripted commit evidence marks the session committed. A 401/403
finalize without commit evidence leaves the session active for a legitimate retry.

Web asset cookies are derived from the active owner on every hop. Android sticky
bearer policy grants a bearer only to eligible exact hosts and drops it after
leaving that host set within a redirect chain; returning to a bearer host in the
same chain does not restore it. A new batch URL starts a fresh policy. Signed
capability hops never receive a bearer.

The real-curl router uses an owned `curl_slist` for `CONNECT_TO`, explicit test CA
trust, peer and hostname verification, and proxy disablement. `CONNECT_TO` alone
does not fail closed, so every initial and redirected logical URL is validated.
Unknown TLS redirect hosts fail hostname verification. The checked-in loopback
certificate/key are synthetic test material with exact selected DNS SANs.

## Scenario registry and fixture ownership

The backend dispatchers expose sorted `SCENARIOS` and `run_scenario`; integration
tests and the stress runner use those same functions.

| Family | Scenario owner | Registry selection and fixture source |
| --- | --- | --- |
| Baseline Web | `tests/_fault_server/web_scenarios.py` | Web RPC, retry, refresh, malformed/truncated response, cancellation, and close cases; batchexecute builders in `web.py`. |
| R1/R2 | `web_resilience_scenarios.py`, `android_resilience_scenarios.py` | Replay, refusal, composed retry/auth, queue expiry, old generation, shared bearer. |
| R3/R4/R5/R9 | `web_transfers.py`, `android_transfers.py`, `android_downloads.py` | Upload and artifact fixtures; generated protobuf rows and valid media payloads. |
| R6 | `web_streaming.py` | Real batchexecute chat frames, including an unescaped multibyte fixture. |
| R7/R8 | `web_concurrency.py` plus the resilience modules | Mixed admission/transfer/poll, shared refresh/mint/poll, queue and close ownership. |
| R10/R11 | `web_generation_faults.py`, `web_workflows.py` | Framework-free generation/research workflows, collection readback, aggregate artifact lookup. |
| R12 | `android_drive.py` | Real multipart stage body and generated AddSources/GetProject replies. |
| R13 | `web_connections.py`, `curl_scenarios.py`, `curl_routing.py` | Keepalive/restart/slow consumers and the separate native curl registry. |
| R14 | `adapter_scenarios.py`, `adapter_lifecycle.py`, `adapter_listener.py` | REST/MCP/CLI mapping and live downstream-disconnect ownership. |

Portable registration totals 105 Web and 69 Android cases. The Web registry
includes 14 upload, 30 download, 7 chat, 4 connection, 4 concurrency, 8 workflow,
9 adapter, and the baseline/resilience cases. Android includes 15 direct upload,
30 download, 13 Drive, 2 added resilience, and 9 predecessor cases.

The curl registry is exactly:

- `curl_read_recovery`;
- `curl_upload_success`, `curl_upload_prefix_failure`,
  `curl_upload_commit_loss`, `curl_upload_cancel`;
- `curl_download_success`, `curl_download_prefix_failure`,
  `curl_download_body_stall`, `curl_download_cancel`,
  `curl_download_close_reopen`.

Fixture provenance remains in the owning module and its unit tests. Principal
sources are `test_source_upload_pipeline.py`, Android source proto/upload tests,
`test_artifact_downloads_coverage.py`, `test_prepared_artifact_downloads.py`,
Android asset tests, `test_streaming_chat_wire.py`,
`chat_stream_final_response.json`, operation-journal tests, artifact polling tests,
and research reconciliation/completeness tests.

## R1: Replay and commit evidence

| Case | Entry and fault | Acceptance contract |
| --- | --- | --- |
| R1-01 `queue_expiry_no_dispatch` | Web `notebooks.create` waits behind a sole-permit gated read and its 0.05 s operation scope expires. | `OperationTimeoutError`; journal state `NOT_SENT` with no attempt; 0 create requests/commits; recovery read succeeds. |
| R1-02 `connection_refusal_recovery` | Web `notebooks.list` targets a reserved then closed loopback port. | With two server retries: 3 connect attempts, `NetworkError`, no server journal or inferred metadata; retarget the same instance-owned transport and recover. |
| R1-03 `read_disconnect_recovery` | Web list request is observed, then the socket closes before a complete response. | One safe replay, exactly 2 reads and 0 commits; second result decodes; later same-client read succeeds. |
| R1-04 `rename_commit_loss_converges` | Web `notebooks.rename(id, "Stable")` commits, loses its reply, then replays. | Exactly 2 byte-identical renames, one service commit, final `GET_NOTEBOOK` title `Stable`. Android `MutateProject` remains non-replayable and is excluded. |
| R1-05 `tentative_registration_refused` | Android `sources.add_text`; `AddTentativeSources` returns decoded `UNAUTHENTICATED` before registration. | `AuthError`, `CommitState.REJECTED`, not unconfirmed; 1 registration, 0 IDs and 0 AddSources; repaired same client recovers. |
| R1-06 `committed_create_disconnect` / `commit_lost_response` | Web and Android create commits then loses the response. | Public server/network error is `UNKNOWN` and unconfirmed; exactly 1 create and 1 commit despite nonzero retry settings; no candidate authorizes replay. |

R1 uses current policy: Web list/get/rename are replay-safe set operations;
create is `NON_IDEMPOTENT_NO_RETRY`. Android derives its retry ceiling from its
own manifest, including the deliberate `MutateProject` difference. I1, I2, I5,
I6, and I8 apply as shown by each row.

## R2: Retry, refresh, and aggregate deadlines

All four cases call Web `notebooks.list` through a decoded auth failure carried
inside an HTTP 200 batchexecute response. Refresh uses the real homepage parser.
Server-error and rate-limit counters remain independent across auth re-entry.

| Case | Sequence and exact acceptance |
| --- | --- |
| R2-01 `retry_auth_rate_exhaustion` | Budgets server=2, rate=1. Sequence 503, decoded auth, refresh, 503, 429, 429. The operation raises `RateLimitError` after 5 RPC POSTs and 1 homepage; a separate recovery makes the sixth read. |
| R2-02 `retry_auth_server_budget` | Budget server=1. Sequence 503, decoded auth, refresh, 503. It raises `ServerError` after 3 operation POSTs and 1 homepage; the success sentinel is untouched until the recovery call. |
| R2-03 `retry_auth_operation_deadline` | Server/rate budgets 3, operation timeout 0.20 s, then a 429 Retry-After longer than remaining time. `OperationTimeoutError`; 3 POSTs, 1 refresh, no post-expiry send; release the gated sleeper and recover. |
| R2-04 `retry_auth_backoff_cancelled` | Same 503/auth/refresh/429 prefix with a gated instance sleeper and long operation budget. Cancel only after backoff begins; `CancelledError` escapes, no next send, permit settles, recovery succeeds. |

These cases assert retry telemetry, credential generation changes, gate ordering,
request counts, and cleanup. I1, I2, I4, I5, and I8 apply.

## R3: Direct upload

Public entry is `client.sources.add_file`. Web uses TXT and chunked POST finalize;
Android uses an opaque PDF body and PUT with Content-Length. Registration uses the
real Web decoder or generated Android protobuf. Start and finalize use distinct
issued session capabilities.

| Variants | Acceptance contract |
| --- | --- |
| success | One register/start/finalize, exact size/digest and one commit; returned source ID matches registration. |
| registration failure/refusal | Web covers registration failure; Android covers failure and positive refusal. All precede start; arbitrary statuses never imply rejection. |
| start failure | One registration and start, zero finalize; public error retains source ID and start-stage identity. |
| prefix disconnect | Nonempty body prefix observed, incomplete body, one finalize, zero commit; registered ID and finalize stage survive. |
| body stall | A sufficiently large producer meets a held consumer and the real transfer deadline; no duplicate registration/finalize or commit. |
| commit loss | Full body validates and commits once, then acknowledgment is lost; no upload restart or source deletion. |

Separate start 401, start 403, finalize 401, and finalize 403 cases preserve each
backend's mapper. Web uses its existing HTTP error types. Android start/finalize
401 maps to `AuthError`; other statuses, including 403, retain `SourceAddError`.
Descriptors, children, HTTP clients, and semaphore slots settle. I1–I4, I7, I8.

Web registers `upload_success`, `upload_registration_failure`,
`upload_start_failure`, `upload_prefix_disconnect`, `upload_body_stall`,
`upload_commit_loss`, and the four start/finalize authorization cases. Android
uses the same names and adds `upload_registration_refusal`; R4 owns the remaining
cancellation and close names.

## R4: Upload cancellation and generation ownership

| Case | Acceptance contract |
| --- | --- |
| Web before finalize dispatch | Cancel after registration/start but before finalize client entry; zero upload finalize, one tracked Scotty cancel while the epoch is open. |
| Web after body prefix | Cancel after nonzero consumption; shielded finalize settles before cancellation escapes; at most one finalize/commit. |
| Android before finalize | Cancel during held start response; zero finalize and ordinary `CancelledError`. |
| Android after body prefix | Cancel the HTTP child after consumption; child/body/client settle with no invented commit. |
| Repeated cancellation | Re-cancel during shielded/native writer settlement; the original cancellation survives and the child settles exactly once. |
| Close/reopen | Force close during body transfer, settle the retired generation, reopen the same object, and recover; no old request publishes into the new epoch. |

Web and Android retain their distinct cancellation semantics. Neither performs a
speculative source/session delete after ambiguous finalize. I1, I3–I5, I7, I8.

The Web registry names are `upload_cancel_before_dispatch`,
`upload_cancel_after_prefix`, `upload_cancel_repeated`, and
`upload_close_reopen`. Android uses `upload_cancel_before_finalize`,
`upload_cancel_after_prefix`, `upload_repeated_cancel`, and
`upload_close_reopen`.

## R5: Artifact publication

Public single entries cover audio, video, infographic, and slide-deck downloads.
Web batch and Android guarded batch are assembled service mechanisms because no
separate public batch API exists. The test must not describe them as new APIs.

| Mechanism | Exact variants and required evidence |
| --- | --- |
| Web HTTPX single and batch | success, truncation, prefix disconnect, body stall, cancel, close/reopen; valid descriptor decoding, exact bytes, atomic replace, staging cleanup, response/writer/client settlement. |
| Android guarded single and batch | The same six transfer variants using generated artifact protobuf rows and valid 1×1 PNG/media signatures; typed transport errors and no partial publication. |
| Curl buffered single | success, prefix failure, body stall, cancel, close/reopen as selected in R13; real native handle and response cleanup. |

Failure preserves an existing destination and leaves no `.tmp` or `.part` file.
Web transport failures retain established `ArtifactDownloadError` causes or batch
failed results. Android exposes bounded `code=transport` data without raw causes.
I2–I4, I6–I8.

## R6: Web streamed chat

Public entry is `client.chat.ask` with explicit sources and conversation input.
Every case uses real length-prefixed batchexecute frames and production parsing.

| Registry variant | Acceptance contract |
| --- | --- |
| `chat_success` | Complete valid stream decodes once and recovery succeeds. |
| `chat_multibyte_fragmented_success` | Hold inside an unescaped UTF-8 codepoint, then prove exact byte reassembly without assuming TCP boundaries. |
| `chat_partial_frame_disconnect` / `chat_partial_frame_stall` | Cut or hold within a frame; public `NetworkError`/deadline, no replay. |
| `chat_partial_answer_disconnect` / `chat_partial_answer_stall` | Lose transport after answer bytes but before full response; no partial public answer is claimed. |
| `chat_partial_answer_cancel` | Cancel after delivery evidence; stream resources settle and same-client recovery succeeds. |

Web currently buffers the response before parsing, so delivery of bytes to HTTPX
is not delivery of a public partial answer. Android predecessor stream cases are
comparative evidence only. I1, I2, I4, I7, I8.

## R7: Admission, queue ownership, and mixed close

Mixed cases use one client and, where stated, `max_concurrent_rpcs=1`. Artifact
body I/O is outside the RPC permit; descriptor/list/finalize RPCs still use it.

| Case | Acceptance contract |
| --- | --- |
| R7-01 `mixed_rpc_transfer_poll_progress` | Hold one read permit; queue download descriptor and poll; release it, gate the asset body, and prove another read progresses while transfer remains held. Both complete and all registries empty. |
| R7-02 `queue_cancel_no_dispatch` | Cancel a second read behind the holder. It sends zero requests and consumes no permit; holder and later read succeed. Total successful reads: 2. |
| R7-03 `queue_expiry_no_dispatch` | A queued call inside `client.operation(timeout=0.05)` raises `OperationTimeoutError`, records `NOT_SENT`, and never dispatches after release. |
| R7-04 `configured_queue_expiry_no_dispatch` | The same proof using configured default `operation_timeout=0.05` with no call-site scope. Keep it distinct from explicit expiry. |
| R7-05 `close_mixed_load_and_reopen` | Close with active/queued read, gated download, and poll leader/follower. No queued dispatch during close; response, poll, permit, and transfer owners settle; reopen recovers. |

No scheduler ordering is asserted between independently queued RPCs. I1, I2,
I4–I6, I8.

## R8: Shared refresh, mint, and poll work

| Case | Acceptance contract |
| --- | --- |
| R8-01 `auth_refresh_cancelled_waiter` | Two Web reads join one delayed refresh. Cancel one; the survivor succeeds. Exactly one homepage and bounded stale/fresh POSTs; recovery and refresh slot settle. |
| R8-02 `shared_poll_last_waiter_cancelled` | Two waiters share one gated poll leader. Both cancel, but neither owns the leader. It consumes its terminal reply or lifecycle drains once; registry empties and recovery succeeds. |
| R8-03 `bearer_shared_failure_recovery` | Two Android reads share one failing mint and make zero gRPC calls. Repair the same minter; the third read performs one new mint and one RPC. |
| R8-04 `auth_refresh_old_generation` | Hold an old Web refresh, close/reopen, publish new credentials, then release old response. Old completion cannot overwrite new CSRF/session/cookie state. |
| R8-05 `shared_refresh_failure_then_recovery` | Two Web reads share one malformed refresh failure; both get the established RPC error, no stale replay occurs, and a later call starts one new refresh and succeeds. |

Shared producers are shielded and registered with lifecycle ownership. Waiter
cancellation is not producer cancellation. I4, I5, I8.

## R9: Credentials, redirects, and content

Both asset owners cover 401, 403, trusted redirect, untrusted redirect, redirect
loop, expired signed capability, and HTTP 200 HTML. Android additionally covers
wrong media signature and bearer-to-capability-to-bearer bounce. Web adds cookie
trusted/untrusted redirect cases for both single and batch paths.

| Boundary | Acceptance contract |
| --- | --- |
| 401/403 | Artifact paths raise `AuthError`; there is no promised signed-URL refresh. Android invalidates only the bearer generation attached to an eligible failing hop. |
| Trusted redirect | Exact per-hop credential policy and content validation; each batch URL gets a fresh policy. |
| Untrusted redirect | Target receives zero requests; logical destination fails closed. |
| Redirect loop | Existing 20-redirect ceiling remains; Android reports `too_many_hops`, Web retains HTTPX/curl mapping. |
| Expired/HTML/signature | No publication, old destination preserved, bounded sanitized error. Web has no general signature contract and gains none here. |
| Bearer bounce | Android drops bearer after capability hop and does not restore it within that chain; next independent download reacquires normally. |

Upload 401/403 cases remain separate for start and finalize under R3. No case
silently mints a new URL or replays an already dispatched upload. I1–I3, I6–I8.

## R10: Generate and wait

The entry is framework-free `notebooklm._app.generate.execute_generation` with
`AudioGenerationRequest`. It invokes public artifact generation and polling under
one operation scope and uses production create/list decoders.

| Case | Acceptance contract |
| --- | --- |
| R10-01 `workflow_generation_poll_exhaustion` | Task `task-1` is confirmed; four 503 poll attempts exhaust three retries. `ServerError`, one kickoff, task metadata retained, same-client recovery. Production 2/4/8-second arithmetic is recorded through an instance sleeper. |
| R10-02 `workflow_generation_lost_kickoff` | Create commits `task-2` then disconnects. `NetworkError`, `UNKNOWN`/unconfirmed; 1 kickoff, 1 commit, 0 polls, no task fabricated from server-only state. |
| R10-03 `workflow_generation_terminal_failure` | Create returns `task-3`; real failed row yields typed `GenerationOutcome(status="failed")`. One kickoff and the fixture-required terminal poll count; no exception-to-success conversion. |
| R10-04 `workflow_generation_shared_original_timeout` | Gate kickoff and a shared poll under a real 2-second operation budget; `execute_generation` times out while an independent follower retains `task-4`, receives the released completion, and settles the registry. |

The arithmetic case does not claim elapsed waiting. R10-04 is the separate
real-clock aggregate-deadline proof. I1, I2, I4–I6, I8.

## R11: Ordered batches, complete reads, and readback

| Case | Acceptance contract |
| --- | --- |
| R11-01 `workflow_research_import_ordered_loss` | `execute_research_import` confirms an earlier candidate, then loses the later reply. Earlier evidence survives, later outcome remains unknown/candidate, each member sends once, original failure escapes. |
| R11-02 `workflow_artifact_incomplete_lookup` | Studio misses while note-backed mind-map listing returns 503. Lookup is `UNKNOWN` with `notes` unavailable; strict getters raise `RPCError`, never authoritative not-found. Recovery proves found and missing states. |
| R11-03 `workflow_collection_readback_failure` | Collection create decodes and commits once; mandatory list readback exhausts. Escaping server/network error retains mutation `CONFIRMED` and separate failed readback; create is never resent. |
| R11-04 `workflow_research_import_candidates` | Import may commit then loses response; bounded read-only reconciliation exposes matching IDs only as candidates. Original unknown/unconfirmed error escapes; `newly_imported` is never fabricated. |

The complete-lookup case checks both Studio and note-backed reads and preserves a
positive hit even when another component fails. I1, I2, I4, I6, I8.

## R12: Android Drive staging

Public `sources.add_file` with CSV selects Drive staging. The fixture uses a real
multipart body, stage response ID, generated AddSources request/reply, readiness
GetProject, and exact staged-file DELETE. Drive cleanup is separate from
NotebookLM source deletion.

| Variants | Acceptance contract |
| --- | --- |
| `drive_success` | One stage/import/readiness flow and one exact staged DELETE; confirmed source result. |
| `drive_registration_refusal` | Positive refusal permits one staged cleanup; primary exception and prerequisite ID survive. |
| `drive_registration_ambiguous` / `drive_import_ambiguous` | Lost acknowledgment retains staging and sends zero DELETE because refusal is unproven. |
| `drive_import_timeout` | Real RPC/readiness timeout retains staging and original timeout/source evidence. |
| `drive_terminal_failure` | Confirmed `SourceProcessingError` with recovery NONE permits safe staged cleanup while the fence is open. |
| `drive_cleanup_failed_success` / `drive_cleanup_failed_refusal` | DELETE failure stays observable but does not replace success or the original import failure; no source DELETE. |
| `drive_cancel_during_stage` | No staged identity is invented; stage client/body settle and no speculative cleanup runs. |
| `drive_cancel_after_stage` / `drive_cancel_during_import` | Preserve staged prerequisite because dependent import settlement is unknown; zero DELETE. |
| `drive_close_during_import` / `drive_deadline_before_cleanup` | Closed epoch retains staging. The deadline case advances only an instance-owned cleanup-fence clock after gated readiness; no DELETE. |

Production has a 300-second minimum Drive cleanup lifetime. The injected deadline
case proves arithmetic only; `drive_import_timeout` separately proves a real-clock
timeout. Cleanup is authorized only by `NOT_SENT`/`REJECTED`, or confirmed terminal
processing failure with recovery NONE. I1, I2, I4, I6–I8.

## R13: Connections and real curl

| Portable HTTPX case | Acceptance contract |
| --- | --- |
| `connection_peer_close` | Two successful reads share one connection ID; peer closes; safe read recovers on a new connection with the same client. |
| `connection_server_restart` | Establish reuse, restart listener on the captured endpoint, then recover on a new connection without rebuilding the client. |
| `connection_slow_read_consumer` | Gate actual request-body consumption, observe a nonzero prefix, release, decode, and recover. The gate alone is not claimed as kernel backpressure. |
| `connection_slow_upload_consumer` | Reuse R3's large upload and body-prefix evidence to prove bounded writer/producer settlement under a slow peer. |

The 10 exact curl names are listed in the registry section. They use real
`AsyncSession` and low-level upload `Curl`, preserve TLS verification, reject
unmapped destinations, count real request/commit evidence, and settle routing
lists, responses, threads, handles, and descriptors. Exceptions may normalize
differently from HTTPX; retry authorization does not change. I1–I4, I7, I8.

## R14: Adapter boundaries and downstream ownership

Six mapping cases use the real loopback Web client. Read retry counts are zero so
they test adapter projection rather than duplicating backend retry tests.

| Case | Public result and independent evidence |
| --- | --- |
| `adapter_rest_transient_read` | `GET /v1/notebooks` returns 502, category `server`, retriable true; exactly 1 upstream list. |
| `adapter_rest_ambiguous_create` | `POST /v1/notebooks` returns 502, category `rpc`, retriable false, unknown/unconfirmed and reconciliation hint; exactly 1 create/commit. |
| `adapter_mcp_transient_read` | `notebook_list` raises scrubbed `ToolError` beginning `SERVER:` with `retriable=true`; 1 upstream list. |
| `adapter_mcp_ambiguous_create` | `notebook_create` raises `ToolError` beginning `RPC:` with non-retriable/unconfirmed markers; 1 create/commit. |
| `adapter_cli_transient_read` | `notebooklm list --json` exits 1 with `NOTEBOOKLM_ERROR`; 1 upstream list and no traceback/secret. |
| `adapter_cli_ambiguous_create` | `notebooklm create ... --json` exits 1 with `UNCONFIRMED_WRITE`, unknown/unconfirmed and inspect-before-retry hint; 1 create/commit and no active-context write. |

Three live listener cases establish response ownership that in-memory transports
cannot prove:

| Case | Gates, budget, and cleanup contract |
| --- | --- |
| `adapter_rest_download_disconnect` | Real caller reads headers and 128-byte prefix then closes. Upstream valid spool completes; `_CleanupFileResponse` removes its private directory, releases limiter, settles transfer, and a third request recovers. |
| `adapter_mcp_download_disconnect` | Real signed route loses its caller after prefix. `_SlotHeldFileResponse` releases slot and spool; token stays private; recovery download succeeds. |
| `adapter_mcp_chat_start_disconnect` | Close after task acceptance and response-send boundary while upstream ask is gated. Job survives on the accepted client epoch; duplicate attaches to the same task; status returns the decoded result. |

Live cases use a 6-second operation watchdog, finite 3-second detached-job budget
where applicable, 2-second component cleanup waits, and a 12-second integration
wrapper. Each child process returns partial evidence on failure. Shutdown closes
the chat registry before its provider and leaves no unhandled task exception.
I1–I6 and I8 apply according to each row.

## Add or investigate a scenario

1. Choose a representative public API or first-party workflow and reuse a real
   decoder fixture. Add one named deterministic variant rather than a random
   alternative.
2. Capture construction dependencies before the first await. Route every logical
   host explicitly and prove an omitted destination fails closed.
3. Record the plan and exact required checks before allocation. Use server gates
   for ordering and production clocks for elapsed-time claims. Label an injected
   clock or sleeper as arithmetic evidence.
4. Assert the public result plus independent request, body, credential-generation,
   and commit evidence. For mutations, use production metadata; do not infer
   `NOT_SENT` or success from a journal alone.
5. In `finally`, release all gates and settle every resource owner. Record a
   sanitized cleanup event even when the operation fails or is cancelled.
6. Register the scenario in its sibling module, run the focused integration case,
   then run it in a complete concurrent deck. Add helper unit tests only for a new
   fault mechanism or evidence rule.

When investigating a failure, start with the operation's plan, failed or omitted
checks, request/commit trace, and cleanup event. Compare the service commit state
with the public exception before deciding whether a retry is safe. Re-run the
same revision, seed, selection, and workload. Reduce timing failures to explicit
gates; repeated random reruns are not a fix.
