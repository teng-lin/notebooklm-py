# ADR-010: Session/Kernel split

## Status

Accepted (Tier 13 PR 13.1).

## Context

`Session` currently owns orchestration, RPC encoding/decoding, drain
tracking, request-id allocation, cookie access, HTTP lifecycle, and the
feature-facing capability surface. The Tier-12 middleware chain isolated
cross-cutting transport concerns, but feature APIs still depend on
per-feature `_<X>Core` Protocols and shared base Protocols in
`src/notebooklm/_capabilities.py`.

That shape blocks the Tier-13 decomposition: feature APIs need one stable
orchestration contract, transport code needs a smaller HTTP-only contract,
and Artifacts needs one close-time hook registration affordance without
expanding the general Session surface.

## Decision

Tier 13 uses three structural contracts in
`src/notebooklm/_session_contracts.py`:

- `Session: Protocol` has exactly five members: `rpc_call`,
  `transport_post`, `next_reqid`, `assert_bound_loop`, and
  `operation_scope`.
- `Kernel: Protocol` has exactly three members: `post`, `cookies`, and
  `aclose`.
- `DrainHookRegistration: Protocol` has exactly one member:
  `register_drain_hook`.

`Session.transport_post` accepts the existing public-ish
`BuildRequest` alias from `src/notebooklm/_request_types.py`. New session
contracts must not expose `_BuildRequest` in signatures.

This PR is type-only. It defines the contracts and documentation, but it
does not create concrete `_session.py` or `_kernel.py` modules, rename
`Session`, move cookies, move `httpx` lifecycle, or wire new runtime
behavior.

Later Tier-13 PRs delete `src/notebooklm/_capabilities.py` and all
per-feature `_<X>Core` Protocols after feature APIs are retyped to the
new contracts. `DrainHookRegistration` remains separate from `Session` so
Artifacts can register close-time polling cleanup without adding
feature-specific lifecycle methods to the five-member Session surface.

## Consequences

Wanted:

- Feature APIs converge on one semantic orchestration surface instead of
  composing many narrow capability fragments.
- The transport boundary is small enough for a concrete Kernel to own
  `httpx.AsyncClient` lifecycle and cookies without also owning RPC
  orchestration.
- Artifacts gets an explicit, typed drain-hook seam while other features
  remain typed only against `Session`.
- The `BuildRequest` alias prevents new Protocol signatures from leaking
  `_BuildRequest`.

Unwanted:

- During the migration window both the old `_capabilities.py` Protocols
  and the new contracts coexist.
- `Session` does not structurally satisfy every new contract in this
  PR; concrete conformance lands with the later extraction and retyping
  PRs.

## Alternatives considered

Keep per-feature `_<X>Core` Protocols. Rejected because the endpoint would
still duplicate the same core session operations across feature sub-client
modules and keep `_capabilities.py` as a permanent coordination point.

Add `register_drain_hook` to `Session`. Rejected because it would expand
the general feature contract for an Artifacts-only lifecycle need and
break the five-member Session gate.

Expose a `Kernel.stream` member. Rejected because chat receives a fully
buffered `httpx.Response`; the streaming byte iteration is an internal
transport implementation detail, not a consumer-facing Kernel operation.

Move concrete classes in this PR. Rejected because PR 13.1 is deliberately
type-only; `_session.py` and `_kernel.py` are reserved for later concrete
implementation PRs.
