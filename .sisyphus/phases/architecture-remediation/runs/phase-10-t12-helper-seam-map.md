# Phase 10 T12 Helper Seam Map

Generated during T12.0 against the speculative T11 stack:

- T11.1 shared runtime primitive in `notebooklm.cli.helpers`
- T11.2 download commands routed through that runtime
- T11.3 completion provider extraction in `notebooklm.cli.completion`
- T11.4 options completion guardrail in `tests/unit/test_cli_boundary.py`

This artifact is a migration contract for T12.1-T12.9. It is intentionally
refactor-only: preserve public CLI behavior, Click option wiring, JSON/text
output, stdout/stderr routing, shell completion values, and existing test patch
seams unless a later task explicitly migrates every affected test in the same
slice.

## Search Snapshot

Commands rerun for this map:

```bash
rtk rg -n "from notebooklm\\.cli\\.helpers import|import notebooklm\\.cli\\.helpers|notebooklm\\.cli\\.helpers\\.|patch\\(\"notebooklm\\.cli\\.helpers" src tests
rtk rg -n "^(class|def|async def) " src/notebooklm/cli/helpers.py
rtk rg -n "console|stderr_console|asyncio\\.sleep|time\\.monotonic|get_auth_tokens|load_auth_from_storage|build_cookie_jar|get_context_path|_complete_notebooks|_complete_sources|_complete_artifacts|_resolve_notebook_for_completion|resolve_.*id|require_notebook|resolve_prompt|read_stdin_text" src/notebooklm/cli tests/unit/cli tests/unit tests/integration/cli_vcr
```

High-risk helper patch seams found:

- `notebooklm.cli.helpers.get_context_path`
- `notebooklm.cli.helpers.load_auth_from_storage`
- `notebooklm.cli.helpers.build_cookie_jar`
- `notebooklm.cli.helpers.get_auth_tokens`
- `notebooklm.cli.helpers.handle_auth_error`
- `notebooklm.cli.helpers.run_async`
- `notebooklm.cli.helpers.console`
- `notebooklm.cli.helpers.asyncio.sleep`
- `notebooklm.cli.helpers.time.monotonic`
- `notebooklm.cli.options._complete_notebooks`
- `notebooklm.cli.options._resolve_notebook_for_completion`
- `notebooklm.cli.options._complete_sources`
- `notebooklm.cli.options._complete_artifacts`

## Symbol Migration Map

