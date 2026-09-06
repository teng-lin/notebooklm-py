# Resilience construction seams and transfer acceptance inventory

Audit baseline: `65dbd21d70f5be8c892da40a3660987b4118cd1c`, 2026-09-06.
This is the F0 construction audit for R3–R6, R9, R12 and R13 in the
[resilience program](fault-resilience-coverage.md). It records current
contracts and proposed private seams; it does not assert that planned socket
cases already exist. Retry authorities and backend differences remain unchanged.

## Exact construction map

Paths below are relative to `src/notebooklm/`. A construction-time substitution
means a synchronous context that ends before opening or awaiting the client.
Captured collaborators must remain instance-owned throughout close and reopen.

| Operation | Current production construction and dispatch | Existing seam / required change |
| --- | --- | --- |
| Web RPC and chat HTTP | `_client_assembly._assemble_client(async_client_factory=...)` → `_web/assembly.py` → `WebRuntimeConfig` → `_web/transport/init.py` kernel/session construction. Chat uses that kernel's `stream_post_with_size_cap`, then the real parser. | Existing factory routes the main session and homepage refresh. `tests/_fault_server/web.py` already captures `server.client_factory`. Preserve that path and real middleware. |
| Android RPC | `_android/assembly.py` constructs `AndroidSession`; session captures `grpc_loader`; loader opens the channel on use. | Existing `tests/_fault_server/android.py` synchronously substitutes the session constructor, capturing a local gRPC loader. It validates the logical production target before opening an insecure loopback channel. HTTP paths still need separate routing. |
| Web upload register | `SourceUploadPipeline._register_file_source_for_upload` uses its real RPC executor and decoder. | Main-session seam above. Preserve registration policy, response correlation, candidate-versus-confirmed identity and source-limit diagnostics. |
| Web upload start/finalize/cancel | `_web/transport/init.py:build_web_runtime` constructs `SourceUploadPipeline` **without** `async_client_factory`. Pipeline `_client_factory()` uses its optional instance factory or resolves transport at call time. `start_resumable_upload` creates a client; `upload_file_streaming` creates another in a tracked child; `cancel_upload_session` creates another. | Existing pipeline constructor seam is sufficient, but production assembly must forward the injected factory. Main RPC injection currently does not reach these clients. Keep live cookies, phase timeouts, epoch tracking and shielded finalize intact. |
| Web Drive fetch | `_web/sources/upload.py:drive_download_scope` constructs `DriveFetcher` while the operation runs. `_web/sources/drive_import.py:DriveFetcher` captures `client_factory`, default `_default_streaming_client`, which directly builds HTTPX with streaming/redirect hooks. | Capture a separate streaming factory on the uploader and pass it when constructing `DriveFetcher`. This intentionally HTTPX streaming path must not accidentally switch to curl buffered GET. Factory needs `(cookies, timeout)` adaptation preserving redirect hooks. |
| Web artifact single | `_web/assembly.py` constructs `WebAssetDownloadService`, which inherits `AssetDownloadService.download_url`. `_artifact/downloads.py` resolves transport during transfer; HTTPX branch directly creates `httpx.AsyncClient` and uses a writer thread plus bounded queue; curl branch builds factory client and calls buffered `get_guarded`. | Add an optional instance-owned transport-selection/construction seam to the asset service. Replacing a module binding only during root construction does not capture these later constructions. Preserve the HTTPX branch identity when injecting a routing factory. |
| Web artifact batch | `AssetDownloadService.download_urls_batch` calls `_artifact/_download_client.py:_make_download_client`, whose HTTPX branch directly creates HTTPX and whose curl branch calls `get_guarded`. Each successful buffered response goes through `AssetPublication.write_file`/`write_staging` and atomic replace. | Pass the captured asset construction collaborator into `_make_download_client`, with existing default resolution unchanged. Single and batch must both use it; overriding only `download_url` misses batch. Preserve per-hop hooks and per-URL policy renewal. |
| Android artifact single/typed/batch | `_android/assembly.py` constructs `AndroidAssetDownloadService` without its supported `client_factory`. Service `_transfer_worker` and `_download_urls_batch_impl` call this captured zero-argument factory. Both use neutral `guarded_transfer` and manual redirect checks. | Existing constructor seam suffices. Capture a zero-argument adapter around cohort factory during assembly, retaining `cookies=None`, `follow_redirects=False`, timeout 60. Guarded transfer already receives the constructed client; it needs no global factory. |
| Android direct upload | `_android/assembly.py` constructs `AndroidUploadPipeline` without its supported `async_client_factory`. `_control_plane` registers over gRPC, `_start_worker` creates HTTP client for POST, `_finalize_worker` creates another for PUT. | Forward captured factory into existing constructor. Keep exact `isinstance(CurlCffiAsyncClient)` branch: curl uses disk-backed `stream_upload`, HTTPX consumes an async iterator. |
| Android Drive download/staging | Upload pipeline `_download_drive_file` uses `_client_factory`; `_drive_staging()` constructs `DriveStagingTransfer` with the bound pipeline factory. `stage` POST and `unstage` DELETE create clients through it. | Forwarding upload factory routes all these existing collaborators. No new Drive-global transport patch is needed. Keep real multipart encoder, import gRPC, cleanup scope and bearer ownership. |
| Android bearer issuance | Assembly takes `master_token_reader`/`oauth_minter`, constructs real `BearerProvider`; otherwise uses profile reader and `MintService`. | Existing synthetic reader/minter seam avoids credentials, disk and issuer traffic. Extend harness minter with gates/counts; retain real provider, generation and waiter behavior. This is deliberate issuer-boundary simulation, not an issuer socket claim. |
| Curl session and upload handle | `_curl_cffi_transport.CurlCffiAsyncClient.__init__` directly constructs `AsyncSession`; `stream_upload` creates an independent low-level `Curl()` inside its worker thread. | Both need captured, private construction collaborators/options. Configuring only `AsyncSession` leaves actual upload unrouted. Neither HTTPX replacement nor environment mutation qualifies curl evidence. |

