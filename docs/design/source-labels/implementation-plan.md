# Source Labels — Implementation Plan

**Status:** Proposed (execution plan; not started)
**Last Updated:** 2026-06-07 (rev 13 — source-removal capability folded in)
- **rev 13** — live capture (2026-06-07, `rpc.md` "Confirmed (2026-06-07)") proved
  `le8sX` supports **source removal** via the third fieldmask slot (`sources_remove`)
  and that **only the first id per group is honoured per call**. Plan changes:
  Phase 1.1 registers a `remove_sources` idempotency variant (`IDEMPOTENT_SET_OP` —
  removal is a confirmed no-op on absent members, so retry-safe; do **not** add it to
  the NO_RETRY `expected` dict); Phase 1.2 builder goes **singular** (`add_source_id` /
  `remove_source_id`) and the prior multi-id `[[sid] for sid in …]` add shape is
  **dropped** (it silently kept only the first id — a real bug); Phase 2.1 adds
  `remove_sources` and makes both `add_sources`/`remove_sources` **loop one `le8sX`
  call per id**; Phase 3.2 adds a `label remove` CLI command (inverse of `label add`,
  **no `--yes` gate** — un-assign is non-destructive, distinct from `label delete`);
  §3.4 + Files-map extend the CLI inventory/JSON gates for `label remove`.
- **rev 12** — three-lens momus (claude+codex; agy stalled/0-output, timeout-killed)
  corrections: moved the `PREEXISTING_GAPS` names edit into **Phase 1.1b** (it must land
  with the enum members, else the Phase-1 CI method-coverage gate goes RED); aligned
  `api.md` UUID-shaped label-name resolution with the plan (full-id passthrough
  **disabled**, exact-name fallback); **re-downgraded** `_DOCUMENTED_PUBLIC_IMPORTS` from
  "mandatory" to recommended hygiene (no completeness gate forces it — rev-11
  over-corrected on a stale review note); `cli/grouped.py` `command_groups` :70→**:71**;
  made the `_row_adapters/__init__.py` submodule re-export explicit.
- **rev 11** — momus re-review (claude+codex) corrections: `PREEXISTING_GAPS` is keyed
  by enum **member name** (`CREATE_LABEL`/…), **not** the wire id — fixed everywhere,
  and flagged as an explicitly-justified deviation from the script's must-not-grow
  ratchet; `_labels.py` is **auto-covered** by the globbed `_*.py` facade-reach-in guard
  (the "extend the inventory" instruction was a category error — removed);
  `_DOCUMENTED_PUBLIC_IMPORTS` made **mandatory** for the 3 root exports (was hedged
  "optional"); dropped `scope` from `_JSON_CONTRACT_DUMMY_ARGS` (it's a Click option,
  not an argument); named the `…document_dedupe_gap` sub-gate as forward-only hygiene.
- **rev 10** — Oracle (design) + momus (plan) review fixes. Corrected stale refs:
  module-size ceiling `client.py` 973→**986** (now AT ceiling — bump-or-split called
  out), mypy gate `test.yml:43`→**:55**, `client.py self.sources` `~:384`→**:390**,
  `_idempotency.py get_entry` ref→**:320** (variant-error branch `:341-356`),
  `test_client_composition.py` path `tests/unit/`→**`tests/_guardrails/`**, integration
  taxonomy allowlists→**`tests/_fixtures/`**. Added: root re-export of
  `Label`/`LabelError`/`LabelNotFoundError` as a Phase-2 task (+`_DOCUMENTED_PUBLIC_IMPORTS`);
  noted `_labels.py` is **auto-covered** by `test_no_facade_reach_in`'s globbed `_*.py`
  guard (no inventory edit); golden fixtures are **hand-authored** (no regen mechanism); a precise
  CLI-JSON-sweep disposition (`JSON_COMMANDS` vs waivers + fake `client.labels`);
  `--cov-fail-under=90` coverage gate; the **decided cassette split** (main PR adds the
  4 method **names** to `PREEXISTING_GAPS` + tracking issue → green without maintainer auth; follow-up
  records cassettes + removes the entries). Downgraded `test_exceptions.py` from
  hard gate to **forward-only hygiene**. Reflected api.md contracts: `delete()`
  idempotent-no-op→`None`, `rename()` emoji-preservation, `--yes/-y` standardization.