| Current helper symbol | Planned home | T12 slice | Compatibility requirement |
|---|---|---:|---|
| `console` | `notebooklm.cli.rendering` | T12.1 | Keep `helpers.console` patchable or migrate every test patch in the same slice. Many command modules import this symbol directly. |
| `stderr_console` | `notebooklm.cli.rendering` | T12.1 | Keep `helpers.stderr_console` patchable; JSON stdout purity depends on stderr routing. |
| `emit_status` | `notebooklm.cli.rendering` | T12.1 | Keep `helpers.emit_status` wrapper/re-export until all imports move. |
| `json_output_response` | `notebooklm.cli.rendering` | T12.1 | Preserve exact JSON serialization and newline behavior. |
| `json_error_response` | `notebooklm.cli.rendering` | T12.1 | Preserve stdout/stderr routing and typed error envelope fields. |
| `display_research_sources` | `notebooklm.cli.rendering` | T12.1 | Preserve table labels, truncation, and max-display behavior. |
| `display_report` | `notebooklm.cli.rendering` | T12.1 | Preserve text truncation and JSON hint behavior. |
| `get_artifact_type_display` | `notebooklm.cli.rendering` | T12.1 | Keep helper import stable for artifact/source command modules. |
| `get_source_type_display` | `notebooklm.cli.rendering` | T12.1 | Keep helper import stable for source command output. |
| `_current_storage_override` | `notebooklm.cli.context` | T12.1/T12.2 | Preserve the value observed by auth and context helpers. Keep it out of `auth_runtime` because `context.py` cannot import `auth_runtime.py`, while `auth_runtime.py` may import `context.py`. |
| `_get_context_value` | `notebooklm.cli.context` | T12.1 | Preserve corrupt-context recovery and context file lock behavior. |
| `_set_context_value` | `notebooklm.cli.context` | T12.1 | Preserve account metadata and remove-key behavior. |
| `get_current_notebook` | `notebooklm.cli.context` | T12.1 | Keep `helpers.get_current_notebook` patch target effective for completion/notebook tests until migrated. |
| `set_current_notebook` | `notebooklm.cli.context` | T12.1 | Preserve account-email/authuser persistence semantics. |
| `clear_context` | `notebooklm.cli.context` | T12.1 | Preserve `clear_account` behavior. |
| `get_current_conversation` | `notebooklm.cli.context` | T12.1 | Preserve conversation context file shape. |
| `set_current_conversation` | `notebooklm.cli.context` | T12.1 | Preserve delete-on-None behavior. |
| `get_context_path` imported from `notebooklm.paths` | `notebooklm.cli.context` wrapper | T12.1 | Existing tests patch `notebooklm.cli.helpers.get_context_path`; resolve through that helper facade at call time or migrate all affected tests, because direct early binding to `notebooklm.paths.get_context_path` bypasses the patch. |
| `get_client` | `notebooklm.cli.auth_runtime` | T12.2 | Preserve storage/profile/authuser/account-email lookup and return tuple shape. |
| `get_auth_tokens` | `notebooklm.cli.auth_runtime` | T12.2 | Preserve call-time patching through `notebooklm.cli.helpers.get_auth_tokens`, including download and completion paths. |
| `load_auth_from_storage` imported from `notebooklm.auth` | `notebooklm.cli.auth_runtime` wrapper | T12.2 | Existing tests patch `notebooklm.cli.helpers.load_auth_from_storage`; keep or migrate in same slice. |
| `build_cookie_jar` imported from `notebooklm.auth` | `notebooklm.cli.auth_runtime` wrapper | T12.2 | Existing tests patch `notebooklm.cli.helpers.build_cookie_jar`; keep or migrate in same slice. |
| `handle_auth_error` | `notebooklm.cli.auth_runtime` | T12.2 | Preserve JSON/text UX, exit code 1, checked path fields, and `NoReturn` behavior. |
| `run_async` | `notebooklm.cli.runtime` | T12.2 | Preserve nested-loop and coroutine cleanup semantics. Existing tests patch `helpers.run_async`. |
| `with_auth_and_errors` | `notebooklm.cli.auth_runtime` | T12.2 | Preserve T11 call-time lookup for `get_auth_tokens`, `handle_errors`, `handle_auth_error`, and `run_async`. |
| `with_client` | `notebooklm.cli.auth_runtime` | T12.2 | Keep decorator signature and command callback contract unchanged. |
| `handle_error` | `notebooklm.cli.runtime` or `notebooklm.cli.rendering` | T12.2 | Preserve exit code 1 and Unicode fallback. |
| `validate_id` | `notebooklm.cli.resolve` | T12.3 | Preserve ClickException wording and trimming behavior. |
| `require_notebook` | `notebooklm.cli.resolve` | T12.3 | Preserve flag/env/context precedence and failure message. |
| `_resolve_partial_id` | `notebooklm.cli.resolve` | T12.3 | Preserve exact/partial/ambiguous/no-match behavior and JSON output suppression. |
| `resolve_notebook_id` | `notebooklm.cli.resolve` | T12.3 | Keep `helpers.resolve_notebook_id` importable until command modules migrate. |
| `resolve_source_id` | `notebooklm.cli.resolve` | T12.3 | Preserve source list lookup and ambiguity messages. |
| `resolve_artifact_id` | `notebooklm.cli.resolve` | T12.3 | Preserve artifact id/title partial matching. |
| `resolve_note_id` | `notebooklm.cli.resolve` | T12.3 | Preserve note id/title partial matching. |
| `resolve_source_ids` | `notebooklm.cli.resolve` | T12.3 | Preserve multi-id error aggregation. |
| `read_stdin_text` | `notebooklm.cli.input` | T12.3 | Preserve stdin empty/error handling and source label messages. |
| `resolve_prompt` | `notebooklm.cli.input` | T12.3 | Preserve prompt/file/stdin precedence and required behavior. |
| `ResearchImportResult` | `notebooklm.cli.research_import` | T12.4 | Preserve dataclass fields and result semantics. |
| `cli_name_to_artifact_type` | `notebooklm.cli.research_import` or `notebooklm.cli.rendering` | T12.4 | Preserve CLI artifact aliases, especially singular `flashcard`. |
| `_normalize_url` | `notebooklm.cli.research_import` | T12.4 | Preserve URL normalization for research import dedupe. |
| `_source_url_norm` | `notebooklm.cli.research_import` | T12.4 | Preserve no-URL handling. |
| `_requested_urls_norm` | `notebooklm.cli.research_import` | T12.4 | Preserve requested URL set behavior. |
| `_has_no_url_entry` | `notebooklm.cli.research_import` | T12.4 | Preserve cited-source import decisions. |
| `_imported_source_entry` | `notebooklm.cli.research_import` | T12.4 | Preserve result dictionary keys. |
| `_merge_imported_sources` | `notebooklm.cli.research_import` | T12.4 | Preserve duplicate merge semantics. |
| `import_with_retry` | `notebooklm.cli.research_import` | T12.4 | Preserve retry/backoff semantics and patch seams for `asyncio.sleep` and `console`. |
| `_select_research_sources_for_import` | `notebooklm.cli.research_import` | T12.4 | Preserve cited/all selection behavior. |
| `_display_cited_import_selection` | `notebooklm.cli.research_import` | T12.4 | Preserve Rich text output. |
| `import_research_sources` | `notebooklm.cli.research_import` | T12.4 | Preserve import orchestration and result counters. |

## Patch Seam Decisions