Recommended implementation: retain `None` defaults and add narrow optional private
constructor dependencies, forwarded by backend assembly. HTTPX asset construction
must distinguish transport *kind* from injected client factory; testing `factory is
httpx.AsyncClient` against an arbitrary routing factory incorrectly selects the curl
branch. A separately captured HTTPX factory preserves current branching and event
hooks. Explicitly test default constructors, injected constructors, both publication
branches and assembly identity with existing guardrails. Avoid adding public options.

## Logical routing and credential isolation

Use production logical HTTPS URLs, exact allowed ports/paths, and normal validators.
`tests/_fault_server/http.py:LogicalHostTransport` rewrites the physical destination
only below HTTPX cookie/header construction, retains the logical Host and response
request, disables environment proxy use, and rejects unmapped hosts. Extend its
per-cohort map to every selected destination; do not replace returned capabilities
with `http://localhost` URLs or widen production allowlists.

Required logical routes include the configured Web notebook host and homepage/login
hop; Web `/upload/_/` sessions; Android
`notebooklm-pa.googleapis.com/upload/upload/{project}`; artifact
`lh3.googleusercontent.com`, `contribution.usercontent.google.com` and selected
Google storage/video capability hosts; and `www.googleapis.com` Drive upload,
metadata/media and DELETE paths. Web Drive routes additionally use the current
`DriveFetcher` URL builder and approved redirect hosts. Every exercised host must
be listed explicitly. A route absent from this list must fail before a network dial;
add an assembly test proving this on each newly injected owner.

The service must journal logical method/host/path/session, request-header receipt,
received bytes/digest, body completion, commit, response prefix and handler settlement
as distinct evidence. Keep credentials only in private assertions. Public reports
contain booleans, counts and opaque IDs, never raw URLs, cookies, bearer strings,
CSRF tokens, capability queries or exception messages/tracebacks. Secret scanning
must cover encoded variants too. Empty server journals alone cannot prove NOT_SENT.

