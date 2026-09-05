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
seed. The neutral `src/notebooklm/_idempotency.py` retains commit evidence and
backend-neutral replay decisions.

Amended on 2026-09-03 to make ambiguous-write outcomes and Android replay
classes mechanical across both backends. The neutral idempotency module now
also owns `call_unconfirmed_on_transport_loss()` and
`unresolved_commit_error()`. The former marks an existing transport exception
without changing its type or message; Android selects its cause-scrubbing
mode, while web keeps normal chaining. The latter constructs or preserves the
domain-specific `RPCError` used by operations that already surfaced an
`UNRESOLVED` reconciliation message. Preservation is selected explicitly with
`preserve_exception=True`; transport text is never treated as a discriminator.
The Drive-reference parser moved to the neutral source plane with the web
parser's exact `google.com`, `googleusercontent.com`, and `googleapis.com` host
families and its raw-host `%`, backslash, and slash rejection intact.

Amended on 2026-09-05 to replace optimistic create probing with explicit
commit evidence. The public `notebooklm.outcomes.CommitState` distinguishes
`NOT_SENT`, `REJECTED`, `UNKNOWN`, and `CONFIRMED`. The private `ReplayGrant`
and `replay_allowed()` decision require positive `REJECTED` or `NOT_SENT`
evidence for refusal retries; absent evidence is conservative `UNKNOWN`.
`PROBE_THEN_CREATE` and its two-attempt helper were removed.

## Context

The NotebookLM RPC surface is `batchexecute` over HTTPS, and any mutating call (create, delete, refresh, share, generate, …) is susceptible to a *commit-lost* failure: the server commits the write, then the response is lost in transit. A naive retry produces a duplicate write — a duplicate notebook, a duplicate source, an extra LLM inference, a re-sent invite email — depending on the RPC.

The transport retry layer runs an inner retry loop for transient 5xx / 429 / network-error failures. That loop is *correct* for read-only RPCs and dangerous for mutating ones. Before the taxonomy existed, the only mitigation was a per-call-site decision (`disable_internal_retries=True`) that did not document *why* an RPC was retry-unsafe, so the decision was easy to lose during refactors.

Four retry-safety profiles cover every verified NotebookLM RPC shape:

| Policy | Meaning | Effect on the inner retry loop |
|---|---|---|
| `UNCLASSIFIED` | Placeholder for hand-built test/future registries; not used by production entries | Retries remain enabled for compatibility |
| `IDEMPOTENT_SET_OP` | Read or stable set semantics | Retries are safe |
| `AT_LEAST_ONCE_ACCEPTED` | The caller explicitly accepts duplicate-side-effect cost | Retries enabled with a rate-limited warning |
| `NON_IDEMPOTENT_NO_RETRY` | No verified dedupe key; inspection may report candidates but never authorizes replay | Force-disable inner retries |

The taxonomy and production registry are consulted by `RpcExecutor` to compute
the effective `disable_internal_retries` value. Variants carry their own
classification when wire semantics differ by call site.

## Decision

Every active `RPCMethod` is registered with one of the four policies. Reads
and verified set operations are replay-safe. At-least-once behavior remains an
explicit opt-in. Every other mutation is single-send.

A read after ambiguous loss is reporting, not recovery. A zero-result read,
same-title row, normalized-URL row, or cumulative collection response does not
prove which caller created a resource. Candidate IDs may be attached to the
preserved exception, but the client never returns them as the result and never
re-sends the mutation.

`CREATE_NOTEBOOK`, URL and Drive `ADD_SOURCE`, and `ADD_SOURCE_FILE` are
therefore `NON_IDEMPOTENT_NO_RETRY`. Research import likewise sends once:
decoded success returns only decoded rows; post-loss rows are candidates and
the original exception remains the outcome.

Replay permission is a separate shared decision in
`notebooklm._idempotency`. `ReplayGrant.REFUSAL_RETRY_AUTHORIZED` requires
positive `CommitState.REJECTED` or `CommitState.NOT_SENT` evidence.
`ReplayGrant.NO_REPLAY` always refuses, and `REPLAY_SAFE` is reserved for
semantically safe work. All grants still honor explicit disablement and the
remaining budget.

The completed production classifications are recorded in `_web/policy.py` (with the per-RPC rationale captured at the registration site). Future classifications continue to land in the same module without changes to the executor; the registry is intentionally extensible.

Three operations previously labelled `PROBE_THEN_CREATE` did not own a probe:
artifact creation, mind-map generation, and notebook sharing. They are
`NON_IDEMPOTENT_NO_RETRY`; their call sites mark transport loss unconfirmed
instead of advertising a reconciliation guarantee they cannot provide. The
sharing outcome boundary includes its mandatory status readback, because that
read can fail after the invitation or access mutation has committed. The
registry no longer carries the unused `probe_key_fn` field.