| Existing patch target | Current owner | Later slice decision |
|---|---|---|
| `notebooklm.cli.helpers.console` | `helpers.py` global | T12.1 must keep this patch target effective until every test and command import is migrated. Pure re-export is not enough if moved code reads a different module global. |
| `notebooklm.cli.helpers.stderr_console` | `helpers.py` global | T12.1 should preserve helper-level patchability for JSON stdout tests. |
| `notebooklm.cli.helpers.get_context_path` | imported path helper | T12.1 must keep helper patch target effective via call-time helper facade lookup for context tests, CLI VCR fixtures, chat/notebook/session/use tests. Moved code must not bind directly to `notebooklm.paths.get_context_path` if those tests still patch the helper symbol. |
| `notebooklm.cli.helpers.load_auth_from_storage` | imported auth helper | T12.2 must keep helper patch target effective for broad CLI fixtures and auth tests. |
| `notebooklm.cli.helpers.build_cookie_jar` | imported auth helper | T12.2 must keep helper patch target effective for `get_auth_tokens` and source-delete tests. |
| `notebooklm.cli.helpers.get_auth_tokens` | helper function | T12.2 must keep call-time lookup through the helper facade for `with_auth_and_errors`, download, and completion until tests migrate. |
| `notebooklm.cli.helpers.handle_auth_error` | helper function | T12.2 must keep call-time lookup or migrate primitive tests in the same slice. |
| `notebooklm.cli.helpers.run_async` | helper function | T12.2 must keep call-time lookup for completion tests and runtime primitive tests. |
| `notebooklm.cli.helpers.asyncio.sleep` | imported module global | T12.4 must either preserve helper-level retry patch target or migrate all research import retry tests to `notebooklm.cli.research_import.asyncio.sleep`. |
| `notebooklm.cli.helpers.time.monotonic` | imported module global | T12.4 must either preserve helper-level elapsed-time patch target or migrate all research import retry tests. |
| `notebooklm.cli.options._complete_notebooks` | options wrapper | T11.3/T11.4 keep this wrapper. Later completion changes must not remove it without migrating shell-complete binding tests. |
| `notebooklm.cli.options._resolve_notebook_for_completion` | options wrapper | Keep wrapper in options while tests patch it for source/artifact completion. |
| `notebooklm.cli.options._complete_sources` | options wrapper | Keep wrapper in options while `source_option` binds it directly. |
| `notebooklm.cli.options._complete_artifacts` | options wrapper | Keep wrapper in options while `artifact_option` binds it directly. |

## Import DAG For Later Slices

- `rendering.py` must not import runtime, auth, context, resolve, or command modules.
- `context.py` must not import runtime, auth, resolve, or command modules.
- `auth_runtime.py` may import `context.py` and `rendering.py`; it must not import command modules.
- `runtime.py` may import `auth_runtime.py` and `error_handler.py`; it must not import command modules.
- `resolve.py` may import `rendering.py`; it must not import runtime/auth or command modules.
- `input.py` should avoid runtime/auth imports.
- `completion.py` must not import `options.py`.
- `cli/services/*` must not import private library modules or `notebooklm.rpc.*`.

## Final T12 Task Order

The current plan order remains valid:

1. T12.1 moves rendering and context first. These are low-level dependencies.
2. T12.2 moves runtime and auth after rendering/context exist.
3. T12.3 moves resolve and input helpers after runtime/auth are stable.
4. T12.4, T12.6, and T12.7 can run together after T12.2/T12.3 because their target command surfaces are separate.
5. T12.5 must run after T12.4 because both touch `source.py`.
6. T12.8 performs final import cleanup and guardrails.
7. T12.9 runs final verification and Phase 11 handoff.

The marker-only package file `src/notebooklm/cli/services/__init__.py` is
created in T12.0 so later service slices do not race on package creation.

## T12.8 Final Helper Facade Imports

After T12.8, production command and service modules import moved helpers from
their canonical owners instead of `notebooklm.cli.helpers`.

Remaining production imports of `cli.helpers` are intentionally limited to:

| File | Import shape | Reason |
|---|---|---|
| `src/notebooklm/cli/__init__.py` | `from .helpers import ...` | Compatibility re-export surface for callers that import helper names through the CLI package. |
| `src/notebooklm/cli/auth_runtime.py` | lazy `from . import helpers` inside `_helpers_facade()` | Preserves call-time patch seams for `load_auth_from_storage`, `build_cookie_jar`, `get_client`, `get_auth_tokens`, `run_async`, `handle_auth_error`, JSON error rendering, and helper-level consoles. |
| `src/notebooklm/cli/completion.py` | lazy `from . import helpers` inside provider methods | Preserves existing completion tests and shell-completion patch seams for `get_auth_tokens`, `get_current_notebook`, and `run_async` while keeping visible completion failures silent. |

`tests/unit/test_cli_boundary.py::test_command_modules_do_not_import_helpers_facade_for_moved_symbols`
guards that no other production CLI module imports moved helper symbols through
the compatibility facade. `test_helpers_remains_compatibility_facade` guards
against `cli.helpers` regaining new top-level command responsibilities.
