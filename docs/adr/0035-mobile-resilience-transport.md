# ADR-0035: Mobile backend as a resilience transport

## Status

Accepted.

## Context

NotebookLM exposes both the browser-oriented batchexecute surface used by this
project and an authenticated mobile gRPC surface. The mobile surface could be
used only as a wire-shape oracle, developed into an alternative production
transport, or deliberately ignored. That product decision must precede the
Phase A public-namespace splits: without a real second backend, those splits
would be relocation without an operational destination.

The existing public namespace APIs and dataclasses already express the
backend-neutral behavior callers rely on. The mobile surface does not yet cover
every namespace or operation, and the web backend remains the mature path.
Runtime failover inside an operation would also make mutation outcomes and
client state ambiguous.

## Decision

Develop the mobile backend as a **resilience transport** behind the existing
public namespace APIs.

During the Phase B pilot it is explicitly opt-in and the web backend remains
the default. A client chooses one backend at construction and does not switch
or silently fall back during its lifetime. Mobile implementations serve each
supported namespace end to end; an unsupported operation raises the public
unsupported-operation error before I/O and names the web alternative.

Offline web/mobile wire tests and authenticated conformance runs are release
gates, but oracle value is a consequence rather than the product boundary. A
later decision may select mobile automatically for eligible master-token
profiles only after the wave-one namespaces pass repeated conformance runs.

## Consequences

- Phase A's backend-neutral bases and `_web` extraction have a concrete second
  implementation target, so work after the first decoding package may proceed.
- Users gain an opt-in path that avoids the browser-cookie ladder for supported
  operations while the complete web backend remains available.
- Partial mobile coverage must be explicit. No mobile method may call web code
  as a fallback, and no live client may mix backend state.
- Both backends return the same public dataclasses and exceptions; the base
  classes' exact abstract methods are pinned as the coverage manifest.
- Automatic backend selection and default changes remain deferred until the
  Phase B evidence threshold is met.

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
