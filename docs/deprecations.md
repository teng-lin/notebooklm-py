# Deprecations

This page is the **single source of truth** for currently-deprecated APIs in
`notebooklm-py`. Each row lists what is deprecated, the recommended
replacement, when the deprecation was introduced, when it is scheduled for
removal, and any cross-references.

`docs/stability.md` links here rather than duplicating the table; if you need
the broader stability policy (semver promise, supported Python versions, the
0.x pre-1.0 semantics), start there.

> **Upgrading to v0.8.0?** See the consolidated
> [Upgrading to v0.8.0](upgrading-to-0.8.0.md) guide for the full set of
> breaking error-and-return contract changes, the exact before→after migration
> for each, and the `NOTEBOOKLM_FUTURE_ERRORS=1` preview flag that lets you run
> your suite against v0.8.0 behavior on 0.7.0.

## Scheduled for removal

| Deprecated | Replacement | Since | Removal | Notes |
|------------|-------------|-------|---------|-------|
| `sources.get()` / `artifacts.get()` / `notes.get()` / `mind_maps.get()` returning `None` on a miss | `try/except SourceNotFoundError` / `ArtifactNotFoundError` / `NoteNotFoundError` / `MindMapNotFoundError` (or `get_or_none()` for a warning-free `None`-on-miss) | v0.7.0 | v0.8.0 | Behavior unchanged this release (still returns `None`); a `DeprecationWarning` now fires **only on a miss**. In v0.8.0 these raise the matching `*NotFoundError`, unifying the not-found contract with `notebooks.get()` (which already raises). `SourceNotFoundError`, `ArtifactNotFoundError`, `NoteNotFoundError`, and `MindMapNotFoundError` all exist today, so the `except` clause can be written now (it is only *raised* starting in v0.8.0). `mind_maps.get()` joined this cohort in v0.7.0 — it was the last namespace without a runway ([#1358](https://github.com/teng-lin/notebooklm-py/issues/1358)). Warning emitted via `src/notebooklm/_deprecation.py::warn_get_returns_none`; suppress with `NOTEBOOKLM_QUIET_DEPRECATIONS`. Flip tracked by [#1247](https://github.com/teng-lin/notebooklm-py/issues/1247) |
| Awaiting `NotebookLMClient.from_storage(...)` | `async with NotebookLMClient.from_storage(...) as client:` | v0.5.0 | v1.0 | The `__await__` form still works. Warning emitted via `src/notebooklm/_deprecation.py::warn_deprecated`; suppress with `NOTEBOOKLM_QUIET_DEPRECATIONS=1` ([#1369](https://github.com/teng-lin/notebooklm-py/issues/1369)) |
| `ResearchAPI.poll(notebook_id, task_id=None)` with multiple in-flight tasks | `poll(notebook_id, task_id=<id>)` (the `task_id` from `research.start`) | v0.6.0 | future major | Ambiguous-selection guard: when more than one task is in flight and no `task_id` is supplied, `poll` keeps returning the latest task for back-compat but emits a `DeprecationWarning`. Warning emitted via `src/notebooklm/_deprecation.py::warn_deprecated`; suppress with `NOTEBOOKLM_QUIET_DEPRECATIONS=1`. Removal version re-pin tracked by [#1363](https://github.com/teng-lin/notebooklm-py/issues/1363) |
| `NotebooksAPI.share()` | `client.sharing.set_public()` (paired with `add_user()` / `set_view_level()` / `get_status()`) | v0.5.0 | future major | No-behavior-change wrapper. Warning emitted via `src/notebooklm/_deprecation.py::warn_deprecated`; suppress with `NOTEBOOKLM_QUIET_DEPRECATIONS=1`. Removal version re-pin tracked by [#1363](https://github.com/teng-lin/notebooklm-py/issues/1363) |
| `ResearchAPI.wait_for_completion(interval=...)` | `initial_interval=...` — same cadence, name now matches `SourcesAPI.wait_until_ready` / `ArtifactsAPI.wait_for_completion` | v0.7.0 | v0.8.0 | Additive: `interval` keeps its default of `5` and still works; passing a non-default value emits a `DeprecationWarning`, passing both `interval` and `initial_interval` raises `TypeError`. Suppress with `NOTEBOOKLM_QUIET_DEPRECATIONS=1`. Helper: `src/notebooklm/_deprecation.py` |
| Dict-subscript access (`result["status"]`) on `research.poll` / `research.start` / `research.wait_for_completion`, `artifacts.generate_mind_map`, and `sources.get_guide` return values | Attribute access (`result.status`, `result.sources`, `result.note_id`, `guide.summary`, …) | v0.7.0 | v0.8.0 | These methods now return typed dataclasses (`ResearchTask` / `ResearchStart` / `MindMapResult` / `SourceGuide`) with a new `ResearchStatus` str-enum, instead of `dict[str, Any]`. The dataclasses mix in `MappingCompatMixin` so the legacy dict shape keeps working for one MINOR cycle: `result["key"]` warns and returns the historical value (from `to_public_dict()`), while `result.get(...)` / `result.keys()` / `"x" in result` / `iter(result)` stay silent. In v0.8.0 the mixin is dropped and the returns become attribute-only. `ResearchStatus` is a `str` enum, so `status == "completed"` keeps working in v0.8.0. Suppress with `NOTEBOOKLM_QUIET_DEPRECATIONS=1`. Helper: `src/notebooklm/_deprecation.py::MappingCompatMixin`. Tracked by [#1209](https://github.com/teng-lin/notebooklm-py/issues/1209) |

## Preview the v0.8.0 error contract early: `NOTEBOOKLM_FUTURE_ERRORS`

The deprecations in the **Scheduled for removal** table above are *warn-runways*:
in v0.7.0 they still behave the old way and only emit a `DeprecationWarning`. In
v0.8.0 three of them flip to a hard error (the breaking error-contract work,
[ADR-0019](adr/0019-error-and-return-contract.md) / umbrella
[#1346](https://github.com/teng-lin/notebooklm-py/issues/1346)). Setting
`NOTEBOOKLM_FUTURE_ERRORS=1` (any of `1` / `true` / `yes` / `on`,
case-insensitive) opts a process — or a CI job — into that **v0.8.0 target
behavior today**, so you can verify your code is forward-compatible before the
flip ships. It is **off by default**, and default-off is byte-identical to
current v0.7.0 behavior.

When the flag is on, these runways adopt their v0.8.0 behavior. The first three
are deprecation *warn-runways* (the warning becomes a raise); the last three are
purely-behavioral previews with no warning today (#1405):

| Runway | v0.7.0 (default / flag off) | With `NOTEBOOKLM_FUTURE_ERRORS=1` | Tracked by |
|--------|-----------------------------|-----------------------------------|------------|
| `sources.get()` / `artifacts.get()` / `notes.get()` / `mind_maps.get()` on a miss | Warns, returns `None` | Raises the matching `*NotFoundError` (`SourceNotFoundError` / `ArtifactNotFoundError` / `NoteNotFoundError` / `MindMapNotFoundError`) | [#1247](https://github.com/teng-lin/notebooklm-py/issues/1247) |
| Dict-style access on the typed research / mind-map / source-guide returns (`MappingCompatMixin`) — `result["key"]`, `result.get(...)`, `"k" in result`, `result.keys()` / `items()` / `values()`, `iter(result)`, `len(result)` | `[...]` warns; the rest are silent; reads preserve the legacy dict semantics | Each raises the exact error a bare attribute-only dataclass would once the mixin is removed: `TypeError` for `[...]` / `in` / `iter` / `len`, `AttributeError` for `get` / `keys` / `items` / `values` | [#1251](https://github.com/teng-lin/notebooklm-py/issues/1251) |
| Deprecated keyword alias `ResearchAPI.wait_for_completion(interval=...)` | Warns, aliases to `initial_interval` | Raises `TypeError` (the deprecated keyword is gone) | [#1254](https://github.com/teng-lin/notebooklm-py/issues/1254) |
| `sources.refresh()` / `chat.delete_conversation()` return value | Returns `True` (uninformative — failures raise first) | Returns `None` (the `-> bool` annotation is preserved until the v0.8.0 flip) | [#1290](https://github.com/teng-lin/notebooklm-py/issues/1290) |
| Synchronous generation refusal (`generate_*` / `revise_slide` / `research.start`) | Swallowed into `GenerationStatus(status="failed")` / returned `None` | Raises the decoder's `RateLimitError` / `RPCError` / `DecodingError` / `ArtifactFeatureUnavailableError` ("couldn't-start" is an error, not data) | [#1342](https://github.com/teng-lin/notebooklm-py/issues/1342) |
| `notes.update()` / `sources.rename(return_object=False)` / `artifacts.rename(return_object=False)` on a missing target | Silent no-op / returns `None` | Raises the matching `*NotFoundError` (the `return_object=False` path still returns `None` on success — the flag gates miss-detection, not the return) | [#1362](https://github.com/teng-lin/notebooklm-py/issues/1362) |

The flag does **not** close those issues — the runways stay until the v0.8.0 flip
actually ships; it only lets you preview the target behavior. The flag changes
only the *deprecated* paths in the table; the sanctioned replacements are
**unaffected** in both modes: `get_or_none()` stays the silent `None`-on-miss
lookup, and attribute access (`result.status`, `result.sources`, …) stays the
warning-free read on the typed dataclasses. Under this flag the **entire**
`MappingCompatMixin` surface raises — `result.get(...)` / `keys()` / `in` /
`iter(...)` / `len(...)` are no longer silent (they were before this preview was
completed), so forward-testing catches every removed access, not just subscript.
Off the flag they stay silent (no warning storm). All of them are removed
wholesale in v0.8.0; the only post-flip read is attribute access. Use them as a
temporary migration aid, not a target shape.

**Precedence over `NOTEBOOKLM_QUIET_DEPRECATIONS`.** When `NOTEBOOKLM_FUTURE_ERRORS`
is on, a runway **raises regardless of the quiet setting** — quiet only silences
the *warning* on the warn path, which future mode replaces with an exception, so
there is nothing left to silence. Setting both is well-defined: future mode wins.

**Scope.** Both the warn-runway flips and the purely-behavioral previews above
are gated by the same flag. The behavioral previews are **runtime-only** — no
public return annotation changes until the v0.8.0 flip — so the default-off path
is byte-identical to v0.7.0 and the api-compat / conformance gates stay green.
The flag does **not** close #1290 / #1342 / #1362 — the default behavior remains
until the v0.8.0 flip ships ([#1405](https://github.com/teng-lin/notebooklm-py/issues/1405)).
The flag is implemented in
`src/notebooklm/_deprecation.py::future_errors_enabled`, routed through
`src/notebooklm/_lookup.py::resolve_get` for the `get()` flip, and consumed at
each behavioral preview's call site via `if future_errors_enabled(): ... else:
...`.

```python
# Verify forward-compatibility in CI: run your suite with the v0.8.0 contract on.
#   NOTEBOOKLM_FUTURE_ERRORS=1 pytest

import os
os.environ["NOTEBOOKLM_FUTURE_ERRORS"] = "1"   # before constructing the client

# Now a missing-source lookup RAISES instead of returning None:
try:
    source = await client.sources.get(nb_id, "missing")
except SourceNotFoundError:
    ...   # the v0.8.0 shape — code that handles this is forward-compatible
```

### Migration: typed research / mind-map / source-guide returns

```python
from notebooklm import ResearchStatus

# BEFORE (still works in v0.7.0; subscript emits a DeprecationWarning)
result = await client.research.poll(nb_id)
if result["status"] == "completed":
    for source in result["sources"]:
        print(source["title"], source["url"])

guide = await client.sources.get_guide(nb_id, src_id)
print(guide["summary"], guide["keywords"])

# AFTER — typed attribute access (warning-free)
result = await client.research.poll(nb_id)
if result.status == ResearchStatus.COMPLETED:   # also == "completed"
    for source in result.sources:               # tuple[ResearchSource, ...]
        print(source.title, source.url)

guide = await client.sources.get_guide(nb_id, src_id)
print(guide.summary, guide.keywords)
```

The new return types (`ResearchStatus`, `ResearchTask`, `ResearchSource`,
`ResearchStart`, `MindMapResult`, `SourceGuide`) are exported from both
`notebooklm` and `notebooklm.types`. Set `NOTEBOOKLM_QUIET_DEPRECATIONS=1` to
silence the subscript warning while migrating.

### Migration: `ResearchAPI.wait_for_completion` poll-interval keyword

```python
# BEFORE (still works in v0.7.0, emits a DeprecationWarning)
await client.research.wait_for_completion(nb_id, task_id, interval=2.0)

# AFTER — canonical keyword, matches the source/artifact waiters
await client.research.wait_for_completion(nb_id, task_id, initial_interval=2.0)
```

The rename closes the last wait/poll inconsistency: every `wait_*` waiter now
spells its poll cadence `initial_interval` and routes its timeout through a
single catchable base, [`WaitTimeoutError`](python-api.md#waittimeouterror).
Set `NOTEBOOKLM_QUIET_DEPRECATIONS=1` to silence the warning while migrating.

> **Decision — `wait_timeout` kept as-is.** The `wait_timeout` keyword on the
> `SourcesAPI.add_*` family (`add_url` / `add_text` / `add_file` / `add_drive`)
> was deliberately **not** renamed to `timeout`. On those methods `timeout`
> would be ambiguous with a per-request HTTP timeout, and `wait_timeout`
> already reads as "how long to wait for readiness after adding". The waiter
> methods (`wait_until_ready` / `wait_until_registered` / the artifact and
> research `wait_for_completion`) already spell the budget `timeout`, so the
> only standardization with a clear win was the research `interval` →
> `initial_interval` rename above.

`SourcesAPI.add_file(mime_type=...)` and `notebooklm source add --mime-type`
(file sources) are **no longer deprecated**: `mime_type` was re-wired to set
the resumable-upload content-type header (overriding filename-extension
inference), so both are now supported parameters. The earlier
`DeprecationWarning` was removed.

## v0.8.0 breaking changes without a deprecation warning

A few v0.8.0 breaks are **return-value or behavioral** changes that cannot emit a
clean `DeprecationWarning` (you cannot warn on "this returns `True` today but
`None` tomorrow" without firing on every call). They are **silent** in v0.7.0 and
surface only under `NOTEBOOKLM_FUTURE_ERRORS=1` — that flag is the *only* v0.7.0
signal for them. Full before/after migrations are in
[`docs/upgrading-to-0.8.0.md`](upgrading-to-0.8.0.md). They ship **together** with
the rest of the break-set (one release; #1344 is not split to 0.9.0).

| Change | v0.7.0 (default) | v0.8.0 | v0.7.0 preview | Tracked by |
|--------|------------------|--------|----------------|------------|
| `sources.refresh()` / `chat.delete_conversation()` return value | `True` (uninformative — failures raise first) | `None` | `NOTEBOOKLM_FUTURE_ERRORS=1` | [#1290](https://github.com/teng-lin/notebooklm-py/issues/1290) |
| Synchronous generation refusal (`generate_*` / `revise_slide` / `research.start`) | swallowed into `GenerationStatus(status="failed")` / returns `None` | raises `RateLimitError` / `RPCError` / `DecodingError` / `ArtifactFeatureUnavailableError` | `NOTEBOOKLM_FUTURE_ERRORS=1` | [#1342](https://github.com/teng-lin/notebooklm-py/issues/1342) |
| `notes.update()` / `sources.rename(return_object=False)` / `artifacts.rename(return_object=False)` on a missing target | silent no-op / returns `None` | raises the matching `*NotFoundError` | `NOTEBOOKLM_FUTURE_ERRORS=1` | [#1362](https://github.com/teng-lin/notebooklm-py/issues/1362) |
| Derived-read drift + lister drift-tightening | malformed / unknown payloads collapse to empty / `None` | raise `DecodingError` | _(no flag preview yet)_ | [#1344](https://github.com/teng-lin/notebooklm-py/issues/1344) |

The flip happens in lockstep at the version bump, enforced by the
`tests/_lint/test_v080_release_gate.py` no-orphans gate (umbrella
[#1346](https://github.com/teng-lin/notebooklm-py/issues/1346)).

## Removed in v0.7.0

| Removed | Replacement | Deprecated since | Removed in | Notes |
|---------|-------------|------------------|------------|-------|
| `NOTEBOOKLM_STRICT_DECODE=0` soft-mode opt-out | Unset the variable (strict is the only mode) | v0.5.0 | v0.7.0 | The env var is now ignored; `safe_index` always raises `UnknownRPCMethodError` on shape drift. Rationale in `docs/stability.md` "Strict decode" + ADR-011 |
| Positional `wait` / `wait_timeout` on `SourcesAPI.add_url`, `SourcesAPI.add_text`, `SourcesAPI.add_file`, `SourcesAPI.add_drive` | Pass `wait=...` and `wait_timeout=...` as keywords | v0.5.0 | v0.7.0 | `wait` / `wait_timeout` are now keyword-only; positional calls raise `TypeError`. CLI already used keyword arguments |
| `ArtifactsAPI.wait_for_completion(poll_interval=...)` | `initial_interval=...` — same cadence, clearer name | v0.5.0 | v0.7.0 | The `poll_interval` keyword was removed; passing it raises `TypeError` |
| `NotesAPI.create_from_chat(...)` | `ChatAPI.save_answer_as_note(...)` | v0.5.0 | v0.7.0 | Pure deprecated forwarder, now removed (two MINOR cycles of warnings served). `ChatAPI.save_answer_as_note(...)` is the canonical citation-rich saved-from-chat method and data owner (ADR-013); call it directly. |

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
* `NOTEBOOKLM_QUIET_DEPRECATIONS=1` suppresses **every** deprecation warning
  this project emits — including the `get()`-returns-`None` warning, the
  renamed-keyword warnings, the dict-subscript bridge, and the one-off
  warnings routed through `src/notebooklm/_deprecation.py::warn_deprecated`
  (awaiting `from_storage(...)`, ambiguous `research.poll`, `NotebooksAPI.share()`).
  All mechanics live in `_deprecation.py`; ADR-018 forbids inline
  `warnings.warn(..., DeprecationWarning)` elsewhere and a lint
  (`tests/_lint/test_no_inline_deprecation_warnings.py`) enforces it. See
  `docs/configuration.md`.
* Not every inline `warnings.warn(...)` is a deprecation. The
  `save_cookies_to_storage(original_snapshot=None)` legacy full-merge path is a
  *permanent* public-API back-compat shim (see
  `docs/auth-cookie-lifecycle.md` §3.4.1), not a scheduled removal, so it emits
  a **`RuntimeWarning`** safety advisory about the stale-overwrite-fresh race —
  outside ADR-018's scope and intentionally **not** silenced by
  `NOTEBOOKLM_QUIET_DEPRECATIONS`.
* `NOTEBOOKLM_FUTURE_ERRORS=1` is the opposite gate: instead of *silencing* a
  warn-runway it makes the runway adopt its v0.8.0 *target* behavior (raise)
  early, for forward-compat testing. It takes precedence over
  `NOTEBOOKLM_QUIET_DEPRECATIONS` — a runway under future mode raises regardless
  of quiet. See the "Preview the v0.8.0 error contract early" section above.
* See `docs/stability.md` "Deprecation Policy" for the broader timeline
  contract (one MINOR cycle of warnings before removal during 0.x).

## Removed in past versions

For deprecations that have already completed their removal cycle, see
`docs/stability.md` "Removed in v0.5.0".
