# ADR-002: Capability Protocol pattern (`ClientCoreCapabilities` fat union)

## Status

Superseded by the `arch-d2-cutover` PR (D2 PR-2). The `ClientCoreCapabilities` adapter and the transitional `ChatStreamingProvider` Protocol have been deleted; sub-clients now consume `ClientCore` directly, typed against per-sub-client narrow Protocols (`_NotebooksCore`, `_SourcesCore`, `_ArtifactsCore`, `_ChatCore`, `_ResearchCore`, `_NotesCore`, `_SettingsCore`, `_SharingCore`) co-located with each sub-client. The base capability Protocols (`CoreRPCProvider`, `SourceListProvider`, `CoreReqIdProvider`, `PollRegistryProvider`, `AuthRouteProvider`, `CookieJarProvider`, `TransportOperationProvider`, `UploadConcurrencyProvider`, `LoopAffinityProvider`) remain in `src/notebooklm/_capabilities.py` because multiple sub-clients compose them.

This ADR documents the pre-cutover pattern for historical context. The "Decision" section below describes the state prior to D2 cutover; the "Alternatives considered" section describes the replacement now adopted.

## Context

`NotebookLMClient` exposes eight namespaced feature APIs (`notebooks`, `sources`, `artifacts`, `chat`, `research`, `notes`, `settings`, `sharing`). Each feature API is implemented in its own module (`_notebooks.py`, `_sources.py`, etc.) and needs structured access to `ClientCore` collaborators (RPC dispatch, auth routing, request-id allocation, polling registry, transport bookkeeping, upload concurrency, etc.).

Two non-negotiable forces shaped the original design:

- **Sub-clients must not import `ClientCore` directly.** Doing so would create a circular dependency (`ClientCore` imports sub-clients to expose them on `NotebookLMClient.notebooks` etc.; sub-clients importing `ClientCore` would close the loop). Mypy enforces the boundary via `TYPE_CHECKING` gates today.
- **Sub-clients should be typeable.** When a sub-client calls `core.rpc_call(...)`, mypy needs to verify the signature; passing `Any` defeats the type system at exactly the place where method-ID drift would otherwise break silently.

The codebase resolved both forces with a *capability Protocol* pattern. Ten narrow `Protocol` classes describe individual collaborator surfaces:

```text
CoreRPCProvider · SourceListProvider · CoreReqIdProvider
ChatStreamingProvider · PollRegistryProvider · AuthRouteProvider
CookieJarProvider · TransportOperationProvider
UploadConcurrencyProvider · LoopAffinityProvider
```

Each Protocol describes the smallest collaborator surface the audit could identify at the time of extraction. A concrete adapter class `ClientCoreCapabilities` then multi-inherits *all ten* Protocols and forwards every method to an underlying `ClientCore` instance (`src/notebooklm/_capabilities.py:149-160`). Sub-clients accept a `ClientCoreCapabilities` parameter in their constructors and rely on it for every collaborator interaction.

The audit (`.sisyphus/plans/arch-biggest-problem-audit.md`, disease D2) classified the result as a *fat-union god-interface wearing a Protocol mask*. The Protocols are narrow individually, but every sub-client takes the union, so the *effective* contract sub-clients depend on is the full ten-Protocol surface. The hoped-for narrowing never materialized because the adapter pre-merges them.

## Decision

For the current arc (tier-10 baseline), the pattern is:

1. Define narrow capability `Protocol` classes in `src/notebooklm/_capabilities.py`, one per collaborator surface.
2. Define a single concrete adapter `ClientCoreCapabilities` that multi-inherits every Protocol and forwards to `ClientCore`.
3. Sub-client constructors accept a `ClientCoreCapabilities` instance, not a `ClientCore` instance.
4. `NotebookLMClient` constructs one `ClientCoreCapabilities` adapter at open time and threads it into every sub-client.

The pattern is *Accepted* today because:

- It provides a single import path for sub-clients (`from ._capabilities import ClientCoreCapabilities`), avoiding the "every sub-client lists its own Protocol grab-bag" boilerplate.
- It guarantees that every Protocol has at least one structural implementer (`ClientCore`, through the adapter), so mypy verifies the contract end-to-end.
- It survived the tier-7 thread-safety arc and the tier-8 RPC/VCR arc without churn, which means the surface is empirically stable.

## Consequences

**Wanted:**

- A single, mypy-verified seam between sub-clients and `ClientCore`.
- The capability Protocols document the collaborator graph in one place, which is useful for greenfield comparison (`docs/architecture-evolution.md`).

**Unwanted (and the reason for the sunset clause):**

- Every sub-client depends on the *union*, not the subset it actually needs. `NotebooksAPI` and `SettingsAPI` do not need `UploadConcurrencyProvider` or `ChatStreamingProvider`; today they advertise both anyway.
- `ClientCore` cannot shrink below ~1,300 lines while the union pins it. Every method named in any Protocol must remain on `ClientCore` (or on the adapter, which delegates to `ClientCore`).
- The property-bridge zoo in `_core.py:450-774` exists partly because the union forces `ClientCore` to expose attributes that have been physically relocated into seams — see ADR-001.
- The adapter has begun to leak private internals. `_capabilities.py:230` forwards `_core._begin_transport_post`, an underscore-prefixed method. The narrow-protocol contract has already started to bend into private territory.
- `ChatStreamingProvider`'s docstring openly self-describes as transitional: *"Chat-aware error mapping still lives on `ClientCore.query_post` until that is extracted into a chat-owned transport."* The fat union is documented as not-yet-done work.

The audit's recommendation is to *type each sub-client on its actual capability subset* and delete the `ClientCoreCapabilities` adapter. `ClientCore` will structurally satisfy each narrow Protocol; no runtime adapter is needed because structural sub-typing is enough. That work is sequenced as the D2 cutover (Wave 3 of the architecture-disease-remediation arc).

## Alternatives considered

- **Per-sub-client narrow `Protocol` class — chosen replacement for the D2 cutover.** Each sub-client declares its own `Protocol` listing only the collaborator surfaces it actually uses. `ClientCore` is not modified; structural sub-typing means `ClientCore` automatically satisfies each per-sub-client `Protocol`. Effect: `NotebooksAPI` depends on `CoreRPC + AuthRoute` only; the type checker enforces narrowing. Cost: each sub-client owns one extra type definition; ~8 new Protocol classes overall. Was not chosen at the time of the original extraction because the team prioritised "one import path for sub-clients" over "minimum coupling per sub-client"; the audit re-prioritises after observing the long-term coupling cost.
- **Constructor injection of individual collaborator dataclasses.** Rejected. Would force every sub-client constructor to take 4–7 typed parameters, and every test to construct that many fakes. The Protocol pattern is strictly more ergonomic and only the *unioned shape* is wrong, not the structural-typing approach.
- **Type sub-clients on `ClientCore` directly.** Rejected. Creates the circular-import problem and defeats the layering. The Protocol pattern is the canonical Python answer to "type me on something I cannot import."
- **`typing.Protocol` with `runtime_checkable=True` + `isinstance` guards instead of an adapter class.** Rejected. `isinstance` checks against `runtime_checkable` Protocols are slow and not actually safer (they do not inspect method signatures). The cost without benefit was clear at the time.
- **Delete `ClientCoreCapabilities` outright in this ADR.** Rejected. Removal must be paired with the per-sub-client Protocol introduction; deleting it first would force every sub-client to type its `core` parameter as `Any` during the migration window, which loses the type-safety benefit the pattern was designed to deliver. The D2 cutover sequences the swap atomically.
