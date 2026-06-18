# ADR-0022: Regenerable test baselines (derive / store / compare / regen)

## Status

Accepted.

## Context

Several guardrails freeze a *snapshot* of a value the code already derives, so a
change to the public surface is a deliberate, diff-visible act rather than a
silent regrowth (the compat audit in `scripts/audit_public_api_compat.py` only
flags removed/changed exports vs the last release tag — it is blind to
*additions*). Examples:

- `notebooklm.types.__all__` — the documented, ordered public type surface.
- The *collected* public surface (`__all__` + resolvable allowlist extras) of
  every ungated public module (`notebooklm`, `notebooklm.config`,
  `notebooklm.exceptions`, …).
- The public CLI command tree, options, help, and aliases.

Historically these were frozen as **hand-typed literals inside the test module**
(`_FROZEN_TYPES_ALL`, `_UNGATED_PUBLIC_ALL_SNAPSHOT` in
`tests/_guardrails/test_public_surface_manifest.py`). Each was an exact copy of a
value the test *already knew how to derive* (`list(notebooklm.types.__all__)`;
`_collected_public_surface(module)`). Adding one public symbol therefore meant
hand-editing several frozen literals to match values the code re-derives on every
run — a high marginal cost and an easy place to introduce a copy error.

One guardrail already did it the right way: `tests/unit/cli/test_cli_contract.py`
*derives* `build_cli_contract()`, commits the result to
`tests/fixtures/cli_contract_baseline.json`, asserts `build_cli_contract() ==
json.loads(baseline)`, and ships a `__main__` printer to regenerate the file.
That pattern — **derive / store / compare / regen** — has the property the inline
literals lack: regeneration is one command, and the committed artifact is the
reviewed acknowledgement.

## Decision

Generalize the CLI-contract pattern into a single **baseline registry** and give
it one clean regen command.

1. **Registry.** `tests/_baselines/registry.py` defines a frozen `Baseline`
   dataclass `(name, path, derive, sort_keys, …)` and a `BASELINES` list. Each
   `derive` callable **reuses** the function that already computes the value — it
   never copies a literal:
   - `types_all` → `list(notebooklm.types.__all__)` (ordered).
   - `ungated_surface` → `{module: collect_public_surface(module) …}` over
     `UNGATED_PUBLIC_MODULES` (ordered lists).
   - `cli_contract` → `build_cli_contract()` (dict, `sort_keys=True`).

2. **Committed JSON.** Baselines live under `tests/fixtures/baselines/`
   (`types_all.json`, `ungated_surface.json`); `cli_contract` keeps its
   pre-existing path `tests/fixtures/cli_contract_baseline.json`, registered as
   its `Baseline.path`. Ordered lists serialize in order (lists are never
   sorted; only dict *keys* are affected by `sort_keys`).

3. **One freeze test.** `test_baseline_matches_committed_file`
   (parametrized over `BASELINES`) loads the committed file and asserts it equals
   `derive()`, and that the committed bytes are in canonical serialized form (so
   a regen is a no-op on a fresh checkout).

4. **Regen seam.** A dev-only `--update-baselines` pytest option (in
   `tests/conftest.py`) flips the freeze test from *assert* to *write
   `derive()` → `path`*. `scripts/regen_baselines.py` is the discoverable wrapper
   that shells `pytest … --update-baselines`.

**Dev-only-regen invariant.** Regeneration only ever happens when a developer
passes `--update-baselines`. **CI must NEVER pass the flag — it only diffs.**
This is enforced, not merely documented: the `update_baselines` fixture and
`scripts/regen_baselines.py` both refuse to regenerate when a CI environment is
detected (`CI` env var), failing loudly instead of silently rewriting a baseline.

## Scope boundary

- **`_DOCUMENTED_PUBLIC_IMPORTS` stays hand-curated.** It encodes authored
  *intent* (the promised import surface), not derived reality; regenerating it
  would make its test tautological.
- **`_TOP_LEVEL_TYPE_EXPORTS` stays authored (Phase 2 candidate).** Its naive
  derivation (names in `notebooklm.__all__` whose object `is` the identical
  `notebooklm.types` attribute) over-collects — it pulls in the exception and
  mind-map re-exports that flow through `notebooklm.types` — so it is not a clean
  derive. Migrating it needs a sharper predicate; deferred.
- **Phase 2 (deferred):** the *derived halves* of `tests/fixtures/rpc_golden/*`
  and the `json_stdout` baselines are only semi-derivable (they need surgical
  per-field regen) and are out of scope here.

## Consequences

**Wanted**

- **One-command regen.** Adding a public symbol is `python
  scripts/regen_baselines.py` + reviewing the diff, not editing hand-typed
  literals.
- **No copy drift.** The committed file and the gate derive from the *same*
  function; they cannot disagree by a typo.
- **Same diff-visibility.** A surface addition still produces a reviewed diff
  line — the deliberate acknowledgement is preserved, just in JSON.

**Unwanted**

- **A regen step.** A surface change now requires running the regen command
  rather than editing inline — but the failing freeze test names the command.
- **Indirection.** The frozen value lives in a JSON fixture, not in the test
  source; the registry is the index that ties name ↔ derive ↔ file.

## Alternatives considered

- **Keep hand-typed literals.** Rejected: the per-symbol edit cost and copy-error
  risk are exactly the friction this ADR removes.
- **Auto-accept on mismatch (no committed file).** Rejected: it forfeits the
  diff-visible acknowledgement — a silent surface regrowth is the failure mode
  these gates exist to prevent.
- **One bespoke regen per baseline (status quo of the CLI contract).** Rejected:
  it does not scale; each new baseline would re-invent the derive/store/compare
  plumbing and its own regen entry point.
