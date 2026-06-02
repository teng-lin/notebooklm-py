# ADR-0019: Error-and-return contract for the public API

## Status

Accepted. The library-wide error-and-return contract is ratified; this ADR
records the decision ahead of the work. The v0.8.0 implementation is tracked
separately under umbrella #1346 and is **not yet scheduled** — no code has
landed. Ratifies the already-committed v0.8.0 work (#1247, #1254, #1251) as
instances of one contract.

## Context

The public API has accreted incompatible conventions for the *same* outcome.
Two grounded surveys found a single concept — "not found" — encoded **eight**
ways (raise `*NotFoundError`; `None`+warn; silent `None`; empty sentinel object;
`""`; a `"not_found"` status string; `ValueError`; silent no-op `None`),
synchronous server refusal **three** ways, and a null/shape-drift result
**five** ways. The divergence is *growing*: `mind_maps` (newest namespace,
#1256) adopted the about-to-be-deprecated None-on-miss convention — without even
the deprecation warning — and reached for `ValueError`.

Related decisions: [ADR-005](0005-idempotency-taxonomy.md) (mutating-RPC retry
policy), [ADR-011](0011-schema-validation-policy.md) (strict-decode default;
shape-drift raises `UnknownRPCMethodError`/`DecodingError` — this ADR extends
that boundary to the hand-rolled list helpers ADR-011 left for follow-up),
[ADR-012](0012-implementation-surface-convention.md) (private surface),
[ADR-017](0017-public-facade-private-implementation.md) (the public facade
surface owns the compatibility contract; logic stays in private modules),
[ADR-018](0018-deprecation-strategy.md) (how breaking changes ship). None of
them says *what a method returns versus raises for each failure mode* — that gap
is what this ADR closes, making the committed Class-1 work (#1247 flip `get()`
to raise; #1254 remove `interval`; #1251 drop `MappingCompat`) instances of one
contract and converging the rest in the same release.

## Decision

> **A return value encodes only success and genuine asynchronous-lifecycle
> state. *Resource* absence is an exception (`*NotFoundError`); the *poll-observed*
> absence of an in-flight task is a typed lifecycle status, not an error. Server
> refusals, shape-drift, and transport faults are always exceptions. `None` is
> reserved for the cases enumerated below — it, `""`, `ValueError`, and an
> untyped `"not_found"` string are never used to signal that an error happened.**

### Return-value vocabulary (allowed meanings)

| Shape | May mean |
| ----- | -------- |
| object / dataclass | success |
| collection | zero-or-more (empty → `[]`) |
| status handle (`GenerationStatus`/`ResearchTask`/`ResearchStart`) | async lifecycle only; terminal `failed`/`removed` and the *poll-observed* `not_found` are typed states. `status="failed"` ⇒ *started-then-failed*, never *couldn't-start* |
| `None` | (a) idempotent `delete`; (b) explicit `get_or_none()`; (c) no-payload **command** success (`update`, `configure`, `remove_from_recent`, `rename(return_object=False)`); (d) a transient *not-ready* read (`get_tree` of an existing-but-unpopulated map); (e) a domain-optional field |

Anything else that today carries an error meaning is banned.

### Contract by operation class

| Class | Methods | Contract |
| ----- | ------- | -------- |
| Lookup one | `get` | found → object; missing → **raise `*NotFoundError`**. Public `get_or_none()` is the sole sanctioned `None`-on-miss path. |
| List many | `list`, `list_*` | always a collection; empty → `[]`. |
| Derived read | `get_summary`, `get_description`, `get_guide`, `get_tree`, `check_freshness` | **do not police parent existence** — missing parent → empty / not-ready value (`""`, empty dataclass, `None` tree); shape-drift → **raise** (`DecodingError`/`UnknownRPCMethodError`). Resource existence is `get()`'s job, not a derived read's. |
| Idempotent mutation | `delete` | success *or* already-absent → `None`; raise only on real failure. |
| Mutate existing | `rename`, `update`, `configure` | target missing → **raise `*NotFoundError`**; no-payload success → `None`. |
| Async kickoff | `generate_*`, `create`, `revise_slide`, `retry_failed`, `research.start`, `mind_maps.generate` | accepted → return status handle; **synchronous refusal → raise**; null/missing-id/shape-drift → raise. |
| Lifecycle status / await | `poll_status`, `research.poll`, `wait_for_completion` | reflect lifecycle; terminal `failed`/`removed` stay returned status; *poll-observed* `not_found` is a typed sentinel (not a raise); does **not** raise for a terminal `failed`, but **does** raise on timeout and on cross-cutting faults. |
| Readiness wait | `wait_until_ready`, `wait_for_sources` | return the ready **resource**; raise `*TimeoutError` on timeout and the domain error on terminal processing failure. (*Distinct from the lifecycle-status handles above.*) |
| Cross-cutting | any | transport→`NetworkError`/`RPCTimeoutError`; auth→`AuthError`; rate-limit→`RateLimitError`; oversize→`RPCResponseTooLargeError`; decode→`DecodingError`. Always raise. |

The load-bearing line: **resource-absent / couldn't-start / timed-out → raise; started-then-reached-a-terminal-state, or a transient poll observation → data.** A synchronous refusal is *couldn't start* (raise); a polled `failed` is *started then failed* (data); a poll that doesn't yet see an accepted task is a transient *not_found* (typed status), categorically different from looking up a resource that does not exist (raise).

Absence detection is single-sourced where shared (e.g. `_detect_kind` for mind maps): the detector raises `*NotFoundError`, and each operation class *interprets* that one signal — a derived read swallows it to empty/`None`, a mutate-existing re-raises, an idempotent `delete` swallows it to `None`. One detector, three contracts, no per-method re-deciding.

### Exception taxonomy

Ratify the existing tree (`NotebookLMError` root; multi-base
`*NotFoundError(NotFoundError, RPCError, <Domain>Error)`; the `RPCError`
transport subtree; `WaitTimeoutError(…, TimeoutError)`). Add, mirroring
`SourceNotFoundError`: `NoteError`+`NoteNotFoundError`, `MindMapError`+`MindMapNotFoundError`
(none exist yet). Standardize the `*TimeoutError` base order umbrella-first
(`ArtifactTimeoutError(ArtifactError, WaitTimeoutError)` is the outlier,
`exceptions.py:1117`). No new "refusal" exception — refusal reuses the existing
`RateLimitError`/`RPCError`.

### Rules

1. **Resource absence raises.** `get()` raises `*NotFoundError`; `get_or_none()`
   is the only sanctioned `None`-on-miss path and must re-raise anything that is
   not a genuine miss. (Poll-observed task absence is *not* resource absence —
   see Rule 4.)
2. **Refusal raises.** A synchronous `USER_DISPLAYABLE_ERROR` propagates as the
   `RateLimitError`/`RPCError` the transport layer raises. The kickoff methods
   **currently swallow** it into `GenerationStatus(status="failed")`
   (`_artifacts.py:1150-1158` `_call_generate`, `:541-549` `revise_slide`) and
   `_parse_generation_result` synthesizes `failed` for a missing artifact id
   (`:1251-1260`); v0.8.0 **removes** both, re-raising the refusal and raising
   `DecodingError`/`ArtifactFeatureUnavailableError` for a missing/degenerate id.
3. **Drift raises.** A malformed/unparseable RPC payload raises
   `DecodingError`/`UnknownRPCMethodError` ([ADR-011](0011-schema-validation-policy.md));
   it is not collapsed to `None`/`""`/`[]`/a sentinel. v0.8.0 tightens the
   **positional shape-drift** collapse in the hand-rolled list helpers
   (`_note_service.py:135`, `_artifact/listing.py:113`). The composite-lister
   `except RPCError`/`HTTPError` that returns *partial* studio artifacts when the
   mind-map sub-fetch is down (`_artifact/listing.py:126-138`) is a **deliberate
   partial-availability** behavior, **not** drift-collapse — it is out of scope
   for Rule 3 and decided separately (see Scope).
4. **Lifecycle is data.** Async status handles carry `failed`/`not_found`/
   `removed` as typed states; `wait_for_completion` returns a terminal `failed`
   and raises only on timeout or a cross-cutting fault. The poll-observed
   `not_found` (artifact not yet listed, or research task absent) is a typed
   sentinel — `GenerationStatus.is_not_found`, and a **new** `ResearchStatus.NOT_FOUND`
   member (distinct from the existing `NO_RESEARCH` "nothing in flight"). The
   *termination* guarantee for a task that never appears lives in
   `wait_for_completion`, not `poll_status`: a sustained run of `not_found`
   (`max_not_found`/`min_not_found_window`) escalates to a terminal `removed`
   status (`_artifact/polling.py:366-384`). `poll_status` is a stateless
   primitive where `not_found` is inherently *lag-or-bogus* ambiguous by design;
   callers needing a terminal answer use `wait_for_completion`.
5. **The facade owns the contract.** Per [ADR-017](0017-public-facade-private-implementation.md)
   the public facade *surface* owns the compatibility contract (logic stays
   private); breaks ship via [ADR-018](0018-deprecation-strategy.md) — #1247/#1254/#1251
   had a v0.7.0 deprecation runway, the refusal/`ValueError`/`update` changes are
   deliberate clean breaks in the already-breaking 0.8.0 — are allowlisted
   (`scripts/api-compat-allowlist.json`), and idempotency is unchanged
   ([ADR-005](0005-idempotency-taxonomy.md): kickoffs stay non-blind-replayable).

`ValueError` remains valid for **input validation**; it is banned only for
resource absence and server failure.

**Retry guidance.** Because `*NotFoundError` multi-inherits `RPCError` (see the
exception taxonomy), transport-retry code must catch the *narrow* transport
exceptions — `NetworkError`/`RPCTimeoutError`/`RateLimitError` — and never the
broad `RPCError`, so a retry loop never silently swallows a `*NotFoundError`.

## Scope

*(This `Scope` section and the `Enforcement` section below intentionally extend the standard six-section ADR template; both carry convergence-specific load — see ADR review thread.)*

In scope: the operation classes above across `notebooks`, `sources`,
`artifacts`, `chat`, `research`, `notes`, `mind_maps`, `sharing`, `settings`.
Explicitly **deferred / follow-existing-contract** (not changed in this ADR;
tracked separately): bulk/derived helpers that today swallow drift to empty
data (`notebooks.get_metadata`/`get_source_ids`/`get_raw`, the research-task
parser fallbacks); `share`; export/download paths; and the chat surface. The
composite-lister partial-availability policy (Rule 3) is decided in its own PR.

## Consequences

**Wanted**

- One predictable rule per operation class; new features stop re-deciding.
- Type-narrowing works: `get()` returns a non-optional object; callers branch on
  exceptions, not on `None`/`""`/sentinel ambiguity.
- A refusal can no longer masquerade as a started-then-failed task.
- Mechanically auditable alongside [ADR-017](0017-public-facade-private-implementation.md).

**Unwanted**

- A large v0.8.0: not-found + refusal + null + reads + mutations land together.
  Mitigated by per-wave verification and the `api-compat`/golden-fixture gates.
- A small extra cost only on `rename`'s default path (it re-fetches to return the
  renamed object; `return_object=False` is the existing opt-out that avoids it).
  Derived reads add **no** existence RPC — they return empty on a missing parent.
- Callers relying on `GenerationStatus(status="failed")` for rate-limit handling
  must catch `RateLimitError`; the public `with_rate_limit_retry` helper is
  rewritten accordingly.
- The convention must be *enforced*, not just documented, or it re-accretes (it
  already did, in `mind_maps`). Enforcement is in scope for 0.8.0 — see below.

## Enforcement (in scope for 0.8.0)

The 8-way divergence happened because consistency was enforced only by review,
not by types — `mind_maps` re-diverged the moment it was added. A documented
contract that nothing checks will re-accrete, so 0.8.0 lands a **tiered
enforcement floor**:

- **Tier 1 — conformance test (mandatory).** A parametrised
  `test_public_api_contract.py` asserts the contract as a **static shape** check
  over the *whole* public surface via its **own** `inspect.signature().return_annotation`
  walk over all namespaces (incl. `mind_maps`, which the `audit_public_api_compat.py`
  collector under-covers; not its comparator, which ignores return types): every
  namespace `get(...)` has a non-Optional return **and** a paired
  `get_or_none(...)` returning Optional;
  `delete(...) -> None`; no public lookup is annotated `X | None`. Deferred
  *behaviours* (see Scope) are carried in an explicit, reason-tagged **exemption
  allowlist** (same idiom as `api-compat-allowlist.json`) so every gap is visible
  and shrinking, never silent. The divergence that occurred
  (`mind_maps.get() -> MindMap | None`) is a signature smell this catches with no
  backend.
- **Tier 2 — single-sourced lookup logic (mandatory, structure-first).** A shared
  `unwrap_or_raise(obj, exc)` helper backs each namespace's own **fully-typed**
  `get`/`get_or_none` (`get()` = `unwrap_or_raise(await self._fetch_one(...),
  <Resource>NotFoundError(...))`). This is **PR #1**: the lookup *logic* is
  single-sourced while signatures stay per-class. A generic `ResourceAPI[T]`
  *base* was considered and **rejected** (momus 2/3) — a `*ids` base erases
  public-signature typing (the namespaces differ in arity) and `delete` is
  irreducibly per-namespace (`mind_maps.delete(..., kind=...)` is non-idempotent
  + kind-dispatched), so `delete` stays per-namespace.
- **Tier 3 — sealed async result types (deferred, own ADR).** Replacing the
  stringly-typed `GenerationStatus.status` with a sealed/discriminated result is
  the deeper fix (it would dissolve the `failed`/`not_found`/`removed` string
  juggling), but it is a larger redesign tracked separately; this ADR keeps the
  typed-string states.

Tier 1 + Tier 2 are required for 0.8.0; together they make this contract
type/CI-enforced rather than review-enforced.

## Alternatives considered

- **Keep `status="failed"` for synchronous refusal** (local consistency).
  Rejected — deepens the soft-fail-as-data pattern #1247 is leaving; makes
  `retry_failed` the outlier.
- **Make `wait_for_completion` raise on terminal failure**, for symmetry with
  timeout. Rejected — a terminal `failed` is real async data; only
  *couldn't-start*, *timeout*, and cross-cutting faults are exceptional.
- **Raise on a poll-observed unknown task.** Rejected — a poll loop cannot treat
  replication-lag as exceptional; a typed sentinel is the right shape.
- **Force `get_summary`/`get_description` to raise on a missing parent.**
  Rejected — no parent-existence signal; would mean an extra RPC per call for a
  rare error; empty stays a legitimate domain value there.
- **Split the convergence across 0.8.0 + 0.9.0.** Considered for safety;
  maintainer chose all-in to fix the divergence once.