- **rev 9** — RPCMethod names singularized to the enum convention (mutations singular, only `LIST_` plural): `CREATE_LABEL` / `UPDATE_LABEL` / `DELETE_LABEL` (mirroring `CREATE_ARTIFACT` / `UPDATE_SOURCE` / `DELETE_ARTIFACT`) + `LIST_LABELS` (mirroring `LIST_ARTIFACTS`). Wire ids unchanged.
- **rev 8** — AI-grouping primitive renamed `auto_label` → `generate(notebook_id, *, scope="all"|"unlabeled")` (the UI's "Reorganize" verb); safe default `scope="unlabeled"`, destructive `scope="all"` CLI-`--yes/-y`-gated; the multi-mode wire RPC (id `agX4Bc`) backs both `generate` and `create`.
- **rev 7** — momus rounds 1–6 (Claude / Gemini / Codex).
**Design source of truth:** [`api.md`](./api.md) (settled; 3 review rounds)
**Wire source of truth:** [`rpc.md`](./rpc.md)
**Scope:** source labels only — RPC layer, `client.labels` API, CLI `label` group
+ `source list --label`. Artifact labels are **out of scope** (api.md §10).

Execution-only (design lives in api.md). **Test-first.** Several CI gates in
this repo are **hardcoded exact-equality tables**; this plan names every one and
the exact entry to add, because a vague "register it" lands RED.

---

## 0. Ground rules

**TDD:** write the failing test first (RED) → implement (GREEN) → refactor.

**Commands (verified — `docs/development.md`, `pyproject.toml`):**
- Fast loop: `uv run pytest tests/unit tests/integration -m "not repo_lint"`
- Full suite (guardrails + repo_lint): `uv run pytest`
- Lint+format: `uv run pre-commit run --all-files` — **Ruff lint + ruff-format
  ONLY** (`.pre-commit-config.yaml` has no mypy). **Mutating** (Ruff `--fix` may
  rewrite files): fix-then-verify, then confirm a clean `git diff` + a second clean run.
- Types (SEPARATE CI gate, NOT in pre-commit): `uv run mypy src/notebooklm
  --ignore-missing-imports` (`test.yml:55`; `:43`/`:44` is the setup-uv step). New typed modules can RED here;
  `_types/labels.py` is under the **strict** `notebooklm._types.*` mypy override
  (`disallow_untyped_defs`/`disallow_any_generics`/`warn_return_any`) — fully type it.
- E2E (manual, auth): `uv run pytest tests/e2e -m readonly --profile <name>`

**Exact-equality CI gates that MUST be edited (file:line verified rev 2):**
| Gate file | Structure to edit | Why it fails otherwise |
|---|---|---|
| `tests/unit/test_idempotency_registry.py:~135` | `expected` dict in `test_retry_disabled_entries_are_intentional_and_documented` | `assert actual == expected` over all NO_RETRY/PROBE entries |
| `tests/unit/test_rpc_golden_payloads.py:~229` | one `tests/fixtures/rpc_golden/<METHOD>.json` per `RPCMethod` | `ALL_METHODS = list(RPCMethod)` drives schema/envelope checks |
| `tests/unit/test_rpc_health_coverage.py:66,147` | `MUTATING_SKIP_LIST` (in this test file) / probe in `check_rpc_health.py` | every `RPCMethod` probed-or-skipped |
| `tests/unit/test_exceptions.py:58` (forward-only hygiene, **NOT a hard gate**) | `exceptions` list in `test_all_exceptions_inherit_from_base` | hand-maintained list; no test forces every exception into it, so omitting Label* does **not** RED — add for parity only |
| `tests/_guardrails/test_module_size_ratchet.py:65,66,71` | `ALLOWLISTED_CEILINGS` for `cli/source_cmd.py`(949)/`exceptions.py`(1460)/`client.py`(986) | files are **at ceiling now** — any net growth RED; raise the ceiling or split. **`client.py` is AT its 986 ceiling**, so adding `self.labels` (Phase 2.2) requires bumping the ceiling — but the ratchet's own header comment (`:62`, "DO NOT raise a ceiling to make room for new code in a fat module — split it") prefers a split. Reconcile in the PR: either call out the bump explicitly with justification, or split `client.py`. |
| `tests/scripts/check_method_coverage.py` (CI step, **not pytest**) | add the 4 enum member **names** (`CREATE_LABEL`/`LIST_LABELS`/`UPDATE_LABEL`/`DELETE_LABEL` — the set is keyed by `RPCMethod.<NAME>`, **not** the wire id, `:94`/`:194`) to `PREEXISTING_GAPS` **in Phase 1.1b** (when the enum members land — the gate sees them immediately; with a tracking-issue ref; ⚠️ deviates from the must-not-grow ratchet `:90-95` — justify in PR); a follow-up cassette PR records `tests/cassettes/*.yaml` + removes them | recording needs maintainer auth (see M12 / Phase 1.1b + 3.2) |
| `.github/workflows/test.yml:215` (`uv run pytest … --cov-fail-under=90`, **CI step, not a list edit**) | the new modules need ≥90% branch coverage | the whole-suite run fails if total coverage drops below 90% — cover every new branch (the `sources()` race-skip, `create` 0/>1-id error, drift-raise, no-op `ValueError`s) |
| `scripts/check_claude_md_freshness.py` (CI step, **not pytest**; `test.yml:90` + `tests/unit/test_claude_md_freshness.py`) | `CLAUDE.md` file-table **and** repo-structure map | every new `src/notebooklm` module/package must be documented in BOTH |
| `tests/_guardrails/test_public_surface_manifest.py:207` | `_FROZEN_TYPES_ALL` (exact order) | `assert list(types.__all__) == _FROZEN_TYPES_ALL` (:464) |
| `…:281` | `_TOP_LEVEL_TYPE_EXPORTS` | parametrized identity check (:469) |
| `…:354` | `_TOP_LEVEL_EXCEPTION_EXPORTS` | closed-set check (:498) + identity (:488) |
| `…:337` | `_TYPES_EXCEPTION_REEXPORTS` | identity-coverage of types-re-exported exceptions (parametrized :479) |
| `…:35` | `_DOCUMENTED_PUBLIC_IMPORTS` | per-module public-name cross-check (:94,:113); **lists sorted case-insensitive** (:111). **Recommended hygiene, NOT a hard gate** (no completeness check forces it — omitting won't RED CI); still add `Label`/`LabelError`/`LabelNotFoundError` to the `notebooklm` root entry since the docs advertise the import |
| `tests/unit/test_public_api_contract.py:57,:74` | `NAMESPACES`, `LOOKUP_NAMESPACES` | `test_lookup_surface_is_pinned` equality |
| `tests/unit/test_public_api_behavior.py:211` | `LOOKUP_CASES` (closed set) | `test_table_covers_all_lookup_namespaces` (:322) |
| `tests/unit/test_public_api_compat_audit.py:174` | pinned representative namespace methods | **subset (`<=`)** check — add ≥1 `labels.*` method (not exact-equality) |
| `scripts/audit_public_api_compat.py:46` | `CLIENT_NAMESPACE_ATTRIBUTES` tuple | method-level drift audit |
| `src/notebooklm/cli/grouped.py:71` | `SectionedGroup.command_groups` | `tests/unit/cli/test_grouped.py` no-orphan |
| `tests/_guardrails/test_cli_vcr_coverage.py:71/:90` | `GROUP_COVERAGE` or `COVERAGE_EXEMPT` | `test_every_cli_group_is_classified` fails on an unclassified new group |

> **Complete CI gate sweep (authoritative completeness mechanism).** This repo has
> an unusually dense gate suite; the table above is the *named* head-start, NOT a
> proof of completeness. The DEFINITIVE complete gate set is **what CI runs** — and
> CI is `uv run pytest` (all tiers incl. `repo_lint` + `tests/_guardrails/`) **PLUS
> standalone non-pytest script steps** in `.github/workflows/test.yml`. Many gates
> fail loud naming the exact missing entry, but several CI script steps are NOT
> collected by pytest, so the convergence procedure is: after Phase 2/3/4, run BOTH
> (a) `uv run pytest` and (b) each workflow script step, and fix every RED until all
> pass. **Authoritative source = every `run:` line in `.github/workflows/test.yml`
> that invokes `uv run` — re-derive from the workflow; do not trust this snapshot to
> stay complete.** Current non-pytest steps (snapshot):
> `uv run mypy src/notebooklm --ignore-missing-imports` (**:55 — the type gate; NOT
> in pre-commit; can RED on the new typed modules**),
> `scripts/check_claude_md_freshness.py`, `scripts/check_docs_module_refs.py`,
> `scripts/audit_public_api_compat.py --check-stale`,
> `scripts/check_coverage_thresholds.py [--coverage-json coverage.json]`,
> `scripts/check_ci_install_parity.py`, `scripts/check_deprecation_targets.py`,
> `tests/scripts/check_method_coverage.py`, `tests/scripts/check_cassettes_clean.py
> --strict --recursive`, `tests/scripts/check_cassettes_clean.py --secrets-only
> --recursive tests/fixtures`, and the workflow-guard scripts
> `check_workflow_permissions.py`/`check_workflow_secret_gates.py`/`check_action_pinning.py`
> (green-by-construction here — they scan only `.github/workflows/`, untouched).
> Also `git grep` the new symbols (`Label`, `LabelError`,
> `LabelNotFoundError`, `LabelsAPI`, `"labels"`, `"label"`, the 4 RPC ids) across
> `tests/` + `scripts/` and add to every pinned list a RED gate reports. Known
> forward-only (won't RED, edit proactively): `tests/unit/test_types.py`
> `_PUBLIC_MOVABLE_CLASSES` (after the `Label.__module__` rewrite),
> `tests/_guardrails/test_no_module_shadowing.py` (`RENAMED_MODULES`/`CLICK_GROUPS_PUBLIC`),
> `tests/_guardrails/test_cli_boundary.py` (`CLI_COMMAND_MODULES`),
> `tests/_guardrails/test_client_composition.py` (`FEATURE_API_NAMES`) — note the
> path is `tests/_guardrails/`, NOT `tests/unit/` (the unit path does not exist).

**ADR posture (consideration, per `docs/adr/README.md`):** this adds a module under
`src/notebooklm/cli/services/` (an architectural-shape trigger that "requires ADR
consideration"). Resolution: it is **pattern-conformant under ADR-0008** (every
`source`/`artifact` command already has a `cli/services/*` module); it changes no
layer contract and adds nothing under `_runtime/`/`_middleware/`/`auth/`. **No new
ADR**; the PR ADR checkbox is ticked citing ADR-0005/0008/0012/0013/0017/0019 +
this plan. (If a reviewer disagrees, add a one-paragraph ADR-0008 addendum — not
a new ADR.)

**Green-at-each-phase:** each phase is a CI-green increment. Public surface is
exposed only in Phase 2, so Phase 1 cannot trip surface/contract gates.

**Dependency DAG:** Phase 1 (wire) → Phase 2 (API + surface) → Phase 3 (CLI) → Phase 4 (docs).

---

## Phase 1 — Wire foundation (internal only)

api.md refs: §3–§6, §8.

### 1.1 RPCMethod members + idempotency (atomic commit)
- **Edit** `src/notebooklm/rpc/types.py` (enum ~:50) — add `CREATE_LABEL="agX4Bc"`,
  `LIST_LABELS="I3xc3c"`, `UPDATE_LABEL="le8sX"`, `DELETE_LABEL="GyzE7e"`.
- **Edit** `src/notebooklm/_idempotency_policy.py` — `registry.register(...)`
  (real signature `register(method, policy, *, variant=None, probe_key_fn=None,
  notes=...)`, `_idempotency.py:289`): `LIST_LABELS`→`IDEMPOTENT_SET_OP`;
  `DELETE_LABEL`→`NON_IDEMPOTENT_NO_RETRY` (conservative, api.md §15);
  `UPDATE_LABEL`→`IDEMPOTENT_SET_OP` default **+** `variant="add_sources"`→
  `NON_IDEMPOTENT_NO_RETRY` **+** `variant="remove_sources"`→`IDEMPOTENT_SET_OP`
  (removal is a confirmed no-op on an absent member, so retry-safe — api.md §4);
  `CREATE_LABEL`→`NON_IDEMPOTENT_NO_RETRY`. Each with a non-empty `notes=`
  (required by `test_registry_classifies_every_rpc_method_at_variant_none`).
- **Gate edit (exact):** in `tests/unit/test_idempotency_registry.py`
  `test_retry_disabled_entries_are_intentional_and_documented` (~:135), add to the
  `expected` dict: `(RPCMethod.DELETE_LABEL, None): NON_IDEMPOTENT_NO_RETRY`,
  `(RPCMethod.CREATE_LABEL, None): NON_IDEMPOTENT_NO_RETRY`,
  `(RPCMethod.UPDATE_LABEL, "add_sources"): NON_IDEMPOTENT_NO_RETRY`. **Do NOT** add
  `(UPDATE_LABEL, None)` **nor** `(UPDATE_LABEL, "remove_sources")` — both are
  `IDEMPOTENT_SET_OP`, excluded by this table's NO_RETRY/PROBE filter.
- **Test add:** explicit cases asserting
  `IDEMPOTENCY_REGISTRY.get_entry(RPCMethod.UPDATE_LABEL, "add_sources").policy is
  IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY` **and**
  `get_entry(RPCMethod.UPDATE_LABEL, "remove_sources").policy is
  IdempotencyPolicy.IDEMPOTENT_SET_OP` (use the singleton; `get_entry` is
  defined on the registry at `_idempotency.py:320`). Variant threading: `get_entry`
  raises `IdempotencyVariantError` for an unknown variant once a method has any
  explicit variant row (the variant-error branch is `_idempotency.py:341-356`) — so
  `rename`/`set_emoji`/`update` MUST pass `operation_variant=None`, and only
  `add_sources`/`remove_sources` pass their registered variant strings (§7).
- **Forward-only (won't RED):** `test_non_idempotent_no_retry_entries_document_dedupe_gap`
  (`test_idempotency_registry.py:180`) iterates a fixed `expected_terms` set, so the 3
  new `NON_IDEMPOTENT_NO_RETRY` entries don't trip it — add their dedupe-gap note terms
  there for parity (optional hygiene, not a hard gate).
- **Verify:** `uv run pytest tests/unit/test_idempotency_registry.py`

### 1.1b RPCMethod-keyed gates (COMPLETE set — adding 4 enum members trips these too)
Several Phase-1 tests iterate `list(RPCMethod)` and go RED on any unclassified new
member (and the Phase-1-exit command `-m "not repo_lint"` collects them). Edit **all**:
- `tests/unit/test_rpc_golden_payloads.py` — `ALL_METHODS = list(RPCMethod)` requires
  one `tests/fixtures/rpc_golden/<METHOD_NAME>.json` per method (42 exist today). Add
  4 golden fixtures (`CREATE_LABEL.json`, `LIST_LABELS.json`, `UPDATE_LABEL.json`,
  `DELETE_LABEL.json`); they drive schema / method-id / request-envelope / decoder /
  mapper checks (~:229). **There is NO regeneration mechanism** — these are
  **hand-authored** per `tests/fixtures/rpc_golden/README.md`. Derive `expected_f_req`
  from the `_label/params.py` builders and `expected_decoded` from the response
  envelopes: `[None, [...]]` for `CREATE_LABEL`/`generate`, `[[...]]` for
  `LIST_LABELS`, and `[]` for `UPDATE_LABEL`/`DELETE_LABEL`. Include `error_frame` +
  `method_id_drift` `drift_cases` to match the peer drift-prone fixtures
  (`CREATE_ARTIFACT`/`ADD_SOURCE`/etc.). Use the README's synthetic
  `SCRUBBED_*` placeholders for all ids.
- `tests/unit/test_rpc_health_coverage.py` — `test_every_rpc_method_is_probed_or_explicitly_skipped`
  (:147): the 3 **write** methods (`CREATE_LABEL`, `UPDATE_LABEL`, `DELETE_LABEL`)
  → `MUTATING_SKIP_LIST` (:66, each with a justifying comment; keep skip-lists disjoint);
  the **read** `LIST_LABELS` → add a read-only probe via `get_test_params`
  (`scripts/check_rpc_health.py:439`) **or** a skip-list entry.
- `tests/scripts/check_method_coverage.py` (**CI step at `test.yml:111`, not a pytest
  test** — but it runs on the Phase-1 PR, so handle it **here in Phase 1**, not Phase 3).
  It iterates `list(RPCMethod)`; the 4 new methods now have a test reference (the golden
  fixtures above) but **no cassette**, so add their 4 enum **names** (`CREATE_LABEL`/
  `LIST_LABELS`/`UPDATE_LABEL`/`DELETE_LABEL` — keyed by `RPCMethod.<NAME>`, `:94`/`:194`)
  to `PREEXISTING_GAPS` in **this same commit**, each with a tracking-issue ref. ⚠️ Deviates
  from the script's must-not-grow ratchet (`:90-95`) — justify in the PR. The Phase-3
  cassette follow-up **removes** these entries (§3.2). *(Without this, Phase 1 is RED: the
  gate sees the new methods the moment the enum + goldens land.)*
- **Already-compliant (note, no edit):** `tests/unit/test_rpc_types.py` (id shape
  `^[A-Za-z0-9]{4,12}$` + uniqueness — the 4 ids pass); `tests/_guardrails/test_rpc_method_ids_only_in_types.py`
  (builders reference `RPCMethod.*`, no raw id strings in src); `tests/_guardrails/test_no_raw_positional_rpc_indexing.py`
  (`LabelRow` uses `safe_index`, not raw `[i]`).
- **Verify:** `uv run pytest tests/unit/test_rpc_golden_payloads.py tests/unit/test_rpc_health_coverage.py tests/unit/test_rpc_types.py`

### 1.2 Param builders
- **Test first:** `tests/unit/test_label_params.py` — exact payloads (scope `[]`/`[0]`;
  create slot[5] `[[name,emoji]]`; update name-only→`[[[name]]]`, emoji-only,
  **single** `add_source_id`→`[[None,[[id]]]]`, **single** `remove_source_id`→
  `[[None,None,[[id]]]]`; delete batch); assert `_opts()` returns a **distinct**
  object each call. The builder is **singular** (one `add_source_id` and/or one
  `remove_source_id`, positionally at `slot[3][0][1]`/`[2]`) — there is **no** multi-id
  list arg (the wire honours only the first id; api.md §5).
- **Add** `src/notebooklm/_label/__init__.py`, `src/notebooklm/_label/params.py` (§5).
- **Verify:** `uv run pytest tests/unit/test_label_params.py`

### 1.3 Row adapter (strict)
- **Test first:** `tests/unit/test_label_row.py` — decode 4-tuple; `sources is None`→`()`;
  **drift raises `UnknownRPCMethodError`** for short tuple, non-str name/id, malformed
  member, non-list/non-None sources, non-str emoji. (Note: `safe_index`
  (`rpc/_safe_index.py:54`) raises only on descent `IndexError/TypeError/KeyError`,
  so the explicit `isinstance` type-raises in `LabelRow` are **load-bearing**.)
- **Add** `src/notebooklm/_row_adapters/labels.py` (`LabelRow`, import `safe_index`
  from `..rpc`); in `src/notebooklm/_row_adapters/__init__.py` add `from . import labels`,
  `from .labels import LabelRow`, and **both** `"labels"` and `"LabelRow"` to `__all__`
  (the pattern re-exports the submodule **and** the class — mirror `sources`/`SourceRow`).
- **Verify:** `uv run pytest tests/unit/test_label_row.py`

### 1.4 Model
- **Test first:** `tests/unit/test_label_model.py` — `Label.from_api_response`
  builds via `LabelRow`; `source_ids` populated; `emoji ""`→`None`; `notebook_id`
  keyword-only.
- **Add** `src/notebooklm/_types/labels.py` (`Label`). **Not** re-exported yet (Phase 2).
- **Verify:** `uv run pytest tests/unit/test_label_model.py`

### 1.5 Exceptions
- **Test first:** `tests/unit/test_label_exceptions.py` — `LabelNotFoundError` is
  `NotFoundError`+`RPCError`+`LabelError`; carries `label_id`/`method_id`.
- **Edit** `src/notebooklm/exceptions.py` — add `LabelError`, `LabelNotFoundError`;
  add both to `exceptions.__all__` and the `NotFoundError` umbrella doc list.
  (Root `__init__`/`types.py` re-exports happen in Phase 2 with the surface gates.)
- **Forward-only hygiene (NOT a hard gate):** add `LabelError`,
  `LabelNotFoundError` to the explicit `exceptions` list in
  `tests/unit/test_exceptions.py::test_all_exceptions_inherit_from_base` (`:58`,
  where `NoteError`/`MindMapError` etc. are pinned). This list is **hand-maintained**
  and only asserts each entry is a `NotebookLMError` subclass — **no test forces
  every exception into it**, so omitting the new classes does **not** turn the suite
  RED. Add them for parity, not because a gate requires it.
- **Module-size ratchet:** `exceptions.py` is **at its 1460 ceiling**; the 2 new
  classes exceed it → raise `ALLOWLISTED_CEILINGS["exceptions.py"]` in
  `tests/_guardrails/test_module_size_ratchet.py:66` to the new line count (or split).
- **Verify:** `uv run pytest tests/unit/test_label_exceptions.py tests/unit/test_exceptions.py`

**Phase 1 exit:** `uv run pytest tests/unit -m "not repo_lint"` green; `pre-commit` clean.

---

## Phase 2 — Public API + wiring + surface gates

api.md refs: §7, §9.

### 2.1 `LabelsAPI`
> **Test tier = `tests/unit/`, NOT `tests/integration/`.** This is a mock-`RpcCaller`
> test (no real HTTP/VCR), so it belongs in `tests/unit/`. Putting it in
> `tests/integration/` would trip the integration-tier gates — the
> `vcr`/`allow_no_vcr` collection-marker check (`tests/integration/conftest.py:166`),
> the integration-taxonomy allowlists (which live under **`tests/_fixtures/`**:
> `tests/_fixtures/integration_allow_no_vcr_files.txt`,
> `tests/_fixtures/integration_allow_no_vcr_nodeids.txt`, and
> `tests/_fixtures/integration_vcr_allow_no_vcr_nodeids.txt`, enforced by
> `tests/_guardrails/test_integration_allow_no_vcr_allowlist.py`), and
> `scripts/test_taxonomy_inventory.py`. Using `tests/unit/test_labels_api.py`
> avoids that entire cluster.
- **Test first:** `tests/unit/test_labels_api.py` (mock `RpcCaller`):
  - `list`→`LIST_LABELS` decodes `[[label,…]]`; `generate`→`CREATE_LABEL`
    decodes `[None,[label,…]]` (assert the two envelopes differ).
  - `get`/`get_or_none` over `list`; `get` raises `LabelNotFoundError` via
    `unwrap_or_raise(obj, exc)` (`_lookup.py:27`), `method_id=LIST_LABELS`.
  - `create` finds the new label by **id-diff** vs a pre-call `list` snapshot;
    raises `LabelError` on 0/>1 new ids.
  - `rename`/`set_emoji`/`update` send `operation_variant=None`; `add_sources`
    sends `operation_variant="add_sources"`; `remove_sources` sends
    `operation_variant="remove_sources"`; **all run the existence preflight and
    raise on a missing label even with `return_object=False`**. (Test note: prime
    the mock to return an **empty** `LIST_LABELS` envelope `[[]]` so the preflight
    raises.)
  - `add_sources([a,b,c])` and `remove_sources([a,b,c])` each issue **one
    `rpc_call` per id** (assert call count == 3, one per source — NOT a single
    multi-id call), then one preflight re-fetch. Empty `source_ids` → `ValueError`
    before any `rpc_call`. `remove_sources` of a non-member does not raise (no-op).
  - `rename` **preserves emoji** (api.md A3): with the preflight returning a label
    whose `emoji="📄"`, `rename(name="X")` sends `[[[X, "📄"]]]` (name+current emoji),
    not `[[[X]]]` — assert the emoji is carried over from the preflight fetch.
  - **No-op mutation `ValueError` BEFORE any RPC** (api.md A6): `add_sources([])`
    and `update(name=None, emoji=None)` raise `ValueError` and issue **no**
    `rpc_call` (assert the mock was not called).
  - `delete` **absent-target is an idempotent no-op returning `None`** (api.md A2) —
    a delete of an unknown id does not raise; assert it returns `None`. (This is the
    API contract; the transport idempotency CLASS stays `NON_IDEMPOTENT_NO_RETRY` —
    a separate axis, asserted in `test_idempotency_registry.py`, §1.1.)
  - `sources()` = one `get_or_none(label)` + one `self._list_sources(nb)`, joined
    in membership order, skipping members absent from the source list; raises on a
    missing label.
  - `delete` accepts str|list; `allow_null=True`.
- **Add** `src/notebooklm/_labels.py` — `LabelsAPI(rpc, *, list_sources)` (§7);
  use a narrow `list_sources` callable (not `SourcesAPI`).
  **Exact return annotations** (required by the contract gates): `list -> list[Label]`,
  `get -> Label` (non-Optional), `get_or_none -> Label | None`, `delete -> None`,
  `sources -> list[Source]`, mutations (`rename`/`set_emoji`/`update`/`add_sources`/
  `remove_sources`) `-> Label | None`. Both `add_sources` and `remove_sources` loop
  one `build_update_label_params(...)` call per id (api.md §7).
- **Facade-reach-in guard: no edit needed — `_labels.py` is auto-covered.**
  `test_no_facade_reach_in.py::test_feature_apis_do_not_add_direct_core_private_state_access`
  globs **every** top-level `src/notebooklm/_*.py` (`:214`), so a new `_labels.py`
  facade is checked for free and passes (it has no `self._core._private` reach-in).
  The hand-maintained inventories (`_ARTIFACT_SERVICE_MODULES`/`_SOURCE_SERVICE_MODULES`/
  `_NOTEBOOK_COMPOSITION_SERVICE_MODULES`/`_REACH_IN_MIGRATED_MODULES`) are for
  `_artifact/`/`_source/` **service-helper** modules, not facades — the peer facades
  `_sources.py`/`_sharing.py` are **not** in them (they appear only in the
  *forbidden-import* allowlist). Add a guarded entry **only if** a future label
  *service* helper module reaches into a facade.
- **Verify:** `uv run pytest tests/unit/test_labels_api.py tests/_guardrails/test_no_facade_reach_in.py`

### 2.2 Client wiring + public exports + ALL surface gates (atomic)
- **Edit** `src/notebooklm/client.py` — after `self.sources` (`:390`), add
  `self.labels = LabelsAPI(internals.executor, list_sources=self.sources.list)`;
  add `labels` to the client docstring Attributes.
- **Edit** `src/notebooklm/types.py` — re-export `Label` (set `Label.__module__ =
  "notebooklm.types"`), add to `types.__all__`; **also** re-export `LabelError`/
  `LabelNotFoundError` from `.exceptions` and add to `types.__all__` (back-compat,
  mirrors `SourceNotFoundError` at `types.py:75`/`:174`).
- **Edit** `src/notebooklm/__init__.py` (api.md A1) — import **all three**
  `Label`, `LabelError`, `LabelNotFoundError` in the import block **and** add them to
  root `__all__` (public **type** dataclasses + their exceptions ARE root-exported:
  `Source`/`Notebook`/`Note`/`MindMap` at `:178-306`, with `Source*`/`Note*`
  `NotFoundError`s at `:56-105`/`:229-277`). **`LabelsAPI` is NOT root-exported**
  (feature API classes never are); surface is `NotebookLMClient.labels`.
- **Gate edits (exact):**
  - `tests/_guardrails/test_public_surface_manifest.py`: because `types.__all__`
    re-exports the Label exceptions (back-compat), the frozen list carries them too —
    add **all three** `"Label"`, `"LabelError"`, `"LabelNotFoundError"` to
    `_FROZEN_TYPES_ALL` (:207) **at the exact indices matching their positions in
    `types.__all__`** (order-sensitive equality, :464; the existing `Source*`
    exception re-exports at :239 are the precedent). Add `"Label"` to
    `_TOP_LEVEL_TYPE_EXPORTS` (:281); add `"LabelError"`,`"LabelNotFoundError"` to
    `_TOP_LEVEL_EXCEPTION_EXPORTS` (:354, closed set :498) **and** to
    `_TYPES_EXCEPTION_REEXPORTS` (:337, parametrized identity :479). Editing
    `_DOCUMENTED_PUBLIC_IMPORTS` (:35) is **recommended hygiene, NOT a hard gate** — no
    test forces root exports into it (its only checks are "listed names import" + sort),
    so omitting won't RED CI. But since the docs advertise `from notebooklm import Label`,
    add all three of `Label`/`LabelError`/`LabelNotFoundError` to its `notebooklm` root
    entry anyway, keeping each list **sorted case-insensitive** (:111). (api.md §9 agrees.)
  - `tests/unit/test_public_api_contract.py`: add `LabelsAPI` to `NAMESPACES` (:57)
    and `"labels"` to `LOOKUP_NAMESPACES` (:74). (This test inspects classes/annotations
    only — it does **not** instantiate, so no factory/`list_sources` here.)
  - `tests/unit/test_public_api_behavior.py`: add a full `LookupCase` to `LOOKUP_CASES`
    (:211; closed set, :322) — `LookupCase` has **7 required fields**:
    `namespace="labels"`, `factory=_make_labels_api`, `arrange_miss=_arrange_list_miss`
    (reuse — it stubs `api.list`, which `labels.get` scans), `get_args=("nb_1","missing")`,
    `resource="label"`, `not_found_error=LabelNotFoundError`, `get_warns=False`
    (v0.8.0 raise-on-miss). Add a `_make_labels_api` helper:
    `LabelsAPI(MagicMock(), list_sources=AsyncMock(return_value=[]))` (the
    `list_sources` kwarg has no default).
  - `scripts/audit_public_api_compat.py`: add `"labels"` to `CLIENT_NAMESPACE_ATTRIBUTES` (:46).
  - `tests/unit/test_public_api_compat_audit.py` (:174): **subset (`<=`)** check —
    add ≥1 representative `labels.*` method to the pinned set (not strictly required
    for green, but keeps the namespace audited).
- **Verify:** `uv run pytest tests/unit/test_public_api_contract.py
  tests/unit/test_public_api_behavior.py tests/unit/test_public_api_compat_audit.py
  tests/_guardrails/test_public_surface_manifest.py` then `uv run pytest -m repo_lint`.

**Phase 2 exit:** full `uv run pytest` green; `pre-commit` clean; `client.labels`
+ `from notebooklm import Label` work.

---

## Phase 3 — CLI

api.md refs: §12. ADR-0008: logic in `cli/services/`, commands thin
(`tests/_guardrails/test_cli_boundary.py` is AST-enforced).

### 3.1 CLI service: join + `resolve_label_id`
- **Test first:** `tests/unit/cli/test_label_listing.py`:
  - `resolve_label_id(client, nb, token)` — exact id / unambiguous prefix; exact
    name; ambiguous name → error listing candidates (id+emoji+source count).
    **IMPORTANT (verified):** `resolve_partial_id_in_items` matches on `id_of`
    only (`cli/resolve.py:290`); `title_of` is **diagnostics text only** and does
    **not** make names matchable. So `resolve_label_id` must do **explicit name
    matching itself** over `client.labels.list()` — e.g. (a) try id/prefix via
    `resolve_partial_id_in_items(..., allow_full_id_passthrough=False)` and, on no
    id match, (b) match `token` against `label.name` (exact; collect all; >1 →
    ambiguity error with candidates). Define precedence (id/prefix first, then
    name) so a UUID-shaped *name* is found by the name pass after the id pass
    misses. Do **not** rely on `title_of` for resolution.
  - title-join builds `{source_id: title}` from a **single** `sources.list()`
    (assert exactly one source-list call; no N+1).
- **Add** `src/notebooklm/cli/services/label_listing.py` — `resolve_label_id`, the
  members+titles join over `client.labels` + one `sources.list()`, and a
  `LabelListPlan`/executor using `prepare_list` with `items_key="labels"`.
- **Verify:** `uv run pytest tests/unit/cli/test_label_listing.py`

### 3.2 `label` command group
- **Test first:** `tests/unit/cli/test_label_cmd.py` (CliRunner): `list`
  (`--json`→`{"labels":[…],"count":N}` with member ids+titles), `sources`
  (delegates to `client.labels.sources()`), `create`/`rename`/`emoji`/`add`/`remove`
  (ids via `resolve_source_ids`, `cli/resolve.py:489`)/`delete`/`generate`
  (`--yes/-y` gate on `--scope all`, the repo-standard confirm flag — `delete` is
  `--yes/-y`-gated too; **`remove` is NOT gated** — un-assign is non-destructive,
  the sources survive). `remove` → `client.labels.remove_sources()`, the inverse of
  `add`, and is distinct from `delete` (which deletes the label entity).
  **`tests/unit/cli/test_grouped.py`** — `label` is binned (no orphan). [Path
  corrected: this test is in `tests/unit/cli/`, not `_guardrails/`.]
- **Add** `src/notebooklm/cli/label_cmd.py` (thin shell → service; route through
  `handle_errors`); export from `src/notebooklm/cli/__init__.py`.
- **Register the group (REQUIRED — otherwise `notebooklm label` does not exist):**
  in `src/notebooklm/notebooklm_cli.py`, import `label` in the `from .cli import (…)`
  block (~:96) and add `cli.add_command(label)` next to the others (~:241–249).
- **Bin in help:** add `"label"` to `SectionedGroup.command_groups` (`cli/grouped.py:71`)
  — else `tests/unit/cli/test_grouped.py` no-orphan fails.
- **CLI VCR coverage + per-method cassette coverage (two gates — DECIDED SPLIT, a
  Phase-3 precondition):** (a)
  `tests/_guardrails/test_cli_vcr_coverage.py::test_every_cli_group_is_classified`
  needs `label` classified — add `label` to `GROUP_COVERAGE` (:71). (b) The CI-only
  `tests/scripts/check_method_coverage.py` was **already satisfied back in Phase 1.1b** —
  the 4 enum names (`CREATE_LABEL`/`LIST_LABELS`/`UPDATE_LABEL`/`DELETE_LABEL`) were added
  to `PREEXISTING_GAPS` when the enum members landed (Phase 1 is the first phase the CI
  gate sees them), each with a tracking-issue ref; ⚠️ that addition deviates from the
  script's must-not-grow ratchet (`:90-95`) — justified in the PR. **Nothing to add here**
  for gate (b). The **follow-up PR (needs maintainer auth)** records the CLI VCR cassettes
  for the `label` group (and one `source list --label`) exercising
  list/generate/create/rename/emoji/add/remove/delete via `NOTEBOOKLM_VCR_RECORD=1` under
  `tests/cassettes/*.yaml` + `tests/integration/cli_vcr/`, then **REMOVES** the 4 names
  from `PREEXISTING_GAPS` (whose `STALE` check then confirms real coverage). Do
  **not** use `COVERAGE_EXEMPT` — exemption ships no cassette and is the wrong tool.
- **Verify:** `uv run pytest tests/unit/cli/test_label_cmd.py
  tests/unit/cli/test_grouped.py tests/_guardrails/test_cli_boundary.py
  tests/_guardrails/test_cli_vcr_coverage.py` **and** `uv run python
  tests/scripts/check_method_coverage.py` — the latter is **green in the main PR via
  the `PREEXISTING_GAPS` entries** (no maintainer cassettes required); the follow-up
  cassette PR is what removes those entries and proves real coverage.

### 3.3 `source list --label` selector
- **Test first:** extend `tests/unit/cli/test_source_*` — `source list --label
  <id|name>` returns only the group's sources; resolution reuses `resolve_label_id`;
  read-only; `--json` envelope key stays `"sources"`; `count`/rows are consistent
  with the filtered set.
- **Edit** `src/notebooklm/cli/services/source_listing.py` — add `label_filter`
  to `SourceListPlan` (it is `@dataclass(frozen=True)`, fields at :24). The current
  `_build_spec` (:35) sets `ListSpec(fetch=lambda c, nb: c.sources.list(nb))` and
  `execute_source_list` (:69) just calls `prepare_list`. **Inject the filter into
  the `fetch` closure** (so `prepare_list`'s `count`/rows match the filtered set —
  do **not** post-filter after `prepare_list`). Reuse the `client.labels.sources()`
  result for the id set to avoid a second `sources.list()`. Thread `label_filter`
  through `_build_spec` — its signature is `_build_spec(source_type_display)` today
  (:35), so it grows a `label_filter` param (or `execute_source_list` builds the
  filtered fetch closure directly).
- **Edit** `src/notebooklm/cli/source_cmd.py` — add the `--label` option and pass it
  into the `SourceListPlan(...)` construction at `cli/source_cmd.py:164` (frozen plan
  built here). **Module-size ratchet:** `cli/source_cmd.py` is **at its 949 ceiling**
  → raise `ALLOWLISTED_CEILINGS["cli/source_cmd.py"]` (`test_module_size_ratchet.py:65`).
- **Verify:** `uv run pytest tests/unit/cli
  tests/_guardrails/test_cli_boundary.py tests/_guardrails/test_cli_rpc_envelope.py`

### 3.4 CLI inventory gates (EXHAUSTIVE — registering a new group/service/json-command trips all of these)
This repo pins the CLI surface with several hardcoded inventory tables. The full
set (swept across `tests/unit/cli/` + `tests/unit/`) that a `label` group, the
`cli/services/label_listing.py` module, and `label … --json` commands will turn
RED — edit **every** one:
- `tests/unit/cli/test_cli_contract.py`: add `"label"` to `TRACKED_GROUPS` (:35)
  and `CLICK_GROUPS` (:49); add a `HELP_SNIPPETS` entry (:71) if required; **regenerate
  `tests/fixtures/cli_contract_baseline.json`** (exact-compared at :259; the file's
  own `__main__`/regen mechanism ~:586). Add `_JSON_CONTRACT_DUMMY_ARGS` keys for the
  new commands' positional **arguments** (e.g. `label_id`, `name`) at :438 — this table
  is for Click *arguments* only; Click **options** (`--scope`, `--emoji`, `--yes`) do
  NOT need entries. **Do
  NOT** add normal CRUD `label … --json` commands to `JSON_CONTRACT_EXEMPTIONS` (:426)
  — `_enforced_json_command_paths` (:519) enforces all `--json` commands *except*
  exemptions, so exemptions are only for intentionally non-envelope (diagnostic/read)
  commands; CRUD commands must instead emit a valid typed envelope.
- `tests/unit/cli/test_services_boundary.py`: add `cli/services/label_listing.py`
  to `GUARDED_PATHS` (:111) — `test_inventory_completeness` (:516) fails any
  unclassified `cli/services/*.py`.
- `tests/unit/test_json_stdout_purity.py`: every `label … --json` command needs a
  sweep entry (`test_all_json_commands_have_sweep_entry` :877 is auto-discovering).
  **Exact disposition (per command):**
  - **Asserted in `JSON_COMMANDS` (:424)** — the read/CRUD commands that emit the
    standard envelope on success: `label list --json`, `label sources --json`,
    `label create --json`, `label rename --json`, `label emoji --json`,
    `label add --json`, `label remove --json`, `label delete --json`,
    `label generate --json`. Each gets an `_FS_SETUPS`/arrange tuple. (`label
    remove` reuses `label add`'s positional arg names `label_id`/`source_id`, so it
    needs no new `_JSON_CONTRACT_DUMMY_ARGS` keys in `test_cli_contract.py`.)
  - **`JSON_SUCCESS_WAIVED` (:664)** — none expected; only waive a label command
    here with a written rationale if it legitimately cannot emit on the success
    path (none of the above qualify).
  - **`JSON_ERROR_WAIVED` (:740, in THIS file — not test_json_error_exit.py)** —
    waive only if a label command's *error* path cannot route through the typed
    `--json` envelope; the CRUD commands all should, so expect none.
  - **Fake arrangement:** add a fake `client.labels` to the `_FS_SETUPS` fixtures
    so each command's facade call returns a canned value — e.g. `labels.list`/
    `labels.sources` → a small `[Label(...)]`/`[Source(...)]`, `labels.create`/
    `rename`/`set_emoji`/`add_sources`/`remove_sources` → a `Label`, `labels.delete`
    → `None`, `labels.generate` → `[Label(...)]`. Add `notebooklm.cli.label_cmd` to
    the mock-patch target list (~:240–249) so the fake `NotebookLMClient` is injected.
- `tests/unit/test_json_error_exit.py`: add label error cases to `JSON_ERROR_CASES`
  (:323) — at minimum a `LabelNotFoundError` case per lookup-bearing command
  (`sources`/`rename`/`emoji`/`add`/`remove`/`delete`) plus an ambiguous-name resolver error;
  arrange the fake `client.labels` to raise the matching exception. Add
  `notebooklm.cli.label_cmd` to its patch-target list (~:123–132).
- **Forward-only (good hygiene, won't RED):** `tests/_guardrails/test_no_module_shadowing.py`
  (`RENAMED_MODULES`/`CLICK_GROUPS_PUBLIC`) and `tests/_guardrails/test_cli_boundary.py`
  (`CLI_COMMAND_MODULES`) — add `label`/`label_cmd` for completeness, but these
  *under-audit* rather than fail, so the discovery loop won't catch them — edit
  proactively.
- **Verify:** `uv run pytest tests/unit/cli/test_cli_contract.py
  tests/unit/cli/test_services_boundary.py tests/unit/test_json_stdout_purity.py
  tests/unit/test_json_error_exit.py`

**Phase 3 exit:** full `uv run pytest` green; `pre-commit` clean.

---

## Phase 4 — Docs & doc-sync gates

- **Edit** `docs/rpc-reference.md` — 4 RPCs in the master table + payload sections
  (incl. `[[…]]` vs `[None,[…]]` envelopes).
- **Edit** `docs/python-api.md` — `client.labels` namespace (no root `LabelsAPI`).
- **Edit** `docs/stability.md` — new public surface + tier.
- **Edit** `docs/cli-reference.md` — `label` group + `source list --label`.
- **Edit `CLAUDE.md`** — add every new `src/notebooklm` module/package to the
  repository-structure map (`_label/`, `_label/params.py`, `_labels.py`,
  `_types/labels.py`, `_row_adapters/labels.py`, `cli/label_cmd.py`,
  `cli/services/label_listing.py`), else the CI step
  `scripts/check_claude_md_freshness.py` (not pytest) exits 1.
- **Verify:** `uv run pytest -m repo_lint`; `uv run pre-commit run --all-files`;
  **and the non-pytest CI steps**: `uv run python scripts/check_claude_md_freshness.py`,
  `uv run python scripts/check_docs_module_refs.py`,
  `uv run python tests/scripts/check_method_coverage.py`,
  `uv run python tests/scripts/check_cassettes_clean.py --strict --recursive`.

---

## Files-to-change map (per phase)

| Phase | Implementation files | Test/gate files |
|---|---|---|
| 1.1 | `rpc/types.py`, `_idempotency_policy.py`, `tests/fixtures/rpc_golden/*.json` (4), `scripts/check_rpc_health.py`, **`tests/scripts/check_method_coverage.py`** (1.1b: add 4 names to `PREEXISTING_GAPS` + tracking issue, M12) | `test_idempotency_registry.py`, `test_rpc_golden_payloads.py`, `test_rpc_health_coverage.py`, `test_rpc_types.py`, **`check_method_coverage.py`** (CI) |
| 1.2 | `_label/__init__.py`, `_label/params.py` | `tests/unit/test_label_params.py` |
| 1.3 | `_row_adapters/labels.py`, `_row_adapters/__init__.py` | `tests/unit/test_label_row.py` |
| 1.4 | `_types/labels.py` | `tests/unit/test_label_model.py` |
| 1.5 | `exceptions.py` (+raise ratchet ceiling) | `tests/unit/test_label_exceptions.py`, `tests/unit/test_exceptions.py`, `tests/_guardrails/test_module_size_ratchet.py` |
| 2.1 | `_labels.py` | `tests/unit/test_labels_api.py` (mock RpcCaller — unit tier) |
| 2.2 | `client.py`, `types.py`, `__init__.py`, `scripts/audit_public_api_compat.py` | `test_public_surface_manifest.py` (`_FROZEN_TYPES_ALL`+`_TOP_LEVEL_TYPE_EXPORTS`+`_TOP_LEVEL_EXCEPTION_EXPORTS`+`_TYPES_EXCEPTION_REEXPORTS`), `test_public_api_contract.py`, `test_public_api_behavior.py`, `test_public_api_compat_audit.py`, `tests/unit/test_types.py` (`_PUBLIC_MOVABLE_CLASSES`), `test_module_size_ratchet.py` (client.py) |
| 3.1 | `cli/services/label_listing.py` | `tests/unit/cli/test_label_listing.py` |
| 3.2 | `cli/label_cmd.py`, `cli/__init__.py`, `cli/grouped.py`, **`notebooklm_cli.py`** | `test_label_cmd.py`, `test_grouped.py`, `test_cli_boundary.py`, `test_cli_vcr_coverage.py` (add `label` to `GROUP_COVERAGE`). **Follow-up PR (maintainer auth):** record `tests/cassettes/*.yaml` + `tests/integration/cli_vcr/`, remove the 4 names from `PREEXISTING_GAPS` (added back in Phase 1.1b) |
| 3.3 | `cli/services/source_listing.py`, `cli/source_cmd.py` (+raise ratchet ceiling) | `tests/unit/cli/test_source_*`, `test_cli_rpc_envelope.py`, `test_module_size_ratchet.py` |
| 3.4 | (gate-table edits only) | `test_cli_contract.py` (+`cli_contract_baseline.json`), `test_services_boundary.py` (`GUARDED_PATHS`), `test_json_stdout_purity.py`, `test_json_error_exit.py`, `test_no_module_shadowing.py`, `test_cli_boundary.py` |
| 4 | `docs/rpc-reference.md`, `python-api.md`, `stability.md`, `cli-reference.md`, **`CLAUDE.md`** | `uv run pytest -m repo_lint`, `check_claude_md_freshness.py` (CI), `check_docs_module_refs.py` (CI) |

---

## Risks & mitigations

- **Exact-equality gates red mid-phase.** → §0 table names every list + entry;
  bundle coupled edits (1.1, 2.2) per commit.
- **Facade reach-in.** → inject `list_sources` callable. No guard edit needed —
  `_labels.py` is auto-covered by the globbed `_*.py` `_core`-reach-in guard (Phase 2.1).
- **CLI boundary.** → logic in `cli/services/label_listing.py`; `test_cli_boundary`.
- **`source list --label` count/row desync.** → filter inside the `fetch` closure,
  not after `prepare_list`.
- **UUID-shaped label name misresolved as id.** → disable full-id passthrough in
  `resolve_label_id` (id/prefix pass), then fall back to explicit exact-name matching
  (NOT `title_of`, which is diagnostics-only — see §3.1).
- **One source per `le8sX` call (confirmed).** The server honours only the first
  id of the add/remove group per call — a multi-id payload silently drops the rest
  (the bug the singular builder + per-id loop fixes). → builder is singular;
  `add_sources`/`remove_sources` loop per id; tests assert call-count == len(ids).
- **Combined add+remove in one call drops the add (confirmed).** → the API never
  sets both fieldmask groups in one call; add and remove are separate calls.
- **Unverified wire semantics** (delete already-absent; add_sources dedup;
  name-only emoji). → ship conservative defaults (api.md §15); post-merge
  capture items, do not block. (Source **removal** is now confirmed, not
  unverified — `remove_sources` ships it.)
- **`pre-commit` mutates files.** → treat as fix-then-verify; confirm clean tree.

## Rollback

Per-phase PR/commit-group. Revert Phase 3/4 leaves the API (Phase 2) usable;
revert Phase 2 removes the namespace cleanly. No data migration at any phase.

## Definition of done (convergence criteria)

1. All phases merged; `uv run pytest` (full, incl. `repo_lint`/guardrails) green,
   **and every `uv run` step in `.github/workflows/test.yml` green** (the non-pytest
   sweep — mypy, method-coverage, claude-md-freshness, cassette scans, etc.).
2. `uv run pre-commit run --all-files` clean (Ruff lint+format) **and** `uv run mypy
   src/notebooklm --ignore-missing-imports` clean (separate CI gate); tree clean after.
3. Every gate in §0 passes for the new surface.
4. `client.labels` (all methods) + CLI (`label …`, `source list --label`) covered
   at the tiers in the files-to-change map.
5. **Branch-coverage gate green:** `uv run pytest … --cov-fail-under=90`
   (`test.yml:215`) passes — the new modules carry full branch coverage, including
   the `sources()` concurrent-deletion race-skip path, `create`'s 0-new-id and
   >1-new-id `LabelError` branches, the row-adapter drift-raise branches, and the
   no-op `ValueError`s (`add_sources([])`, `update(name=None, emoji=None)`).
6. Docs updated; doc-sync gates pass.
7. api.md §15 open items filed as post-merge issues with conservative
   defaults shipped.
