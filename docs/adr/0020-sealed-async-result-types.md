# ADR-0020: Sealed async result types for artifact generation

## Status

Proposed. **Baseline: `main` at #1447 (`f0d2d1be`, v0.8.0)** — `GenerationStatus.status` is already typed `GenerationState(str, Enum)`, with the private `_status_from_code` code→state normaliser and a *test-enforced* poll/wait partition (`tests/unit/test_generation_state.py::test_poll_status_never_returns_removed`). This ADR is the "own ADR" that [ADR-0019](0019-error-and-return-contract.md) Tier 3 — amended by #1446 to "sealed union **rejected for 0.8.0**; if ever revisited, via parallel APIs" — pointed to. It records the sealed design and recommends **continued deferral**. (Code references below use stable symbol/qualified-path names rather than line numbers, which drift.)

## Context

`GenerationStatus` (`_types/artifacts.py`) is one non-frozen `@dataclass` (`task_id, status: GenerationState, url: str|None, error: str|None, error_code: str|None, metadata`) returned by **~13 methods** in two roles:

- **Snapshot** (`generate_*` ×10, `revise_slide`, `retry_failed`, `poll_status`): point-in-time — `pending|in_progress|completed|failed|not_found|unknown`. Kickoff methods take no `wait` flag, so their return is role-stable.
- **Terminal** (`wait_for_completion`): `completed|failed|removed`. Timeout *raises* `ArtifactTimeoutError`.

The partition (`not_found` poll-only, `removed` wait-only) holds by **producer convention** and is **test-enforced** (`test_poll_status_never_returns_removed`), but not **type-enforced**. Residual problems after #1447: (1) illegal field combinations are still representable (all of `url/error/error_code` optional); (2) the partition isn't in the type; (3) `GenerationStatus.is_rate_limited` derives from `error_code == "USER_DISPLAYABLE_ERROR"` **or** a fragile error-message substring fallback. #1342 already removed the load-bearing overload (couldn't-start raises), so this is type hygiene, not a correctness gap.

## Decision

### A. Design of record (the shape if/when built)

**A1 — Two role types.** `PollResult` = `Pending|InProgress|Completed|Failed|NotFound|Unknown` (snapshot methods); `WaitOutcome` = `Completed|Failed|Removed` (the waiter). Encodes the partition in the type.

**A2 — Separate `@dataclass(frozen=True)` variants, NOT subtypes of `GenerationStatus`.** Rationale: a *frozen* variant cannot subclass the *non-frozen* `GenerationStatus`, and a mutable subtype cannot make state immutable; field-ordering (required-after-defaulted) is sidesteppable with `kw_only=True` but immutability is not. **Caveat that shrinks the prize:** the headline benefit — required per-variant fields — is *largely unachievable against today's data*: `Completed.url` is legitimately `None` for non-media artifacts (`ArtifactRow.is_media_ready` returns `True` for non-media types regardless of URL) and for kickoff-completed (`_parse_generation_result` in `_artifacts.py`); `Failed.error` is sometimes `None` (`poll_status` construction in `_artifact/polling.py`); `Removed` carries `error` but no `error_code` (the wait loop's removed-construction in `_artifact/polling.py`). So `Completed.url`/`Failed.error` stay `str | None` (or `Completed` splits into media-completed `url: str` vs document-completed `url: str|None`). The variants thus buy *role separation + exhaustive `match` + structured failure*, **not** illegal-states-unrepresentable. **Before committing to A1–A6, re-evaluate the subtype-refinement alternative below: given this finding, it likely delivers more value per cost.**

**A3 — Timeout stays an exception** (no `TimedOut` variant). Consistent with ADR-0019.

**A4 — `failure_reason: FailureReason` (`RATE_LIMIT | OTHER | UNKNOWN`) on `Failed`/`Removed`.** Derivation must be pinned: `error_code == "USER_DISPLAYABLE_ERROR"` (or the current message heuristic, *retained inside the classifier* because `error_code` is often absent) → `RATE_LIMIT`; a present-but-non-rate-limit failure → `OTHER`; an unclassifiable failure (no `error_code`, no message match) → `UNKNOWN`. A4 does **not** get to drop the substring matching for free — it relocates it into a single classifier. `error`/`error_code` remain optional on the variants.