The same behavioural contract covers every public route over the other
non-replayed mutation families shared by the backends: failed-artifact retry,
slide revision, all three artifact export entry points, automatic/manual label
creation, and collection creation (including the collection's required
post-create list readback).

Android's exact gRPC method names have a checked replay-safety manifest derived
from the corresponding web registry entries: retriable reads are replay-safe;
mutations, paid inference, and operations whose web policy disables internal
retries are not. Android session dispatch reads this manifest rather than
trusting an independently maintained call-site classification. A complete AST
guardrail resolves local and imported method constants, fails closed on every
unresolved unary/stream expression, compares every replay literal with the
manifest, and includes negative self-tests that flip local and imported-constant
sites. Runtime lookup also fails closed: a method absent from the production
manifest is always single-attempt even when a generic caller declares it
replay-safe. A second, behavioural
manifest resolves mutating public methods through both backend MROs, injects
transport loss through their normal test fakes, and requires both outcomes to
carry `unconfirmed=True`; it also pins Android's cause scrubbing and proves a
plain unwrapped raise is rejected.

The four-policy axis is *closed*. Adding a fifth policy requires updating this ADR and the executor in lock-step.

## Consequences

**Wanted:**

- Retry safety is now a *property of the RPC*, not a property of the call site. New call sites inherit the safe behavior without re-deriving it.
- Ambiguous mutation outcomes share one programmatic marker across the web and Android backends, while preserving each backend's established exception type, message, and chaining policy.
- The Android replay manifest and its derivation guardrail prevent a call-site literal from drifting away from the web registry's classification.
- The executor's retry logic is small and local; the policy decisions live in the registry where they can be reviewed in isolation.
- The taxonomy is small enough (four policies) that a reviewer can hold it in mind during a code review. A fifth policy would push past that threshold and is rejected by design.

**Unwanted:**

- The registry is populated at module import time and is effectively immutable. A test that wants to override a classification needs to construct a fresh `IdempotencyRegistry` instance (the contract documents this, but it is friction).
- `AT_LEAST_ONCE_ACCEPTED`'s rate-limited WARN log is per-process-state (a module-level dict). Tests that observe the log behavior have to manage state across test cases; the WARN is throttled to one emission per 30 seconds per `(method, variant)` to avoid drowning operators in spam, which means a noisy test environment can suppress emissions that would have fired in production.
- The taxonomy is *opinionated about caller behavior*. `AT_LEAST_ONCE_ACCEPTED` says "the caller has accepted at-least-once semantics"; if a future contributor classifies an RPC that way without the caller actually having opted in, the registry will silently green-light duplicate side effects. Reviews of new `AT_LEAST_ONCE_ACCEPTED` classifications need to be careful.
- The variant-table fallback (`get_entry(method, variant=v)` on a method with no variant table silently falls back to `(method, None)`; the same call on a method *with* a variant table but for an unknown variant raises) is subtle. The contract is documented in the registry class docstring but is the kind of rule that takes a second read to absorb.

## Alternatives considered

- **Per-call-site `disable_internal_retries=True` flags, no registry.** Rejected. That is the pre-taxonomy state and the audit measured the cost: every refactor risks dropping a flag, and the rationale for "why is this RPC retry-unsafe" lives nowhere. The registry centralises both the decision and the justification (`notes=...` per entry).
- **Per-call annotation on the RPC method ID enum (e.g. `RPCMethod.CREATE_NOTEBOOK.policy`).** Rejected. The RPC enum is the source of truth for *method IDs* and is structured to track Google's wire surface. Coupling it to retry semantics would mix transport policy with protocol identity, and the RPC enum is the kind of file that needs to remain mechanically updatable when Google changes a method ID.
- **No taxonomy; rely on `httpx`'s built-in retry policy.** Rejected. `httpx` retries are not aware of *which* RPCs are commit-safe to retry. A blind transport-level retry of `CREATE_NOTE` produces a duplicate note; a blind retry of `DELETE_NOTEBOOK` is safe. The transport cannot know which is which; the taxonomy is necessarily a layer above the transport.
- **More than four policies.** Rejected. The audit derived the active policy axis by enumerating verified NotebookLM call-site shapes; every shape the codebase has met to date fits one of the four. Adding a policy without a real call-site need would balloon the cognitive surface for reviewers without a corresponding gain.
- **Keep a speculative `CLIENT_TOKEN_DEDUPE` policy.** Rejected. The executor previously had token-injection machinery, but no production registry entry used it and no current RPC has a verified client-token slot. A future verified token-dedupe RPC can reintroduce the policy together with the method registration and focused tests.
- **Per-call idempotency annotation as a decorator on the API method.** Rejected. The registry-based approach lets `RpcExecutor` consult the policy without per-call-site bookkeeping, and it survives refactors that move call sites between modules. A decorator approach would have to be re-applied on every move.
