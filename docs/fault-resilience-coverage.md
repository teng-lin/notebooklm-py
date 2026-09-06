# Resilience fault coverage

This inventory extends [the harness](fault-injection.md). Acceptance follows current
production contracts and [ADR-0038](adr/0038-local-fault-injection-harness.md).
Cases pair production-decoded fixtures with explicit faults, independent evidence,
cleanup, and recovery. The integration parametrizations and stress runner share the
same scenario registries.

## Baseline

Measured at `65dbd21d70f5be8c892da40a3660987b4118cd1c` on macOS 26.6.2 arm64,
Python 3.12.12, with browser/dev/markdown/android/mcp/server extras:

| Command | Result | Duration |
| --- | --- | --- |
| `uv run pytest tests/integration/faults tests/unit/test_http_fault_server.py tests/unit/test_stress_fault_server.py -q` | 61 passed | 2.35 seconds |
| `uv run python scripts/stress_fault_server.py --backend both --seed 42 --iterations 400 --concurrency 4 --timeout 600 --scenario-timeout 15 --json-report /tmp/fault-resilience-report.json` | 400 passed; 16 Web + 9 Android cases selected | 5.79 seconds |

These are predecessor measurements, not acceptance evidence for the expanded cases.

## Evidence and runner acceptance

The runner must distinguish the selected set, planned assignments, executed cases, and
skipped cases. CI must select at least one full deck. A small explicitly requested local
sample may retain its existing iteration count but must not claim full selected coverage.
Portable and optional real-curl selection are separate; missing optional dependencies
must fail the requested lane, never count as passing coverage.

Every case declares its required checks before allocating resources. The declaration
includes backend/transport, operation identity, independent retry budgets and operation
deadline, fixture provenance, fault/gate labels, and applicable invariants. Required
checks include action consumption, observed dispatch/commit counts, recovery, and owned
resource settlement. The runner verifies all declared checks were recorded and passed.
A successful return alone cannot establish cleanup or fault activation.

Reports retain partial events on exception, cancellation, and runner interruption.
Exceptions are classified by type; arbitrary exception text, request objects, task names,
locals, cookies, bearer tokens, CSRF/session values, and signed capability URLs are not
public diagnostics. Controlled in-memory credential comparisons export only generation
labels or booleans. Negative tests must cover omitted cleanup, unused faults, excess
mutation dispatch, skipped required cases, and unhandled child errors. Sentinel tests
must cover success, failure, and interruption, including cleanup-error diagnostics.
Interpreter-exit exceptions retain their control-flow semantics.

## Watchdogs and CI acceptance

Operation budgets must expire before the 15-second scenario limit. Cleanup has an
independent five-second watchdog; integration wrappers reserve that cleanup interval
rather than placing a 15-second workload under the previous eight-second wrapper.
The workload remains bounded by the 600-second runner and 15-minute job limits.
Use measured expanded-deck durations before increasing repetition counts or sharding.

Completion also requires ordinary-suite coverage, configured per-file floors, `make gates`,
current qualification selection, and supported Python/OS portable jobs. Local macOS
measurements do not substitute for that matrix. Optional curl reports name the platform
and real transport tested.

## Case inventories

- [Replay, budgets, shared work and workflows](fault-resilience-contracts.md): R1, R2, R7, R8, R10, R11.
- [Transfer construction and contracts](fault-resilience-seams.md): R3–R6, R9, R12, R13.
- [Adapter contracts](fault-resilience-adapters.md): R14.

## Implemented Android direct-upload cases

`tests/_fault_server/android_transfers.py` owns these explicit portable registry cases.
Each first decodes a successful registration and completes a successful upload, then
runs its selected variant and recovers through the same client (reopened for forced close).
Registration, POST start and PUT finalize counts are independently asserted. Baseline and
fault use distinct session capabilities. The socket service validates expected bytes/digest
before a commit. Outcomes export exception type/stage and retained-identity booleans only.

| Registry case | Coverage |
| --- | --- |
| `upload_success` | R3 successful direct transfer and digest commit |
| `upload_start_failure` | R3 registered identity retained on failed start |
| `upload_prefix_disconnect` | R3 nonempty partial request, no commit |
| `upload_body_stall` | R3 8 MiB producer against held consumer; bounded real transfer timeout |
| `upload_commit_loss` | R3 independently committed bytes, lost acknowledgement, no replay |
| `upload_start_401`, `upload_start_403` | R9 separate start credential/status contracts |
| `upload_finalize_401`, `upload_finalize_403` | R9 separate finalize credential/status contracts |
| `upload_cancel_before_finalize` | R4 cancellation during held start response, zero finalize |
| `upload_cancel_after_prefix` | R4 cancellation after server body consumption |
| `upload_close_reopen` | R4 forced owner close and same-client reopen recovery |

