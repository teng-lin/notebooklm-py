# ADR-0011: Schema validation policy (strict-decode default)

> **Current state (2026-06-11).** The strict-default migration described below
> completed. The legacy `NOTEBOOKLM_STRICT_DECODE=0` warn-and-return-`None`
> soft-mode opt-out was retired in v0.7.0; `rpc/_safe_index.py` is now
> strict-only and raises `UnknownRPCMethodError` on schema drift regardless of
> the env var.

## Status

Accepted (Tier 13 PR 13.9a).

## Context

NotebookLM's batchexecute responses are undocumented and obfuscated. Google
reshapes them without notice. Every decoder call site in the client walks
nested positional lists by integer indices that are pinned only by what we
captured in cassettes and observed in production. When Google rotates a
shape (a single index shifts by one, a leaf becomes a wrapper, an inner
list becomes a dict), the affected call site either: (a) crashes with a
raw `IndexError` / `TypeError` from inside a feature module, or (b)
silently degrades to whatever the surrounding code happened to do with
`None`.

The Tier-12 remediation introduced
`notebooklm.rpc.safe_index` (`src/notebooklm/rpc/_safe_index.py`) as
the single shared schema-drift point: callers descend through it by
integer indices, and on descent failure the helper raises
`notebooklm.exceptions.UnknownRPCMethodError`. PR-12 era introduced the
helper with a temporary `NOTEBOOKLM_STRICT_DECODE=0` soft mode so the
migration of ~30 call sites from hand-rolled `try/except IndexError`
blocks to `safe_index` could land without immediately breaking
downstream code that relied on the silently-degrades-to-`None` contract.
That soft mode is now retired.

Two facts shape this ADR:

1. **The migration is far enough along.** The shared `safe_index` helper
   is the policy point for the ~30 batchexecute descent sites that
   migrated off hand-rolled `try/except IndexError` blocks; every
   migrated site threads a `method_id` / `source` label through. A
   small number of historical positional decoders remained in feature modules
   (now under `_artifact/downloads.py`, `_artifact/polling.py`, and `_chat/`)
   where
   the parsing logic predates the helper; each guards its own descent
   with feature-local error recovery, so the strict-default flip does
   not regress those sites — they will be migrated in Tier 13.x
   follow-ups. The default switch is no longer gated on the remaining
   migration work.
2. **Soft mode masks real drift.** In soft mode a Google-side shape
   change produces a `None` return from a feature method (an empty
   summary, a missing artifact id, an empty `sources` list), which is
   indistinguishable from a legitimately-empty payload. Operators
   discover the drift only via downstream consequence (a cron job that
   reports "no notebooks today") rather than at the decoder boundary
   where the drift actually happened.

The two integration test files
`tests/integration/test_artifacts_drift.py` and
`tests/integration/test_get_summary_drift.py` already pin **both**
soft-mode and strict-mode contracts for the representative call sites,
which gives us confidence that the strict path is itself
well-characterised — flipping the default is a default change, not a
behaviour-discovery exercise.

Three details that shaped this ADR:

1. **The env-var name was preserved for the runway, then retired.**
   During the transition, `NOTEBOOKLM_STRICT_DECODE=0` restored soft
   mode for one release window. Current code ignores the variable for
   drift policy: `safe_index` raises regardless of its value.
2. **The opt-out was explicit and bounded.** Downstream code that was
   ingesting `None` as a legitimate "decoder couldn't descend" signal
   had one cycle to adopt `except UnknownRPCMethodError` (an `RPCError`
   subclass). The soft-mode path was then removed in v0.7.0.
3. **The exception remains under `RPCError`.**
   `UnknownRPCMethodError` is a subclass of `DecodingError` which is a
   subclass of `RPCError`. Any existing `except RPCError:` handler
   already covers the strict-mode raise — the new default does not
   force downstream callers to add a new except-clause unless they are
   intentionally treating drift as a non-error sentinel.

## Decision

`safe_index` raises `UnknownRPCMethodError` on every descent failure.
`NOTEBOOKLM_STRICT_DECODE` is ignored for compatibility with old
environments that still set it; it no longer enables soft mode.

### Behavioural contract

