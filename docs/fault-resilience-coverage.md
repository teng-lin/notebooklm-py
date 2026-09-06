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
