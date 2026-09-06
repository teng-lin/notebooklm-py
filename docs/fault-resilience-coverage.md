# Resilience fault coverage

This inventory extends [the harness](fault-injection.md). Acceptance follows current
production contracts and [ADR-0038](adr/0038-local-fault-injection-harness.md).
Cases remain pending until their production-decoded success fixture, explicit fault,
independent evidence, cleanup, and same-client recovery have passed.

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

Initial combined validation: 110 helper, integration and runner tests passed in 6.09 seconds.
This is partial implementation evidence; remaining cases in the inventories are still pending.

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
