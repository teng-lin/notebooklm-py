# Local fault-injection tests

The harness runs the real library against local HTTP and gRPC services with
scripted failures. It needs no NotebookLM account. Requests cross real sockets;
the services record both requests and committed state so a test can distinguish
a failed request from a write that succeeded before its response disappeared.

## Run the regression suite

From a source checkout:

```bash
uv sync --frozen --extra browser --extra dev --extra markdown --extra android
uv run pytest tests/integration/faults -q
```

The tests also run in the ordinary Python/OS compatibility matrix. Their
`allow_no_vcr` marker means they use local sockets rather than recorded
upstream traffic. No cassette recording or login is necessary.

## Run concurrent workloads

```bash
uv run python scripts/stress_fault_server.py --list-scenarios
uv run python scripts/stress_fault_server.py \
  --backend both --seed 42 --iterations 100 --concurrency 4 \
  --timeout 120 --scenario-timeout 15 \
  --json-report /tmp/fault-stress-report.json
```

`--backend` accepts `web`, `android`, or `both`. An iteration runs one scenario
cohort with its own client and server. Cohorts for authentication races issue
concurrent requests through a shared client. `--concurrency` limits active
cohorts, rather than counting every RPC they issue. Increase iterations for a
longer run; keep finite per-scenario and aggregate deadlines.

Fault choices are assigned before workers start, using a private seeded random
generator. The report preserves the assignments, scenario plans, events,
checks, and outcomes, including incomplete operations. The seed reproduces
logical assignments for the same scenario registry and code revision. It does
not reproduce OS scheduling, exact timing, or a later version of a scenario.

The process returns nonzero if an invariant fails, an unexpected exception
occurs, or a deadline expires. An injected network/auth exception is an expected
outcome only when the scenario verifies its category and associated behavior.
Successful completion alone is insufficient: scenarios also check request
counts, credential transitions, committed state, and cleanup.

The **Fault Server Stress** workflow runs a bounded workload on pull requests
and a longer workload daily or on manual dispatch. Its report artifact contains
the seed and event trace even when the runner reports a failure.

## What the harness covers

| Boundary | Faults and evidence |
| --- | --- |
| HTTP retry policy | 429 with Retry-After, 503, recovery and exhausted budgets; observed attempts and backoff |
| HTTP protocol and deadlines | Invalid RPC bodies, truncated responses, delayed headers and stalled bodies; public errors and bounded completion |
| Web authentication | Stale CSRF/session/cookies, homepage refresh, login redirects and failed bootstrap; actual refresh parsing and concurrent refresh coordination |
| gRPC retries and authentication | UNAVAILABLE, RESOURCE_EXHAUSTED and UNAUTHENTICATED; real bearer generations, coordinated token minting and bounded replay |
| Streaming | Partial responses, stalled streams and authentication failure after delivery; cancellation and no stream replay |
| Mutation outcomes | Commit before losing the response; one server-side object and no unsafe create retry |
| Lifecycle | Cancel active calls, close under load and reopen; server handlers, channels and client scopes released |

Web routing preserves logical HTTPS URLs and Host/cookie behavior while a
test-only transport connects to local HTTP/1.1. Android uses a real local
insecure gRPC channel and the production bearer provider with a synthetic token
issuer. The production Google hostname allowlist remains enforced.

TLS certificate verification, DNS failures, browser login, actual Google token
issuance, and kernel packet loss are outside this harness. These tests establish
library resilience to the modeled protocol/socket faults; cassettes and live
tests establish agreement with the real upstream service.

The HTTP service closes each response connection. This version exercises the
HTTPX transport's new-connection recovery; stale pooled keep-alive connections
and the optional curl-cffi transport need separate scenarios.

## Add a scenario

The implementation lives in `tests/_fault_server/`; `web_scenarios.py` and
`android_scenarios.py` expose sorted `SCENARIOS` tuples and `run_scenario`.
The stress runner and integration tests call the same functions.

1. Add an explicit fault script for a representative public API operation.
   Use synchronization gates to force race ordering. If several operations
   share a server, assign faults by operation identity or a gated batch whose
   members deliberately receive the same action.
2. Record a `plan` event before allocating resources, including faults and
   cohort operation identifiers. Use the supplied `ScenarioResult` throughout
   so partial evidence survives cancellation and unexpected errors.
3. Assert public outcomes and independent server evidence with
   `result.require("unique_check_name", condition)`. Check successful payloads,
   retry counts, credential generations, or committed-object counts as
   appropriate. Unexpected requests and unused required script actions fail.
4. Close clients and servers in `finally`, record cleanup evidence, and bound
   waits. A failed assertion must still drain the resources it created.
5. Register the name in `SCENARIOS`, run its integration case, then exercise it
   concurrently with other scenarios. Add helper-level tests when a new fault
   mechanism needs its own behavioral coverage.

`ScenarioResult.record(kind, **payload)` snapshots JSON-safe evidence; it
rejects nonfinite numeric values. Check names are unique. A false requirement
raises `ScenarioFailure` carrying the result and trace. Avoid credentials in
events: generation labels and synthetic identifiers are sufficient.

Keep construction substitutions local and synchronous. Do not mutate process
environment or patch production module bindings across an `await` in a cohort:
the runner can execute other cohorts at that point. Keep all credentials inline
and synthetic, and reject any unmapped network destination before connecting.

## Investigate a failure

Start with the failed operation's scenario and checks, then inspect its plan,
request/credential events and cleanup evidence. Compare the server's committed
state and request count with the client's exception before deciding a retry
would be safe. Preserve the full report, code revision, and command when
reporting a bug.

Re-run with the same revision, seed and workload configuration. For a race,
reduce the failure to an explicit gate sequence and add it as a deterministic
regression; repeating random timing until a test passes does not resolve it.

The architectural rationale is in
[ADR-0038](adr/0038-local-fault-injection-harness.md).
