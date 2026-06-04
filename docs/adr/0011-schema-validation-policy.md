# ADR-0011: Schema validation policy (strict-decode default)

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
integer indices, and on descent failure the helper either logs a warning
and returns `None` (soft mode) or raises
`notebooklm.exceptions.UnknownRPCMethodError` (strict mode). The
toggle is `NOTEBOOKLM_STRICT_DECODE`. PR-12 era introduced the helper
with a default of `"0"` (soft) so the migration of ~30 call sites from
hand-rolled `try/except IndexError` blocks to `safe_index` could land
without breaking downstream code that relied on the
silently-degrades-to-None contract.

Two facts shape this ADR:

1. **The migration is far enough along.** The shared `safe_index` helper
   is the policy point for the ~30 batchexecute descent sites that
   migrated off hand-rolled `try/except IndexError` blocks; every
   migrated site threads a `method_id` / `source` label through. A
   small number of legacy positional decoders remain in feature modules
   (`_artifact_downloads`, `_artifact_polling`, `_chat_protocol`) where
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

1. **The env-var name is preserved.** `NOTEBOOKLM_STRICT_DECODE` keeps
   its existing name and the truthy set `{"1", "true", "True"}`. The
   only change is the unset-fallback: pre-flip the absent env-var read
   as `"0"`, post-flip it reads as `"1"`. This keeps every existing CI
   workflow that explicitly sets the var continuing to work without
   modification.
2. **The opt-out is explicit and bounded.** `NOTEBOOKLM_STRICT_DECODE=0`
   restores soft mode for one release window. Downstream code that was
   ingesting `None` as a legitimate "decoder couldn't descend" signal
   has one cycle to adopt `except UnknownRPCMethodError` (an
   `RPCError` subclass), after which the soft-mode path will be retired
   and the env var either deleted or repurposed to a no-op alias.
3. **The exception remains under `RPCError`.**
   `UnknownRPCMethodError` is a subclass of `DecodingError` which is a
   subclass of `RPCError`. Any existing `except RPCError:` handler
   already covers the strict-mode raise — the new default does not
   force downstream callers to add a new except-clause unless they are
   intentionally treating drift as a non-error sentinel.

## Decision

`_env.is_strict_decode_enabled()` defaults to `True` when
`NOTEBOOKLM_STRICT_DECODE` is unset. Soft mode is reachable only via
explicit `NOTEBOOKLM_STRICT_DECODE=0` (or any other non-truthy value
such as `"false"`, `"False"`, `"no"`, `"off"`, or `""`). Anything not
in the truthy set `{"1", "true", "True"}` is treated as non-truthy.

### Behavioural contract

The single decision point is `_env.is_strict_decode_enabled()`. It
returns `True` if `os.environ.get("NOTEBOOKLM_STRICT_DECODE", "1")` is
in `{"1", "true", "True"}`; `False` otherwise. Every drift call site
that depended on the pre-flip soft default (the
`tests/integration/test_*_drift.py` and the unit drift tests under
`tests/unit/test_*_helpers.py`, `test_research.py`,
`test_notebooks_extractors.py`, `test_swallow_observability.py`) now
pins soft mode explicitly via
`monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "0")`. The intent of
those tests — exercising the warn-and-return-`None` legacy fallback —
is preserved; the dependency on the env-var default is now explicit.

### Opt-out lifecycle

- **Now (PR 13.9a):** unset = strict; explicit `=0` = soft.
- **One release later:** soft-mode path produces a `DeprecationWarning`
  when the helper falls through it, naming the call site that
  triggered the warn-and-return-`None` so operators can locate and
  fix the consumer.
  Fulfillment: shipped in v0.5.0; explicit soft-mode fallback via
  `NOTEBOOKLM_STRICT_DECODE=0` now logs the drift warning and emits
  `DeprecationWarning` before returning `None`.
- **Two releases later:** soft-mode path removed; `NOTEBOOKLM_STRICT_DECODE`
  becomes a no-op (or its presence at `"0"` raises `ConfigurationError`
  at process startup so operators discover the opt-out was retired).
  The exact mechanic will be re-evaluated in a follow-up ADR once
  downstream usage telemetry is available.

### Test surface

The new `tests/unit/test_strict_decode_default.py` file is the
canonical pin for the flipped default. It asserts:

- Unset env → `is_strict_decode_enabled() is True`.
- Unset env → `safe_index([], 0, ...)` raises
  `UnknownRPCMethodError`.
- Explicit `=0` → helper returns `False` and `safe_index(...)` returns
  `None` with a WARN log.
- Truthy aliases (`"1"`, `"true"`, `"True"`) all enable strict mode.
- Falsy aliases (`"0"`, `"false"`, `"no"`, `"off"`, `""`, `"False"`)
  all disable strict mode.

Soft-mode contract coverage continues to live in the call-site-local
drift test files (`test_artifacts_drift.py`,
`test_get_summary_drift.py`, etc.) with explicit `setenv("0")` opt-in.

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
  is always strict, and the env-var opt-out is documented for the
  rare downstream user who needs it during migration.

**Unwanted:**

- Downstream code that relied on `None` as a legitimate "shape didn't
  match" sentinel now sees a raised exception. The opt-out
  (`NOTEBOOKLM_STRICT_DECODE=0`) covers the one-release migration
  window, but any consumer that does not adopt either the exception
  handler or the env-var opt-out will see fresh `UnknownRPCMethodError`
  bubbles after upgrading.
- Sixteen existing soft-mode tests across thirteen unit and integration
  test files now carry an explicit ``monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "0")``
  line so they continue to exercise the warn-and-return-``None``
  fallback. The edits are mechanical and surface-only — no test logic
  changed — but they enlarge the diff. The exact file list is in this
  PR's `git diff --stat` output and is bounded by the call sites that
  consume `safe_index`.
- The opt-out window means the soft-mode code path inside `safe_index`
  remains live for one more release. The path is small (a single
  `if/else` branch around the raise) so the carrying cost is
  negligible, but it does mean the helper is not yet a
  raise-or-success function and the warn log it emits stays in the
  WARN floor of the package logger until removal.

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
Rejected for this PR. The opt-out gives one release of migration
runway for downstream consumers who were relying on the pre-flip
contract; removing the branch in the same PR would force every
consumer to migrate in one release window with no graceful
downgrade. The two-PR sequence (flip default now, remove branch
later) is the standard deprecation cadence for the project (see ADR-0007,
"Phase 1: warn; Phase 2: error"; same pattern here).

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
Rejected for the soft-rollout window. Downstream code that consumes
the library at the Python API surface (rather than the CLI) may have
its own integration-test cassettes that exercise drift shapes through
the soft path. Giving those callers a one-release window with an
explicit opt-out is cheaper than a hard break, and the env-var
removal can land in a follow-up PR once the opt-out's usage drops to
zero in downstream telemetry.

**Couple this flip to the `__all__` audit (PR 13.9b).** Rejected. The
`__all__` audit and migration doc (originally part of t13-9) must
wait for t13-8's session/kernel landing to stabilise the public
surface; the strict-decode flip has zero overlap with t13-8's code
moves and can ship in parallel with waves 4-8 of Tier-13. Splitting
t13-9 into t13-9a (this ADR + flip) and t13-9b (`__all__` audit)
unblocks the parallel window and shortens the tier's critical path.
