# ADR-0020: Sealed async result types for artifact generation

## Status

Proposed. **Baseline: `main` at #1447 (`f0d2d1be`, v0.8.0)** — `GenerationStatus.status` is already typed `GenerationState(str, Enum)`, with the private `_status_from_code` code→state normaliser and a *test-enforced* poll/wait partition (`tests/unit/test_generation_state.py::test_poll_status_never_returns_removed`). This ADR is the "own ADR" that [ADR-0019](0019-error-and-return-contract.md) Tier 3 — amended by #1446 to "sealed union **rejected for 0.8.0**; if ever revisited, via parallel APIs" — pointed to. It records the sealed design and recommends **continued deferral**.

## Context

`GenerationStatus` is one non-frozen `@dataclass` (`_types/artifacts.py:361`; `task_id, status: GenerationState, url: str|None, error: str|None, error_code: str|None, metadata`) returned by **~13 methods** in two roles:

- **Snapshot** (`generate_*` ×10, `revise_slide`, `retry_failed`, `poll_status`): point-in-time — `pending|in_progress|completed|failed|not_found|unknown`. Kickoff methods take no `wait` flag, so their return is role-stable.
- **Terminal** (`wait_for_completion`): `completed|failed|removed`. Timeout *raises* `ArtifactTimeoutError`.

The partition (`not_found` poll-only, `removed` wait-only) holds by **producer convention** and is **test-enforced**, but not **type-enforced**. Residual problems after #1447: (1) illegal field combinations are still representable (all of `url/error/error_code` optional); (2) the partition isn't in the type; (3) `is_rate_limited` derives from `error_code == "USER_DISPLAYABLE_ERROR"` **or** a fragile message-substring fallback (`_types/artifacts.py:441-456`). #1342 already removed the load-bearing overload (couldn't-start raises), so this is type hygiene, not a correctness gap.

## Decision

### A. Design of record (the shape if/when built)

**A1 — Two role types.** `PollResult` = `Pending|InProgress|Completed|Failed|NotFound|Unknown` (snapshot methods); `WaitOutcome` = `Completed|Failed|Removed` (the waiter). Encodes the partition in the type.

**A2 — Separate `@dataclass(frozen=True)` variants, NOT subtypes of `GenerationStatus`.** Rationale (corrected): a *frozen* variant cannot subclass the *non-frozen* `GenerationStatus`, and a mutable subtype cannot make state immutable; field-ordering (required-after-defaulted) is sidesteppable with `kw_only=True` but immutability is not. **Caveat that shrinks the prize:** the headline benefit — required per-variant fields — is *largely unachievable against today's data*: `Completed.url` is legitimately `None` for non-media artifacts (`is_media_ready` returns `True` for non-media types regardless of URL, `_row_adapters/artifacts.py:450-455`) and for kickoff-completed (`_artifacts.py:1327-1330`); `Failed.error` is sometimes `None` (`polling.py:140-150`); `Removed` carries `error` but no `error_code` (`polling.py:405-414`). So `Completed.url`/`Failed.error` stay `str | None` (or `Completed` splits into media-completed `url: str` vs document-completed `url: str|None`). The variants thus buy *role separation + exhaustive `match` + structured failure*, **not** illegal-states-unrepresentable.

**A3 — Timeout stays an exception** (no `TimedOut` variant). Consistent with ADR-0019.

**A4 — `failure_reason: FailureReason` (`RATE_LIMIT | OTHER | UNKNOWN`) on `Failed`/`Removed`.** Derivation must be pinned: `error_code == "USER_DISPLAYABLE_ERROR"` → `RATE_LIMIT`, else the *current message heuristic is retained inside the classifier* (it exists because `error_code` is often absent), else `UNKNOWN`. A4 does **not** get to drop the substring matching for free — it relocates it into a single classifier. `error`/`error_code` remain optional on the variants.

**A5 — Compatibility is duck-typed/source-level only.** A shared `Protocol`/mixin exposing `.status` + `.is_*` lets *predicate* consumers (`cli/services/artifact_generation.py:217`, `polling.py:351`) and `match`/`.status` callers migrate incrementally. It does **NOT** preserve nominal checks: `isinstance(result, GenerationStatus)` gates (`artifact_generation.py:145,191`), direct JSON field-mirroring (`artifact_cmd.py:472-480,553-559,675-681`), `on_status_change` callbacks and `ArtifactTimeoutError.status_transitions` (`_artifacts.py:1007`, `polling.py:344-349`, `exceptions.py:1160-1168`) all reference the concrete type and are **explicit migration work**. The frozen variants also **drop the str-Enum's raw-string-construction tolerance** (`GenerationStatus(status="completed")` is valid today; a frozen union is not) — an intended behavior change to call out.

