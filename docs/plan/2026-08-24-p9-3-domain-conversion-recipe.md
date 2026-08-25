# P9.3 domain conversion recipe

**Status:** completed 2026-08-24 at `b9e1c7d3`. This file is the frozen execution recipe; its
slice-local counts describe P9.3 before P9.4 row conversion and the later P9.2 hoists.

The settings/suggestions PR was the pattern. Each subsequent domain PR converted every leaf the
gate table (`2026-08-24-p9-composite-gate-table.md` §7) listed as a codec row for that domain, one
domain per PR, and left composites, input-defaulting members and custom-row candidates as handlers.
Each step below was derivation, never a loosening of a guard.

## 1. Rows

- Create `src/notebooklm/_web/bindings/<domain>.py`. Every row is a **module-level assignment**
  `NAME = CodecBinding(definition=<OP>_DEF, encode=…, decode=…, native=NativeCallSpec.constant(RPCMethod.X, "variant"))`
  so `scripts/_operation_catalog_ast.py` sees it (`NAME` becomes the authority site
  `_web/bindings/<domain>.py:NAME`). Input-keyed handlers use `NativeCallSpec.keyed(selector, NativeChoice(...), ...)`;
  the selector must return one of the declared choices.
- `deadline=DeadlineMode.IGNORE` only where the handler passed `deadline=None` (`SOURCE_WAIT`);
  `forward_disable_internal_retries=True` only where the handler passed that kwarg; `map_error` only
  where the handler translated a raw native error semantically (`RESEARCH_START`).
- Export `<DOMAIN>_ROWS: Mapping[Operation, Binding]` keyed by `row.definition.key` and add it to
  `_DOMAIN_ROWS` in `src/notebooklm/_web/bindings/__init__.py`.

## 2. Codec helpers

- In the matching `_web/codec/<domain>.py`, add `encode_<operation>(value) -> CodecPayload`
  (params + `source_path` + `allow_null`/`raise_on_null_status`/`attempt_timeout` exactly as the
  handler passed them; never a method) and `decode_<operation>(value, raw)`. Keep the existing
  `encode_*`/`decode_*` functions — other callers and the golden tests use them.
- Add golden assertions for each new payload builder to the domain's codec test
  (`tests/unit/test_<domain>_codec.py`): params, `source_path`, option flags.

## 3. Registry and handlers

- Remove the converted entries from `_HANDLER_NAMES` in `_web/registry.py`; nothing else there
  changes (the partition check `supported == handler names ∪ rows` and the historical P9.3
  87-operation / 82-supported / 0-service-owned pins hold).
- Delete the handler methods from `_web/<domain>.py`. If the chain class is empty, delete it
  and re-link its neighbour's base (`class Next(Deleted)` → `class Next(DeletedBase)`), then
  update `REVIEWED_BACKEND_IMPORTS`, `docs/architecture.md` and the measurement test's chain pins.

## 4. Catalog

- Run `uv run python scripts/audit_operation_catalog.py`. Replace every hand-written
  `SHARED_RPC_AUTHORITY_RULES` / `RECENCY_CONTRACTS` site naming a deleted handler with the row
  site (`derive_row_authorities()` lists them); single-consumer natives derive automatically.
- Apply the exact `REVIEWED_BACKEND_IMPORTS` delta the audit prints (removed handler-module
  tuples, added `bindings/<domain>.py` and codec tuples).
- Regenerate the committed baseline: `uv run python scripts/regen_baselines.py`, then review the
  `tests/fixtures/baselines/operation_catalog.json` diff — only the converted operations' sites
  may change.

## 5. Inventories and readers

- `tests/_guardrails/test_semantic_p8_provider_boundary_audit.py::KNOWN_WEB_PACKAGE_FIRST_PARTY_IMPORTS`:
  add `_web.bindings.<domain>`.
- `tests/unit/test_operation_catalog.py` site lists that named a deleted handler.
- Handler-name readers already accept rows: `test_semantic_p4_convergence_characterization.py`
  asserts `handler_name xor row`; `test_semantic_deadline_seeding.py` walks only handler-backed
  composites; `test_binding_core.py` counts `resolved_handler_count`/`codec_count` from the
  registry — the pattern slice began at four rows and the P9.3 phase ended at 51. P9.4 and P9.2
  later moved the integrated table to 80 rows and zero handlers.
- `docs/architecture.md`: per-file index line + tree entry for the new module; adjust the
  `_web/<domain>.py` line.

## 6. Tests to add per domain (copy `tests/unit/test_web_binding_rows_settings.py`)

- rows replace handlers in registry and table; handler attributes are gone;
- identical keyword set reaches the runtime for every converted operation, including explicit
  `False`/`None` values and the row's `source_path`/`allow_null`;
- a `ServerError` translates to the same `BackendError` as before with `dispatched=True`;
- an `RPCTimeoutError` after expiry becomes `BackendDeadlineExceededError` with the same
  diagnostics; a pre-dispatch expiry is `dispatched=False`.

## 7. Verification

`uv run ruff format . && uv run ruff check .`; mypy; `scripts/audit_operation_catalog.py`; the PR
suite; the `repo_lint` suite; `scripts/measure_web_backend_chain.py` (handler names drop, binding
rows rise by the converted count).
