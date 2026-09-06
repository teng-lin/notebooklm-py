# ADR-0038: Local fault-injection services

## Status

Accepted.

## Context

Transport mocks and recorded responses pin protocol and retry behavior, but
they do not exercise socket deadlines, truncated bodies, cancellation reaching
the server, or ambiguous writes whose response is lost. The repository already
has small local HTTP and gRPC servers inside individual tests. Authentication
and lifecycle regressions need reusable scenarios that combine those boundaries
with concurrent calls through the library.

## Decision

Keep reusable services and scenario definitions in `tests/_fault_server/`.
Use ephemeral loopback sockets, synthetic credentials, and a small stateful
subset of the upstream protocol. Run scenarios through production client
assembly and public feature APIs. Substitute transport routing and credential
issuance at construction; retain the real decoders, auth refresh coordinator,
bearer provider, retry policy, and lifecycle owners.

Web requests retain their logical HTTPS URL, Host, and cookie semantics while
the test transport connects to local HTTP. The Android loader verifies the
production target and opens a local insecure gRPC channel. Neither path changes
production hostname restrictions. These services test socket and protocol
behavior; they do not emulate TLS, DNS, browser login, or kernel packet loss.

Each scenario owns a server/client cohort, scripts faults explicitly, and
records requests, credential generations, committed state, invariant checks,
and cleanup. Gates establish concurrency ordering. A separate source-checkout
runner schedules bounded concurrent cohorts from a precomputed seed-based
assignment plan and writes diagnostic JSON, including incomplete operations.
A seed reproduces logical fault assignments, not operating-system scheduling.

Real-socket regression tests live under `tests/integration/faults/`, with
documented `allow_no_vcr` inventory entries. They run in the ordinary test
matrix. The runner also runs in a credential-free PR and scheduled/manual
workflow that preserves reports as artifacts.

## Consequences

- Server-side state distinguishes a rejected write from a committed write
  whose response was lost, so retry safety is checked independently of the
  exception observed by the client.
- Auth tests exercise production recovery with synthetic issuers, without
  depending on live accounts or weakening client security policy.
- Shared scenarios make stress failures available as ordinary regressions.
- The fake service intentionally implements a representative protocol subset.
  Recorded fixtures and live tests remain the evidence for upstream fidelity.
- No runtime dependency or installed command is added. Developing or running
  the harness requires a source checkout and development dependencies.

See [the harness guide](../fault-injection.md) for commands, coverage, and
scenario extension rules.

## Alternatives considered

- Extend transport mocks and cassettes alone. They remain useful for protocol
  fidelity, but cannot show whether cancellation reaches a real server or
  distinguish a committed mutation from a response lost on the socket.
- Add a network proxy or kernel fault injector as the primary harness. Those
  can model packet loss and connection faults, but cannot directly script
  authentication generations, partial application responses, or committed
  objects. They would also add setup requirements to ordinary test runs.
- Build a complete upstream emulator. Maintaining every API and backend
  would duplicate protocol work without strengthening the selected resilience
  invariants. Representative public operations keep the harness bounded.
- Run stress scenarios against live accounts. Live tests are still needed to
  verify upstream agreement, but cannot reliably force authentication races
  or commit-before-disconnect ordering and require credentials and quotas.
