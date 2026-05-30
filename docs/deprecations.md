# Deprecations

This page is the **single source of truth** for currently-deprecated APIs in
`notebooklm-py`. Each row lists what is deprecated, the recommended
replacement, when the deprecation was introduced, when it is scheduled for
removal, and any cross-references.

`docs/stability.md` links here rather than duplicating the table; if you need
the broader stability policy (semver promise, supported Python versions, the
0.x pre-1.0 semantics), start there.

## Scheduled for removal

| Deprecated | Replacement | Since | Removal | Notes |
|------------|-------------|-------|---------|-------|
| `sources.get()` / `artifacts.get()` / `notes.get()` returning `None` on a miss | `try/except SourceNotFoundError` / `ArtifactNotFoundError` / `NoteNotFoundError` | v0.7.0 | v0.8.0 | Behavior unchanged this release (still returns `None`); a `DeprecationWarning` now fires **only on a miss**. In v0.8.0 these raise the matching `*NotFoundError`, unifying the not-found contract with `notebooks.get()` (which already raises). `SourceNotFoundError` and `ArtifactNotFoundError` already exist; **`NoteNotFoundError` is added in v0.8.0** (the warning's migration hint flags this), so notes callers should defer the `except NoteNotFoundError` clause until v0.8.0. Warning emitted via `src/notebooklm/_deprecation.py::warn_get_returns_none`; suppress with `NOTEBOOKLM_QUIET_DEPRECATIONS`. Flip tracked by [#1247](https://github.com/teng-lin/notebooklm-py/issues/1247) |
| `NotesAPI.create_from_chat(...)` | `ChatAPI.save_answer_as_note(...)` | v0.5.0 | v0.7.0 | Warning at `src/notebooklm/_notes.py:192` |
| Awaiting `NotebookLMClient.from_storage(...)` | `async with NotebookLMClient.from_storage(...) as client:` | v0.5.0 | v1.0 | The `__await__` form still works; warning at `src/notebooklm/client.py:__await__` |

`SourcesAPI.add_file(mime_type=...)` and `notebooklm source add --mime-type`
(file sources) are **no longer deprecated**: `mime_type` was re-wired to set
the resumable-upload content-type header (overriding filename-extension
inference), so both are now supported parameters. The earlier
`DeprecationWarning` was removed.

## Removed in v0.7.0

| Removed | Replacement | Deprecated since | Removed in | Notes |
|---------|-------------|------------------|------------|-------|
| `NOTEBOOKLM_STRICT_DECODE=0` soft-mode opt-out | Unset the variable (strict is the only mode) | v0.5.0 | v0.7.0 | The env var is now ignored; `safe_index` always raises `UnknownRPCMethodError` on shape drift. Rationale in `docs/stability.md` "Strict decode" + ADR-011 |
| Positional `wait` / `wait_timeout` on `SourcesAPI.add_url`, `SourcesAPI.add_text`, `SourcesAPI.add_file`, `SourcesAPI.add_drive` | Pass `wait=...` and `wait_timeout=...` as keywords | v0.5.0 | v0.7.0 | `wait` / `wait_timeout` are now keyword-only; positional calls raise `TypeError`. CLI already used keyword arguments |
| `ArtifactsAPI.wait_for_completion(poll_interval=...)` | `initial_interval=...` — same cadence, clearer name | v0.5.0 | v0.7.0 | The `poll_interval` keyword was removed; passing it raises `TypeError` |

## Removed in v0.6.0

| Removed | Replacement | Deprecated since | Removed in | Notes |
|---------|-------------|------------------|------------|-------|
| `NotebookLMClient.rpc_call(source_path=...)` | Omit the argument; the canonical `"/"` default is applied unconditionally | v0.5.0 | v0.6.0 | Public escape-hatch wrapper kept; only the kwarg was cut. No public replacement — callers that need a non-`"/"` source path should add a typed sub-client method (open an issue) rather than reaching across the wrapper. |
| `NotebookLMClient.rpc_call(_is_retry=...)` | Omit the argument | v0.5.0 | v0.6.0 | Internal-only retry flag; never part of the supported public surface. |
| `NotebookLMClient.rpc_call(operation_variant=...)` | Omit the argument | v0.5.0 | v0.6.0 | Internal-only routing key for the mutating-RPC idempotency registry. |

## How deprecations work in this project

* Every deprecated surface emits a `DeprecationWarning` from the call site
  the user wrote, so the warning's `filename`/`lineno` point at user code
  rather than at the library internals.
* Default-shape calls remain silent. A deprecation only fires when the
  caller actually passes the deprecated argument — or, for the
  `get()`-returns-`None` deprecation, only when the lookup misses (successful
  lookups stay silent).
* `NOTEBOOKLM_QUIET_DEPRECATIONS=1` suppresses the `get()`-returns-`None`
  warning for callers that have not yet migrated (see
  `docs/configuration.md`).
* See `docs/stability.md` "Deprecation Policy" for the broader timeline
  contract (one MINOR cycle of warnings before removal during 0.x).

## Removed in past versions

For deprecations that have already completed their removal cycle, see
`docs/stability.md` "Removed in v0.5.0".
