# P9.2 composite decomposition gate table

**Status:** review artifact for the P9.2 gate (entry criterion of
[the semantic backend refactor plan](2026-08-13-semantic-backend-refactor.md), "P9 — Decompose the
web backend"). Measured 2026-08-24 on `refactor/semantic-backend-dev` at `436573ba`. Every row is
derived from code, not from the plan's prose; where the code contradicts the plan the last section
says so. Nothing here is implemented.

Reading guide: §2 re-measures the 34 multi-native members by *maximum calls per input* and keeps
only the sequential ones as hoist scope; §3 is the table proper, one subsection per sequential row
with the plan's columns (a)–(i); §4 fixes each row's disposition and the PR order; §5 fixes the
primitive vocabulary; §6–§8 answer the plan's specific questions.

## 1. Method

The 82 active bindings and their native sets come from the reviewed ledger, not from handler
prose:

```python
from notebooklm._web.policy import WEB_CALL_POLICY_BINDINGS as L
from notebooklm._web.deadlines import SEMANTIC_DEADLINE_AUTHORITIES as D
from notebooklm._web.registry import _HANDLER_NAMES as H
multi = [o for o, b in L.items() if len(b.native_bindings) > 1]      # 34
single = [o for o, b in L.items() if len(b.native_bindings) == 1]    # 48
```

Maximum calls per input were counted by reading each handler named by `_HANDLER_NAMES` and every
private helper it reaches (`_web/backend.py`, `chat.py`, `source_variants.py`, `studio_facade.py`,
`settings_suggestions.py`, `research.py`, `sharing.py`, `labels.py`, `studio_data.py`,
`studio_media.py`, `studio_documents.py`), plus the transport-neutral services the handlers
delegate to (`_source/add.py`, `_source/batch.py`, `_source/upload.py`, `_note_service.py`,
`_idempotency.py`). A call is counted when one input can execute it; `idempotent_create` is
counted at its `max_attempts=2` bound. Test pins were located with `grep -rl` over `tests/` for
the operation's reason members and public message strings; backend-level tests were attributed
per operation by an AST walk over `tests/unit/test_*.py` that keeps test functions which call
`invoke(`, `._backend.`, `WebRpcBackend(` or `build_web_backend(` and name the operation or its
`*_DEF`.

## 2. Re-measurement of the 34 multi-native members

Classes: **single** (the handler issues one native; the ledger's second native is issued elsewhere),
**branch-exclusive** (one call per input, method chosen from the input), **sequential** (one input
can execute more than one call). Only *sequential* is hoist scope.

| Operation | Ledger natives | Max calls / input (as the code branches) | Class |
|---|---|---|---|
| `NOTEBOOK_CREATE` | LIST_NOTEBOOKS, CREATE_NOTEBOOK, GET_USER_SETTINGS | LIST (baseline) → CREATE ×≤2 with LIST (probe) ×≤2; on `RPCError` code 3: GET_USER_SETTINGS → LIST | sequential |
| `NOTEBOOK_UPDATE` | RENAME_NOTEBOOK, GET_NOTEBOOK | RENAME → GET | sequential |
| `SOURCE_ADD_URL` | GET_NOTEBOOK, ADD_SOURCE/url, UPDATE_SOURCE | finalize: UPDATE_SOURCE → [GET on null echo]; create: GET (baseline) → ADD ×≤2 with GET (probe) ×≤2 → [UPDATE_SOURCE → [GET]] | sequential |
| `SOURCE_ADD_URL_BATCH` | ADD_SOURCE/url, GET_NOTEBOOK | ADD → [GET reconciliation when the echo omits items] | sequential |
| `SOURCE_ADD_DRIVE` | ADD_SOURCE/drive, GET_NOTEBOOK, UPDATE_SOURCE | same shape as `SOURCE_ADD_URL` (no null-echo hydration) | sequential |
| `SOURCE_ADD_FILE` | ADD_SOURCE_FILE, GET_NOTEBOOK, GET_USER_SETTINGS, UPDATE_SOURCE | GET_USER_SETTINGS (limit) → GET (baseline) → ADD_SOURCE_FILE ×≤2 with GET (probe) → Scotty start/stream (not `batchexecute`) → GET × poll ticks → [UPDATE_SOURCE] | sequential |
| `SOURCE_UPDATE` | UPDATE_SOURCE, GET_NOTEBOOK | UPDATE_SOURCE → [GET when the echo is null] | sequential |
| `CHAT_ASK` | GET_NOTEBOOK, GET_LAST_CONVERSATION_ID | streamed POST → [GET_LAST_CONVERSATION_ID when `resolved_conversation_id is None`]. **GET_NOTEBOOK is never issued by the handler** (`_chat/api.py:278-279` resolves the default source set through `NotebooksAPI.get_source_ids` → `NOTEBOOK_GET`, above the port) | sequential |
| `CHAT_GET_HISTORY` | GET_LAST_CONVERSATION_ID, GET_CONVERSATION_TURNS | GET_CONVERSATION_TURNS only (`_web/chat.py:112-126`); the conversation is resolved by the facade through `CHAT_GET_CONVERSATION` | **single** |
| `CHAT_CONFIGURE` | GET_NOTEBOOK, RENAME_NOTEBOOK | `action is GET` → GET_NOTEBOOK, else RENAME_NOTEBOOK | branch-exclusive |
| `LABEL_CREATE` | LIST_LABELS, CREATE_LABEL | LIST (baseline) → CREATE_LABEL (full-set echo) | sequential |
| `LABEL_UPDATE` | LIST_LABELS, UPDATE_LABEL (∅, add_sources, remove_sources) | membership: UPDATE_LABEL/add_sources × n → UPDATE_LABEL/remove_sources × m → LIST (mandatory readback); field: LIST (preflight) → UPDATE_LABEL → [LIST if `return_object`] | sequential |
| `COLLECTION_CREATE` | LIST_LABELS, CREATE_LABEL | LIST → CREATE_LABEL → LIST | sequential |
| `COLLECTION_UPDATE` | LIST_LABELS, UPDATE_LABEL (∅, add_notebooks, remove_notebooks) | as `LABEL_UPDATE` on the account path; name-only mask rejected before any call | sequential |
| `NOTEBOOK_SUGGEST_PROMPTS` | GET_NOTEBOOK, SUGGEST_PROMPTS | [GET if `source_ids is None`] → SUGGEST_PROMPTS | sequential (input-defaulting) |
| `ARTIFACT_LIST` | LIST_ARTIFACTS, GET_NOTES_AND_MIND_MAPS | LIST_ARTIFACTS → [GET_NOTES_AND_MIND_MAPS if `family in {None, "mind_map"}`, via `LegacyNoteBackedService`] | sequential |
| `ARTIFACT_GET` | LIST_ARTIFACTS, GET_NOTES_AND_MIND_MAPS | LIST_ARTIFACTS → GET_NOTES_AND_MIND_MAPS | sequential |
| `ARTIFACT_GENERATE_{AUDIO,QUIZ,FLASHCARDS,VIDEO,REPORT,INFOGRAPHIC,SLIDE_DECK,DATA_TABLE}` (8) | GET_NOTEBOOK, CREATE_ARTIFACT | [GET if `source_ids is None`] → CREATE_ARTIFACT | sequential (input-defaulting) |
| `ARTIFACT_GENERATE_MIND_MAP` | GET_NOTEBOOK, GENERATE_MIND_MAP, CREATE_NOTE/plain, UPDATE_NOTE, DELETE_NOTE | [GET] → GENERATE_MIND_MAP → [CREATE_NOTE → UPDATE_NOTE (→ DELETE_NOTE on cancel) when a tree came back] | sequential |
| `ARTIFACT_RENAME` | RENAME_ARTIFACT, LIST_ARTIFACTS | RENAME_ARTIFACT → LIST_ARTIFACTS (no mind-map merge) | sequential |
| `ARTIFACT_DOWNLOAD` | LIST_ARTIFACTS, GET_NOTES_AND_MIND_MAPS, GET_INTERACTIVE_HTML | one of three by `value.action` | branch-exclusive |
| `MIND_MAP_GENERATE_NOTE` | GET_NOTEBOOK, GENERATE_MIND_MAP | [GET] → GENERATE_MIND_MAP | sequential (input-defaulting) |
| `MIND_MAP_GENERATE_INTERACTIVE` | GET_NOTEBOOK, CREATE_ARTIFACT | [GET] → CREATE_ARTIFACT | sequential (input-defaulting) |
| `SHARING_SET_PUBLIC` | SHARE_NOTEBOOK, GET_SHARE_STATUS | SHARE_NOTEBOOK → GET_SHARE_STATUS | sequential |
| `SHARING_SET_VIEW_LEVEL` | RENAME_NOTEBOOK, GET_SHARE_STATUS | RENAME_NOTEBOOK (view-level mask) → GET_SHARE_STATUS | sequential |
| `SHARING_UPDATE_USERS` | SHARE_NOTEBOOK, GET_SHARE_STATUS | SHARE_NOTEBOOK → GET_SHARE_STATUS | sequential |
| `RESEARCH_START` | START_FAST_RESEARCH, START_DEEP_RESEARCH | one of two by `value.mode` | branch-exclusive |

**Counts.** 34 multi-native by ledger = **30 sequential + 3 branch-exclusive + 1 single**. The
plan's "2 of the 34 are `BRANCH_EXCLUSIVE`" is the deadline ledger's count (`CHAT_CONFIGURE`,
`ARTIFACT_DOWNLOAD`); `RESEARCH_START` is a third input-keyed row that the deadline ledger does not
list because it has one syntactic `_rpc_call` site. Of the 30 sequential, **11 are input-defaulting
members** kept adapter-owned by contract 1 (the eight `ARTIFACT_GENERATE_*` above, the two
`MIND_MAP_GENERATE_*`, and `NOTEBOOK_SUGGEST_PROMPTS`) and one, `ARTIFACT_GENERATE_MIND_MAP`, is
input-defaulting *and* persists through the legacy note seam. That leaves **18 rows** in §3 that
are genuine hoist candidates, of which §4 hoists 11.

## 3. The table proper — sequential rows outside contract 1

Column key: (a) native sequence, (b) leaf members per native (new primitives in **bold**),
(c) backend-identity argument, (d) new neutral `BackendErrorReason`, (e) pinned error identities /
messages, (f) raw native exception types caught or leaked, (g) input-defaulting (contract 1),
(h) primitives introduced, (i) count deltas and consumer set. `SL` = supported, `SO` =
`service_owned`, `OP` = `len(Operation)`.

### 3.1 `LABEL_UPDATE` — `_web/labels.py:_label_update`

- (a) membership (`add_member_ids or remove_member_ids`): `UPDATE_LABEL/add_sources` once per
  added id, then `UPDATE_LABEL/remove_sources` once per removed id, each with
  `outcome_unknown_on_expiry=write_may_have_committed` (false for the first write only), then a
  **mandatory** `LIST_LABELS` readback whose miss raises not-found and whose record is returned only
  when `return_object`. Field path: `LIST_LABELS` preflight (miss → not-found), `UPDATE_LABEL`
  with `emoji` carried through from the preflight when only `name` is set, then `LIST_LABELS`
  readback **only when** `return_object`.
- (b) `LIST_LABELS` → `LABEL_GET` (existing leaf; `_label_set_get` is list-then-select).
  `UPDATE_LABEL/*` → **`LABEL_MUTATE`** (one call per member).
- (c) A second backend would run the same loop: the per-id write is a server rule ("only the first
  id of a set-op group is honoured", docstring at `labels.py:_label_update`), not a `batchexecute`
  artefact, and the readback is the only existence evidence because `le8sX` echoes `[]`. No
  protocol-variance claim applies. Strongest hoist argument in the table.
- (d) None. The workflow raises `LABEL_NOT_FOUND` from its own readback result.
- (e) `LABEL_NOT_FOUND` → `LabelNotFoundError`/`CollectionNotFoundError` by kind
  (`tests/unit/test_semantic_labels_collections_slice_characterization.py:377-410`); reason listed
  in the closed set at `tests/unit/test_web_backend.py:2014`; expiry-after-write truth table at
  `tests/unit/test_semantic_outcome_unknown_readback.py:213` (`LABEL_UPDATE_DEF`) and the
  read-only-preflight case at `:269`. No test pins the message text "Label not found".
- (f) None — no `except` in the handler; every native error reaches `invoke()`'s translator.
- (g) No.
- (h) **`LABEL_MUTATE`**: input `LabelMutateInput(kind, label_id, notebook_id | None, name,
  emoji, add_member_id | None, remove_member_id | None)` (exactly one of name/emoji-mask,
  add, remove), output `LabelMutateResult()`; `CallPolicy.MUTATION`; natives
  `(UPDATE_LABEL, None | add_sources | remove_sources | add_notebooks | remove_notebooks)`
  selected by `NativeCallSpec.select(input)` from `(kind, which field is set)`; cardinality one
  call per member; call sites `LabelSetService.update` (`_label_service.py:165-200`) for both
  kinds. Shared with `COLLECTION_UPDATE` → **foundational**.
- (i) Foundational PR: OP +1, SL +1, SO 0. Hoist PR: SL −1, SO +1; `KNOWN_ACTIVE_SEMANTIC_OPERATIONS`
  drops `LABEL_UPDATE`, gains `LABEL_MUTATE`. Leaf conjunction row: `{LABEL_GET, LABEL_MUTATE}`.

### 3.2 `COLLECTION_UPDATE` — `_web/labels.py:_collection_update`

- (a) As 3.1 with `add_notebooks`/`remove_notebooks` on the account path; `name is None` on the
  field path raises `BackendContractError` before any call ("no emoji-only field mask").
- (b) `LIST_LABELS` → `COLLECTION_GET`; `UPDATE_LABEL/*` → **`LABEL_MUTATE`**.
- (c) Same as 3.1.
- (d) None.
- (e) `test_semantic_labels_collections_slice_characterization.py::test_collection_update_has_no_emoji_only_field_mask`
  and `::test_backend_rejects_a_request_addressed_to_the_other_dialect` (dialect guard: the
  service keeps `_require_label_kind`'s contract error); reasons as 3.1.
- (f) None.
- (g) No.
- (h) None new (consumes `LABEL_MUTATE`).
- (i) SL −1, SO +1. Leaf conjunction `{COLLECTION_GET, LABEL_MUTATE}`.

### 3.3 `SOURCE_UPDATE` — `_web/source_variants.py:_source_update`

- (a) `UPDATE_SOURCE` with `allow_null=True`; if the echo is truthy, decode and return (record only
  when `return_object`); else `GET_NOTEBOOK` snapshot with `outcome_unknown_on_expiry=True`,
  select by id, miss → `SOURCE_NOT_FOUND`.
- (b) `UPDATE_SOURCE` → **`SOURCE_PATCH_TITLE`**; `GET_NOTEBOOK` → `SOURCE_GET` (existing leaf,
  same native, same recency side effect; after the hoist `_source_get` loses its
  `operation=`/`outcome_unknown_on_expiry=` parameters).
- (c) Same sequence on any backend whose rename echo may be sparse or null; the null-echo
  hydration is a server behaviour, not a wire-grammar one.
- (d) None.
- (e) `SOURCE_NOT_FOUND` in the closed set (`test_web_backend.py:2018`); recorded-kwargs pins in
  `tests/unit/test_semantic_source_variants.py::test_simple_web_bindings_preserve_shapes_and_null_echo_recency`
  (`allow_null`, null-echo recency read). Public message "Source not found: <id>" is pinned only
  through the CLI/integration layers (`tests/unit/cli/test_source.py`,
  `tests/integration/test_sources_integration.py`), which the compat projector reproduces from
  `diagnostics["source_id"]`.
- (f) None.
- (g) No.
- (h) **`SOURCE_PATCH_TITLE`**: input `SourcePatchTitleInput(notebook_id, source_id, new_title)`,
  output `SourcePatchTitleResult(source: SourceRecord | None)` (`None` on a null echo);
  `CallPolicy.MUTATION`; native `(UPDATE_SOURCE, None)` constant; one call; call site
  `SourceService.update` (`_source_service.py:217`). The add-url/drive/file rename callbacks keep
  calling `UPDATE_SOURCE` through their own custom rows' declared specs (§3.10–3.13), not through
  this member. Single consumer.
- (i) OP +1, SL +1 −1 = 0, SO +1. Leaf conjunction `{SOURCE_PATCH_TITLE, SOURCE_GET}`.

### 3.4 `SHARING_SET_PUBLIC` — `_web/sharing.py:_sharing_set_public`

- (a) `SHARE_NOTEBOOK` (visibility params, `allow_null=True`) → `GET_SHARE_STATUS` with
  `outcome_unknown_on_expiry=True`.
- (b) `SHARE_NOTEBOOK` → **`SHARING_MUTATE`**; `GET_SHARE_STATUS` → `SHARING_GET`.
- (c) Mutate-then-readback; the readback exists because `SHARE_NOTEBOOK` echoes nothing useful.
  Backend-independent.
- (d) None.
- (e) No backend-level test names the operation (attribution walk: 0);
  `tests/unit/test_semantic_sharing_slice_characterization.py` carries 7 recorded-kwargs lines
  (readback `outcome_unknown_on_expiry`, `allow_null`) and a handler-name assertion at `:143`
  that becomes row-based.
- (f) None.
- (g) No.
- (h) **`SHARING_MUTATE`**: input `SharingMutateInput(notebook_id, mutation)` where `mutation` is
  a closed union `SharingVisibility(public: bool) | SharingGrants(grants, notify,
  welcome_message)`; output `SharingMutateResult()`; `CallPolicy.MUTATION`; native
  `(SHARE_NOTEBOOK, None)` constant, `PROBE_THEN_CREATE` retry class retained on the ledger row;
  one call; call sites `SharingService.set_public` (`_sharing_service.py:75`), `set_users`
  (`:138`), `remove_user` (`:189`). Shared by two workflows → **foundational**.
- (i) Foundational PR: OP +1, SL +1. Hoist: SL −1, SO +1. Leaf conjunction
  `{SHARING_MUTATE, SHARING_GET}`.

### 3.5 `SHARING_UPDATE_USERS` — `_web/sharing.py:_sharing_update_users`

- (a) `SHARE_NOTEBOOK` (grants params) → `GET_SHARE_STATUS` (`outcome_unknown_on_expiry=True`).
- (b)–(g) As 3.4. `remove_user` and `set_users` both invoke this workflow today
  (`_sharing_service.py:138,189`); both become `invoke(SHARING_MUTATE)` + `invoke(SHARING_GET)`.
- (h) None new.
- (i) SL −1, SO +1.

### 3.6 `SHARING_SET_VIEW_LEVEL` — `_web/sharing.py:_sharing_set_view_level`

- (a) `RENAME_NOTEBOOK` with the view-level field mask
  (`codec/sharing.py:build_share_view_level_params`) → `GET_SHARE_STATUS` decoded with
  `view_level=value.view_level`.
- (b) `RENAME_NOTEBOOK` (view mask) → **`SHARING_PATCH_VIEW_LEVEL`**; readback → `SHARING_GET`.
- (c) Backend-independent, with one data fact the service must own: the wire does not carry the
  view level — `decode_share_status` defaults it to `FULL_NOTEBOOK` and the composite overrides it
  from the input (`codec/sharing.py:268`). After the hoist the service does
  `replace(status, view_level=value.view_level)` on the `SHARING_GET` result.
- (d) None.
- (e) As 3.4.
- (f) None.
- (g) No.
- (h) **`SHARING_PATCH_VIEW_LEVEL`**: input `(notebook_id, view_level: ShareViewScope)`, output
  `()`; `CallPolicy.MUTATION`; native `(RENAME_NOTEBOOK, None)` constant; one call; call site
  `SharingService.set_view_level` (`_sharing_service.py:90`). Single consumer. (Alternative
  considered: one `NOTEBOOK_PATCH` member with a mask union covering title/emoji, view level and
  chat settings. Rejected: `CHAT_CONFIGURE` stays an input-keyed row, and a sharing service
  should not import notebook-property vocabulary; two members, one native, is the plan's
  "`(operation, allowed_variants)` edge" shape.)
- (i) OP +1, SL 0, SO +1.

### 3.7 `LABEL_CREATE` — `_web/labels.py:_label_create`

- (a) `LIST_LABELS` baseline → `CREATE_LABEL` (manual params, `allow_null=True`) whose echo is the
  full post-operation set → id-diff against the baseline; ≠ 1 new id raises
  `LABEL_AMBIGUOUS_CREATE`.
- (b) `LIST_LABELS` → `LABEL_LIST`; `CREATE_LABEL` → **`LABEL_ALLOCATE`** (not `LABEL_GENERATE`:
  same native, incompatible contract — auto-grouping vs manual allocation, `_NO_RETRY` vs
  `_IDEMPOTENT` retry class on the ledger).
- (c) Baseline-diff attribution is a product rule ("never by name"); backend-independent.
- (d) None (the service raises `LABEL_AMBIGUOUS_CREATE` itself).
- (e) Message `expected exactly 1 new label, found 2` pinned through `LabelError`
  (`test_semantic_labels_collections_slice_characterization.py:439`); reason mapping `:379`;
  closed set `test_web_backend.py:2013`. The service must build the identical message
  (`_reconcile_created_label` moves with it).
- (f) None.
- (g) No.
- (h) **`LABEL_ALLOCATE`**: input `LabelAllocateInput(kind, notebook_id | None, name, emoji)`,
  output `LabelAllocateResult(echo: tuple[LabelRecord, ...])` — the decoded echo for source labels,
  `()` for collections (whose echo shape was never captured; §3.8 re-lists); `CallPolicy.MUTATION`;
  native `(CREATE_LABEL, None)` constant, `NON_IDEMPOTENT_NO_RETRY`; one call; call sites
  `LabelSetService.create` for both kinds (`_label_service.py:52-70`). Shared → **foundational**.
- (i) Foundational PR: OP +1, SL +1. Hoist: SL −1, SO +1. Leaf conjunction
  `{LABEL_LIST, LABEL_ALLOCATE}`.

### 3.8 `COLLECTION_CREATE` — `_web/labels.py:_collection_create`

- (a) `LIST_LABELS` → `CREATE_LABEL` (collection params; echo discarded) → `LIST_LABELS` with
  `outcome_unknown_on_expiry=True` → id-diff.
- (b) `COLLECTION_LIST`, **`LABEL_ALLOCATE`**, `COLLECTION_LIST`.
- (c) As 3.7.
- (d) None.
- (e) `expected exactly 1 new collection, found 0` (`…_characterization.py:443`);
  expiry-after-write case `test_semantic_outcome_unknown_readback.py:213`
  (`COLLECTION_CREATE_DEF`).
- (f) None.
- (g) No.
- (h) None new.
- (i) SL −1, SO +1.

### 3.9 `ARTIFACT_RENAME` — `_web/studio_facade.py:_artifact_rename`

- (a) `RENAME_ARTIFACT` (`allow_null=True`) → `LIST_ARTIFACTS` via `_artifact_catalog_records(...,
  include_mind_maps=False, outcome_unknown_on_expiry=True)` → select by id, miss →
  `ARTIFACT_NOT_FOUND`.
- (b) `RENAME_ARTIFACT` → **`ARTIFACT_PATCH_TITLE`** (`MIND_MAP_UPDATE` sends the identical
  `[[id, title], [["title"]]]` payload but is a different member with a different input record;
  contract 1 forbids reuse); `LIST_ARTIFACTS` → **`ARTIFACT_CATALOG`** (a plain catalog read;
  `ARTIFACT_LIST` is not a substitute because it merges note-backed maps through the raw-swallow
  seam in §3.14, and `ARTIFACT_WAIT` returns one task's status, not the catalog).
- (c) Mutate-then-readback; backend-independent.
- (d) None.
- (e) `test_web_backend.py::test_artifact_rename_missing_target_uses_closed_backend_reason` (`:530`);
  closed set `:2007`; public "Artifact not found: <id>" through CLI (`tests/unit/cli/test_artifact.py:428-502`)
  and `tests/unit/test_exceptions.py:993`, reproduced by the projector from
  `diagnostics["artifact_id"]`.
- (f) None.
- (g) No.
- (h) **`ARTIFACT_PATCH_TITLE`**: `(notebook_id, artifact_id, new_title)` → `()`, `MUTATION`,
  `(RENAME_ARTIFACT, None)`, one call. **`ARTIFACT_CATALOG`**: `(notebook_id)` →
  `ArtifactCatalogResult(artifacts: tuple[ArtifactRecord, ...])`, `READ`, `(LIST_ARTIFACTS, None)`,
  one call. Call site `StudioManagementService.rename` (`_studio/management.py:47`). Both
  single-consumer today; `ARTIFACT_CATALOG` is the read the §3.14 rows would reuse if they ever
  hoist.
- (i) OP +2, SL +2 −1 = +1, SO +1.

### 3.10 `NOTEBOOK_UPDATE` — `_web/backend.py:_notebook_update`

- (a) `RENAME_NOTEBOOK` (title/emoji mask, `allow_null=True`) → `GET_NOTEBOOK` with
  `outcome_unknown_on_expiry=True`, decoded with chat settings; an empty row or a blank record
  raises `NOTEBOOK_NOT_FOUND`; a `ClientError` whose status normalises to `NOT_FOUND` is
  translated to `NOTEBOOK_NOT_FOUND` with `detail`, `original_message`, `rpc_code` diagnostics
  (`backend.py:807-829`); any other `ClientError` is re-raised.
- (b) `RENAME_NOTEBOOK` → **`NOTEBOOK_PATCH`**; `GET_NOTEBOOK` → `NOTEBOOK_GET` (existing leaf,
  `include_notebook=True`, same native and side effect).
- (c) Backend-independent sequence.
- (d) **Decision required.** `NOTEBOOK_GET` today lets the `NOT_FOUND` `ClientError` through as
  reason `CLIENT` and the *facade* translates it (`_notebooks.py:326-349`); the composite translates
  it *below* the port. Three ways to keep `NOTEBOOK_UPDATE`'s identity after the hoist:
  (1) a `map_error` on the `NOTEBOOK_GET` row that maps `NOT_FOUND` to `NOTEBOOK_NOT_FOUND` for
  every caller — changes `notebook.get`'s error path, which `tests/unit/test_get_or_none.py:215-236`
  pins down to the chained `ClientError` cause and `rpc_code == 5`; (2) the service recognises
  `reason is CLIENT` plus a status code — names wire vocabulary in a service; (3) a neutral
  `BackendErrorReason.NOT_FOUND` emitted by `map_error` only on a readback-specific leaf. None is
  free; (1) is cleanest if the compat projector's `NOTEBOOK_NOT_FOUND` branch
  (`_backend_compat.py:335-365`) is shown to reproduce the `test_get_or_none` chain. This row is
  ordered after every row that needs no such decision.
- (e) `test_web_backend.py::test_notebook_title_update_mutates_then_reads_back` (3 kwargs lines),
  `::test_notebook_update_readback_not_found_preserves_public_error_context`,
  `tests/unit/test_semantic_deadline_seeding.py` (2 tests, deadline identity across both natives),
  `tests/unit/test_semantic_p4_convergence_characterization.py::test_web_rpc_backend_passes_single_deadline_without_nested_resets`,
  `tests/unit/test_backend_compat.py:369-371` ("Notebook not found: missing" + reason).
- (f) **Catches `ClientError`** (raw) on the readback — a leaf-level translation, not a swallow.
  Per the plan's rule this keeps the row adapter-owned until the catch is expressed on the leaf
  (option (1)/(3) above).
- (g) No.
- (h) **`NOTEBOOK_PATCH`**: `(notebook_id, title | None, emoji | None)` → `()`, `MUTATION`,
  `(RENAME_NOTEBOOK, None)`, one call, call site `NotebookMutationService.update`
  (`_notebook_mutation_service.py:57`). Single consumer.
- (i) OP +1, SL 0, SO +1.

### 3.11 `NOTEBOOK_CREATE` — `_web/backend.py:_notebook_create`

- (a) `LIST_NOTEBOOKS` baseline (any failure → baseline unavailable, warning) →
  `idempotent_create(create, probe)`: `CREATE_NOTEBOOK` with `disable_internal_retries=True`; on
  a transport-class failure, `LIST_NOTEBOOKS` probe filtered by baseline (unique new title →
  adopt; unavailable baseline with matches, or >1 match → `mark_unconfirmed(RPCError(...))`; none →
  retry create once); on `RPCError` with `rpc_code == 3` from `CREATE_NOTEBOOK`:
  `GET_USER_SETTINGS` → `LIST_NOTEBOOKS`, and if `owned ≥ limit − 1` raise `NOTEBOOK_LIMIT`, else
  re-raise the original.
- (b) `LIST_NOTEBOOKS` → `NOTEBOOK_LIST`; `CREATE_NOTEBOOK` → **`NOTEBOOK_ALLOCATE`**;
  `GET_USER_SETTINGS` → `SETTINGS_GET_LIMITS` (existing leaf, returns `notebook_limit`).
- (c) Snapshot/create/probe/reconcile is the ADR-0005 product policy and would run on any backend
  whose create is not idempotent. Backend-independent.
- (d) The quota branch keys on a wire code (`_CREATE_NOTEBOOK_QUOTA_RPC_CODE = 3`). A service may
  not read it; the `NOTEBOOK_ALLOCATE` row's `map_error` must expose it neutrally — recommended:
  keep reason `RPC` (so the not-at-limit re-raise keeps today's public projection) and add a
  neutral diagnostic `quota_rejection: True`; the service diagnoses on that flag. No new reason.
- (e) `test_backend_compat.py:381-383` ("notebook limit reached", `NOTEBOOK_LIMIT`);
  `test_web_backend.py` ×4 (`::test_notebook_create_uses_baseline_and_disables_executor_retries`,
  `::…_adopts_unique_baseline_diff_after_transport_loss`,
  `::…_marks_neutral_probe_failure_unconfirmed` (`failure.unconfirmed is True`, `:1715`),
  `::…_reconciliation_deadline_keeps_parent_attribution`);
  `tests/unit/test_operation_catalog.py::test_notebook_create_catalog_has_no_phantom_get_notebook_recency`;
  "disambiguate" through the public facade (`tests/unit/test_notebook_api.py:465`,
  `tests/integration/concurrency/test_idempotency_create.py:405,501`, `pytest.raises(RPCError, …)`
  — the service must raise reason `RPC` with `outcome_unknown=True` and the same message).
- (f) **Catches raw** `RPCError` (quota branch), `AuthError | RateLimitError | ServerError |
  NetworkError` (probe, `mark_unconfirmed`), `RPCTimeoutError` (probe re-raise), `Exception`
  (baseline and probe wrap). After the hoist these become `may_have_committed(error)` /
  `mark_backend_outcome_unknown` over `BackendError`; the probe's own wrapped errors are raised as
  `BackendError(reason=RPC, outcome_unknown=True)`. Most raw catches of any row → **last** in
  hoist order.
- (g) No.
- (h) **`NOTEBOOK_ALLOCATE`**: `(title)` → `NotebookAllocateResult(notebook: NotebookRecord)`,
  `MUTATION`, `(CREATE_NOTEBOOK, None)`, `PROBE_THEN_CREATE` retained on the ledger row,
  `forward_disable_internal_retries=True`, one call per attempt; call site
  `NotebookMutationService.create` (`_notebook_mutation_service.py:40`). Single consumer.
- (i) OP +1, SL 0, SO +1. Leaf conjunction `{NOTEBOOK_LIST, NOTEBOOK_ALLOCATE, SETTINGS_GET_LIMITS}`.

### 3.12 `SOURCE_ADD_URL` — `_web/source_variants.py:_source_add_url`

- (a) See §2. The create/probe loop lives in `SourceAddService.add_url` (`_source/add.py:135-436`);
  the rename is `honor_requested_title[_if_fresh]`.
- (b) Would need `SOURCE_CREATE_URL` (ADD_SOURCE/url) and `SOURCE_PATCH_TITLE`; not introduced.
- (c) **Protocol-variance clause applies**: source registration has a tentative-source mobile
  variant (plan, principle 2). Stays adapter-owned regardless of (f).
- (d) n/a.
- (e) `invoke()`'s raw re-raise for the four source-add rows (`backend.py:455-464`); receipts and
  `SourceAddCommitState` pinned in `test_web_backend.py` ×5 and
  `tests/unit/test_semantic_source_variants.py::test_waited_url_title_finalize_keeps_add_attribution_and_null_hydration`.
- (f) `honor_requested_title` catches raw `(RPCError, NetworkError)` (`add.py:84`);
  `SourceAddService._create` catches `RPCError` → `SourceAddError` (`add.py:273-276`); `_probe`
  catches the transport tuple and `Exception` (`add.py:327-360`); the handler catches
  `NotebookLMError` → `BackendError(reason=SOURCE_ADD)` (`source_variants.py:361-384`).
- (g) No. (h) None. (i) No change. **Disposition: adapter-owned (protocol) custom row**, specs
  `{GET_NOTEBOOK, ADD_SOURCE/url, UPDATE_SOURCE}`, `error_mode=RAW_PASSTHROUGH`.

### 3.13 `SOURCE_ADD_URL_BATCH`, `SOURCE_ADD_DRIVE`, `SOURCE_ADD_FILE`

Same disposition as 3.12 for the same reasons:

- `SOURCE_ADD_URL_BATCH`: `SourceBatchAddService.add_urls` catches raw `AuthError`, the transport
  tuple (rewrites `exc.args` to the "UNRESOLVED — do not blindly retry" text and marks
  unconfirmed), `DecodingError`, `RPCError` by grpc status (`_source/batch.py:157-185`). Specs
  `{ADD_SOURCE/url, GET_NOTEBOOK}`. Pinned by
  `test_semantic_source_variants.py::test_batch_web_binding_is_one_shot_and_reconciles_omissions_once`.
- `SOURCE_ADD_DRIVE`: as 3.12 with `hydrate_on_null=False` (`add.py:484-770`). Specs
  `{ADD_SOURCE/drive, GET_NOTEBOOK, UPDATE_SOURCE}`. Pinned by
  `test_semantic_source_variants.py::test_waited_drive_title_finalize_keeps_add_attribution_without_null_hydration`.
- `SOURCE_ADD_FILE`: the whole upload workflow (`_source/upload.py:391-535`, registration
  `:573-660`); raw catches of `Exception` (post-register partial failure), the transport tuple,
  `(RPCError, NetworkError)` (rename); Scotty legs are not `batchexecute` calls; `deadline` is
  deleted on entry (`WORKFLOW_OWNED`). Specs `{ADD_SOURCE_FILE, GET_NOTEBOOK, GET_USER_SETTINGS,
  UPDATE_SOURCE}`; the uploader's three callbacks must execute through this row's `RowInvoker`
  (plan open item 1). Pinned by four `test_semantic_source_variants.py` tests. **Protocol** category
  (file upload).

`SOURCE_ADD_TEXT` (single-native by ledger) is also not a codec row: the handler wraps
`SourceAddService.add_text`, which catches raw `RPCError` → `SourceAddError` (`add.py:462-469`),
and `invoke()` re-raises it raw. It is a **custom row (compatibility)** with spec
`{ADD_SOURCE/text}` and `error_mode=RAW_PASSTHROUGH`; it can become a codec row once the
`SourceAddError` wrap is expressible as `map_error`.

### 3.14 `ARTIFACT_LIST`, `ARTIFACT_GET` — `_web/backend.py:_artifact_catalog_records`

- (a) `LIST_ARTIFACTS` (`allow_null=True`), then when mind maps are included
  `NoteBackedMindMapService(LegacyNoteBackedService(DeadlineRpcCaller(self, deadline, operation)))
  .list_mind_maps` → `GET_NOTES_AND_MIND_MAPS`; `DecodingError` re-raised; **`(RPCError,
  httpx.HTTPError)` swallowed** into a warning and a partial catalog (`backend.py:1035-1043`).
- (b) `ARTIFACT_CATALOG` (§3.9) and `MIND_MAP_LIST` would be the leaves.
- (c) Backend-independent merge, but the swallow set is raw (`httpx.HTTPError` reaches here only
  because a failed auth refresh passes through `invoke()` untranslated — plan §P9.2 "leak").
- (d) Would need the swallow set expressed in neutral reasons; not defined until the leak is
  translated at `WebTransport.call` (its own reviewed PR).
- (e) "Failed to fetch mind maps" warning pinned at
  `tests/unit/test_semantic_compatibility_regressions.py:81` (direct composite call); `tests/unit/test_artifact_listing.py`
  shapes; the `json_envelope` evidence tuple naming `backend.py:note = await LegacyNoteBackedService`
  (plan acceptance criteria).
- (f) Raw catch of `RPCError` and `httpx.HTTPError` → **adapter-owned (compatibility)** custom
  rows, specs `{LIST_ARTIFACTS, GET_NOTES_AND_MIND_MAPS}`, `DeadlineRpcCaller` adapted through the
  `RowInvoker` with both methods declared.
- (g) No. (h) None. (i) No change.

### 3.15 `ARTIFACT_GENERATE_MIND_MAP` — `_web/studio_data.py:_mind_map_generate`

- (a) `[GET_NOTEBOOK]` → `GENERATE_MIND_MAP` → if a leaf came back,
  `_persist_generated_mind_map` → `LegacyNoteBackedService.create_note` through `DeadlineRpcCaller`:
  `CREATE_NOTE/plain` → shielded `UPDATE_NOTE` → best-effort `DELETE_NOTE` on cancel
  (`_note_service.py:281-436`).
- (b) The persistence leg already has a service-side twin: `MIND_MAP_GENERATE_NOTE` is invoked
  from `NoteService.generate_mind_map` (`_note_service.py:613-655`), which then persists through
  `NOTE_CREATE`/`NOTE_UPDATE`/`NOTE_DELETE` above the port. A hoist would reuse that path.
- (c) Backend-independent, but two facts block it now: `DeadlineRpcCaller` converts
  `BackendDeadlineExceededError` into an **unchained** `RPCTimeoutError` (`_web/deadline_rpc.py:62-72`;
  plan open item 2), and the semantic `NoteService.create_note` is invoked with `deadline=None`
  in the twin path, so the composite's single deadline identity is not yet threaded.
- (d) None. (e) `tests/unit/test_semantic_data_views.py::test_mind_map_generate_persists_json_through_plain_note_variant`,
  `::test_mind_map_absent_leaf_preserves_empty_success`. (f) `LegacyNoteBackedService` raises raw
  `RPCError` on a missing id and swallows `Exception` in cleanup. (g) Yes — input-defaulting *and*
  compatibility. (h) None. (i) No change. **Disposition: adapter-owned (compatibility)**, specs
  `{GET_NOTEBOOK, GENERATE_MIND_MAP, CREATE_NOTE/plain, UPDATE_NOTE, DELETE_NOTE}`; a later hoist
  is the cheapest of the compatibility rows because the neutral twin exists.

### 3.16 `CHAT_ASK` — `_web/chat.py:_chat_ask`

- (a) Streamed POST through `chat_aware_authed_post` (own `attempt_timeout` / `retry_deadline`
  budget), then `GET_LAST_CONVERSATION_ID` only when the caller did not supply
  `resolved_conversation_id`; a null id after a non-empty answer raises `ChatError`.
- (b)/(c) **Protocol**: the conversation-id fetch after the streamed answer is the canonical
  protocol-forced sequence (plan). Specs `{GET_LAST_CONVERSATION_ID}` plus `stream`; the ledger's
  `GET_NOTEBOOK` is not part of this row (§2).
- (d) None. (e) `tests/unit/test_semantic_chat_slice_characterization.py:479` (`ChatError`, "did not
  register a conversation"); error scrub of request URLs (`_CHAT_OPERATIONS`). (f) Catches raw
  `NetworkError` (maps expiry to `BackendDeadlineExceededError(outcome_unknown=True)`) and
  `NotebookLMError` (log-and-re-raise). (g) No. (h) None. (i) No change. **Custom row (protocol)**,
  `error_mode=TRANSLATE` with scrub.

### 3.17 Input-defaulting rows kept adapter-owned (contract 1)

| Member | Resolver used | Natives (specs) | Category |
|---|---|---|---|
| `ARTIFACT_GENERATE_AUDIO` | `_audio_source_ids` (silent on malformed rows) | GET_NOTEBOOK, CREATE_ARTIFACT | deferred-product |
| `ARTIFACT_GENERATE_QUIZ`, `…_FLASHCARDS` | `_generation_source_ids` (warns) | GET_NOTEBOOK, CREATE_ARTIFACT | deferred-product |
| `ARTIFACT_GENERATE_INFOGRAPHIC`, `…_SLIDE_DECK` | `_visual_source_selection` → `_generation_source_ids` | GET_NOTEBOOK, CREATE_ARTIFACT | deferred-product |
| `ARTIFACT_GENERATE_VIDEO`, `…_REPORT` | `_document_source_ids` → `_generation_source_ids` | GET_NOTEBOOK, CREATE_ARTIFACT | deferred-product |
| `ARTIFACT_GENERATE_DATA_TABLE` | `_data_source_ids` → `_generation_source_ids` | GET_NOTEBOOK, CREATE_ARTIFACT | deferred-product |
| `MIND_MAP_GENERATE_NOTE` | `_audio_source_ids` | GET_NOTEBOOK, GENERATE_MIND_MAP | deferred-product |
| `MIND_MAP_GENERATE_INTERACTIVE` | `_audio_source_ids` | GET_NOTEBOOK, CREATE_ARTIFACT | deferred-product |
| `NOTEBOOK_SUGGEST_PROMPTS` | `codec/suggestions.decode_prompt_source_ids` (warns during decoding) | GET_NOTEBOOK, SUGGEST_PROMPTS | deferred-product |

(f) for all eleven: none — no `except` in any of these handlers; `raise_on_null_status=True` and
the null-result `ARTIFACT_FEATURE_UNAVAILABLE` mapping are codec-level. (e): `test_web_backend.py`
×14 and `tests/unit/test_semantic_data_views.py` ×3 (attribution walk). Hoisting any of them
requires a resolved-input primitive per family and the product member becoming `service_owned`
(eleven-plus new members), which P9 does not assume. The resolver collapse (plan: one `_web/`
helper with a per-family diagnostics mode) is in scope: there are **six** resolution paths, not
five — the five named in the plan plus `decode_prompt_source_ids`, which `NOTEBOOK_GET` also
uses.

## 4. Disposition per row and PR order

| Disposition | Rows |
|---|---|
| **hoist** (11) | `LABEL_UPDATE`, `COLLECTION_UPDATE`, `SOURCE_UPDATE`, `SHARING_SET_PUBLIC`, `SHARING_UPDATE_USERS`, `SHARING_SET_VIEW_LEVEL`, `LABEL_CREATE`, `COLLECTION_CREATE`, `ARTIFACT_RENAME`, `NOTEBOOK_UPDATE`, `NOTEBOOK_CREATE` |
| **adapter-owned: protocol** (5) | `CHAT_ASK`, `SOURCE_ADD_URL`, `SOURCE_ADD_URL_BATCH`, `SOURCE_ADD_DRIVE`, `SOURCE_ADD_FILE` |
| **adapter-owned: compatibility** (4) | `ARTIFACT_LIST`, `ARTIFACT_GET`, `ARTIFACT_GENERATE_MIND_MAP`, `SOURCE_ADD_TEXT` |
| **adapter-owned: deferred-product** (11) | the §3.17 members |
| **input-keyed `NativeCallSpec` row** (3) | `CHAT_CONFIGURE`, `ARTIFACT_DOWNLOAD`, `RESEARCH_START` |
| **codec row** (single by handler code) | `CHAT_GET_HISTORY` and the 48 ledger-single members except `SOURCE_ADD_TEXT` |

The hoist order applies the plan's rule (strongest backend-identity argument and zero raw catches
first; raw-catch rows last):

| PR | Content | OP | SL | SO | `KNOWN_ACTIVE_SEMANTIC_OPERATIONS` |
|---|---|---|---|---|---|
| P9.2-0 | catalog walker derives authorities from rows; `dispatched`/`may_have_committed`/`rebind_operation`/`require_leaves`; `RecordingBackend.set_sequence`; `planned:_idempotency_create.py` | 87 | 82 | 0 | unchanged |
| P9.2-1 (foundational) | primitives **`LABEL_MUTATE`**, **`LABEL_ALLOCATE`**, **`SHARING_MUTATE`** as codec rows; no hoist | 90 | 85 | 0 | + 3 primitives |
| P9.2-2 | hoist `LABEL_UPDATE` | 90 | 84 | 1 | − `LABEL_UPDATE` |
| P9.2-3 | hoist `COLLECTION_UPDATE` | 90 | 83 | 2 | − `COLLECTION_UPDATE` |
| P9.2-4 | **`SOURCE_PATCH_TITLE`** + hoist `SOURCE_UPDATE` | 91 | 83 | 3 | + primitive, − workflow |
| — | **stop/go review** (three hoists merged) | | | | |
| P9.2-5 | hoist `SHARING_SET_PUBLIC` | 91 | 82 | 4 | |
| P9.2-6 | hoist `SHARING_UPDATE_USERS` | 91 | 81 | 5 | |
| P9.2-7 | **`SHARING_PATCH_VIEW_LEVEL`** + hoist `SHARING_SET_VIEW_LEVEL` | 92 | 81 | 6 | |
| P9.2-8 | hoist `LABEL_CREATE` | 92 | 80 | 7 | |
| P9.2-9 | hoist `COLLECTION_CREATE` | 92 | 79 | 8 | |
| P9.2-10 | **`ARTIFACT_PATCH_TITLE`**, **`ARTIFACT_CATALOG`** + hoist `ARTIFACT_RENAME` | 94 | 80 | 9 | |
| P9.2-11 | **`NOTEBOOK_PATCH`** + hoist `NOTEBOOK_UPDATE` (after the §3.10(d) decision) | 95 | 80 | 10 | |
| P9.2-12 | **`NOTEBOOK_ALLOCATE`** + hoist `NOTEBOOK_CREATE` | 96 | 80 | 11 | |

Invariant per PR: `SL + SO + unsupported(5) == OP`. Rollback runs in reverse; the three
foundational primitives stay until their last consumer is reverted. `_web/deadlines.py` loses the
hoisted members' `CLIENT_TIMEOUT` entries in each hoist PR (the service mints the deadline), so
`_EXPECTED_CLIENT_TIMEOUT_OPERATIONS` in `tests/unit/test_semantic_deadline_seeding.py` shrinks
from 28 to 17 over the sequence.

Residual custom rows at P9.4 if every hoist lands: **20** (11 deferred-product + 5 protocol +
4 compatibility). The plan's estimate of "at least fourteen" omits the three URL/Drive/batch
source-add rows, `SOURCE_ADD_TEXT`, and the two catalog rows.

## 5. Primitive vocabulary

Nine members, all `<DOMAIN>_<VERB>` like the existing enum. Five are the plan's lower bound
(one per native that appears only in multi-native handler code and is not an input-defaulting
native); four come from incompatible-contract natives or the rename readback.

| Member | Native(s) `(method, variant)` | Policy | Input → output | Consumers | Kind |
|---|---|---|---|---|---|
| `LABEL_MUTATE` | UPDATE_LABEL × {∅, add_sources, remove_sources, add_notebooks, remove_notebooks} (input-keyed) | MUTATION | `LabelMutateInput` → `LabelMutateResult` | `LABEL_UPDATE`, `COLLECTION_UPDATE` | foundational |
| `LABEL_ALLOCATE` | (CREATE_LABEL, ∅) manual contract | MUTATION | `LabelAllocateInput` → `LabelAllocateResult(echo)` | `LABEL_CREATE`, `COLLECTION_CREATE` | foundational |
| `SHARING_MUTATE` | (SHARE_NOTEBOOK, ∅) | MUTATION | `SharingMutateInput(notebook_id, visibility \| grants)` → `SharingMutateResult` | `SHARING_SET_PUBLIC`, `SHARING_UPDATE_USERS` | foundational |
| `SOURCE_PATCH_TITLE` | (UPDATE_SOURCE, ∅) | MUTATION | `(notebook_id, source_id, new_title)` → `(source \| None)` | `SOURCE_UPDATE` | single |
| `SHARING_PATCH_VIEW_LEVEL` | (RENAME_NOTEBOOK, ∅) view mask | MUTATION | `(notebook_id, view_level)` → `()` | `SHARING_SET_VIEW_LEVEL` | single |
| `ARTIFACT_PATCH_TITLE` | (RENAME_ARTIFACT, ∅) | MUTATION | `(notebook_id, artifact_id, new_title)` → `()` | `ARTIFACT_RENAME` | single |
| `ARTIFACT_CATALOG` | (LIST_ARTIFACTS, ∅) | READ | `(notebook_id)` → `(artifacts)` | `ARTIFACT_RENAME` (later: catalog rows) | single |
| `NOTEBOOK_PATCH` | (RENAME_NOTEBOOK, ∅) property mask | MUTATION | `(notebook_id, title, emoji)` → `()` | `NOTEBOOK_UPDATE` | single |
| `NOTEBOOK_ALLOCATE` | (CREATE_NOTEBOOK, ∅), `disable_internal_retries` forwarded | MUTATION | `(title)` → `(notebook)` | `NOTEBOOK_CREATE` | single |

Natives that need **no** primitive: `CREATE_ARTIFACT`, `GENERATE_MIND_MAP`, `SUGGEST_PROMPTS`
(inside deferred-product rows); `ADD_SOURCE/url`, `ADD_SOURCE/drive`, `ADD_SOURCE_FILE`
(inside protocol rows — note that as `(method, variant)` pairs the two `ADD_SOURCE` variants are
multi-native-only, which the plan's method-level count hides); `GET_CONVERSATION_TURNS` (single by
handler code); `START_FAST_RESEARCH`/`START_DEEP_RESEARCH` (input-keyed row); `CREATE_NOTE/plain`,
`UPDATE_NOTE`, `DELETE_NOTE` (compatibility row; leaves already exist as `NOTE_*`).

Ledger form after a hoist (plan contract 5): a `service_owned` row is `policy` plus
`leaf_operations` edges, e.g. `LABEL_UPDATE: MUTATION, [(LABEL_GET, {∅}), (LABEL_MUTATE,
{∅, add_sources, remove_sources})]` and `COLLECTION_UPDATE: …, [(COLLECTION_GET, {∅}),
(LABEL_MUTATE, {∅, add_notebooks, remove_notebooks})]`, so the transitive native set of each
workflow is exactly today's ledger row.

## 6. Ledger re-examinations

- **`RESEARCH_IMPORT`** (`WORKFLOW_OWNED`): the handler passes the caller's `deadline` through and
  additionally forwards `attempt_timeout=value.attempt_timeout`, which `ResearchService`
  computes from its own budget (`_research_service.py:408-420`). Row: codec, `deadline=INHERIT`,
  `attempt_timeout` a typed `CodecPayload` option. The ledger entry means only "not client-timeout
  seeded"; keep it and label it `INHERIT, unseeded`. No behaviour change.
- **`ARTIFACT_WAIT`** (`WORKFLOW_OWNED`): `_studio_rows(..., deadline=deadline)` passes the
  deadline through; `ArtifactLifecycleService.observe` receives it from the polling loop
  (`_studio/lifecycle.py:35-135`). Row: codec, `INHERIT`, unseeded. Same label.
- **`SOURCE_WAIT`**: the only `deadline=None`-at-site row (`source_variants.py:633`) → `IGNORE`.
- **`ARTIFACT_LIST`/`ARTIFACT_GET`**: sequential via the collaborator, raw swallow → compatibility
  custom rows (§3.14). The deadline-seeding test's `hidden_collaborator_composites` set
  (`test_semantic_deadline_seeding.py:133`) becomes unnecessary once rows declare their specs.
- **`CHAT_ASK`/`CHAT_GET_HISTORY`** ledger rows over-attribute one native each (§2). The P4
  parity audit derives `(method, variant)` per row from the specs a row declares; both rows will
  show a divergence that must be recorded in `known_divergence`, or the ledger rows corrected, in
  P9.2-0.

## 7. P9.3 leaf column

Codec-row candidates per domain in the plan's order (everything not in §4's custom/hoist sets):

| Domain (PR) | Codec rows | Notes |
|---|---|---|
| settings/suggestions | 4: `SETTINGS_GET`, `SETTINGS_GET_LIMITS`, `SETTINGS_SET_LANGUAGE`, `ARTIFACT_SUGGEST_REPORTS` | `NOTEBOOK_SUGGEST_PROMPTS` is deferred-product |
| sharing | 2: `SHARING_GET`, `LEGACY_SHARE_ARTIFACT` | three hoists in P9.2 |
| research | 4: `RESEARCH_POLL`, `RESEARCH_CANCEL`, `RESEARCH_IMPORT` (INHERIT), `RESEARCH_START` (input-keyed spec + `map_error` for `RESEARCH_START_UNAVAILABLE`, which today catches raw `RPCError` at `research.py:79-84`) | |
| notes | 5: `NOTE_LIST`, `NOTE_GET`, `NOTE_CREATE`, `NOTE_UPDATE`, `NOTE_DELETE` | |
| mind maps | 4: `MIND_MAP_LIST`, `MIND_MAP_GET`, `MIND_MAP_UPDATE`, `MIND_MAP_DELETE` | two generate members deferred-product |
| labels/collections | 7: `LABEL_LIST`, `LABEL_GET`, `LABEL_GENERATE`, `LABEL_DELETE`, `COLLECTION_LIST`, `COLLECTION_GET`, `COLLECTION_DELETE` | `LABEL_GET`/`COLLECTION_GET` are list-then-select under `decode(value, raw)` |
| Studio | 6: `ARTIFACT_EXPORT`, `ARTIFACT_REVISE_SLIDE`, `ARTIFACT_RETRY`, `ARTIFACT_DELETE`, `ARTIFACT_WAIT` (INHERIT), `ARTIFACT_DOWNLOAD` (input-keyed) | 8 generate + 2 catalog + mind-map rows are custom |
| notebook/source reads | 14: `NOTEBOOK_LIST` (non-uniform: row + `decode` helper for the `[]`/`None`/list shapes), `NOTEBOOK_GET`, `NOTEBOOK_DELETE`, `NOTEBOOK_REMOVE_RECENT`, `NOTEBOOK_SUMMARIZE`, `NOTEBOOK_DESCRIBE`, `SOURCE_LIST`, `SOURCE_GET` (non-uniform: list-then-filter under `decode(value, raw)`), `SOURCE_DELETE`, `SOURCE_REFRESH`, `SOURCE_CHECK_FRESHNESS`, `SOURCE_GET_GUIDE`, `SOURCE_GET_FULLTEXT`, `SOURCE_WAIT` (IGNORE) | `SOURCE_ADD_TEXT` is a custom row |
| chat | 5: `CHAT_GET_CONVERSATION`, `CHAT_GET_HISTORY`, `CHAT_DELETE_HISTORY`, `CHAT_SAVE_NOTE`, `CHAT_CONFIGURE` (input-keyed) | `CHAT_ASK` custom |

Total **51 codec rows** + 11 hoists + 20 custom rows = 82. The plan's "54 leaf names" is
`82 − 28 CLIENT_TIMEOUT entries`; 51 = 54 − `CHAT_ASK` − `SOURCE_ADD_FILE` − `SOURCE_ADD_TEXT`.

## 8. First three hoists for the stop/go review

1. **`LABEL_UPDATE`** (P9.2-2). Risk: the membership loop's `outcome_unknown_on_expiry=
   write_may_have_committed` becomes service-side `write_dispatched` state and must reproduce
   `test_semantic_outcome_unknown_readback.py` case for case, including the read-only-preflight
   case that stays *not* unknown; the `LABEL_NOT_FOUND` discriminator (kind → public class) moves
   from the handler's `_label_not_found` into the service with identical diagnostics keys
   (`label_kind`, `label_id`, `notebook_id`, `method_id`), which the compat projector reads. No raw
   catch, a shared primitive, and an existing truth-table oracle make it the safest first hoist.
2. **`COLLECTION_UPDATE`** (P9.2-3). Risk: the two dialects share one service class
   (`LabelSetService`), so the hoist is mostly the `LABEL_MUTATE` variant selection by kind;
   the dialect guard (`_require_label_kind`) must keep raising `BackendContractError` *before*
   any leaf invocation, and the "no emoji-only field mask" contract error likewise. Lands the
   `(operation, allowed_variants)` edge form for a shared primitive, which the P4 parity audit
   needs before any other shared primitive is consumed.
3. **`SOURCE_UPDATE`** (P9.2-4). Risk: the null-echo hydration reuses `SOURCE_GET`, whose
   `MUTATION` policy (recency side effect) is unchanged, but the workflow's `CLIENT_TIMEOUT` seed
   moves to `SourceService.update`, which is constructed in the source facade — this is the first
   hoist that carries `RuntimeDeadlineFactory` into a facade-constructed service and therefore the
   first exercise of contract 3's constructor plumbing (`_sources.py`, `_client_composition.py`).
   Single consumer, one new primitive, one pinned reason.

If the review outcome is REVISE or ABANDON, the remaining nine hoist rows become
`CustomBinding` rows under *deferred-product* and the residual count in §4 rises from 20 to 29
(with `LABEL_MUTATE`, `LABEL_ALLOCATE` and `SHARING_MUTATE` already landed as codec rows).

## 9. Where the code contradicts the plan's assumptions

- **`CHAT_ASK` never issues `GET_NOTEBOOK`** below the port; the ledger row and the plan's
  "ledger natives `GET_NOTEBOOK`, `GET_LAST_CONVERSATION_ID`, plus the streamed POST" attribute a
  native the handler cannot execute. The custom row declares one RPC spec, not two.
- **Branch-exclusive rows are three, not two**: `RESEARCH_START` joins `CHAT_CONFIGURE` and
  `ARTIFACT_DOWNLOAD`; it is absent from the deadline ledger only because it is one syntactic site.
- **`SOURCE_ADD_TEXT` is single-native but not a codec row** (raw passthrough through
  `SourceAddService`). The plan's "48 single-native … can be expressed as a table row" overcounts
  by one.
- **Residual custom rows are at least 20, not 14**; the plan's floor omits the three URL/Drive/
  batch source-add rows, `SOURCE_ADD_TEXT`, and the two catalog rows.
- **Primitive count is nine, not five**: the lower bound holds, but `LABEL_ALLOCATE` (incompatible
  `CREATE_LABEL` contract), `ARTIFACT_PATCH_TITLE` + `ARTIFACT_CATALOG` (`artifact.rename`), and
  `SHARING_PATCH_VIEW_LEVEL` (a second `RENAME_NOTEBOOK` mask) are required by contract 1; the
  plan anticipated the first two families.
- **`NOTEBOOK_UPDATE` catches a raw `ClientError`** on its readback and translates it to
  `NOTEBOOK_NOT_FOUND` below the port, while the `NOTEBOOK_GET` leaf lets the same error through
  for the facade to translate. The plan lists `NOTEBOOK_NOT_FOUND` as a `map_error` example
  without noting that installing it on `NOTEBOOK_GET` changes `notebook.get`'s pinned error chain
  (§3.10(d)).
- **`ARTIFACT_GENERATE_MIND_MAP` already has a service-side persistence twin**
  (`MIND_MAP_GENERATE_NOTE` + `NoteService.create_note` through `NOTE_*` leaves). The plan treats
  the row as compatibility-only; the actual blockers are the unchained `RPCTimeoutError` in
  `DeadlineRpcCaller` and the un-threaded deadline in the twin, both narrower than "raw swallow".
- **Six source-id resolution paths, not five**: `decode_prompt_source_ids` in
  `codec/suggestions.py` is the sixth, shared with `NOTEBOOK_GET`.
- **`ADD_SOURCE/url` and `ADD_SOURCE/drive` are multi-native-only as `(method, variant)` pairs**;
  the plan's method-level "12 by the ledger / 8 by handler code" treats `ADD_SOURCE` as
  single-native because of the `text` variant. No primitive is needed only because those rows stay
  adapter-owned.
- The plan's "`_idempotency.py` 18 `RPCMethod` references" holds as word occurrences (18) — none is
  a member access; the module's web coupling is the type name in signatures plus the
  `register_default_policies` import-time side effect, as the plan says.
