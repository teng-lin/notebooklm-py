# P9.4 custom-row conversion recipe

The sharing composites PR (P9.4a) is the pattern. A P9.4b domain PR converts every remaining
handler-backed composite of its domain into a `CustomBinding` row, deletes the emptied chain
class, and moves the ratchets by exactly the converted set. Each step is derivation, never a
loosening of a guard. `docs/plan/2026-08-24-p9-composite-gate-table.md` §4 fixes each row's
category; the P9.3 recipe (`2026-08-24-p9-3-domain-conversion-recipe.md`) still governs codec
helpers, inventories and goldens.

## 1. Row shape

- In `src/notebooklm/_web/bindings/<domain>.py`, a **module-level assignment**
  `NAME = CustomBinding(definition=<OP>_DEF, handler=_fn, native=(spec, ...), justification=...,
  category=..., error_mode=..., collaborators=(...))` so the catalog walker sees it.
- `native`: one `NativeCallSpec` per native the ledger (`WEB_CALL_POLICY_BINDINGS[op]`) declares,
  each with a unique `key=` (`"mutate"`, `"readback"`, `"probe"`, `"create"`, …); input-keyed
  specs use `NativeCallSpec.keyed(selector, NativeChoice(...), ..., key=...)`. `audit_row_bindings`
  compares the declared set with the ledger — correct the ledger (reviewed) rather than hide a
  mismatch behind `known_divergence`.
- `category` and a one-sentence `justification` come from gate table §4: *protocol* (the wire
  forces the sequence), *compatibility* (a public identity or raw swallow cannot yet be
  reproduced from records), *deferred-product* ("Hoist candidate P9.2-N per gate table §4;
  awaits the stop/go review").
- `error_mode`: `TRANSLATE` (default), `RAW_PASSTHROUGH` (the four source-add rows the head used
  to list by operation — remove them from `_RAW_PASSTHROUGH_HANDLER_OPERATIONS` in
  `_web/backend.py` when their rows land), `TRANSLATE_SCRUBBED` (`CHAT_ASK`). `map_error` only
  where the handler translated a raw native error semantically.

## 2. Handler body

- Signature `async def _fn(value, deadline, invoke: RowInvoker)`. Every native call goes through
  `await invoke.call(<spec key>, <CodecPayload>, deadline=deadline, disable_internal_retries=...,
  outcome_unknown_on_expiry=...)` (or `invoke.stream(...)`), with exactly the options the handler
  passed to `_rpc_call` for that phase. Per-phase payloads are `encode_*` helpers in the codec
  module returning `CodecPayload` (params, `source_path`, `allow_null`, `raise_on_null_status`,
  `attempt_timeout`) — never a method.
- Failures raised by the invoker are tagged with the selected spec (`binding_native` on the
  exception and its wrapped original); `map_error` runs on failure with that choice.
- Objects the handler needs beyond the transport are **declared** collaborators reached through
  `invoke.collaborator(name)`. The head provides exactly `ROW_COLLABORATOR_NAMES`
  (`source_uploader`, `deadline_factory`, `capture_public_failure`); extend that set and
  `WebRpcBackend._row_collaborators()` in the same PR if a row needs more (e.g. the note-backed
  mind-map services for the compatibility rows), and never expose the transport or the runtime.
  `DeadlineRpcCaller` is replaced by an invoker-backed caller that declares its `RPCMethod`s as
  the row's specs, so selected-spec attribution survives its rethrow (plan open item 2); the
  `SourceUploadPipeline` callbacks run through the `SOURCE_ADD_FILE` row's invoker (open item 1).
- Composites that today call a private helper on the chain (`_list_notebooks`,
  `_source_snapshot_records`, `_label_set_list`, …) inline the helper's call as a keyed spec or
  move the helper into the row module as a plain function over the invoker.

## 3. Chain deletion

- Delete the converted handler methods; when a chain class is empty delete it and re-link its
  neighbour's base (`class Next(Deleted)` → `class Next(DeletedBase)`). The MRO shrinks by one per
  emptied class; `WebRpcBackend` ends with no bases in P9.4c.
- Remove the converted operations from `_HANDLER_NAMES` in `_web/registry.py` (pins unchanged).

## 4. Ratchets (`tests/_guardrails/test_web_binding_ratchets.py`)

- `CUSTOM_ROW_COUNTS`: raise the converted rows' categories by exactly the converted count
  (`RESIDUAL_COMPOSITE_CEILING` is unchanged by a conversion; only a P9.2 hoist lowers it).
- `MULTI_CALL_HANDLER_ALLOWLIST`: remove every converted `file.py:Class.method` entry (a
  function bound as `handler=` of a `CustomBinding` is exempt by construction).
- `OVERSIZED_CLASS_CEILINGS`: tighten the shrunk class's ceiling, or remove the entry once it is
  under 500 body lines or deleted.

## 5. Catalog, inventories, docs

- `scripts/_operation_catalog_authorities.py`: point the converted operations' rules at the row
  site (`_web/bindings/<domain>.py:NAME`, discriminators unchanged); `derive_row_authorities()`
  lists them. Apply the exact `REVIEWED_BACKEND_IMPORTS` delta the audit prints; regenerate
  `tests/fixtures/baselines/operation_catalog.json` (`uv run python scripts/regen_baselines.py`)
  and confirm only the converted operations' sites changed.
- `KNOWN_WEB_PACKAGE_FIRST_PARTY_IMPORTS`: remove a deleted `_web.<module>`; `docs/architecture.md`
  index and tree: drop the deleted module, describe the rows.

## 6. Tests (copy `tests/unit/test_web_binding_rows_sharing_custom.py`)

- Registry partition (custom rows, categories, spec keys); phase sequence and the identical
  runtime kwargs per phase incl. explicit `False`/`None`; a failure in each phase is translated
  (or passed through raw, per `error_mode`), `dispatched`, and tagged with its spec; deadline
  projection (pre-dispatch expiry on a post-write phase is `outcome_unknown=True`,
  `dispatched=False`); collaborators declared vs provided; the domain's characterization test
  updated for the new partition; the unbound `_translate_error` call sites keep working.