Web assets derive fresh cookies from the active Web owner at each hop; domain and
secure matching still apply. Android `_StickyBearerPolicy` grants bearers only to
its exact eligible hosts, reacquires on eligible hops, and permanently drops bearer
after leaving that set within a chain, even on a bounce back. Signed storage/video
hops receive no bearer. A new batch URL starts a fresh policy. Upload bearer checks
are separately owned by `_bearer_for`; they must not reuse the artifact policy.
Drive uses its existing issuer/provider scope. No cohort reads the user cookie jar,
profile, master token or environment credentials.

## Curl routing feasibility

A local 2026-09-06 probe against the installed `curl_cffi` demonstrated both real
`AsyncSession(impersonate="chrome")` and independent `Curl().impersonate("chrome")`
GETs to an ephemeral TLS loopback server while the URL remained
`https://notebook.google.com/...`. Both returned 200 and the local server observed
two requests. The probe used a temporary certificate with that logical DNS SAN,
explicit CA trust, `SSL_VERIFYPEER=1`, `SSL_VERIFYHOST=2`, `PROXY=""`, and
`CONNECT_TO=notebook.google.com:443:127.0.0.1:<ephemeral-port>`.

A Python list for `CONNECT_TO` is **not supported** by the installed binding:
`Curl.setopt` special-cases list conversion for `RESOLVE`, but not `CONNECT_TO`.
The successful probe passed a `curl_slist` pointer created with
`curl_cffi.lib.curl_slist_append`; it retained the list until both handles closed
and then freed it. A production-default-preserving test seam must own that lifetime,
including upload worker settlement. An alternative is a local CONNECT proxy with
strict route rejection and local TLS endpoints; it needs its own evidence.

This proves routing feasibility, not completed R13 upload/cancellation coverage.
The optional lane still needs production adapter construction, POST/PUT body
consumption, per-hop redirect routing, timeout/close evidence, and negative TLS tests.
`CONNECT_TO` alone does not reject unmapped destinations: validate every requested
logical URL in the captured adapter and either disable auto-follow or preflight
redirects through the existing production guards. Any automatic redirect route
must also be unable to fall through to DNS/live transport. Never set `verify=False`,
change system DNS/hosts, or treat HTTPX emulation as a curl result.

## Fixture and case inventory

Case suffixes below are separate deterministic cases, never random alternatives.
Each owning socket module must expose a success baseline before its failure family.
Suggested owners: `tests/_fault_server/web_transfers.py`, `android_transfers.py`,
`web_streaming.py`, `android_drive.py`, and `connection_scenarios.py`, dispatched
from the existing backend registries. The public integration tests belong under
`tests/integration/faults/`. Names are inventory assignments, not existing files.

All cases record backend, public method, fixture provenance, expected outcome,
request/commit counts per phase, gates, configured timeout/retry counters, cleanup,
and same-client recovery. Use short configured phase deadlines and a generous
outer watchdog; a stalled body is bounded by the actual read/write/deadline owner.
For close cases the recovery client is the same reopened object. Compare bytes and
SHA-256 on success; seed an existing destination and assert it survives failure.

### R3: direct upload

Public entry: `client.sources.add_file(notebook_id, small_txt_path)` on both backends.
Web fixture provenance: `tests/unit/test_source_upload_pipeline.py` registration
shape cases and `test_start_resumable_upload_uses_injected_http_client`. Android:
`tests/unit/android/test_source_upload.py` real generated `sources_pb2` tentative
registration fixtures, session URL and upload headers; start body comes from real
`build_upload_start_body`. Finalize is Web POST/chunked iterator versus Android
PUT/Content-Length. Start returns the existing required upload URL/status headers;
Android final response requires `x-goog-upload-status: final`.