**A5 — Compatibility is duck-typed/source-level only.** A shared `Protocol`/mixin exposing `.status` + `.is_*` lets *predicate* consumers (the `hasattr(status, "is_complete")` duck-type in `cli/services/artifact_generation.py`; `_artifact/polling.py`'s `is_complete or is_failed`) and `match`/`.status` callers migrate incrementally. It does **NOT** preserve nominal checks: the `isinstance(result, GenerationStatus)` gates in `cli/services/artifact_generation.py`, the direct JSON field-mirroring in `cli/artifact_cmd.py`, `wait_for_completion`'s `on_status_change` callbacks, and `ArtifactTimeoutError.status_transitions` (`exceptions.py`) all reference the concrete type and are **explicit migration work**. The frozen variants also **drop the str-Enum's raw-string-construction tolerance** (`GenerationStatus(status="completed")` is valid today; a frozen union is not) — an intended behavior change to call out.

**A6 — Additive-first migration, with an honest gap.**
- **Phase 1 (additive):** add `poll_result()` / `wait_result()` returning the variants, implemented via a `GenerationStatus → variant` adapter that **`match`es on the constructed `.status`** (the `GenerationState` member). `_status_from_code` is *not* the seam — it is the upstream int→state normaliser and never emits `NOT_FOUND`/`REMOVED` (those are constructed directly in `_artifact/polling.py`). The ~13 flat methods keep returning `GenerationStatus`.
- **Gap (no cheap additive pair for kickoff):** the 12 snapshot *kickoff* methods (`generate_*`, `revise_slide`, `retry_failed`) cannot each get a `*_result()` twin without 12 new methods. Options: (a) a public `GenerationStatus.as_poll_result()` converter callers opt into; (b) accept that kickoff return-flipping is a **Phase-3 breaking change only**. The ADR picks (a) as the cheaper bridge.
- **Phase 2 (runway):** deprecate the flat-returning methods (ADR-0018 runway, real contract); migrate the CLI/services/docs and the **105 `GenerationStatus(...)` construction sites across 15 test files**.
- **Phase 3 (one breaking flip, at a major):** remove/re-annotate the flat path + flip kickoff returns. Re-annotating in place is `changed-return`-flagged by `scripts/audit_public_api_compat.py`, so it only happens here.

### B. Timing decision — defer (strengthened)

**Adopt A1–A6 as design of record, but do NOT build it now.** Both the original rationale and the review findings point the same way, more strongly than v1 claimed: the load-bearing overload is already gone (#1342); the str-Enum + test-enforced partition captured the realizable cheap value; and **the headline prize (required per-variant fields) is mostly unachievable against real data (A2)** — so the breaking full-split now buys only role-separation + exhaustive `match` + structured `failure_reason` over the *non-breaking* subtype refinement. That margin does not justify a multi-phase program across 13 methods, the CLI, callbacks/exceptions, and 105 test sites right after v0.8.0. **If a trigger fires, start by re-evaluating the subtype refinement (Alternatives) — A1–A6 is the spec only if that lighter path is rejected.**

**Revisit triggers (build when any holds):** (a) a concrete recurring bug from the flat shape; (b) a planned major / version-runway window already open; (c) a feature needing per-variant fields; (d) a research-side convergence — `ResearchStatus` is already a `str, Enum` with `NOT_FOUND`, so a *shared* sealed-result pattern could be built once and amortised across both lifecycles (the project values building a pattern with its gate once over per-namespace re-deciding).

## Scope

In: the type shape, role split, timeout/`failure_reason`/compat decisions, migration sequence. Out: the exception model, the `GenerationState` enum (#1447, done), `Source`/`Research` status types, and — under (B) — implementation. (Like ADR-0019, this ADR carries an explicit `Scope` section.)

## Consequences

- **Deferred (recommended):** zero new code; the design is recorded so it is neither re-litigated nor accidentally owed; supersedes ADR-0019's "Tier 3 deferred." The #1447 groundwork keeps a future Phase 1 cheap.
- **If built:** exhaustive `match`, role-distinct poll/wait types, `failure_reason` consolidated into one classifier. **Not** delivered: illegal-states-unrepresentable (fields stay optional, A2). Costs: a dual surface during the runway; migration of the CLI/services/callbacks/exceptions/105 test sites; lost raw-string construction; one breaking flip at a major.

## Alternatives considered

- **Status quo — typed-flat `GenerationStatus` (recommended resting state).** Accepted as the deferral baseline.
- **Subtype refinement (`Completed(GenerationStatus)` …).** Non-breaking, covers all 13 methods (annotations stay `-> GenerationStatus`), gives `isinstance`/`match` + variant methods. Given A2 shows required fields are unachievable anyway, **this is the stronger candidate *if* the trigger ever fires** — it delivers the realizable benefits (role discrimination, `match`) without the runway or the breaking flip. Its only loss vs separate unions (true immutability + required fields) is the part that doesn't hold against the data. The Decision (A1–A6) specs the full split for completeness, but implementation should re-confirm this path is not the better choice first.
- **Re-annotate methods in place.** Rejected — `changed-return` break, no runway.
- **`TimedOut`/`RateLimited` as variants.** Rejected — timeout is exceptional (A3), rate-limit is failure-detail (A4).