The direct-upload deck also includes registration transport failure, positive registration
refusal, and repeated cancellation while the actual native writer settles. Captured file
objects, child tasks, HTTP clients, and semaphore permits must settle before recovery.

## Implemented Android download mechanisms

`android_downloads.py` registers 15 explicit variants each for public infographic download
and the assembled guarded batch service (no new public batch API). Variants cover valid
PNG publication, truncation, response-prefix disconnect, operation deadline during body
stall, cancellation, close/reopen, separate 401 and 403, trusted/disallowed/loop redirects,
expired capability, HTML, wrong signature, and a bearer-to-capability-to-bearer bounce.
Each case preserves an existing destination on failure, checks the staging directory,
asserts owned clients/tasks settle, and downloads successfully again on the same client.
Per-hop checks prove bearer removal remains sticky across a bounce and resets for the
next download. Initial concurrent validation: 30/30 cases passed with concurrency 2.

## Implemented Android Drive staging

`android_drive.py` registers successful staging/import/readiness/cleanup, positively
refused registration, ambiguous registration/import, import timeout, terminal processing
failure, cleanup failures after success/refusal, and cancellation during stage, after stage,
during import, and forced close. Exact multipart size/digest is independently validated
before Drive commit; prerequisite IDs survive supported public failures. No unknown import
causes a Drive DELETE, and no case deletes a NotebookLM source. Failed DELETE status remains
observable without masking the primary outcome. Owned tasks settle before their input
directory is removed.

`drive_deadline_before_cleanup` combines real staging/import sockets with an explicitly
injected instance clock for cleanup-fence arithmetic: production has a 300-second minimum
Drive lifetime. The clock advances only after readiness reaches its server gate. This case
does not claim real-clock deadline coverage; `drive_import_timeout` uses the actual clock
and public RPC timeout for that separate property.


## Expanded implementation and measured validation

The portable deck contains **174 cases: 105 Web and 69 Android**. The separate real-curl
deck contains **10 cases**. Registration is shared with the integration tests; all nine
adapter cases, including the three live disconnect cases, belong to the portable deck.

| Families | Implementation |
| --- | --- |
| R1, R2 | Web and Android replay/refusal/loss contracts; independent retry counters across auth refresh; pre-send cancellation and aggregate deadlines |
| R3, R4 | Web/native uploads with issued-session validation, incremental body/digest commit, lost acknowledgements, cancellation, forced close, and captured resource settlement |
| R5, R9 | Web and Android guarded single/batch publication, truncation, redirects, expired/error content, and per-hop credential evidence |
| R6 | Chat stream loss, stall, cancellation, and UTF-8 fragmentation through the actual decoder |
| R7, R8 | Mixed RPC admission, transfers and shared pollers; waiter cancellation, shared refresh/mint failures, old-generation fences, close/reopen |
| R10, R11 | Generation kickoff/poll outcomes and original deadline; ordered research-import outcomes, incomplete lookup, and confirmed mutation/readback failure |
| R12 | Drive staging/import ownership, safe cleanup, retained ambiguous prerequisites, cancellation, and cleanup failure precedence |
| R13 | Actual keepalive connection reuse, restart, slow consumers, and native curl upload/download handles |
| R14 | REST/MCP/CLI mappings; real downstream transfer disconnects and detached MCP job survival |

At candidate `d821ecf5a`, on macOS 26.6.2 arm64 with Python 3.12.12:

| Command selection | Executed | Time |
| --- | --- | --- |
| Portable, seed 42, concurrency 4, `--iterations 400 --require-all-scenarios` | 400/400; all 174 selected cases, zero skipped | 11.66 seconds |
| `--backend web --transport curl_cffi`, seed 42, concurrency 2, 40 iterations | 40/40; all 10 selected cases, zero skipped | 2.42 seconds |

Polling retry exhaustion records the production 2/4/8-second arithmetic through an
instance-owned sleeper; the separate shared-poller case measures a real two-second
aggregate deadline. Drive cleanup-fence arithmetic uses the explicitly injected clock
already described above. Neither arithmetic case claims to measure elapsed waiting.

The fault-stress CI job selects at least the full portable deck; its separate curl job
installs `impersonate` and executes the complete curl selection on Ubuntu. Local curl
measurements cover macOS arm64; CI provides the Ubuntu result. The ordinary compatibility
matrix runs portable tests on its configured supported Python/OS cells. Local results do
not claim that those remote checks have completed.