| Cases | Contract and wire evidence |
| --- | --- |
| R3-W/A-success | Register once, start once, finalize once; exact digest committed; real source readback fixture if finalizer requests it; returned `Source.id` matches confirmed registration. |
| R3-W/A-register-refused | Use an existing decoder-supported refusal; no start/finalize. Web registration exceptions retain current mapper behavior; Android tentative-registration refusal provides positive rejection evidence. Do not equate arbitrary status with rejection. |
| R3-W/A-start-failed | Register once, start once, no finalize. Web 503 → `ServerError`, with `source_id` and `stage="start_session"`; Android invalid/failed start → `SourceAddError`, `source_id`, `stage="start"`. |
| R3-W/A-prefix-disconnect | Gate after actual nonzero body consumption, then disconnect before full body. One finalize dispatch, no transfer commit. Web transport error → `NetworkError`, `stage="upload_finalize"`; Android → `SourceAddError`, `stage="finalize"`; both retain registered ID. |
| R3-W/A-body-stall | Hold request consumer after prefix; use body large enough to establish producer backpressure, plus watchdog. Same stage identity contract as above; inspect actual timeout/outcome rather than assuming a read timeout while still writing. No duplicate registration/finalize. |
| R3-W/A-finalize-commit-loss | Consume/validate whole body, journal commit independently, close before acknowledgment. One registration/start/finalize and one commit; same failure identity as prefix disconnect; do not restart upload or delete registered source. |

All cases prove descriptor/child/client settlement, release the upload slot, and run
another operation on the same client (I1–I4, I7–I8). Registration failures preserve
current outcome metadata; post-registration ID attributes do not themselves claim
that bytes finalized successfully.

### R4: upload cancellation and generation

Reuse `tests/integration/concurrency/test_upload_cancel_dangling_session.py` and
`tests/unit/test_source_upload_pipeline.py` pre-dispatch/forced-close contracts;
Android counterparts are `test_source_upload.py` and `test_upload_pipeline.py`.

- R4-W-before-dispatch: gate admission of finalize child before its first POST;
  cancel caller. Expect `CancelledError`, zero finalize dispatch, one best-effort
  tracked Scotty cancel while epoch remains open. Settle descriptor and child.
- R4-W-after-prefix: server consumes body prefix, cancel caller, release transfer;
  Web shield waits for finalize settlement before propagating cancellation. Expect
  at most one finalize/commit and no speculative source/session deletion.
- R4-A-before-dispatch and R4-A-after-prefix: cancel HTTP child and gather it;
  ordinary cancellation propagates `CancelledError`. Preserve Android's own
  cancellation semantics; do not require Web's background completion behavior.
- R4-W/A-close-reopen: hold body, force close, await owner settlement, reopen same
  client and probe. Android close interruption may surface its established
  `RuntimeError("Android upload was interrupted by transport close.")`; Web close
  fences its tracked children. No old descriptor, transport or cancel request may
  touch the new generation. Repeat cancellation during settlement as a separate
  variant.

Required invariants: I1, I3–I5 where shared admission is involved, I7–I8.

### R5: artifact publication mechanisms

Representative public entries are `artifacts.download_audio`, `download_video`,
`download_infographic` and `download_slide_deck`, with fixed artifact IDs and real
artifact-list/readback decoder fixtures. Reuse artifact selection/row fixtures in
`tests/unit/test_artifact_downloads_coverage.py`, `test_prepared_artifact_downloads.py`
and `tests/unit/android/test_asset_downloads.py`; use tiny valid PNG, PDF, WAV/MP4
payloads with provenance rather than arbitrary text marked as media.

Inventory mechanisms: W-single HTTPX producer/writer queue; W-batch buffered GET plus
`write_staging` at the assembled service layer (no current public caller reaches
its private batch wrapper); A-guarded streaming
single/typed; A-guarded batch at service layer where no distinct public batch API
exists; and optional W-curl buffered single. Public single entries must exercise
both backend owners. Narrower-layer Android batch disposition is intentional and
must not be described as a new public API.

