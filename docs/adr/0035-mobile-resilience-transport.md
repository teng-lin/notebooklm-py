# ADR-0035: Mobile backend as a resilience transport

## Status

Accepted.

**Implementation update (2026-08-31):** the decision below allowed explicit,
operation-shaped Web compatibility callables while mobile coverage was being
qualified. The final three were subsequently closed. Current explicit Android
selection still follows this ADR's all-eleven, no-runtime-failover decision,
but its installed namespace graph now has zero Web operation collaborators.
See [`../android/web-compat-seam-closure.md`](../android/web-compat-seam-closure.md).

## Context

NotebookLM exposes both the browser-oriented batchexecute surface used by this
project and an authenticated mobile gRPC surface. The mobile surface could be
used only as a wire-shape oracle, developed into an alternative production
transport, or deliberately ignored. That product decision must precede a
public-namespace split: without a real second backend, the split would be
relocation without an operational destination.

The existing public namespace APIs and dataclasses already express the
backend-neutral behavior callers rely on. The mobile surface does not expose
every public operation, and the web backend remains the mature path.
Runtime failover inside an operation would also make mutation outcomes and
client state ambiguous.

## Decision

Develop the mobile backend as a **resilience transport** behind the existing
public namespace APIs.

It is explicitly opt-in and the web backend remains the default. A client
chooses its namespace graph at construction and does not switch that graph
during its lifetime. Explicit `backend="android"` installs Android adapter
objects for all eleven public namespaces. Where the recovered mobile contract
cannot express a public operation, the composition root injects a narrow,
named Web compatibility callable instead of hiding a namespace-level fallback.

Offline web/mobile wire tests and authenticated conformance runs are release
gates, but oracle value is a consequence rather than the product boundary. A
later decision may select mobile automatically for eligible master-token
profiles only after the complete graph passes repeated conformance runs.

## Consequences

- The backend-neutral bases and `_web` extraction have a concrete second
  implementation target, so work after the first decoding package may proceed.
- Users gain an opt-in path that avoids the browser-cookie ladder for supported
  operations while the complete web backend remains available.
- Partial operation coverage must be explicit. Compatibility is injected at
  assembly through operation-shaped collaborators and documented per method;
  Android adapters do not import or construct Web implementations.
- Both backends return the same public dataclasses and exceptions; the base
  classes' exact abstract methods are pinned as the coverage manifest.
- Automatic backend selection and default changes remain deferred until the
  Android conformance threshold is met.

### Mind-map contract

The selected Android mind-map adapter composes the base-typed artifact and note
namespace interfaces; it adds no separate mobile protobuf declarations.
Interactive generation, tree reads, rename, and delete use the Android artifact
contract. Note-backed list, tree, rename, and delete use the Android Notes
contract. Note-backed generation uses the current-bundle `ActOnSources` request
on the mobile gRPC route and persists the returned JSON through native
`CreateNote`; no Web compatibility callable remains in the mind-map namespace.

## Alternatives considered

**Oracle only.** Rejected because it captures drift but does not provide users
an operational path when cookie authentication or batchexecute is unavailable.
It would not justify splitting every public namespace into backend subclasses.

**Never implement mobile.** Rejected because validated bearer-authenticated
reads, upload, and artifact transfer demonstrate a credible independent path,
while retaining web-only architecture leaves recurring cookie failures as a
single point of failure.

**Automatic per-call failover.** Rejected because a lost response to a mutation
cannot safely be replayed on another transport, and mixed-backend cache and
lifecycle state would weaken the existing client contract.
