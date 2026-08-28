# ADR-0005: Mutating-RPC idempotency taxonomy

## Status

Accepted (retroactive). Originally documented the six-policy
classification shipped in tier-9 (B1 foundation + Wave-2 classifications
across `b-research-notes`, `b-generation`, `b-sources`, and
`b-side-effects`). This ADR is the canonical home for the rationale that
previously lived in the now-gitignored tier-9 plan; the registry code in
`src/notebooklm/_web/policy.py` references this ADR as ADR-0005.

Amended on 2026-05-29 to remove the unregistered `CLIENT_TOKEN_DEDUPE`
policy and executor token-injection hook. No current `RPCMethod` has a
verified client-token slot, so keeping the policy made the registry
advertise a dead retry-safety mechanism.

Amended again on 2026-05-29 after the registry audit was completed:
the production `IDEMPOTENCY_REGISTRY` now has an explicit entry for
every active `RPCMethod`. `UNCLASSIFIED` remains only as a hand-built
placeholder for tests and future development, not as the production
classification for read-only RPCs.

Amended on 2026-08-27 to make the backend boundary explicit. The live
singleton, registry types, resolution function, and declarative table now live
together in `src/notebooklm/_web/policy.py`, which owns the single import-time
seed. The neutral `src/notebooklm/_idempotency.py` retains only create probing
and the unconfirmed-write marker.

## Context

The NotebookLM RPC surface is `batchexecute` over HTTPS, and any mutating call (create, delete, refresh, share, generate, …) is susceptible to a *commit-lost* failure: the server commits the write, then the response is lost in transit. A naive retry produces a duplicate write — a duplicate notebook, a duplicate source, an extra LLM inference, a re-sent invite email — depending on the RPC.

The transport retry layer runs an inner retry loop for transient 5xx / 429 / network-error failures. That loop is *correct* for read-only RPCs and dangerous for mutating ones. Before the taxonomy existed, the only mitigation was a per-call-site decision (`disable_internal_retries=True`) that did not document *why* an RPC was retry-unsafe, so the decision was easy to lose during refactors.

Five retry-safety profiles cover every verified NotebookLM RPC shape:

| Policy | Meaning | Effect on the inner retry loop |
|---|---|---|
| `UNCLASSIFIED` | Placeholder for hand-built test/future registries; not used by the production registry for active RPCs | Silent, retries enabled (preserves pre-taxonomy behavior) |
| `PROBE_THEN_CREATE` | Caller owns a probe loop; transport must not blind-retry | Force-disable inner retries |
| `IDEMPOTENT_SET_OP` | Server applies set semantics (delete / rename) | Retries are safe; left enabled |
| `AT_LEAST_ONCE_ACCEPTED` | Caller has accepted duplicate-side-effect cost | Retries enabled; rate-limited WARN emitted |
| `NON_IDEMPOTENT_NO_RETRY` | No dedupe key, no probe; first failure must surface | Force-disable inner retries |

The taxonomy and the production registry (`IDEMPOTENCY_REGISTRY` in `_web/policy.py`) are consulted by `RpcExecutor` to compute the effective `disable_internal_retries` value. Variants (e.g. `ADD_SOURCE` `"url"` vs `"text"` vs `"drive"`, artifact generation/revise flows, and source-label `UPDATE_LABEL` `"add_sources"` / `"remove_sources"`) carry their own classifications when the wire-shape differs by call-site.

An internal architecture audit (disease D3) flagged ten references to "Wave 2" in the idempotency policy code whose design rationale lived only in internal planning notes. This ADR is the public home for that rationale; the in-code references now point here.

## Decision

Every active `RPCMethod` is registered in the `_web/policy.py` `IDEMPOTENCY_REGISTRY` singleton with one of five `IdempotencyPolicy` values, optionally per *operation variant* when the call-site shape differs. That module owns the only production seed. The registry is the single source of truth consumed by `RpcExecutor`, and the registry-audit tests fail if a new enum member keeps the default placeholder.

The classification rules are:

- **Read-only RPCs** classify as `IDEMPOTENT_SET_OP`; replay is explicitly safe because the RPC does not mutate server state.
- **Mutating RPCs with a stable server-side dedupe key** classify as `IDEMPOTENT_SET_OP` (delete / rename / set-state) — retries are explicitly safe.
- **Mutating RPCs without a dedupe key but with a probe RPC** classify as `PROBE_THEN_CREATE`; the inner retry loop is force-disabled, and the per-API call site owns a probe-then-create wrapper (see `idempotent_create()` in `_idempotency.py` and per-API uses such as `_notebooks.py`, `_web/sources/add.py`, and `_web/sources/upload.py`).

  **A `PROBE_THEN_CREATE` probe must not answer what it does not know (#2220).** It may return `None` — "no match", which `idempotent_create` reads as evidence the create did not land and acts on by repeating it — only when it has affirmatively established that no matching resource exists. A probe that cannot answer, because its own list RPC failed for a non-transport reason, must raise; the retry loop then aborts and the caller is told the create is unresolved.

  This is the boundary between two policies that would otherwise blur. Swallowing a probe failure and retrying anyway makes the operation at-least-once — which is `AT_LEAST_ONCE_ACCEPTED`, a policy this taxonomy requires the *caller* to opt into by name and which emits a rate-limited WARN precisely so the trade-off stays visible. A `PROBE_THEN_CREATE` path that degrades into it silently, at the one moment its guarantee is load-bearing, has misclassified itself. The four probe implementations (`notebooks.create`, `sources.add_url`, `sources.add_drive`, `register_file_source`) therefore all propagate a probe failure. The errors they raise carry an `unconfirmed` marker attribute (`_idempotency.mark_unconfirmed`) so consumers can distinguish "the write may have committed" from "the write was rejected" without matching on message text — `_app.errors` relies on it to keep an unconfirmed create out of the *retriable* and *isolate-and-continue* bands.

  The *pre-create baseline* read each of those probes takes is deliberately not held to this rule: it runs before anything is written, so degrading is safe, and failing there would break creates that would otherwise succeed. It logs at WARNING (the `notebooklm` logger's default level) so the degraded call is at least observable. What it degrades *to* is uniform across all four: `None`, the "baseline unavailable" sentinel, never an empty `set()`. An empty set reads as "nothing existed" and disables the filter entirely, so the probe adopts the first same-key match it sees — for `notebooks.create` that meant a pre-existing same-titled notebook returned as freshly created, with every later `sources.add_*` / `chat.ask` in the session then aimed at the wrong notebook (#2232, the #2204-class defect for notebooks). With the sentinel, a probe running without a baseline raises on *any* match rather than on more than one. The trade is deliberate: refusing to attribute can cost a duplicate create, which is loud and visible in the notebook list, where the silent wrong identity it replaces gave the caller nothing to act on and no reason to look.
- **Mutating RPCs that produce visible side effects (emails, billing, notifications) and that the caller has explicitly opted into** classify as `AT_LEAST_ONCE_ACCEPTED`; retries are enabled but a rate-limited WARN is emitted so operators can observe the trade-off.
- **Mutating RPCs with no dedupe key and no reliable probe** classify as `NON_IDEMPOTENT_NO_RETRY`; the inner retry loop is force-disabled and the first failure surfaces to the caller for manual disambiguation.

The completed production classifications are recorded in `_web/policy.py` (with the per-RPC rationale captured at the registration site). Future classifications continue to land in the same module without changes to the executor; the registry is intentionally extensible.

The five-policy axis is *closed*. Adding a sixth policy requires updating this ADR and the executor in lock-step.

## Consequences

**Wanted:**

- Retry safety is now a *property of the RPC*, not a property of the call site. New call sites inherit the safe behavior without re-deriving it.
- The executor's retry logic is small and local; the policy decisions live in the registry where they can be reviewed in isolation.
- The taxonomy is small enough (five policies) that a reviewer can hold it in mind during a code review. A sixth policy would push past that threshold and is rejected by design.

**Unwanted:**

- The registry is populated at module import time and is effectively immutable. A test that wants to override a classification needs to construct a fresh `IdempotencyRegistry` instance (the contract documents this, but it is friction).
- `AT_LEAST_ONCE_ACCEPTED`'s rate-limited WARN log is per-process-state (a module-level dict). Tests that observe the log behavior have to manage state across test cases; the WARN is throttled to one emission per 30 seconds per `(method, variant)` to avoid drowning operators in spam, which means a noisy test environment can suppress emissions that would have fired in production.
- The taxonomy is *opinionated about caller behavior*. `AT_LEAST_ONCE_ACCEPTED` says "the caller has accepted at-least-once semantics"; if a future contributor classifies an RPC that way without the caller actually having opted in, the registry will silently green-light duplicate side effects. Reviews of new `AT_LEAST_ONCE_ACCEPTED` classifications need to be careful.
- The variant-table fallback (`get_entry(method, variant=v)` on a method with no variant table silently falls back to `(method, None)`; the same call on a method *with* a variant table but for an unknown variant raises) is subtle. The contract is documented in the registry class docstring but is the kind of rule that takes a second read to absorb.

## Alternatives considered

- **Per-call-site `disable_internal_retries=True` flags, no registry.** Rejected. That is the pre-taxonomy state and the audit measured the cost: every refactor risks dropping a flag, and the rationale for "why is this RPC retry-unsafe" lives nowhere. The registry centralises both the decision and the justification (`notes=...` per entry).
- **Per-call annotation on the RPC method ID enum (e.g. `RPCMethod.CREATE_NOTEBOOK.policy`).** Rejected. The RPC enum is the source of truth for *method IDs* and is structured to track Google's wire surface. Coupling it to retry semantics would mix transport policy with protocol identity, and the RPC enum is the kind of file that needs to remain mechanically updatable when Google changes a method ID.
- **No taxonomy; rely on `httpx`'s built-in retry policy.** Rejected. `httpx` retries are not aware of *which* RPCs are commit-safe to retry. A blind transport-level retry of `CREATE_NOTE` produces a duplicate note; a blind retry of `DELETE_NOTEBOOK` is safe. The transport cannot know which is which; the taxonomy is necessarily a layer above the transport.
- **More than five policies.** Rejected. The audit derived the active policy axis by enumerating verified NotebookLM call-site shapes; every shape the codebase has met to date fits one of the five. Adding a policy without a real call-site need would balloon the cognitive surface for reviewers without a corresponding gain.
- **Keep a speculative `CLIENT_TOKEN_DEDUPE` policy.** Rejected. The executor previously had token-injection machinery, but no production registry entry used it and no current RPC has a verified client-token slot. A future verified token-dedupe RPC can reintroduce the policy together with the method registration and focused tests.
- **Per-call idempotency annotation as a decorator on the API method.** Rejected. The registry-based approach lets `RpcExecutor` consult the policy without per-call-site bookkeeping, and it survives refactors that move call sites between modules. A decorator approach would have to be re-applied on every move.
