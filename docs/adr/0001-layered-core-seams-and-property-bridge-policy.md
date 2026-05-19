# ADR-001: Layered `_core` seams and the property-bridge policy

## Status

Accepted (retroactive).

Documents a pattern shipped incrementally across tier-1 through tier-10 (PRs roughly mid-2025 through 2026-05). This ADR backfills the rationale; the code already reflects the decision.

## Context

`NotebookLMClient` is an `async` client for an undocumented Google RPC surface (`batchexecute`). The earliest implementation co-located transport, RPC dispatch, auth refresh, drain coordination, metrics, request-id assignment, cookie persistence, lifecycle, polling, and conversation caching inside one `_core.py` module. By the time the codebase reached tier 7 the module had crossed ~1,800 lines and 90+ methods. Two pressures broke the monolith:

- **Independent testability** — the auth-refresh loop, the keepalive task, and the drain coordinator each have non-trivial timing semantics. Testing them through `NotebookLMClient` required spinning the full client and patching half a dozen unrelated collaborators per test.
- **Independent reasoning** — landlocked invariants (e.g. "drain must complete before close returns") had no module-level home, so reviewers had to re-derive them on every PR.

Tier 8/9/10 extracted the cross-cutting concerns into named seam modules. As of HEAD the seams are:

```text
_core_transport.py            authed-POST path + retry loops
_core_rpc.py                  RPC dispatch executor (DecodeResponse, RpcOwner protocols)
_core_auth.py                 AuthRefreshCoordinator + auth-snapshot lock
_core_drain.py                TransportDrainTracker + _TransportOperationToken
_core_metrics.py              ClientMetrics counters + on_rpc_event callback
_core_reqid.py                ReqidCounter (monotonic _reqid for chat backend)
_core_cache.py                Per-instance LRU conversation cache
_core_polling.py              Pending-poll registry for long-running artifact gens
_core_lifecycle.py            Open/close lifecycle (loop-affinity guard + keepalive task)
_core_cookie_persistence.py   Cookie-jar persistence + __Secure-1PSIDTS rotation
_core_constants.py            Module-level DEFAULT_* knobs
_core_helpers.py              is_auth_error / AUTH_ERROR_PATTERNS / keepalive helpers
_core_error_injection.py      _SyntheticErrorTransport + env-var guard
```

The extraction was constrained by an unusually high test-coupling load: tests reach into the live `Session` instance with `core._save_lock`, `core._metrics_lock`, `core._on_rpc_event`, and many other private attributes — patterns that pre-date the seam extraction. When the storage for these attributes moved into the seams, the legacy attribute names had to keep resolving on the `Session` instance or hundreds of tests would break in a single PR.

The chosen mechanism is a *property-bridge policy*. Each migrated attribute keeps its legacy name on `Session`, but the property delegates reads and writes to the owning seam:

```python
# src/notebooklm/_core.py:450-774 (representative excerpt)
@property
def _save_lock(self) -> threading.Lock:
    return self.cookie_persistence.save_lock

@_save_lock.setter
def _save_lock(self, value: threading.Lock) -> None:
    self.cookie_persistence.save_lock = value
```

Roughly 324 lines of `_core.py` (lines ~450-774) are property bridges of this form. They exist for two distinct reasons:

1. **Sub-client compatibility** — the capability adapter (`_capabilities.py`) refers to attribute names that have been physically extracted; the bridge keeps the adapter's contract intact.
2. **Test compatibility** — `monkeypatch.setattr(core, "_save_lock", fake_lock)` is a load-bearing test idiom across ~273 sites (see ADR-003 and the forthcoming ADR-007); the bridge makes such patches write through to the real storage.

## Decision

`Session` is decomposed into thirteen seam modules, one per cross-cutting concern. `Session` itself becomes an orchestrator that wires the seams together and owns the public RPC surface.

Property bridges in `_core.py` are *permitted but tracked*. Each bridge must:

- Be added only when the attribute being relocated has live external readers (tests, sub-clients, downstream code).
- Read and write through to the owning seam — never store state of its own.
- Be retired the moment its only readers are themselves retired (see ADR-002 for the sub-client-compat half; ADR-007 will cover the test-compat half).

The seam extractions are behavior-preserving moves. Each one ships with a unit-test fixture that exercises the seam in isolation; nothing on the seam should require a full `Session` to test.

## Consequences

**Wanted:**

- Each seam can be reviewed, tested, and refactored without touching the others. The drain coordinator, the keepalive loop, and the metrics counters now have file-local invariants.
- The orchestrator (`Session`) makes the wiring graph readable in one place. The pattern is "instantiate seams in `__init__`, expose them as plain attributes, delegate public methods through them."
- The seam extraction is reversible. Because each seam owns one concept, mistakes can be undone without unrolling unrelated work.

**Unwanted:**

- `_core.py` is currently ~1,323 lines even after extraction because of the property-bridge zoo. The bridges are the *exit cost* of the architecture's test patterns, not the seam pattern itself. ADR-002 and ADR-007 plan the removal.
- The seam protocols (`RpcOwner`, `DecodeResponse`, `_AuthedTransportHost`, etc.) introduce extra type surface. This pays for itself only because the seams are tested independently — if the seams collapsed back into `_core.py`, the protocols would be ceremonial.
- New contributors must learn the seam map. The CLAUDE.md "Repository Structure" section exists for this reason; it should remain a thin onboarding guide rather than a duplicate of this ADR.

## Alternatives considered

- **Keep `_core.py` monolithic.** Rejected. The module had crossed the maintainability threshold (90+ methods, mixed concerns, mixed locking strategies, mixed lifetimes). Reviewers were re-deriving the same invariants on every PR; the cost was paid on every change, not just on the rare refactor.
- **Extract into a sibling package (`src/notebooklm/core/`).** Rejected at the time because the seams have a clear single owner (`Session`) and a sibling package implies "multiple consumers" — which is not (yet) the situation. The seams may be promoted to a sub-package in a future tier if a second consumer appears; the current flat layout keeps the cognitive load low.
- **Skip property bridges, accept the test breakage.** Rejected. The audit (`.sisyphus/plans/arch-biggest-problem-audit.md`) measured ~273 test-coupling sites that would have broken simultaneously, blocking the extraction PRs and forcing a "big bang" rewrite of the test suite. The property-bridge policy let the extraction land incrementally; the eventual bridge cleanup is sequenced after the test-pattern cleanup (D1 arc), which is the correct order.