**A6 — Additive-first migration, with an honest gap.**
- **Phase 1 (additive):** add `poll_result()` / `wait_result()` returning the variants, implemented via a `GenerationStatus → variant` adapter that **`match`es on the constructed `.status`** (the `GenerationState` member). `_status_from_code` is *not* the seam — it is the upstream int→state normaliser and never emits `NOT_FOUND`/`REMOVED` (those are constructed directly at `polling.py:156,408`). The ~13 flat methods keep returning `GenerationStatus`.
- **Gap (no cheap additive pair for kickoff):** the 12 snapshot *kickoff* methods (`generate_*`, `revise_slide`, `retry_failed`) cannot each get a `*_result()` twin without 12 new methods. Options: (a) a public `GenerationStatus.as_poll_result()` converter callers opt into; (b) accept that kickoff return-flipping is a **Phase-3 breaking change only** (no additive runway). The ADR picks (a) as the cheaper bridge.
- **Phase 2 (runway):** deprecate the flat-returning methods (ADR-0018 runway, real contract); migrate the CLI/services/docs and the **105 `GenerationStatus(...)` construction sites across 15 test files**.
- **Phase 3 (one breaking flip, at a major):** remove/re-annotate the flat path + flip kickoff returns. Re-annotating in place is `changed-return`-flagged (`audit_public_api_compat.py:580-582`), so it only happens here.

### B. Timing decision — defer (strengthened)

**Adopt A1–A6 as design of record, but do NOT build it now.** Both the original rationale and the review findings point the same way, more strongly than v1 claimed: the load-bearing overload is already gone (#1342); the str-Enum + test-enforced partition captured the realizable cheap value; and **the headline prize (required per-variant fields) is mostly unachievable against real data (A2)** — so the breaking full-split now buys only role-separation + exhaustive `match` + structured `failure_reason` over the *non-breaking* subtype refinement. That margin does not justify a multi-phase program across 13 methods, the CLI, callbacks/exceptions, and 105 test sites right after v0.8.0.

**Revisit triggers (build when any holds):** (a) a concrete recurring bug from the flat shape; (b) a planned major / version-runway window already open; (c) a feature needing per-variant fields; (d) a research-side convergence — `ResearchStatus` is already a `str, Enum` with `NOT_FOUND`, so a *shared* sealed-result pattern could be built once and amortised across both lifecycles (the project values building a pattern with its gate once over per-namespace re-deciding).

## Scope

In: the type shape, role split, timeout/`failure_reason`/compat decisions, migration sequence. Out: the exception model, the `GenerationState` enum (#1447, done), `Source`/`Research` status types, and — under (B) — implementation.

## Consequences

- **Deferred (recommended):** zero new code; the design is recorded so it is neither re-litigated nor accidentally owed; supersedes ADR-0019's "Tier 3 deferred." The #1447 groundwork keeps a future Phase 1 cheap.
- **If built:** exhaustive `match`, role-distinct poll/wait types, `failure_reason` consolidated into one classifier. **Not** delivered: illegal-states-unrepresentable (fields stay optional, A2). Costs: a dual surface during the runway; migration of the CLI/services/callbacks/exceptions/105 test sites; lost raw-string construction; one breaking flip at a major.

## Alternatives considered

- **Status quo — typed-flat `GenerationStatus` (recommended resting state).** Accepted as the deferral baseline.
- **Subtype refinement (`Completed(GenerationStatus)` …).** Non-breaking, covers all 13 methods (annotations stay `-> GenerationStatus`), gives `isinstance`/`match` + variant methods. Given A2 shows required fields are unachievable anyway, **this is now the stronger candidate *if* the trigger ever fires** — it delivers the realizable benefits (role discrimination, `match`) without the runway or the breaking flip. Its only loss vs separate unions (true immutability + required fields) is the part that doesn't hold against the data.
- **Re-annotate methods in place.** Rejected — `changed-return` break, no runway.
- **`TimedOut`/`RateLimited` as variants.** Rejected — timeout is exceptional (A3), rate-limit is failure-detail (A4).