For each selected mechanism register separate `success`, `truncation`,
`body-stall`, `prefix-disconnect`, `cancel`, and `close-reopen` cases. Truncation
advertises more bytes than sent; prefix-disconnect requires server response-prefix
evidence; stalls/cancellation use deterministic gates. W-single transport failures
raise `ArtifactDownloadError` with the established cause; W-batch accumulates
`DownloadResult.failed` and public aggregation retains its established error.
Android transport failures become `ArtifactDownloadError` with bounded
`code=transport`, approved host/hop and no raw cause. Cancellation remains control
flow, and retired-epoch failures retain lifecycle semantics. Never accept a partial
file as success. Assert no `.tmp`/`.part` files, writer threads, response contexts,
client registrations or stale publication remain (I2–I4, I6–I8).

### R6: Web streamed chat

Public entry: `client.chat.ask` with explicit source IDs and conversation inputs.
Fixtures: `tests/unit/fixtures/chat_stream_final_response.json`,
`tests/unit/test_streaming_chat_wire.py`, and recorded `tests/cassettes/web/chat_ask.yaml`.
Produce valid length-prefixed batchexecute-style chat wire frames, then split at
real frame boundaries and inside a frame in separate cases.

Cases: R6-W-success, R6-W-partial-frame-disconnect, R6-W-partial-answer-disconnect,
R6-W-partial-answer-stall, R6-W-partial-answer-cancel. Web currently buffers through
`stream_post_with_size_cap` and parses only after response completion; do not claim
that a partial public answer was delivered. Disconnect/read stall surfaces
`NetworkError` with real transport evidence, no replay of transmitted chat, and
current operation metadata. Cancel settles stream and client, then same-client
ask/read succeeds. Reuse Android registry `stream_auth` and `deadlines_cancel`
partial delivery cases as comparative evidence; they do not establish Web decoding.
Applicable invariants: I1–I2, I4, I7–I8.

### R9: credentials, redirects and content

Use the R5 public entries and fixtures plus
`tests/unit/test_download_redirect_revalidation.py` and Android asset fixtures.
Cases for both owners: `401`, `403`, `trusted-redirect`, `untrusted-redirect`,
`redirect-loop`, `expired-signed-capability`, and `200-html`. Android adds
`bearer-to-capability-to-bearer`, `application-redirect`, and `200-wrong-signature`;
Web has no equivalent general signature requirement and must not gain one here.
Also exercise upload `401` and `403` independently on start and finalize under R3.

Artifact 401/403 raises `AuthError`; no transparent signed-URL refresh is promised.
Android invalidates only the bearer generation actually attached to the failing
hop; a signed capability's 401 does not invalidate an unrelated bearer. Web retains
its scrubbed HTTP-status cause. Android upload specifically treats 401 as `AuthError`
with stage/ID; other statuses, including 403, follow its `SourceAddError` path.
Web upload uses its existing HTTP mapper and attached stage/ID.

Allowed redirect success must establish exact credentials per logical hop and
fresh policy per batch URL. Disallowed hops produce zero requests at the target.
Loop boundedness follows the existing 20-redirect budget (the initial attempt is
additional). Android reports `too_many_hops`; Web retains HTTPX/curl mapping.
HTML-as-success is rejected by both asset owners; Android signature/MIME failures
must not publish. No error/recovery case may silently mint a new URL or replay
an already dispatched upload. Inspect secret-free public reports and preserve the
old destination, cleanup and same-client recovery (I1–I3, I6–I8).

### R12: Android Drive staging

Public entry: `sources.add_file` with a tiny `.csv` fixture, selecting the current
Drive route. Fixtures: `tests/unit/android/test_source_upload.py` and
`test_drive_staging.py`; staging response `{"id":"opaque-staged-id"}`, real
multipart body, generated AddSources/GetProject replies. Keep Drive cleanup separate
from NotebookLM source deletion.

