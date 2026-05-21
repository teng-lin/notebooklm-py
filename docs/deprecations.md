# Deprecations

This page is the **single source of truth** for currently-deprecated APIs in
`notebooklm-py`. Each row lists what is deprecated, the recommended
replacement, when the deprecation was introduced, when it is scheduled for
removal, and any cross-references.

`docs/stability.md` links here rather than duplicating the table; if you need
the broader stability policy (semver promise, supported Python versions, the
0.x pre-1.0 semantics), start there.

## Scheduled for removal in v0.6.0

| Deprecated | Replacement | Since | Removal | Notes |
|------------|-------------|-------|---------|-------|
| `NOTEBOOKLM_STRICT_DECODE=0` soft-mode | Unset the variable (strict is the only mode) | v0.5.0 | v0.6.0 | Warning at `tests/unit/test_strict_decode_default.py:73`; rationale in `docs/stability.md` "Strict decode" + ADR-011 |
| `SourcesAPI.add_file(mime_type=...)` | Omit `mime_type` — server infers from filename extension | v0.5.0 | v0.6.0 | Warning emitted at `src/notebooklm/_source_upload.py:287` |
| `notebooklm source add --mime-type` (file sources) | Omit `--mime-type`; Drive-source `--mime-type` remains live | v0.5.0 | v0.6.0 | Warning at `src/notebooklm/cli/source.py:437` |
| `ArtifactsAPI.wait_for_completion(poll_interval=...)` | `initial_interval=...` — same cadence, clearer name | v0.5.0 | v0.6.0 | Warning at `src/notebooklm/_artifact_polling.py:154` |
| `NotesAPI.create_from_chat(...)` | `ChatAPI.save_answer_as_note(...)` | v0.5.0 | v0.6.0 | Warning at `src/notebooklm/_notes.py:192` |
| `NotebookLMClient.rpc_call(source_path=...)` | Omit the argument, or pass `"/"` explicitly | v0.5.0 | v0.6.0 | Explicit `source_path="/"` is silent (matches default); any other value warns |
| `NotebookLMClient.rpc_call(_is_retry=...)` | Omit the argument | v0.5.0 | v0.6.0 | Internal-only; any explicit value (`True` or `False`) warns |
| `NotebookLMClient.rpc_call(operation_variant=...)` | Omit the argument | v0.5.0 | v0.6.0 | Internal-only; will be removed once the mutating-RPC idempotency registry stabilizes |

## How deprecations work in this project

* Every deprecated surface emits a `DeprecationWarning` from the call site
  the user wrote (`stacklevel=2`), so the warning's `filename`/`lineno`
  point at user code rather than at the library internals.
* Default-shape calls remain silent. A deprecation only fires when the
  caller actually passes the deprecated argument.
* See `docs/stability.md` "Deprecation Policy" for the broader timeline
  contract (one MINOR cycle of warnings before removal during 0.x).

## Removed in past versions

For deprecations that have already completed their removal cycle, see
`docs/stability.md` "Removed in v0.5.0".
