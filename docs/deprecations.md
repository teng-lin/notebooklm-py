# Deprecations

This page is the **single source of truth** for currently-deprecated APIs in
`notebooklm-py`. Each row lists what is deprecated, the recommended
replacement, when the deprecation was introduced, when it is scheduled for
removal, and any cross-references.

`docs/stability.md` links here rather than duplicating the table; if you need
the broader stability policy (semver promise, supported Python versions, the
0.x pre-1.0 semantics), start there.

> **Upgrading to v0.8.0?** The breaking error-and-return contract changes
> **shipped in v0.8.0**. See the consolidated
> [Upgrading to v0.8.0](upgrading-to-0.8.0.md) guide for the full set and the
> exact before→after migration for each. The `NOTEBOOKLM_FUTURE_ERRORS` preview
> flag that staged these changes in v0.7.0 has been **removed** (it is now a
> no-op).

## Scheduled for removal

| Deprecated | Replacement | Since | Removal | Notes |
|------------|-------------|-------|---------|-------|
| Awaiting `NotebookLMClient.from_storage(...)` | `async with NotebookLMClient.from_storage(...) as client:` | v0.5.0 | v1.0 | The `__await__` form still works. Warning emitted via `src/notebooklm/_deprecation.py::warn_deprecated`; suppress with `NOTEBOOKLM_QUIET_DEPRECATIONS=1` ([#1369](https://github.com/teng-lin/notebooklm-py/issues/1369)) |

> The v0.8.0 error-contract runways (`get()`-returns-`None`, the
> `wait_for_completion(interval=...)` alias, the dict-subscript bridge,
> `NotebooksAPI.share()`, and the ambiguous `research.poll(task_id=None)` guard)
> all completed their removal cycle in **v0.8.0** — see
> [Removed in v0.8.0](#removed-in-v080) below.

## Removed in v0.8.0

These error-and-return contract changes completed their v0.7.0 deprecation /
preview cycle and are now the **default** behavior. The full before→after
migration for each is in
[`docs/upgrading-to-0.8.0.md`](upgrading-to-0.8.0.md).

| Removed | Replacement | Deprecated since | Removed in | Notes |
|---------|-------------|------------------|------------|-------|
| `sources.get()` / `artifacts.get()` / `notes.get()` / `mind_maps.get()` returning `None` on a miss | `get_or_none()` (warning-free `None`-on-miss), or `try/except SourceNotFoundError` / `ArtifactNotFoundError` / `NoteNotFoundError` / `MindMapNotFoundError` | v0.7.0 | v0.8.0 | A miss now **raises** the matching `*NotFoundError`, unifying the not-found contract with `notebooks.get()`; return annotations narrow from `X \| None` to `X`. The v0.7.0 `DeprecationWarning` (and the `warn_get_returns_none` helper) are gone. [#1247](https://github.com/teng-lin/notebooklm-py/issues/1247) |
| Dict-subscript access (`result["status"]`) on `research.poll` / `research.start` / `research.wait_for_completion`, `artifacts.generate_mind_map`, and `sources.get_guide` returns | Attribute access (`result.status`, `result.sources`, `guide.summary`, …) | v0.7.0 | v0.8.0 | The typed returns (`ResearchTask` / `ResearchStart` / `MindMapResult` / `SourceGuide` / `ResearchSource`) are now pure attribute-only frozen dataclasses: `result["key"]` raises `TypeError`; `result.get(...)` / `.keys()` / `.items()` / `.values()` raise `AttributeError`; `"k" in result` / `iter(result)` / `len(result)` raise `TypeError`. Only attribute access and `to_public_dict()` survive. `ResearchStatus` stays a `str`-enum, so `status == "completed"` keeps working. The `MappingCompatMixin` bridge is removed. [#1251](https://github.com/teng-lin/notebooklm-py/issues/1251) |
| `ResearchAPI.wait_for_completion(interval=...)` | `initial_interval=...` — same cadence, matching `SourcesAPI.wait_until_ready` / `ArtifactsAPI.wait_for_completion` | v0.7.0 | v0.8.0 | The deprecated `interval=` keyword alias is gone; passing it now raises the standard `TypeError` for an unexpected keyword. The `deprecated_kwarg` helper that powered the alias is removed. [#1254](https://github.com/teng-lin/notebooklm-py/issues/1254) |
| `sources.refresh()` / `chat.delete_conversation()` returning `True` | (no replacement — discard the value) | n/a (clean break) | v0.8.0 | Both now return `None`; their annotations change from `-> bool` to `-> None`. The `True` carried no information (any failure raised first). `chat.clear_cache(...)` is unchanged and stays `-> bool`. [#1290](https://github.com/teng-lin/notebooklm-py/issues/1290) |
| Synchronous generation-kickoff refusal swallowed into `GenerationStatus(status="failed")` / returned `None` | Catch the re-raised `RateLimitError` / `RPCError` / `DecodingError` / `ArtifactFeatureUnavailableError` | n/a (clean break) | v0.8.0 | `generate_*` / `revise_slide` / `_parse_generation_result` / `research.start` now **raise** on a "couldn't-start" refusal instead of returning a soft-failed status. `research.start`'s return narrows from `ResearchStart \| None` to `ResearchStart`; `with_rate_limit_retry` retries only on a raised `RateLimitError`. [#1342](https://github.com/teng-lin/notebooklm-py/issues/1342) |
| Derived-read / lister drift collapsing malformed payloads to empty / `None` | Catch the raised `DecodingError` (distinct from a genuine miss) | n/a (clean break) | v0.8.0 | `sources.check_freshness()`, the note lister, and the artifact raw lister now raise `DecodingError` on a structurally-unrecognized payload. Legitimate empty / stale shapes are unchanged. [#1344](https://github.com/teng-lin/notebooklm-py/issues/1344) |
| `notes.update()` / `sources.rename(return_object=False)` / `artifacts.rename(return_object=False)` silently succeeding on a missing target | Catch the raised `*NotFoundError` | n/a (clean break) | v0.8.0 | These now run an existence preflight and raise `NoteNotFoundError` / `SourceNotFoundError` / `ArtifactNotFoundError` on a miss. `return_object=False` still returns `None` on success. [#1362](https://github.com/teng-lin/notebooklm-py/issues/1362) |
| `NotebooksAPI.share()` | `client.sharing.set_public()` + `client.notebooks.get_share_url()` | v0.5.0 | v0.8.0 | The deprecated no-behavior-change wrapper is removed. [#1363](https://github.com/teng-lin/notebooklm-py/issues/1363) |
| `ResearchAPI.poll(task_id=None)` / `wait_for_completion(task_id=None)` silently guessing among multiple in-flight tasks | Pass the explicit `task_id` from `research.start` | v0.6.0 | v0.8.0 | With two or more tasks in flight these now raise the new `AmbiguousResearchTaskError` instead of warning and returning the latest task; with a single in-flight task they resolve it silently. [#1363](https://github.com/teng-lin/notebooklm-py/issues/1363) |
| `NOTEBOOKLM_FUTURE_ERRORS` opt-in preview flag | (no replacement — the previewed behavior is now the default) | v0.7.0 | v0.8.0 | The forward-compat preview gate is removed; setting it is a no-op. The dict-subscript / get-returns-`None` / kwarg-alias deprecation helpers it gated are deleted with it. [#1365](https://github.com/teng-lin/notebooklm-py/issues/1365) |

> **`wait_timeout` was deliberately kept.** The `wait_timeout` keyword on the
> `SourcesAPI.add_*` family (`add_url` / `add_text` / `add_file` / `add_drive`)
> was **not** renamed to `timeout`: on those methods `timeout` would be ambiguous
> with a per-request HTTP timeout, while `wait_timeout` reads as "how long to wait
> for readiness after adding". `SourcesAPI.add_file(mime_type=...)` and
> `notebooklm source add --mime-type` are likewise **not** deprecated —
> `mime_type` sets the resumable-upload content-type header.

> **`notebooklm.rpc` public surface tightened — not a removal (v0.8.0,
> [#1589](https://github.com/teng-lin/notebooklm-py/issues/1589)).**
> `notebooklm.rpc.__all__` now advertises only the two documented power-user
> imports, `RPCMethod` and `resolve_rpc_id`. The ~47 other names it used to list
> — the batchexecute wire helpers (`encode_rpc_request`, `decode_response`,
> `extract_rpc_result`, `safe_index`, …), the endpoint URL constants/helpers, and
> the enum / exception **re-exports** — are **not removed**: they remain
> importable as `notebooklm.rpc.<name>` for back-compat. They were never part of
> the supported public API (`docs/stability.md` has always marked
> `notebooklm.rpc.*` internal); this change only stops the compat gate from
> advertising them. New code should import the canonical public name where one
> exists: most enums as `notebooklm.<X>` / `notebooklm.types.<X>`, but
> `ArtifactStatus` and `artifact_status_to_str` only as `notebooklm.types.<X>`;
> the exceptions as `notebooklm.<X>` / `notebooklm.exceptions.<X>`. The wire
> helpers, the endpoint URL constants/helpers, `safe_index`, `ArtifactTypeCode`,
> and `RPCErrorCode` are internal with **no** blessed public alias and stay
> importable only as `notebooklm.rpc.<name>`. For raw-RPC power use, import
> `from notebooklm.rpc import RPCMethod, resolve_rpc_id`.

> **`notebooklm.auth` public surface tightened — not a removal (v0.8.0,
> [#1592](https://github.com/teng-lin/notebooklm-py/issues/1592)).**
> `auth.__all__` no longer advertises 23 internal re-exports that only first-party
> `src`/tests imported (cookie-snapshot/storage helpers, the WIZ-extraction helpers,
> `authuser_query`/`format_authuser_value`, `load_httpx_cookies`/`normalize_cookie_map`,
> `ALLOWED_COOKIE_DOMAINS`/`MINIMUM_REQUIRED_COOKIES`, the keepalive/refresh env +
> URL constants, `load_auth_from_storage`, `fetch_tokens`, `recover_psidts_in_memory`).
> These were migration leftovers from the `_auth/*` extraction (ADR-0003 → ADR-0014).
> They are **not removed**: each remains importable as `notebooklm.auth.<name>` for
> back-compat — first-party code now imports them from their `notebooklm._auth.<sub>`
> home. `notebooklm.auth.*` has always been internal (`docs/stability.md`) except the
> documented imports (`AuthTokens`, `convert_rookiepy_cookies_to_storage_state`, the
> cookie-domain constants) and the cohesive operations (`enumerate_accounts`,
> `fetch_tokens_with_domains`, `fetch_tokens_passive`, …), which are unchanged. A
> deeper service-interface refactor of the remaining cli/_app-forced names was
> evaluated and deferred (limited encapsulation payoff while names stay importable).


## Removed in v0.7.0

| Removed | Replacement | Deprecated since | Removed in | Notes |
|---------|-------------|------------------|------------|-------|
| `NOTEBOOKLM_STRICT_DECODE=0` soft-mode opt-out | Unset the variable (strict is the only mode) | v0.5.0 | v0.7.0 | The env var is now ignored; `safe_index` always raises `UnknownRPCMethodError` on shape drift. Rationale in `docs/stability.md` "Strict decode" + ADR-0011 |
| Positional `wait` / `wait_timeout` on `SourcesAPI.add_url`, `SourcesAPI.add_text`, `SourcesAPI.add_file`, `SourcesAPI.add_drive` | Pass `wait=...` and `wait_timeout=...` as keywords | v0.5.0 | v0.7.0 | `wait` / `wait_timeout` are now keyword-only; positional calls raise `TypeError`. CLI already used keyword arguments |
| `ArtifactsAPI.wait_for_completion(poll_interval=...)` | `initial_interval=...` — same cadence, clearer name | v0.5.0 | v0.7.0 | The `poll_interval` keyword was removed; passing it raises `TypeError` |
| `NotesAPI.create_from_chat(...)` | `ChatAPI.save_answer_as_note(...)` | v0.5.0 | v0.7.0 | Pure deprecated forwarder, now removed (two MINOR cycles of warnings served). `ChatAPI.save_answer_as_note(...)` is the canonical citation-rich saved-from-chat method and data owner (ADR-0013); call it directly. |

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
  caller actually passes the deprecated argument or surface.
* `NOTEBOOKLM_QUIET_DEPRECATIONS=1` suppresses **every** deprecation warning
  this project emits — the one-off warnings routed through
  `src/notebooklm/_deprecation.py::warn_deprecated` (e.g. awaiting
  `from_storage(...)`). All mechanics live in `_deprecation.py`; ADR-0018 forbids
  inline `warnings.warn(..., DeprecationWarning)` elsewhere and a lint
  (`tests/_guardrails/test_no_inline_deprecation_warnings.py`) enforces it. See
  `docs/configuration.md`.
* Not every inline `warnings.warn(...)` is a deprecation. The
  `save_cookies_to_storage(original_snapshot=None)` legacy full-merge path is a
  *permanent* public-API back-compat shim (see
  `docs/auth-cookie-lifecycle.md` §3.4.1), not a scheduled removal, so it emits
  a **`RuntimeWarning`** safety advisory about the stale-overwrite-fresh race —
  outside ADR-0018's scope and intentionally **not** silenced by
  `NOTEBOOKLM_QUIET_DEPRECATIONS`.
* `NOTEBOOKLM_FUTURE_ERRORS` was the v0.7.0 forward-compat preview gate for the
  v0.8.0 error contract; it was **removed in v0.8.0** now that every break it
  staged is the default, and setting it is a no-op.
* See `docs/stability.md` "Deprecation Policy" for the broader timeline
  contract (one MINOR cycle of warnings before removal during 0.x).

## Removed in past versions

For deprecations that have already completed their removal cycle, see
`docs/stability.md` "Removed in v0.5.0".