| Cases | Current cleanup and outcome contract |
| --- | --- |
| R12-success | One stage, one confirmed import/readiness workflow, one DELETE of exact staged ID, successful source result. |
| R12-import-refused | After staging, a positively NOT_SENT/REJECTED import permits one cleanup attempt while epoch/deadline open. Preserve primary public exception and `prerequisite_ids`. |
| R12-import-ambiguous | Lost import acknowledgment retains staged file, zero DELETE; preserve ambiguity and prerequisite identity. |
| R12-import-timeout | Readiness timeout or unknown import retains staging, zero DELETE; preserve original source/timeout evidence where exposed. |
| R12-terminal-processing-failure | Confirmed `SourceProcessingError` with recovery NONE permits cleanup while fence open. |
| R12-cleanup-failed-success / cleanup-failed-refusal | DELETE failure logs bounded warning and exact staging identity; preserve success or original import failure. No new source DELETE. |
| R12-cancel-during-stage | No invented staged identity if response was lost. Cancel settles stage client/body; no speculative cleanup. |
| R12-cancel-after-stage / during-import | Cancellation keeps staged prerequisite because it does not prove dependent import settlement. No unconditional DELETE. |
| R12-close-before-cleanup / deadline-before-cleanup | Closed epoch or workflow cleanup deadline retains staging even after a successful body; bounded warning records retention. Reopen probe must use same client. |

`drive_staging._cleanup_allowed_after_import_error` is authoritative: only
NOT_SENT/REJECTED, or CONFIRMED plus recovery NONE on `SourceProcessingError`,
authorize failed-import cleanup. Arbitrary exception classes never prove refusal.
Keep safe cleanup observable without masking primary outcome (I1–I2, I4, I6–I8).

### R13: connection behavior and optional transport

Mandatory HTTPX cases: `reused-peer-close`, `server-restart-read`,
`slow-request-consumption-read`, and `slow-request-consumption-upload`. Use the
existing notebook list/get response fixtures and R3 upload body. Keep-alive is
opt-in; two observed requests must share one connection ID before the peer closes
it. Then a safe read recovers under actual policy without constructing a replacement
client. Restart the server on the same local endpoint between safe reads and
record separate connection generations. A slow consumer must journal body progress
and transport settlement; an application gate alone does not prove socket write
backpressure. Preserve default connection-close server mode.

Optional curl lane: separate real-adapter `read`, `upload-success`,
`upload-prefix-failure`, `download-success`, `download-prefix-failure` and
`transfer-cancel` cases. Cover AsyncSession and standalone upload Curl construction,
TLS verification, forbidden destination rejection and thread/response settlement.
Use actual curl request/commit counts; exceptions may normalize differently from
HTTPX. This lane requires the routing seam above and remains incomplete until it
runs. No production retry authorization changes are permitted (I1–I4, I7–I8).

## Audit validation

Inspected owners, existing harness assembly, listed unit/concurrency fixture sources
and generated protocol imports at the baseline. The isolated underlying curl/TLS
probe succeeded after using an explicitly owned CONNECT_TO slist; no live endpoints
were contacted. This documentation change does not claim a suite run, stress timing,
or implementation completion. Suite counts/timings and R1–R2/R7–R8/R10–R11/R14 are
owned by the companion whole-program inventory.


## Implemented Web evidence

`tests/_fault_server/web_transfers.py` registers 13 upload and 26 download cases;
`web_streaming.py` registers five chat cases. The resulting Web registry has 60
scenarios including the original 16. Each new case performs a valid baseline and
same-client read recovery, or reopens the same object for intentional close.
Public audio cases obtain artifact rows through real LIST_ARTIFACTS RPC decoding;
only buffered batch uses the backend-owned service directly. Asset read/write
budgets use the private constructor seam because those APIs expose fixed default
transport windows. Upload phases use the existing upload timeout configuration.

Web start and finalize routes use distinct exact session capabilities for baseline
and target transactions. Pre-finalize cancellation gates one instance's real HTTPX
client context entry and observes that the caller has attached to the finalize
shield before cancelling; the tracked Scotty cancel is observed on the socket.
Declared abandoned request bodies use the server's explicit abandonment contract;
malformed framing is still a service failure. The Web socket cases and exact
construction signature guardrail passed together: 135 tests in 5.01 seconds.
This does not claim optional curl faults, repeated-cancellation variants, or other
backend families are complete.