The single decision point is `rpc/_safe_index.py::safe_index`. It does
not consult `_env`; it either returns the successfully descended value
or raises `UnknownRPCMethodError` with `method_id`, `source`, `path`,
and a truncated `data_at_failure` preview.

### Historical opt-out lifecycle

- **PR 13.9a:** unset meant strict; explicit `=0` temporarily restored
  soft mode.
- **v0.5.0:** explicit soft-mode fallback logged the drift warning and
  emitted `DeprecationWarning` before returning `None`.
- **v0.7.0:** soft-mode path was removed. `NOTEBOOKLM_STRICT_DECODE`
  became a no-op for schema-drift policy and no longer affects decode
  behavior.

### Test surface

`tests/unit/test_strict_decode_default.py` is the canonical pin for the
retired opt-out. It asserts that unset env and every old truthy/falsy
`NOTEBOOKLM_STRICT_DECODE` spelling still result in `safe_index`
raising `UnknownRPCMethodError` on drift. `tests/unit/test_safe_index.py`
pins the helper's structured diagnostics and import surface.

## Consequences

**Wanted:**

- Google-side shape changes surface at the decoder boundary as typed
  exceptions with `method_id`, `path`, `source`, and `data_at_failure`
  attributes — operators learn about drift before downstream
  consequences accumulate.
- The "the call site returned `None` but I don't know whether the
  payload was empty or the decoder failed" ambiguity is eliminated:
  empty payload still produces an empty value at the feature boundary;
  drift now produces an exception with structured context.
- Integration tests that exercise real-shape cassettes implicitly test
  the strict path (the production default), increasing the value of
  every cassette playback as a drift canary.
- Adding a new feature call site no longer requires the author to
  reason about "should this default to soft or strict?" — the answer
  is always strict, and the historical env-var opt-out is documented
  only to explain why old environments that still set it see no effect.

**Unwanted:**

- Downstream code that relied on `None` as a legitimate "shape didn't
  match" sentinel now sees a raised exception. The migration window has
  closed; callers should catch `UnknownRPCMethodError` / `DecodingError`
  / `RPCError` depending on how broadly they want to handle drift.
- Tests that used to exercise soft-mode fallback now assert strict-only
  behavior or guard legitimate absent optional fields before calling
  `safe_index`.

## Alternatives considered

**Leave the default at `0` indefinitely; document strict mode as a
recommended opt-in.** Rejected. The migration is complete; the
asymmetry between "the decoder knows the payload doesn't match" and
"the caller silently returns an empty value" is the exact failure mode
this helper was introduced to eliminate. Leaving the soft default
permanent defeats the purpose of the migration that landed in Tier-12
and preserves the very ambiguity ADR-0006 (cassette scrubbing) and
ADR-0007 (test monkeypatch policy) work to remove from the test suite.

**Flip the default AND simultaneously delete the soft-mode branch.**
Rejected for the original PR. The opt-out gave one release of migration
runway for downstream consumers who were relying on the pre-flip
contract. The branch was deleted later, after the warning window.

**Move the toggle to a runtime constructor argument
(`NotebookLMClient(strict_decode=True)`) instead of an env var.**
Rejected. Decoder strictness is a process-wide deployment decision,
not a per-client one. A constructor argument would force every test
fixture and every CLI invocation to thread the toggle through, while
the env var is set once at the process boundary and reads atomically
on each `safe_index` call. The CLI does not need to expose a
`--strict-decode` flag either — operators set the env in their shell
or systemd unit and the entire process picks up the policy.

**Make `safe_index` raise unconditionally and delete the env var.**
Rejected for the soft-rollout window, but this is the current behavior
except that the env var remains a no-op compatibility spelling rather
than a process-start failure.

**Couple this flip to the `__all__` audit (PR 13.9b).** Rejected. The
`__all__` audit and migration doc (originally part of t13-9) must
wait for t13-8's session/kernel landing to stabilise the public
surface; the strict-decode flip has zero overlap with t13-8's code
moves and can ship in parallel with waves 4-8 of Tier-13. Splitting
t13-9 into t13-9a (this ADR + flip) and t13-9b (`__all__` audit)
unblocks the parallel window and shortens the tier's critical path.
