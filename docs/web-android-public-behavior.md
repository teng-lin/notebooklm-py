# Web vs Android public-behavior inventory

**Status:** Active
**Last Updated:** 2026-09-06

Typed `NotebookLMClient` methods share one public contract across Web
(`batchexecute`) and Android (protobuf/gRPC), but a few remaining splits are
**not** "just the wire." This page is the committed inventory of those **public**
differences so callers — and future refactors — do not treat a projection
difference as a defect, a tracked default flip as already shipped, or a bug as
policy.

See also:

- [Python API — backend selection](python-api.md#backend-selection)
- [Independent C3/C4/C5 migration rows](deprecations.md#independent-c3c4c5-migration-rows)
  for independently gated default flips (`C3-02`, `C5A-01`)
- [#2384](https://github.com/teng-lin/notebooklm-py/issues/2384) for the
  `chat.get_history` strictness fix (shipped separately in [#2389](https://github.com/teng-lin/notebooklm-py/pull/2389))

## Non-goals

This inventory is documentation plus a guardrail. It is **not** an architecture
program. Explicitly out of scope for this page:

- Unifying protobuf vs batchexecute codecs
- Extracting a generic mutation executor from sharing/notes
- Implementing the [#2384](https://github.com/teng-lin/notebooklm-py/issues/2384)
  `get_history` strictness fix
- v1 credential work

## Inventory

| Method | What is actually true | Class | Pinning tests |
| --- | --- | --- | --- |
| `notes.get` after `notes.delete` | Conformance: both backends can still see a **persisted soft-delete tombstone** on the wire. Android's public `get` / `get_or_none` project **absence** (`NoteNotFoundError` / `None`). Web `get` / `get_or_none` can still return a cleared `Note` (`id` retained, title and content empty). Android is not proven to hard-delete the row — do not "fix" this by assuming hard-delete. | Projection difference — document, do not "fix" by assuming hard-delete | `tests/e2e/test_android_notes_conformance.py` (live opt-in; do not require a re-run here); `tests/unit/android/test_notes_frontend_parity.py` (`test_android_notes_satisfy_public_nullable_raw_and_absence_contracts`) |
| `chat.get_history` on turn-fetch failure | Both backends now propagate turn-fetch `ChatError` / `NetworkError`. Web previously returned `[]` on failure. For positive limits, `[]` means no conversation or no turns; Android also returns `[]` for non-positive limits. | Resolved bug — strict contract shipped separately in [#2389](https://github.com/teng-lin/notebooklm-py/pull/2389), fixing [#2384](https://github.com/teng-lin/notebooklm-py/issues/2384) | `tests/unit/test_chat_characterization.py` (`test_get_history_raises_on_chat_error`, `test_get_history_raises_on_network_error`); `tests/unit/android/test_chat.py` (`test_get_history_raises_on_turns_rpc_error`, `test_get_history_returns_empty_when_no_conversation`) |
| `get_prompt(..., require_complete=False)` | Web's prompt decoder is already strict (direct Studio/note path; `require_complete=True` does not add a lookup preflight). Android's 0.x default still uses legacy `get_or_none` and can project an incomplete aggregate no-hit as absence (with the registered `artifact_ambiguous_absence` warning). First-party consumers pass `require_complete=True`. | Tracked default flip — independently gated as [C3-02](deprecations.md#independent-c3c4c5-migration-rows); do not flip the default in this inventory | Web: `tests/unit/test_artifact_completeness.py` (`test_web_strict_prompt_keeps_direct_path_without_lookup_preflight`). Android legacy: `tests/unit/android/test_artifacts.py` (`test_android_legacy_prompt_warns_only_when_absence_is_ambiguous`) |
| Interactive `mind_maps.generate` wait FAILED/REMOVED | Web hydrates the interactive tree after a waited failed/removed completion unless `failure_policy="raise"` (and emits `mind_map_legacy_terminal_hydration` only when that legacy hydration actually continues). Android already raises `ArtifactNotReadyError` in legacy mode. | Tracked default flip — independently gated as [C5A-01](deprecations.md#independent-c3c4c5-migration-rows); postpone the default flip if that gate is unmet | `tests/unit/test_creation_conformance.py` (`test_waited_interactive_outcome_matrix`); `tests/unit/test_mind_maps_base.py` (`test_interactive_wait_failure_policy_preserves_each_backend_contract`) |
| `research.import_sources` | Android requires a canonical UUID `task_id` (parseable-but-non-canonical spellings are rejected). Web accepts opaque task ids, requires explicit report fields (`title` + `report_markdown` + `result_type == 5`) for report rows, and may reorder reports-first. Keep both policies. | Deliberate policy — keep | Neutral policy/order: `tests/unit/test_research_import_helpers.py` (`test_import_policies_are_immutable_and_preserve_task_id_validation`, `test_import_classification_preserves_backend_report_order`). Android UUID: `tests/unit/android/test_research_guards.py` (`test_a_parseable_but_non_canonical_run_id_is_rejected`) |
| Interactive mind-map `language` | The public `mind_maps.generate(..., language=...)` input is accepted on both backends. Web does not encode `language` on the interactive `CREATE_ARTIFACT` wire (no language slot). Android encodes `language_code`. Do not reject Web `language` in a "parity" patch. | Capability metadata — keep; Web lists interactive mind maps as instructions-only in `creation_capabilities` | `tests/unit/test_creation_conformance.py` (`test_interactive_domain_normalization_and_language_encoding`) |
| `notebooks.get_raw` | Raw escape hatch. Web returns the batchexecute list payload. Android returns a **dict** from `message_to_known_dict` (known protobuf fields as snake_case), **not** a protobuf message and not Web positional rows. | Raw escape hatch — document the actual types | Web list: `tests/integration/test_notebooks_integration.py` (`test_get_raw`). Android dict: `tests/unit/android/test_notebook_source_reads.py` (`test_get_raw_is_known_field_snake_case_message_dict`) |
| `notes.list_mind_maps` | Return type is opaque `list[Any]`. Android returns minimal compatibility `[id, content]` rows. Web returns full note rows (content nested in the current metadata envelope). Compare decoded content, not byte-for-byte raw-row parity. | Raw/compat rows — document | `tests/e2e/test_android_notes_conformance.py`; `tests/unit/android/test_notes_frontend_parity.py` (`test_android_notes_satisfy_public_nullable_raw_and_absence_contracts`) |
| `chat.get_conversation_turns` | Backend-shaped `Any`: Web returns batchexecute turn rows; Android returns a `ListChatTurnsResponse` protobuf message. Typed history is `chat.get_history` → `list[tuple[str, str]]`. | Raw — document, do not wrap in this inventory | Web rows: `tests/unit/test_chat_characterization.py` (`test_get_conversation_turns`). Android protobuf: `tests/unit/android/test_chat.py` (`test_list_sessions_raw_turns_and_history_decode_exact_requests`) |
| `artifacts.generate_*` input validation | Omitted sources resolve the notebook inventory on both backends. Web preserves an explicit empty `source_ids=[]`, empty language, non-string instructions, and legacy enum-like values where previously accepted. Android rejects those inputs before dispatch and requires membership in the expected enum. Quiz/flashcard quantity and difficulty are validated on both. | Deliberate policy — preserve existing input compatibility | `tests/unit/test_creation_conformance.py` (`test_source_omission_and_empty_are_backend_policy`, `test_legacy_web_accepted_inputs_do_not_acquire_android_rejections`); [creation contracts](architecture.md#artifact-creation-contracts) |
| `artifacts.generate_report` with `ReportFormat.CONCEPT_EXPLANATION` | Android supports concept-explanation reports; Web rejects this format as unsupported. | Capability metadata — keep | `tests/unit/test_creation_conformance.py` (`test_android_concept_report_support_is_not_fabricated_on_web`) |
| Interactive mind-map `instructions` | Web omits a whitespace-only prompt and preserves nonblank text. Android preserves the supplied text, including whitespace. | Deliberate policy — keep | `tests/unit/test_creation_conformance.py` (`test_interactive_domain_normalization_and_language_encoding`) |
| `sources.add_file` for `.csv`, `.docx`, `.pptx` | Web exposes `Source.kind` as `CSV`, `DOCX`, or `POWERPOINT`. Android stages these formats through Drive and exposes the Drive type (`GOOGLE_DRIVE`, wire code 14); captured ingestion has content parity, not type parity. Code 14 can be disambiguated for other MIME types such as Sheets and PDF. | Projection difference — preserve the actual source kind | `tests/unit/android/test_source_upload.py` (`test_every_drive_staged_extension_routes_through_drive`); `tests/unit/test_types.py` (source-kind mappings); [captured Web/Android upload matrix](android/web-compat-seam-closure.md#source-type-does-not-survive-any-android-upload-route) |
| `notebooks.get_toolset_preferences` | Web reads per-notebook Connected Apps / toolset preferences via `ToolManagementService.ListNotebookToolsetPreferences` (`EEr8Nb`). Android has no `ToolManagementService` gRPC surface and raises `UnsupportedOperationError`. | Capability metadata — Web-only service | `tests/unit/test_toolset_preferences.py` (`test_calls_eer8nb_with_notebook_source_path_and_allow_null`, `test_raises_unsupported_operation_error_without_transport_call`) |

The live Android notes conformance probe is opt-in and destructive only to the
uniquely prefixed resources it creates; this inventory cites it as the
tombstone/list_mind_maps oracle and does **not** require a live re-run.

`tests/_guardrails/test_web_android_public_behavior_inventory.py` fails if this
table disappears or a required row drops its class, pinning tests, or linked
issue/gate.
